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


def build_features(df: pd.DataFrame) -> pd.DataFrame:
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
    """Treeni kõigel peale viimase N päeva, hinda viimasel N päeval."""
    cut = frame.index.max() - pd.Timedelta(days=holdout_days)
    tr, te = frame[frame.index <= cut], frame[frame.index > cut]
    if len(tr) < 24 * 60 or len(te) < 24:
        return {}
    m = _fit(tr[features], tr["price_ee"], _weights(tr.index))
    p = predict(m, te[features])
    y = te["price_ee"].to_numpy()
    naive = te["price_ee"].shift(24 * 7).dropna()  # "sama tund nädal tagasi"
    return {
        "holdout_days": holdout_days,
        "mae": round(float(np.mean(np.abs(p[:, 1] - y))), 2),
        "coverage_p10_p90": round(float(np.mean((y >= p[:, 0]) & (y <= p[:, 2]))), 3),
        "naive_week_ago_mae": round(float(np.mean(np.abs(naive - te["price_ee"].loc[naive.index]))), 2)
        if len(naive) else None,
    }


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
    base_f = REQUIRED_FEATURES + opt
    full_f = base_f + ["fi_price"]
    log.info("Treeningridu: %d (FI hinnaga %d); valikulised tunnused: %s%s", len(data), len(full_data),
             opt or "-", f" (sisu puudub, välja jäetud: {dropped})" if dropped else "")
    metrics = {"base": evaluate(data, base_f), "full": evaluate(full_data, full_f),
               "features": full_f}
    base = _fit(data[base_f], data["price_ee"], _weights(data.index))
    full = _fit(full_data[full_f], full_data["price_ee"], _weights(full_data.index))
    return full, base, metrics, len(data), full_f, base_f
