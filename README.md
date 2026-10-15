# volt-ee — Eesti elektrihinna 7 päeva prognoos oma serveris

Iseseisev teenus, mis ennustab Nord Pooli **Eesti hinnapiirkonna** börsihinda
tunni kaupa 7 päeva ette ja serveerib tulemust JSON-ina Home Assistantile.
Idee on sama mis [Sähkövatkainil](https://sahkovatkain.web.app) Soome jaoks.

## Kuidas see töötab

```
Elering API ───── EE/FI/LV hinnad (ajalugu + homne) ─┐
Open-Meteo ────── tuul 100 m (EE, LV, FI), temp, päike ┼─> XGBoost kvantiilmudel ─> forecast.json ─> nginx ─> Home Assistant
Sähkövatkain ──── Soome hinnaprognoos ───────────────┘      (p10 / p50 / p90)
```

* **Sihtmuutuja:** Eesti tunnihind €/MWh (15-min hinnad keskmistatakse tunniks).
* **Tunnused:** kellaaeg, nädalapäev, hooaeg, Eesti riigipühad, tuulekiirus 100 m
  kõrgusel Eesti, Läti ja Soome tuulealadel, temperatuur Tallinnas/Tartus ja Helsingis,
  päikesekiirgus, ning **Soome hind**.
* **Soome hinna nipp:** EE hind liigub FI hinnaga tugevalt koos. Treenimisel kasutatakse
  tegelikku FI hinda, ennustamisel Sähkövatkaini FI prognoosi. Kus see ei ulatu
  (tavaliselt viimased 1–2 päeva), kasutatakse ilma FI hinnata baasmudelit
  (JSON-is `"model": "base"`).
* **Ülekandeühendused:** EE↔FI (Estlink 1+2) ja EE↔LV saadaolev võimsus (MW) on mudeli
  tunnused (`cap_fi`, `cap_lv`). Ajalugu ja homne päev tulevad Eleringi
  `cross-border-capacity` API-st. Kaugemate päevade jaoks võetakse tavavõimsus (viimase 30 päeva
  mediaan) ja lahutatakse Nord Pooli UMM-teadetest (`ummapi.nordpoolgroup.com`, ilma võtmeta)
  teada olevad plaanilised ja ootamatud katkestused. Katkestuste nimekiri on JSON-is
  `meta.outages` all ja HA-s andur `sensor.elektri_uhenduste_katkestused`.
* **Eesti jaamad:** Auvere, Eesti, Balti, Iru jt jaamade katkestused (MW kokku) tulevad samuti
  Nord Pooli UMM-teadetest ja on tunnus `gen_out_ee` - nii treeningajaloos kui prognoosis.
  Teated puhverdatakse faili `data/cache/umm.json`.
  NB: suuri katkestusi on ajaloos vähe, seega võib mudel nende mõju alahinnata.
* **Kinnitatud hinnad:** kui Nord Pool on homse hinna avaldanud (~14:00), näidatakse
  päris hinda (`confirmed: true`) ja prognoos algab ülehomsest.
* Uuemaid andmeid kaalutakse rohkem (poolestusaeg 180 päeva), sest turg on pärast
  Euroopa sagedusalaga liitumist (02/2025) ja 15-min hindu (10/2025) muutunud.

## Paigaldus (Docker)

```bash
cd volt-ee
docker compose up -d --build
docker compose logs -f forecaster
```

Esimene käivitus laeb alla ~1,5 aasta hinnad ja ilma (paar minutit). Seejärel
uueneb prognoos kell 06:15, 14:15 ja 20:15. Tulemus:

    http://SERVERI_IP:8088/forecast.json

Andmed ja puhver on kaustas `./data` (võib julgelt kustutada, laetakse uuesti).

### Ilma Dockerita

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m voltee.run              # üks käivitus -> data/public/forecast.json
python -m voltee.run --schedule   # jääb tööle (nt systemd teenusena)
python -m voltee.run --demo       # test ilma võrguta, sünteetilised andmed
```

Raspberry Pi 4/5 peal toimib (treenimine ~1 min).

## Home Assistant

1. Kopeeri `homeassistant/volt_ee.yaml` faili `/config/packages/volt_ee.yaml`,
   asenda `SERVERI_IP`. Kui packages pole veel lubatud, lisa `configuration.yaml`-i:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
2. Taaskäivita HA. Tekivad:
   * `sensor.elektrihind_ee_prognoos` — praeguse tunni hind, atribuutides kogu prognoos
   * `sensor.elektrihind_ee_homme_keskmine`
   * `sensor.elektrihind_ee_odavaim_paev` — järgmise 7 päeva odavaim päev
   * `binary_sensor.elektrihind_ee_odav_tund` — kas praegu on päeva 6 odavama tunni seas
3. Graafik: paigalda HACS-ist **apexcharts-card** ja lisa kaart `homeassistant/card.yaml` sisuga.

Hinnad on **börsihinnad ilma käibemaksu ja võrgutasudeta** (€/MWh; c/kWh = /10).

### EMHASS

`homeassistant/volt_ee.jinja` teisendab prognoosi EMHASS-i vormingusse
(€/kWh, ajatempel → hind, lisab marginaali, võrgutasu päev/öö, muud tasud ja km).

1. Kopeeri fail HA-s kausta `/config/custom_templates/volt_ee.jinja`.
2. **Muuda faili alguses olevad tasud oma lepingu järgi.**
3. Arendaja tööriistad → YAML → **Laadi uuesti mallid**.
4. Kasuta EMHASS-i kutses, nt:

```yaml
rest_command:
  emhass_mpc:
    url: http://EMHASS_IP:5000/action/naive-mpc-optim
    method: post
    content_type: application/json
    timeout: 60
    payload: >
      {% from 'volt_ee.jinja' import volt_import, volt_export %}
      {
        "prediction_horizon": 48,
        "load_cost_forecast": {{ volt_import(48) }},
        "prod_price_forecast": {{ volt_export(48) }}
      }
```

Kontroll: Arendaja tööriistad → Mall, kleebi
`{% from 'volt_ee.jinja' import volt_import %}{{ volt_import(48) }}`.

Ajatemplitega kuju töötab nii 30- kui 60-minutilise `optimization_time_step`-iga.
`prediction_horizon` sammudes: 30 min sammuga 48 = 24 h, 60 min sammuga 48 = 48 h.

## JSON-i formaat

```jsonc
{
  "meta": {
    "generated_at_utc": "...",
    "holdout": {                       // viimase 30 päeva test (mudel neid päevi ei näinud)
      "full": {"mae": 38.2, "coverage_p10_p90": 0.74, "naive_week_ago_mae": 61.0},
      "base": {...}
    },
    "live_mae_by_day_ahead": {"1": 30.1, "2": 41.7, ...}   // päris ennustuste viga, kogub end ise
  },
  "hourly": [
    {"ts_utc": "2026-10-17T09:00Z", "local": "2026-10-17T12:00",
     "p10": 18.4, "p50": 33.6, "p90": 44.9,
     "actual": null, "confirmed": false, "price": 33.6, "model": "full"}
  ],
  "daily": [{"date": "2026-10-17", "mean": 51.0, "min": 11.6, "max": 107.4,
             "cheapest_hour": "03:00", "confirmed": false}]
}
```

`price` = kinnitatud hind, kui on teada, muidu p50. Automaatikates kasuta seda.

## Täpsuse jälgimine

* `meta.holdout` — kas mudel on parem kui naiivne "sama tund nädal tagasi" (`naive_week_ago_mae`).
  Kui `mae` ei ole sellest selgelt väiksem, pole mudelist kasu.
* `meta.live_mae_by_day_ahead` — iga käivituse ennustused logitakse (`data/cache/prediction_log.csv`)
  ja võrreldakse hiljem päris hindadega. Ausaim mõõdik; täitub mõne päevaga.
* `coverage_p10_p90` peaks olema ~0,8. Kui palju madalam, on vahemik liiga kitsas.

Ootused: hinnapiike (Estlinki rikked, tuulevaikus + külm) ilmast ei näe, need jäävad mööda.
Suund ("reede odav, esmaspäev kallis") tuleb tavaliselt hästi välja.

## Edasiarendus

* `voltee/config.py` → `WEATHER_POINTS`: lisa/muuda ilmapunkte.
* `voltee/model.py` → `build_features`: lisa tunnuseid. Head kandidaadid:
  Estlink/EE-LV ülekandevõimsus ja Olkiluoto töö (ENTSO-E Transparency, tasuta token),
  Eleringi tarbimisplaan (`/api/system/with-plan`), gaasi hind.
* Kui lisad tunnuse, millel ajalugu on, aga tulevikku mitte, lisa see ainult `FULL_FEATURES`-i.
