"""ENTSO-E Transparency Platform: Eesti tootmisüksuste (Auvere, Narva plokid jt) katkestused.

Dokumenditüüp A80 (unavailability of generation units), hinnapiirkond EE.
Vajab tokenit (config.ENTSOE_TOKEN). Tulemus puhverdatakse faili data/cache/entsoe_gen.csv:
  - vanem ajalugu (lõpp > 30 päeva tagasi) jääb puhvrisse ega muutu,
  - viimased 30 päeva ja kogu tulevik laetakse igal käivitusel uuesti (teated muutuvad).
"""
import logging

import pandas as pd

from . import config

log = logging.getLogger(__name__)
CACHE = lambda: config.CACHE_DIR / "entsoe_gen.csv"  # noqa: E731
REFRESH_BACK = pd.Timedelta(days=30)
REFRESH_AHEAD = pd.Timedelta(days=60)
COLS = ["mrid", "revision", "name", "unit", "start", "end", "nominal_mw", "avail_mw", "type", "status"]
_BTYPE = {"planned maintenance": "planeeritud", "unplanned outage": "ootamatu"}


def _query(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    from entsoe import EntsoePandasClient
    from entsoe.exceptions import NoMatchingDataError

    client = EntsoePandasClient(api_key=config.ENTSOE_TOKEN, timeout=60, retry_count=3)
    try:
        df = client.query_unavailability_of_generation_units(
            "EE", start=start.tz_convert("Europe/Tallinn"), end=end.tz_convert("Europe/Tallinn"))
    except NoMatchingDataError:
        return pd.DataFrame(columns=COLS)
    if df is None or df.empty:
        return pd.DataFrame(columns=COLS)
    df = df.reset_index(drop=True)
    g = lambda c: df[c] if c in df else None  # noqa: E731
    out = pd.DataFrame({
        "mrid": g("mrid"),
        "revision": pd.to_numeric(g("revision"), errors="coerce"),
        "name": g("production_resource_name"),
        "unit": g("production_resource_psr_name"),
        "start": pd.to_datetime(g("start"), utc=True),
        "end": pd.to_datetime(g("end"), utc=True),
        "nominal_mw": pd.to_numeric(g("nominal_power"), errors="coerce"),
        "avail_mw": pd.to_numeric(g("avail_qty"), errors="coerce"),
        "type": g("businesstype"),
        "status": g("docstatus"),
    })
    return out


def _load_cache() -> pd.DataFrame:
    if not CACHE().exists():
        return pd.DataFrame(columns=COLS)
    df = pd.read_csv(CACHE())
    for c in ("start", "end"):
        df[c] = pd.to_datetime(df[c], utc=True)
    return df


def fetch(offline: bool = False) -> pd.DataFrame:
    """Kõik teadaolevad tootmiskatkestuste lõigud (puhver + värske päring)."""
    cached = _load_cache()
    if offline or not config.ENTSOE_TOKEN:
        return cached
    now = pd.Timestamp.now(tz="UTC").floor("h")
    fresh_from = now - REFRESH_BACK
    try:
        if cached.empty:
            start = pd.Timestamp(config.TRAIN_START, tz="UTC")
            log.info("ENTSO-E: laen jaamade katkestuste ajaloo alates %s (esimene kord)", start.date())
        else:
            start = fresh_from
        new = _query(start, now + REFRESH_AHEAD)
    except Exception as e:  # noqa: BLE001
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code == 401 or "401" in str(e) or "Unauthorized" in str(e):
            log.warning("ENTSO-E: token ei kehti (401). Kontrolli .env faili ENTSOE_TOKEN väärtust.")
        else:
            log.warning("ENTSO-E päring ebaõnnestus, kasutan puhvrit: %s", e)
        return cached
    if cached.empty:
        df = new
    else:
        # Vana ajalugu jääb; viimased 30 päeva ja tulevik asendatakse värskega
        df = pd.concat([cached[cached["end"] < fresh_from], new], ignore_index=True)
    df = df.drop_duplicates()
    df.to_csv(CACHE(), index=False)
    log.info("ENTSO-E: %d katkestuse lõiku (%d värsket), %d üksust", len(df), len(new),
             df["unit"].fillna(df["name"]).nunique())
    return df


def outages(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> list:
    """Lõigud -> sama kuju katkestused nagu fetch.parse_outages (border 'gen')."""
    if df is None or df.empty:
        return []
    d = df.copy()
    # Ainult aktiivsed ja iga dokumendi uusim versioon
    st = d["status"].astype(str).str.lower()
    d = d[~st.isin(["cancelled", "withdrawn"])]
    if d["revision"].notna().any():
        last = d.groupby("mrid")["revision"].transform("max")
        d = d[d["revision"] == last]
    # Konserveeritud/suletud plokid (dokument kestab üle MAX_OUTAGE_DAYS) -> püsimuutus, mitte katkestus
    span = d.groupby("mrid")["end"].transform("max") - d.groupby("mrid")["start"].transform("min")
    d = d[span <= pd.Timedelta(days=config.MAX_OUTAGE_DAYS)]
    d = d[(d["end"] > start) & (d["start"] < end)]
    d["unav"] = (d["nominal_mw"] - d["avail_mw"]).clip(lower=0)
    d = d[d["unav"] >= config.ENTSOE_MIN_MW]
    out = []
    for r in d.itertuples(index=False):
        name = r.unit if isinstance(r.unit, str) and r.unit else r.name
        out.append({"kind": "production", "border": "gen", "name": str(name),
                    "start": r.start, "stop": r.end, "unavailable_mw": float(r.unav),
                    "available_mw": r.avail_mw, "type": _BTYPE.get(str(r.type).lower(), str(r.type))})
    return out
