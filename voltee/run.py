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

from . import config, entsoe_src, fetch, model

log = logging.getLogger("voltee")


def _r(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), 2)


def collect(demo=False):
    """Tagastab (andmed, katkestuste list)."""
    if demo:
        from .demo import synthetic
        df, outages = synthetic()
        return fill_future_capacity(df, outages, pd.Timestamp.now(tz="UTC").floor("h")), outages
    prices = fetch.prices()
    weather = fetch.weather()
    fi_fc = fetch.fi_forecast()
    cap = fetch.capacity()
    df = weather.join(prices, how="left").join(fi_fc, how="left").join(cap, how="left")

    now = pd.Timestamp.now(tz="UTC").floor("h")
    msgs = fetch.umm_messages()
    outages = all_outages(msgs, entsoe_src.fetch(), df.index.min(), df.index.max() + pd.Timedelta(hours=1))
    # Eesti jaamade (Auvere, Eesti, Balti, Iru jt) katkestuste summa, MW - nii ajalugu kui tulevik
    df["gen_out_ee"] = fetch.outage_hours(outages, df.index)["unav_gen"]
    df = fill_future_capacity(df, outages, now)
    return df, outages


def all_outages(msgs: list, entsoe_df, start, end) -> list:
    """Ühendused Nord Pooli UMM-ist; Eesti jaamad ENTSO-E-st, kui see on olemas, muidu UMM-ist."""
    umm = fetch.parse_outages(msgs, start, end)
    gen = entsoe_src.outages(entsoe_df, start, end)
    if entsoe_df is not None and not entsoe_df.empty:
        umm = [o for o in umm if o["kind"] != "production"]
    return umm + gen


def fill_future_capacity(df: pd.DataFrame, outages: list, now: pd.Timestamp) -> pd.DataFrame:
    """Kus Eleringi võimsust pole (tulevik), võta tavavõimsus (viimase 30 päeva mediaan)
    ja lahuta UMM-teadetest teada olevad katkestused."""
    df = df.copy()
    unav = fetch.outage_hours(outages, df.index)
    for b in ("fi", "lv"):
        col = f"cap_{b}"
        if col not in df:
            df[col] = np.nan
        hist = df.loc[(df.index < now) & (df.index >= now - pd.Timedelta(days=30)), col].dropna()
        if hist.empty:
            continue  # pole ajalugu -> jätame NaN, mudel saab sellega hakkama
        normal = float(hist.median())
        fut = df.index >= now
        missing = fut & df[col].isna()
        df.loc[missing, col] = (normal - unav.loc[missing, f"unav_{b}"]).clip(lower=0)
        # Kui Eleringi tuleviku väärtus on olemas, aga UMM teatab hiljem lisanduvast katkestusest
        known = fut & ~missing
        df.loc[known, col] = df.loc[known, col].clip(upper=(normal - unav.loc[known, f"unav_{b}"]).clip(lower=0))
    return df


def forecast(df: pd.DataFrame, outages: list | None = None) -> dict:
    frame = model.build_features(df)
    full, base, metrics, n_train, full_f, base_f = model.train(frame)

    local_now = pd.Timestamp.now(tz=config.LOCAL_TZ)
    start = local_now.normalize().tz_convert("UTC")
    end = start + pd.Timedelta(days=config.HORIZON_DAYS + 1)
    fut = frame[(frame.index >= start) & (frame.index < end)].dropna(subset=model.REQUIRED_FEATURES)

    # Soome hinnaga mudel ainult päevadel, kus see oli holdout-testis vähemalt sama hea kui ilma
    by_day = metrics.get("full", {}).get("mae_by_day", {})
    base_mae = metrics.get("base", {}).get("mae")
    better = {int(h): (base_mae is None or v <= base_mae * 1.02) for h, v in by_day.items()}
    use_full = fut["fi_price"].notna() & fut["fi_h"].map(lambda h: h == 0 or better.get(int(h), True))
    pred = np.full((len(fut), 3), np.nan)
    if use_full.any():
        pred[use_full.to_numpy()] = model.predict(full, fut.loc[use_full, full_f])
    if (~use_full).any():
        pred[~use_full.to_numpy()] = model.predict(base, fut.loc[~use_full, base_f])

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
            "cap_fi": _r(row.get("cap_fi")),   # EE<->FI saadaolev võimsus, MW
            "cap_lv": _r(row.get("cap_lv")),   # EE<->LV saadaolev võimsus, MW
            "gen_out_ee": _r(row.get("gen_out_ee")),  # Eesti jaamade katkestused, MW
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
            "outages": [
                {"border": {"fi": "EE-FI", "lv": "EE-LV", "gen": "EE tootmine"}[o["border"]],
                 "name": o["name"],
                 "start": o["start"].tz_convert(config.LOCAL_TZ).strftime("%Y-%m-%dT%H:%M"),
                 "stop": o["stop"].tz_convert(config.LOCAL_TZ).strftime("%Y-%m-%dT%H:%M"),
                 "unavailable_mw": o["unavailable_mw"], "type": o["type"]}
                for o in sorted(outages or [], key=lambda o: o["start"])
                if o["stop"] > start and o["start"] < end
            ],
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
    result = forecast(*collect(demo))
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
    # Käivitumisel (nt pärast uuendust või restarti) tee prognoos kohe, et see oleks värske
    _safe_run()
    # Ajaaken, mille sees just käivitusime, loetakse tehtuks (muidu jookseks kohe teist korda)
    now = pd.Timestamp.now(tz=config.LOCAL_TZ)
    done = set()
    for t in config.RUN_TIMES:
        slot = pd.Timestamp(f"{now.date()} {t}", tz=config.LOCAL_TZ)
        if slot <= now < slot + pd.Timedelta(minutes=30):
            done.add(slot)
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
