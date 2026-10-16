"""Seaded. Kõik on üle kirjutatav keskkonnamuutujatega (vt docker-compose.yml)."""
import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
CACHE_DIR = DATA_DIR / "cache"
PUBLIC_DIR = DATA_DIR / "public"

LOCAL_TZ = "Europe/Tallinn"

# Treeningandmete algus. Balti riigid liitusid Euroopa sagedusalaga 9.02.2025,
# seetõttu on varasemad andmed vähem esinduslikud.
TRAIN_START = os.getenv("TRAIN_START", "2025-02-09")

# Mitu päeva ette ennustada (alates tänasest südaööst)
HORIZON_DAYS = int(os.getenv("HORIZON_DAYS", "7"))

# Kellaajad (kohalik aeg), millal ajakava ennustuse uuesti teeb.
# 14:15 = pärast Nord Pooli homsete hindade avaldamist.
RUN_TIMES = [t.strip() for t in os.getenv("RUN_TIMES", "06:15,14:15,20:15").split(",") if t.strip()]

# Sähkövatkaini Soome prognoos (c/kWh, sisaldab Soome km 25,5%)
FI_FORECAST_URL = os.getenv("FI_FORECAST_URL", "https://sahkovatkain.web.app/prediction.json")
FI_VAT = float(os.getenv("FI_VAT", "1.255"))

ELERING_URL = "https://dashboard.elering.ee/api/nps/price"
ELERING_CAPACITY_URL = "https://dashboard.elering.ee/api/transmission/cross-border-capacity"
NORDPOOL_UMM_URL = os.getenv("NORDPOOL_UMM_URL", "https://ummapi.nordpoolgroup.com/messages")

# ENTSO-E Transparency Platform token (valikuline). Pane see serveris faili .env:
#   ENTSOE_TOKEN=xxxxxxxx-xxxx-...
# Ilma tokenita töötab kõik muu; Eesti jaamade katkestused jäävad siis välja.
ENTSOE_TOKEN = os.getenv("ENTSOE_TOKEN", "").strip()
# Väiksemaid jaamu (nt üksikud tuulikud) ei arvestata
ENTSOE_MIN_MW = float(os.getenv("ENTSOE_MIN_MW", "20"))

# Pikemad UMM-katkestused on püsimuutused (konserveeritud jaam), mitte hooldus -> välja
MAX_OUTAGE_DAYS = int(os.getenv("MAX_OUTAGE_DAYS", "60"))

# Hinnapiirkondade EIC-koodid (UMM-teadetes)
EIC_EE = "10Y1001A1001A39I"
EIC_FI = "10YFI-1--------U"
EIC_LV = "10YLV-1001A00074"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

# Ilmapunktid: (grupp, laius, pikkus). Gruppide keskmistest saavad tunnused.
WEATHER_POINTS = [
    # Eesti tuulealad
    ("wind_ee", 58.95, 23.55),   # Läänemaa
    ("wind_ee", 58.35, 22.40),   # Saaremaa
    ("wind_ee", 58.90, 22.60),   # Hiiumaa
    ("wind_ee", 59.35, 27.00),   # Ida-Virumaa
    # Läti rannik
    ("wind_lv", 56.90, 21.20),   # Kurzeme
    ("wind_lv", 57.40, 21.60),   # Ventspils
    # Soome tuulealad (Pohjanmaa)
    ("wind_fi", 64.70, 24.60),
    ("wind_fi", 62.80, 21.40),
    ("wind_fi", 65.70, 24.60),
    # Temperatuur ja päike
    ("city_ee", 59.44, 24.75),   # Tallinn
    ("city_ee", 58.38, 26.72),   # Tartu
    ("city_fi", 60.17, 24.94),   # Helsingi
    ("city_lv", 56.95, 24.10),   # Riia
]
