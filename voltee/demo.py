"""Sünteetilised andmed võrguta testimiseks (python -m voltee.run --demo).
Hinnad EI OLE päris, ainult torustiku kontrollimiseks."""
import numpy as np
import pandas as pd

from . import config


def synthetic(seed=1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    now = pd.Timestamp.now(tz="UTC").floor("h")
    idx = pd.date_range(pd.Timestamp(config.TRAIN_START, tz="UTC"), now + pd.Timedelta(days=9), freq="1h")
    n = len(idx)
    local = idx.tz_convert(config.LOCAL_TZ)
    doy = local.dayofyear.to_numpy()

    def smooth(scale, k=24):
        return pd.Series(rng.normal(0, 1, n)).rolling(k, min_periods=1).mean().to_numpy() * scale

    season = np.cos(2 * np.pi * (doy - 15) / 365)         # talvel 1, suvel -1
    df = pd.DataFrame(index=idx)
    df["wind_fi"] = np.clip(7 + smooth(9, 36), 0, 25)
    df["wind_ee"] = np.clip(0.6 * df["wind_fi"] + 2 + smooth(5, 24), 0, 25)
    df["wind_lv"] = np.clip(0.7 * df["wind_ee"] + smooth(3), 0, 25)
    df["temp_ee"] = 6 - 12 * season + smooth(10, 48)
    df["temp_fi"] = df["temp_ee"] - 2 + smooth(2)
    hour = local.hour.to_numpy()
    df["solar"] = np.clip(np.sin(np.pi * (hour - 5) / 15), 0, None) * (400 - 300 * season) * (0.6 + 0.4 * rng.random(n))

    daily = np.where((hour >= 7) & (hour <= 21), 1.0, 0.4) + np.where((hour >= 17) & (hour <= 20), 0.5, 0)
    fi = 20 + 90 * daily * np.exp(-df["wind_fi"] / 9) * (1.2 + 0.5 * season) - 0.05 * df["solar"] + smooth(15, 6)
    ee = fi + 25 * np.exp(-df["wind_ee"] / 6) * daily + np.clip(-df["temp_ee"], 0, None) * 1.5 + smooth(20, 3)
    spikes = rng.random(n) < 0.01
    ee = ee + spikes * rng.uniform(100, 400, n)
    df["price_fi"], df["price_ee"], df["price_lv"] = fi, ee, ee + smooth(10)

    published = local.normalize() <= (pd.Timestamp.now(tz=config.LOCAL_TZ).normalize() + pd.Timedelta(days=1))
    for c in ("price_fi", "price_ee", "price_lv"):
        df.loc[~published, c] = np.nan
    fc_mask = (~published) & (idx < now + pd.Timedelta(days=6))
    df["fi_forecast"] = np.where(fc_mask, fi + rng.normal(0, 15, n), np.nan)
    return df
