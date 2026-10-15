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
BASE_FEATURES = CALENDAR_FEATURES + WEATHER_FEATURES + ["wind_ee_6h", "wind_fi_6h"]
FULL_FEATURES = BASE_FEATURES + ["fi_price"]   # + Soome hind (tegelik või Sähkövatkaini prognoos)

PARAMS = dict(
    objective="reg:quantileerror", quantile_alpha=QUANTILES,
    n_estimators=600, learning_rate=0.03, max_depth=6, min_child_weight=5,
    subsample=0.8, colsample_bytree=0.8, tree_method="hist", n_jobs=0,
)


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
    # Soome hind: kus tegelik on teada, kasuta seda; muidu Sähkövatkaini prognoosi
    fc = out["fi_forecast"] if "fi_forecast" in out else np.nan
    out["fi_price"] = out["price_fi"].fillna(fc) if "price_fi" in out else fc
    return out


def _weights(index: pd.DatetimeIndex, half_life_days=180) -> np.ndarray:
    age = (index.max() - index).total_seconds() / 86400
    return 0.5 ** (age / half_life_days)


def _fit(X, y, w):
    m = xgb.XGBRegressor(**PARAMS)
    m.fit(X, y, sample_weight=w)
    return m


def predict(model, X) -> np.ndarray:
    p = model.predict(X)
    return np.sort(p, axis=1)  # tagame p10 <= p50 <= p90


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


def train(frame: pd.DataFrame):
    """Tagastab (täismudel, baasmudel, hindamistulemused)."""
    data = frame.dropna(subset=["price_ee"] + BASE_FEATURES)
    full_data = data.dropna(subset=["fi_price"])
    log.info("Treeningridu: %d (FI hinnaga %d)", len(data), len(full_data))
    metrics = {"base": evaluate(data, BASE_FEATURES), "full": evaluate(full_data, FULL_FEATURES)}
    base = _fit(data[BASE_FEATURES], data["price_ee"], _weights(data.index))
    full = _fit(full_data[FULL_FEATURES], full_data["price_ee"], _weights(full_data.index))
    return full, base, metrics, len(data)
