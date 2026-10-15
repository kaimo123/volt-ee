"""Diagnostika: mis on UMM-puhvris ja kas katkestuste andmed on mõistlikud.

Kasutus (Dockeris):  docker compose exec forecaster python -m voltee.diag
Loeb ainult kettal olevat puhvrit (data/cache), võrku ei kasuta.
"""
import collections
import json

import pandas as pd

from . import config, fetch


def main():
    cache = config.CACHE_DIR / "umm.json"
    if not cache.exists():
        print("UMM-puhvrit pole veel. Käivita enne: python -m voltee.run")
        return
    msgs = list(json.loads(cache.read_text()).get("messages", {}).values())
    g = fetch._g
    print(f"UMM-sõnumeid puhvris: {len(msgs)}")

    # --- Tootmisüksused: mis jaamad on sees
    units = collections.Counter()
    unit_mw = collections.defaultdict(float)
    long_events = collections.Counter()
    for m in msgs:
        if fetch._inactive(m):
            continue
        for u in (g(m, "productionUnits", []) or []) + (g(m, "generationUnits", []) or []):
            if not fetch._is_ee_unit(u):
                continue
            name = g(u, "name", "?")
            units[name] += 1
            for tp in g(u, "timePeriods", []) or []:
                unit_mw[name] = max(unit_mw[name], float(g(tp, "unavailableCapacity") or 0))
                try:
                    if fetch._utc(g(tp, "eventStop")) - fetch._utc(g(tp, "eventStart")) > pd.Timedelta(
                            days=config.MAX_OUTAGE_DAYS):
                        long_events[name] += 1
                except Exception:  # noqa: BLE001
                    pass
    print(f"\nEesti tootmisüksused UMM-teadetes ({len(units)} tk), enim teateid:")
    for name, n in units.most_common(20):
        extra = f", {long_events[name]} pikka (>{config.MAX_OUTAGE_DAYS} p, välja jäetud)" if long_events[name] else ""
        print(f"  {name:30s} {n:5d} teadet, suurim katkestus {unit_mw[name]:6.0f} MW{extra}")

    # --- Ajalugu: kui palju jaamu tüüpiliselt maas
    start = pd.Timestamp(config.TRAIN_START, tz="UTC")
    now = pd.Timestamp.now(tz="UTC").floor("h")
    idx = pd.date_range(start, now, freq="1h")
    outs = fetch.parse_outages(msgs, start, now + pd.Timedelta(days=31))
    hist = fetch.outage_hours(outs, idx)
    gen = hist["unav_gen"]
    print(f"\nEesti jaamad maas ajaloos ({start.date()}..{now.date()}):")
    print(f"  tunde, kus midagi maas: {100 * (gen > 0).mean():.0f}%   keskmine {gen.mean():.0f} MW   "
          f"p90 {gen.quantile(.9):.0f} MW   max {gen.max():.0f} MW")
    for b, label in (("fi", "EE-FI"), ("lv", "EE-LV")):
        s = hist[f"unav_{b}"]
        print(f"  {label}: katkestusega tunde {100 * (s > 0).mean():.1f}%, max {s.max():.0f} MW")

    # --- Kas katkestused mõjutasid hinda (lihtne võrdlus samadel kellaaegadel)
    pc = config.CACHE_DIR / "prices.csv"
    if pc.exists():
        pr = pd.read_csv(pc, index_col=0, parse_dates=True)["price_ee"]
        pr.index = pd.to_datetime(pr.index, utc=True)
        df = pd.DataFrame({"p": pr, "gen": gen}).dropna()
        if len(df):
            big = df["gen"] >= 200
            print("\nEE keskmine hind (€/MWh):")
            print(f"  kui Eesti jaamu oli maas >=200 MW: {df.loc[big, 'p'].mean():6.1f}  ({big.sum()} tundi)")
            print(f"  muul ajal:                         {df.loc[~big, 'p'].mean():6.1f}  ({(~big).sum()} tundi)")

    # --- Eelseisvad katkestused (30 päeva)
    up = [o for o in outs if o["stop"] > now]
    print(f"\nEelseisvad katkestused järgmise 30 päeva jooksul ({len(up)}):")
    lbl = {"fi": "EE-FI", "lv": "EE-LV", "gen": "jaam"}
    for o in sorted(up, key=lambda o: o["start"])[:40]:
        print(f"  {lbl[o['border']]:5s} {o['name'][:28]:28s} "
              f"{o['start'].tz_convert(config.LOCAL_TZ):%d.%m %H:%M} -> "
              f"{o['stop'].tz_convert(config.LOCAL_TZ):%d.%m %H:%M}  -{o['unavailable_mw']:.0f} MW  {o['type']}")


if __name__ == "__main__":
    main()
