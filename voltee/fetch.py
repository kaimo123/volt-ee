"""Andmete allalaadimine: Elering (Nord Pooli hinnad), Open-Meteo (ilm), Sähkövatkain (FI prognoos).

Kõik funktsioonid tagastavad pandas DataFrame'i, mille indeks on tunnipõhine UTC aeg.
"""
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from . import config

log = logging.getLogger(__name__)
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "volt-ee/1.0 (self-hosted price forecast)"


def _get(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=60)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            log.warning("Päring ebaõnnestus (%s), proovin uuesti: %s", url, e)
            time.sleep(3 * (attempt + 1))


# --------------------------------------------------------------------------- hinnad
def _fetch_prices_range(start: datetime, end: datetime) -> pd.DataFrame:
    rows = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=30), end)
        js = _get(config.ELERING_URL, {
            "start": cur.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "end": nxt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        })
        for zone in ("ee", "fi", "lv"):
            for rec in js.get("data", {}).get(zone, []):
                rows.append((rec["timestamp"], zone, rec["price"]))
        cur = nxt
    if not rows:
        return pd.DataFrame(columns=["price_ee", "price_fi", "price_lv"])
    df = pd.DataFrame(rows, columns=["ts", "zone", "price"])
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df = df.pivot_table(index="ts", columns="zone", values="price")
    # Alates 1.10.2025 on hinnad 15-minutilised -> tunnikeskmine
    df = df.resample("1h").mean()
    df.columns = [f"price_{c}" for c in df.columns]
    return df


def prices() -> pd.DataFrame:
    """Ajaloolised + juba avaldatud (homsed) hinnad. Puhverdatakse kettale."""
    cache = config.CACHE_DIR / "prices.csv"
    now = datetime.now(timezone.utc)
    end = (now + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    if cache.exists():
        old = pd.read_csv(cache, index_col=0, parse_dates=True)
        old.index = pd.to_datetime(old.index, utc=True)
        start = (old.dropna(subset=["price_ee"]).index.max() - pd.Timedelta(days=3)).to_pydatetime()
    else:
        old = None
        start = pd.Timestamp(config.TRAIN_START, tz="UTC").to_pydatetime()
    log.info("Laen hinnad %s .. %s", start.date(), end.date())
    new = _fetch_prices_range(start, end)
    df = new if old is None else pd.concat([old[old.index < new.index.min()] if len(new) else old, new])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.to_csv(cache)
    return df


# --------------------------------------------------------------------------- ilm
WEATHER_VARS = ["wind_speed_100m", "temperature_2m", "shortwave_radiation"]


def _weather_call(url, params) -> pd.DataFrame:
    lats = ",".join(str(p[1]) for p in config.WEATHER_POINTS)
    lons = ",".join(str(p[2]) for p in config.WEATHER_POINTS)
    js = _get(url, {"latitude": lats, "longitude": lons, "hourly": ",".join(WEATHER_VARS),
                    "timezone": "GMT", "wind_speed_unit": "ms", **params})
    if isinstance(js, dict):
        js = [js]
    frames = []
    for (group, _, _), loc in zip(config.WEATHER_POINTS, js):
        h = loc["hourly"]
        f = pd.DataFrame(h).assign(group=group)
        f["time"] = pd.to_datetime(f["time"], utc=True)
        frames.append(f)
    raw = pd.concat(frames)
    return aggregate_weather(raw)


def aggregate_weather(raw: pd.DataFrame) -> pd.DataFrame:
    """Punktide andmed -> tunnused (grupi keskmised)."""
    g = raw.groupby(["time", "group"])[WEATHER_VARS].mean().unstack("group")
    out = pd.DataFrame(index=g.index)
    for grp in ("wind_ee", "wind_lv", "wind_fi"):
        out[grp] = g[("wind_speed_100m", grp)]
    out["temp_ee"] = g[("temperature_2m", "city_ee")]
    out["temp_fi"] = g[("temperature_2m", "city_fi")]
    out["solar"] = g["shortwave_radiation"][["city_ee", "city_lv"]].mean(axis=1)
    out.index.name = "ts"
    return out


def weather() -> pd.DataFrame:
    """Ajalugu (ERA5 arhiiv, puhverdatud) + viimased 3 kuud ja 8 päeva prognoosi."""
    cache = config.CACHE_DIR / "weather_archive.csv"
    archive_end = (datetime.now(timezone.utc) - timedelta(days=7)).date()
    if cache.exists():
        arch = pd.read_csv(cache, index_col=0, parse_dates=True)
        arch.index = pd.to_datetime(arch.index, utc=True)
        start = (arch.index.max() - pd.Timedelta(days=2)).date()
    else:
        arch, start = None, pd.Timestamp(config.TRAIN_START).date()
    if start < archive_end:
        log.info("Laen ilmaarhiivi %s .. %s", start, archive_end)
        new = _weather_call(config.OPEN_METEO_ARCHIVE, {"start_date": str(start), "end_date": str(archive_end)})
        arch = new if arch is None else pd.concat([arch, new])
        arch = arch[~arch.index.duplicated(keep="last")].sort_index()
        arch.to_csv(cache)

    log.info("Laen ilmaprognoosi")
    fc = _weather_call(config.OPEN_METEO_FORECAST, {"past_days": 92, "forecast_days": config.HORIZON_DAYS + 2})
    # Prognoosi-API andmed on värskemad ja järjepidevad tulevikuga -> eelistame neid
    df = pd.concat([arch[arch.index < fc.index.min()], fc]) if arch is not None else fc
    return df[~df.index.duplicated(keep="last")].sort_index()


# --------------------------------------------------------------------------- FI prognoos
def fi_forecast() -> pd.Series:
    """Sähkövatkaini Soome hinnaprognoos €/MWh-des ilma käibemaksuta. Tühi, kui pole kättesaadav."""
    try:
        js = _get(config.FI_FORECAST_URL)
    except Exception as e:  # noqa: BLE001
        log.warning("Sähkövatkaini prognoosi ei saanud: %s", e)
        return pd.Series(dtype=float, name="fi_forecast")
    s = pd.Series({pd.Timestamp(int(ts), unit="ms", tz="UTC"): v * 10 / config.FI_VAT for ts, v in js},
                  name="fi_forecast")
    return s.sort_index()


# --------------------------------------------------------------------------- ülekandevõimsus
def _cap_value(rec: dict):
    """Ühe rea võimsus (MW). Eelistame NTC-d (füüsiline piir), muidu ATC-d; suurem suund."""
    ntc = [v for k, v in rec.items() if "ntc" in k.lower() and isinstance(v, (int, float))]
    atc = [v for k, v in rec.items() if "atc" in k.lower() and isinstance(v, (int, float))]
    vals = ntc or atc
    return max(vals) if vals else None


def _fetch_capacity_range(start: datetime, end: datetime) -> pd.DataFrame:
    rows = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=30), end)
        js = _get(config.ELERING_CAPACITY_URL, {
            "start": cur.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "end": nxt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        })
        data = js.get("data", {}) if isinstance(js, dict) else {}
        for zone in ("FI", "LV"):
            for rec in data.get(zone, data.get(zone.lower(), [])) or []:
                v = _cap_value(rec)
                if v is not None and "timestamp" in rec:
                    rows.append((rec["timestamp"], zone.lower(), v))
        cur = nxt
    if not rows:
        return pd.DataFrame(columns=["cap_fi", "cap_lv"])
    df = pd.DataFrame(rows, columns=["ts", "zone", "cap"])
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df = df.pivot_table(index="ts", columns="zone", values="cap").resample("1h").mean()
    df.columns = [f"cap_{c}" for c in df.columns]
    return df


def capacity() -> pd.DataFrame:
    """EE<->FI ja EE<->LV ülekandevõimsus (MW) Eleringist. Puhverdatakse kettale.
    Tühi DataFrame, kui andmeid ei saa (mudel töötab siis ilma nende tunnusteta)."""
    cache = config.CACHE_DIR / "capacity.csv"
    now = datetime.now(timezone.utc)
    end = (now + timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        if cache.exists():
            old = pd.read_csv(cache, index_col=0, parse_dates=True)
            old.index = pd.to_datetime(old.index, utc=True)
            start = (old.dropna(how="all").index.max() - pd.Timedelta(days=3)).to_pydatetime()
        else:
            old = None
            start = pd.Timestamp(config.TRAIN_START, tz="UTC").to_pydatetime()
        log.info("Laen ülekandevõimsused %s .. %s", start.date(), end.date())
        new = _fetch_capacity_range(start, end)
        if old is not None and len(new):
            df = pd.concat([old[old.index < new.index.min()], new])
        else:
            df = new if old is None else old
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df.to_csv(cache)
        return df
    except Exception as e:  # noqa: BLE001
        log.warning("Ülekandevõimsusi ei saanud: %s", e)
        return pd.DataFrame(columns=["cap_fi", "cap_lv"])


# --------------------------------------------------------------------------- UMM katkestused
def _g(d: dict, name, default=None):
    """Tõstutundetu võtme lugemine (API kasutab camelCase'i, mõni klient PascalCase'i)."""
    if not isinstance(d, dict):
        return default
    if name in d:
        return d[name]
    low = name.lower()
    for k, v in d.items():
        if k.lower() == low:
            return v
    return default


def _border_of(unit: dict):
    """Mis piiriga on tegu: 'fi', 'lv' või None. Otsib EIC-koode ja nimesid kogu objektist."""
    s = json.dumps(unit, ensure_ascii=False)
    sl = s.lower()
    if config.EIC_EE not in s and "estonia" not in sl and '"ee"' not in sl and "estlink" not in sl:
        return None
    if "estlink" in sl or config.EIC_FI in s or "finland" in sl or '"fi"' in sl:
        return "fi"
    if config.EIC_LV in s or "latvia" in sl or '"lv"' in sl:
        return "lv"
    return None


_UTYPE = {"1": "planeeritud", "planned": "planeeritud", "2": "ootamatu", "unplanned": "ootamatu"}


def _utc(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")


def _inactive(msg: dict) -> bool:
    if _g(msg, "isOutdated"):
        return True
    st = str(_g(msg, "eventStatus", "")).lower()
    return any(w in st for w in ("dismiss", "cancel", "inactive"))


def umm_outages(start: pd.Timestamp, end: pd.Timestamp) -> list:
    """Nord Pooli UMM-teadetest EE piiride katkestused ajavahemikus.
    Tagastab listi: {border, name, start, stop, unavailable_mw, available_mw, type}."""
    items, skip, limit = [], 0, 200
    try:
        for _ in range(15):
            js = _get(config.NORDPOOL_UMM_URL, {
                "areas": config.EIC_EE, "includeOutdated": "false",
                "eventStopDate": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": limit, "skip": skip,
            })
            page = _g(js, "items", []) if isinstance(js, dict) else (js or [])
            items.extend(page)
            if len(page) < limit:
                break
            skip += limit
    except Exception as e:  # noqa: BLE001
        log.warning("UMM-teateid ei saanud: %s", e)
        return []

    # Iga sõnumi viimane versioon
    latest = {}
    for m in items:
        mid = _g(m, "messageId") or id(m)
        if mid not in latest or (_g(m, "version") or 0) >= (_g(latest[mid], "version") or 0):
            latest[mid] = m

    out = []
    for m in latest.values():
        if _inactive(m):
            continue
        utype = str(_g(m, "unavailabilityType", ""))
        for unit in _g(m, "transmissionUnits", []) or []:
            border = _border_of(unit)
            if not border:
                continue
            installed = _g(unit, "installedCapacity")
            for tp in _g(unit, "timePeriods", []) or []:
                try:
                    t0, t1 = _utc(_g(tp, "eventStart")), _utc(_g(tp, "eventStop"))
                except Exception:  # noqa: BLE001
                    continue
                if t1 <= start or t0 >= end:
                    continue
                unav = _g(tp, "unavailableCapacity")
                avail = _g(tp, "availableCapacity")
                if unav is None and installed is not None and avail is not None:
                    unav = installed - avail
                if unav is None:
                    continue
                out.append({"border": border, "name": _g(unit, "name", ""), "start": t0, "stop": t1,
                            "unavailable_mw": float(unav), "available_mw": avail,
                            "type": _UTYPE.get(utype.lower(), utype)})
    n_tr = sum(1 for m in latest.values() if _g(m, "transmissionUnits"))
    log.info("UMM: %d sõnumit (%d ülekandeteadet), %d EE piiri katkestust prognoosiperioodis",
             len(latest), n_tr, len(out))
    if latest and not n_tr:
        # Diagnostika: kui väljanimed erinevad oodatust, näeb logist, mis kujul andmed tulid
        log.info("UMM näidis-sõnumi väljad: %s", sorted(next(iter(latest.values())).keys()))
    return out


def outage_hours(outages: list, index: pd.DatetimeIndex) -> pd.DataFrame:
    """Katkestuste summa (MW) tunni kaupa piiri järgi."""
    df = pd.DataFrame(0.0, index=index, columns=["unav_fi", "unav_lv"])
    for o in outages:
        mask = (index >= o["start"].floor("h")) & (index < o["stop"])
        df.loc[mask, f"unav_{o['border']}"] += o["unavailable_mw"]
    return df
