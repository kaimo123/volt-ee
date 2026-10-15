"""Põhiskript: lae andmed -> treeni -> ennusta -> kirjuta data/public/forecast.json

Kasutus:
  python -m voltee.run            # üks käivitus päris andmetega
  python -m voltee.run --demo     # võrguta test sünteetiliste andmetega
  python -m voltee.run --schedule # jääb tööle ja käivitab RUN_TIMES kellaaegadel
"""
import argparse
import json
import logging
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import config, fetch, model

log = logging.getLogger("voltee")


def _r(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), 2)


def collect(demo=False):
    if demo:
        from .demo import synthetic
        return synthetic()
    prices = fetch.prices()
    weather = fetch.weather()
    fi_fc = fetch.fi_forecast()
    df = weather.join(prices, how="left").join(fi_fc, how="left")
    return df


def forecast(df: pd.DataFrame) -> dict:
    frame = model.build_features(df)
    full, base, metrics, n_train = model.train(frame)

    local_now = pd.Timestamp.now(tz=config.LOCAL_TZ)
    start = local_now.normalize().tz_convert("UTC")
    end = start + pd.Timedelta(days=config.HORIZON_DAYS + 1)
    fut = frame[(frame.index >= start) & (frame.index < end)].dropna(subset=model.BASE_FEATURES)

    use_full = fut["fi_price"].notna()
    pred = np.full((len(fut), 3), np.nan)
    if use_full.any():
        pred[use_full.to_numpy()] = model.predict(full, fut.loc[use_full, model.FULL_FEATURES])
    if (~use_full).any():
        pred[~use_full.to_numpy()] = model.predict(base, fut.loc[~use_full, model.BASE_FEATURES])

    hourly = []
    for (ts, row), p, uf in zip(fut.iterrows(), pred, use_full):
        actual = _r(row.get("price_ee"))
        hourly.append({
            "ts_utc": ts.strftime("%Y-%m-%dT%H:%MZ"),
            "local": ts.tz_convert(config.LOCAL_TZ).strftime("%Y-%m-%dT%H:%M"),
            "p10": _r(p[0]), "p50": _r(p[1]), "p90": _r(p[2]),
            "actual": actual,
            "confirmed": actual is not None,
            # "price" = parim teadaolev väärtus: kinnitatud hind või prognoosi mediaan
            "price": actual if actual is not None else _r(p[1]),
            "model": "full" if uf else "base",
        })

    daily = []
    h = pd.DataFrame(hourly)
    if len(h):
        h["date"] = h["local"].str[:10]
        for d, g in h.groupby("date"):
            cheapest = g.loc[g["price"].idxmin()]
            daily.append({
                "date": d, "mean": _r(g["price"].mean()), "min": _r(g["price"].min()),
                "max": _r(g["price"].max()), "cheapest_hour": cheapest["local"][11:],
                "confirmed": bool(g["confirmed"].all()),
            })

    return {
        "meta": {
            "zone": "EE",
            "unit": "EUR/MWh (ilma km-ta, börsihind)",
            "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model": "xgboost-quantile",
            "train_rows": n_train,
            "fi_forecast_hours": int(use_full.sum()),
            "holdout": metrics,
            "live_mae_by_day_ahead": live_accuracy(),
        },
        "hourly": hourly,
        "daily": daily,
    }


# ------------------------------------------------- reaalajas täpsuse jälgimine
LOG_FILE = lambda: config.CACHE_DIR / "prediction_log.csv"  # noqa: E731


def log_predictions(result: dict):
    run = pd.Timestamp(result["meta"]["generated_at_utc"])
    rows = [(run, h["ts_utc"], h["p50"]) for h in result["hourly"] if not h["confirmed"]]
    if rows:
        pd.DataFrame(rows, columns=["run_utc", "ts_utc", "p50"]).to_csv(
            LOG_FILE(), mode="a", header=not LOG_FILE().exists(), index=False)


def live_accuracy():
    """Varasemate ennustuste viga vs tegelik hind, päevade kaupa ette (1 = homne)."""
    if not LOG_FILE().exists() or not (config.CACHE_DIR / "prices.csv").exists():
        return {}
    lg = pd.read_csv(LOG_FILE(), parse_dates=["run_utc", "ts_utc"])
    pr = pd.read_csv(config.CACHE_DIR / "prices.csv", index_col=0, parse_dates=True)["price_ee"]
    pr.index = pd.to_datetime(pr.index, utc=True)
    lg["ts_utc"] = pd.to_datetime(lg["ts_utc"], utc=True)
    lg["run_utc"] = pd.to_datetime(lg["run_utc"], utc=True)
    lg = lg[lg["ts_utc"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)]
    lg["actual"] = lg["ts_utc"].map(pr)
    lg = lg.dropna(subset=["actual"])
    if lg.empty:
        return {}
    tz = config.LOCAL_TZ
    lg["days"] = (lg["ts_utc"].dt.tz_convert(tz).dt.normalize()
                  - lg["run_utc"].dt.tz_convert(tz).dt.normalize()).dt.days
    lg["err"] = (lg["p50"] - lg["actual"]).abs()
    return {str(int(k)): round(float(v), 1) for k, v in lg.groupby("days")["err"].mean().items() if k >= 1}


# ------------------------------------------------------------------------ main
def run_once(demo=False):
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    config.PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    result = forecast(collect(demo))
    out = config.PUBLIC_DIR / "forecast.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    tmp.replace(out)  # aatomiline, et HA ei loeks poolikut faili
    if not demo:
        log_predictions(result)
    m = result["meta"]["holdout"].get("full") or result["meta"]["holdout"].get("base") or {}
    log.info("Valmis %.0fs: %d tundi, holdout MAE %s €/MWh, katvus %s -> %s",
             time.time() - t0, len(result["hourly"]), m.get("mae"), m.get("coverage_p10_p90"), out)


def schedule():
    log.info("Ajakava: %s (%s)", ", ".join(config.RUN_TIMES), config.LOCAL_TZ)
    if not (config.PUBLIC_DIR / "forecast.json").exists():
        _safe_run()
    done = set()
    while True:
        now = pd.Timestamp.now(tz=config.LOCAL_TZ)
        for t in config.RUN_TIMES:
            slot = pd.Timestamp(f"{now.date()} {t}", tz=config.LOCAL_TZ)
            # käivita, kui oleme ajaakna [t, t+30min) sees ja pole täna veel jooksnud
            if slot <= now < slot + pd.Timedelta(minutes=30) and slot not in done:
                done.add(slot)
                _safe_run()
        done = {s for s in done if s.date() == now.date()}
        time.sleep(30)


def _safe_run():
    try:
        run_once()
    except Exception:  # noqa: BLE001
        log.exception("Käivitus ebaõnnestus, vana forecast.json jääb alles")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="sünteetilised andmed, võrku pole vaja")
    ap.add_argument("--schedule", action="store_true", help="jää tööle ja käivita RUN_TIMES ajal")
    a = ap.parse_args()
    schedule() if a.schedule else run_once(demo=a.demo)


if __name__ == "__main__":
    main()
