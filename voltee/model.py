"""Tunnused, XGBoosti kvantiilmudel (p10/p50/p90) ja hindamine."""
import logging

import holidays
import numpy as np
import pandas as pd
import xgboost as xgb

from . import config

log = logging.getLogger(__name__)

QUANTILES = np.array([0.1, 0.5, 0.9])
WEATHER_FEATURES = ["wind_ee", "wind_lv", "wind_fi", "temp_ee", "temp_fi", "solar"]
CALENDAR_FEATURES = ["hour", "dow", "month", "doy_sin", "doy_cos", "holiday"]
REQUIRED_FEATURES = CALENDAR_FEATURES + WEATHER_FEATURES + ["wind_ee_6h", "wind_fi_6h"]
# Ülekandevõimsus EE<->FI (Estlink 1+2) ja EE<->LV, MW, ning Eesti jaamade (Auvere, Eesti,
# Balti, Iru jt) UMM-teadetest teada olevad katkestused kokku, MW. Võivad puududa (NaN).
CAP_FEATURES = ["cap_fi", "cap_lv", "gen_out_ee"]
BASE_FEATURES = REQUIRED_FEATURES + CAP_FEATURES
FULL_FEATURES = BASE_FEATURES + ["fi_price"]   # + Soome hind (tegelik või Sähkövatkaini prognoos)

PARAMS = dict(
    objective="reg:quantileerror", quantile_alpha=QUANTILES,
    learning_rate=0.03, max_depth=6, min_child_weight=5,
    subsample=0.8, colsample_bytree=0.8, tree_method="hist",
)
N_ROUNDS = 600

# Sähkövatkaini (Soome prognoos) mõõdetud viga päevade kaupa, arvutatud nende git-ajaloo
# 1429 prognoosist vs tegelik hind (12/2025-10/2026, 113 000 tundi):
#   päev: (keskmine |viga| / (|FI hind| + 20), keskmine nihe €/MWh)
FI_ERR = {1: (0.245, 0.6), 2: (0.282, -0.9), 3: (0.332, -2.7), 4: (0.364, -4.0),
          5: (0.381, -4.4), 6: (0.391, -5.1), 7: (0.409, -3.8)}
FI_COPIES = 2   # mitu mürastatud koopiat iga treeningrea kohta


def _fi_noise(fi: np.ndarray, h: np.ndarray, rng) -> np.ndarray:
    rel = np.array([FI_ERR[int(x)][0] for x in h])
    bias = np.array([FI_ERR[int(x)][1] for x in h])
    return rng.laplace(bias, rel * (np.abs(fi) + 20))


def augment(df: pd.DataFrame, rng) -> pd.DataFrame:
    """Treeningandmed: üks puhas koopia (fi_h=0, Soome hind teada) + FI_COPIES koopiat, kus
    Soome hinnale on lisatud Sähkövatkaini tüüpiline viga juhusliku horisondi (1-7 p) jaoks."""
    parts = [df.assign(fi_h=0.0)]
    for _ in range(FI_COPIES):
        h = rng.integers(1, 8, len(df))
        c = df.copy()
        c["fi_price"] = c["fi_price"].to_numpy() + _fi_noise(c["fi_price"].to_numpy(), h, rng)
        c["fi_h"] = h.astype(float)
        parts.append(c)
    return pd.concat(parts)


def build_features(df: pd.DataFrame, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """df: tunnipõhine UTC indeks, veerud ilm + price_* + fi_forecast."""
    out = df.copy()
    local = out.index.tz_convert(config.LOCAL_TZ)
    ee_hol = holidays.country_holidays("EE", years=sorted(set(local.year)))
    out["hour"] = local.hour
    out["dow"] = local.dayofweek
    out["month"] = local.month
    doy = local.dayofyear.to_numpy()
    out["doy_sin"] = np.sin(2 * np.pi * doy / 365.25)
    out["doy_cos"] = np.cos(2 * np.pi * doy / 365.25)
    out["holiday"] = [int(d in ee_hol) for d in local.date]
    out["wind_ee_6h"] = out["wind_ee"].rolling(6, min_periods=1, center=True).mean()
    out["wind_fi_6h"] = out["wind_fi"].rolling(6, min_periods=1, center=True).mean()
    for c in CAP_FEATURES:
        if c not in out:
            out[c] = np.nan
    # Soome hind: kus tegelik on teada, kasuta seda; muidu Sähkövatkaini prognoosi
    fc = out["fi_forecast"] if "fi_forecast" in out else np.nan
    out["fi_price"] = out["price_fi"].fillna(fc) if "price_fi" in out else fc
    # fi_h: mitu päeva ette on Soome hind prognoos (0 = tegelik hind teada)
    now_l = (now or pd.Timestamp.now(tz="UTC")).tz_convert(config.LOCAL_TZ).normalize()
    days = ((local.normalize() - now_l).days).to_numpy().clip(1, 7).astype(float)
    known = out["price_fi"].notna().to_numpy() if "price_fi" in out else np.zeros(len(out), bool)
    out["fi_h"] = np.where(known, 0.0, days)
    return out


def _weights(index: pd.DatetimeIndex, half_life_days=180) -> np.ndarray:
    age = (index.max() - index).total_seconds() / 86400
    return 0.5 ** (age / half_life_days)


def _fit(X, y, w):
    # Natiivne xgboosti API: ei vaja scikit-learni
    dtrain = xgb.DMatrix(X.astype(float), label=y.astype(float), weight=w)
    return xgb.train(PARAMS, dtrain, num_boost_round=N_ROUNDS)


def predict(model, X) -> np.ndarray:
    p = model.predict(xgb.DMatrix(X.astype(float)))
    return np.sort(p.reshape(len(X), -1), axis=1)  # tagame p10 <= p50 <= p90


def evaluate(frame: pd.DataFrame, features, holdout_days=30) -> dict:
    """Treeni kõigel peale viimase N päeva, hinda viimasel N päeval.
    Kui tunnustes on Soome hind, testitakse realistlikult: Soome hinnale lisatakse
    Sähkövatkaini tegelik viga iga horisondi (1-7 p) jaoks ja tulemus antakse päevade kaupa."""
    cut = frame.index.max() - pd.Timedelta(days=holdout_days)
    tr, te = frame[frame.index <= cut], frame[frame.index > cut]
    if len(tr) < 24 * 60 or len(te) < 24:
        return {}
    y = te["price_ee"].to_numpy()
    naive = te["price_ee"].shift(24 * 7).dropna()  # "sama tund nädal tagasi"
    out = {"holdout_days": holdout_days,
           "naive_week_ago_mae": round(float(np.mean(np.abs(naive - te["price_ee"].loc[naive.index]))), 2)
           if len(naive) else None}
    if "fi_price" not in features:
        m = _fit(tr[features], tr["price_ee"], _weights(tr.index))
        p = predict(m, te[features])
        out["mae"] = round(float(np.mean(np.abs(p[:, 1] - y))), 2)
        out["coverage_p10_p90"] = round(float(np.mean((y >= p[:, 0]) & (y <= p[:, 2]))), 3)
        return out
    rng = np.random.default_rng(7)
    tra = augment(tr, rng)
    m = _fit(tra[features], tra["price_ee"], _weights(tra.index))
    by_day, cov = {}, []
    for h in range(1, 8):
        t = te.copy()
        hh = np.full(len(t), h)
        t["fi_price"] = t["fi_price"].to_numpy() + _fi_noise(t["fi_price"].to_numpy(), hh, rng)
        t["fi_h"] = float(h)
        p = predict(m, t[features])
        by_day[str(h)] = round(float(np.mean(np.abs(p[:, 1] - y))), 2)
        cov.append(np.mean((y >= p[:, 0]) & (y <= p[:, 2])))
    out["mae"] = round(float(np.mean(list(by_day.values()))), 2)
    out["mae_by_day"] = by_day
    out["coverage_p10_p90"] = round(float(np.mean(cov)), 3)
    return out


ABLATION_TOLERANCE = 0.03


def usable_optional(data: pd.DataFrame, min_share=0.01) -> list:
    """Valikulised tunnused, millel on treeningandmetes sisu: vähemalt 1% ridadest
    on väärtus olemas JA see erineb tavapärasest (mediaanist). Muidu on tunnus müra."""
    keep = []
    for c in CAP_FEATURES:
        s = data[c].dropna() if c in data else pd.Series(dtype=float)
        if len(s) < min_share * len(data):
            continue
        if (s != s.median()).mean() < min_share:
            continue
        keep.append(c)
    return keep


def train(frame: pd.DataFrame):
    """Tagastab (täismudel, baasmudel, hindamistulemused, ridu, täis-tunnused, baas-tunnused)."""
    data = frame.dropna(subset=["price_ee"] + REQUIRED_FEATURES)
    full_data = data.dropna(subset=["fi_price"])
    opt = usable_optional(data)
    dropped = [c for c in CAP_FEATURES if c not in opt]
    log.info("Treeningridu: %d (FI hinnaga %d); valikulised tunnused: %s%s", len(data), len(full_data),
             opt or "-", f" (sisu puudub, välja jäetud: {dropped})" if dropped else "")

    # Kõrvutitest samadel andmetel: kas katkestuste/võimsuse tunnused parandavad tulemust?
    # Jäetakse alles, kui viga ei kasva üle ABLATION_TOLERANCE (katkestusi on harva, seega
    # lubame väikest halvenemist, et mudel oskaks neid harvu juhtumeid arvestada).
    with_opt = evaluate(full_data, REQUIRED_FEATURES + opt + ["fi_price", "fi_h"])
    full_metrics = with_opt
    ablation = {"with": with_opt.get("mae")}
    if opt:
        without = evaluate(full_data, REQUIRED_FEATURES + ["fi_price", "fi_h"])
        ablation["without"] = without.get("mae")
        if with_opt and without and with_opt["mae"] > without["mae"] * (1 + ABLATION_TOLERANCE):
            log.info("Kõrvutitest: valikulistega MAE %.2f, ilma %.2f -> jätan %s välja",
                     with_opt["mae"], without["mae"], opt)
            opt = []
            full_metrics = without
        else:
            log.info("Kõrvutitest: valikulistega MAE %s, ilma %s -> kasutan %s",
                     with_opt.get("mae"), without.get("mae"), opt)
    base_f = REQUIRED_FEATURES + opt
    full_f = base_f + ["fi_price", "fi_h"]
    metrics = {"base": evaluate(data, base_f), "full": full_metrics,
               "features": full_f, "ablation_mae": ablation}
    log.info("Holdout päevade kaupa (Soome prognoosi veaga): %s; ilma Soome hinnata: %s",
             full_metrics.get("mae_by_day"), metrics["base"].get("mae"))
    base = _fit(data[base_f], data["price_ee"], _weights(data.index))
    fa = augment(full_data, np.random.default_rng(11))
    full = _fit(fa[full_f], fa["price_ee"], _weights(fa.index))
    return full, base, metrics, len(data), full_f, base_f
