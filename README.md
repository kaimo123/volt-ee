# volt-ee — Eesti elektrihinna 7 päeva prognoos EMHASS-ile ja Home Assistantile

Nord Pool avaldab börsihinnad ainult **järgmise päeva** kohta. Aku, elektriauto või boileri
optimeerimiseks (nt [EMHASS](https://emhass.readthedocs.io)) on aga kasulik näha kaugemale.

**volt-ee** on oma serveris töötav teenus, mis:

* ennustab Nord Pooli **Eesti hinnapiirkonna** börsihinda **tunni kaupa 7 päeva ette**
  (koos vahemikuga p10/p50/p90),
* kasutab teadaolevaid **kinnitatud hindu** seal, kus need on olemas (täna ja homme),
* arvestab **ülekandeühenduste katkestusi** (Estlink, EE–LV),
* annab Home Assistantile andurid ja **EMHASS-ile valmis hinnaprognoosi €/kWh-des** koos
  Elektrilevi võrgutasude (päev/öö/tipuajad), kõigi lisatasude ja käibemaksuga.

Idee pärineb Soome projektist [Sähkövatkain](https://sahkovatkain.web.app)
([lähtekood](https://github.com/vividfog/nordpool-predict-fi)), mille Soome prognoosi kasutatakse siin ka sisendina.

---

## Kuidas see töötab

```
Elering ──────── EE/FI/LV hinnad, ülekandevõimsused ─┐
Open-Meteo ───── tuul 100 m (EE/LV/FI), temp, päike ─┤
Sähkövatkain ─── Soome hinnaprognoos ────────────────┼─> XGBoost ─> forecast.json ─> nginx :8088
Nord Pool UMM ── Estlinki / EE-LV katkestused ───────┘  (p10/p50/p90)        │
                                                                              ▼
                       Home Assistant: andurid + volt_ee.jinja ─> EMHASS (€/kWh, koos tasudega)
```

1. Server (Docker) laeb andmed, treenib mudeli ja kirjutab `forecast.json` — vaikimisi
   3× päevas ja alati konteineri käivitumisel.
2. Home Assistant loeb faili iga 15 min järel (REST-andur).
3. Mall `volt_ee.jinja` lisab börsihinnale sinu tasud ja annab EMHASS-ile hinnad.

---

## 1. Serveri paigaldus

**Nõuded:** Docker + Docker Compose, ~1 GB vaba RAM-i treenimise ajal, ~2 GB kettaruumi.
Proxmoxis sobib hästi [community-scripts](https://community-scripts.org) **Docker LXC**
(konteiner, mitte VM; ketas 8 GB).

```bash
git clone https://github.com/kaimo123/volt-ee.git
cd volt-ee
docker compose up -d --build
docker compose logs -f forecaster
```

Esimene käivitus laeb alla ajaloo alates 02/2025 (hinnad, ilm, ülekandevõimsused,
UMM-teated) ja võtab paar minutit. Kui logis on `Valmis ... holdout MAE ...`, on prognoos olemas:

```
http://SERVERI_IP:8088/forecast.json
```

**Uuendamine:**
```bash
cd volt-ee && git pull && docker compose up -d --build
```

**Proxmoxis:** `pct enter <ID>` viib konteinerisse ilma paroolita. Kontrolli, et
LXC **Options → Start at boot** oleks *Yes*. Docker taaskäivitab teenuse ise
(`restart: unless-stopped`).

### Uuendamise ajad

Vaikimisi `06:15, 14:15, 20:15` (Eesti aeg). Homsed hinnad avaldatakse ~14:00.
Muutmiseks loo `docker-compose.override.yml` (seda `git pull` ei puuduta):

```bash
cat > docker-compose.override.yml <<'EOF'
services:
  forecaster:
    environment:
      RUN_TIMES: "00:15,03:15,06:15,09:15,12:15,14:30,15:15,18:15,21:15"
EOF
docker compose up -d
```

---

## 2. Home Assistant

Kõik failid on kaustas [`homeassistant/`](homeassistant/).

| Fail | Kuhu | Mis |
|---|---|---|
| `volt_ee.jinja` | `/config/custom_templates/volt_ee.jinja` | tasude ja tariifi arvutus, EMHASS-i vorming |
| `volt_ee.yaml` | `/config/packages/volt_ee.yaml` | andurid (asenda `SERVERI_IP`) |
| `card_lopphind.yaml` | armatuurlaud → Käsitsi | lõpphinna graafik |
| `card.yaml` | armatuurlaud → Käsitsi | börsihinna graafik koos p10–p90 vahemikuga |
| `card_koos_prognoosiga.yaml` | armatuurlaud → Käsitsi | Nord Pooli täna/homme 15-min hinnad + 7 p prognoos ühel graafikul |

1. Kopeeri **`volt_ee.jinja`** kausta `/config/custom_templates/`.
2. Kopeeri **`volt_ee.yaml`** kausta `/config/packages/` ja asenda `SERVERI_IP`.
3. Kui packages pole lubatud, lisa `configuration.yaml`-i:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
4. **Arendaja tööriistad → YAML → Kontrolli konfiguratsiooni → Taaskäivita.**

### Tasud (`input_number` olemid)

Mall loeb tasud samadest olemitest, mida kasutab tavaline Eesti `nordpool_import` mall.
Ühik peab olema sama mis `sensor.nordpool_sell` hindadel (atribuut `price_in_cents`:
`true` → c/kWh, muidu €/kWh).

| Olem | Tähendus |
|---|---|
| `input_number.elektri_edastamine_paev` | võrgutasu, päev |
| `input_number.elektri_edastamine_oo` | võrgutasu, öö ja nädalavahetus |
| `input_number.elektri_edastamine_paev_tipuaeg` | võrgutasu, talvine päevane tipp |
| `input_number.elektri_edastamine_oo_puhkepaeva_tipuaeg` | võrgutasu, talvine puhkepäeva tipp |
| `input_number.taastuvenergia_tasu` | taastuvenergia tasu |
| `input_number.aktsiis` | elektriaktsiis |
| `input_number.tasakaalustamisvoimsuse_kulu` | tasakaalustamisvõimsuse tasu |
| `input_number.varustuskindluse_tasu` | varustuskindluse tasu |
| `input_number.minu_ostu_marginaal` | müüja marginaal (ost) |
| `input_number.minu_muugi_marginaal` | *valikuline* — marginaal müügil |
| `input_number.kaibemaks` | käibemaks, % |

**Elektrilevi ajad:** päev suveajal 8–24, talveajal 7–23 (määratakse iga tunni enda järgi);
nädalavahetus ja riigipühad (`calendar.estonia`) = öö; novembrist märtsini tööpäeviti
9–12 ja 16–20 päevane tipp, puhkepäeviti 16–20 puhkepäeva tipp.

### Andurid

| Olem | Väärtus |
|---|---|
| `sensor.elektrihind_ee_prognoos` | praeguse tunni börsihind €/MWh; atribuutides kogu prognoos (`hourly`, `daily`, `meta`) |
| `sensor.elektrihind_ee_lopphind` | praeguse tunni lõpphind (koos tasude ja km-ga); atribuut `raw_all` 8 päeva |
| `sensor.elektrihind_ee_homme_keskmine` | homse börsihinna keskmine |
| `sensor.elektrihind_ee_odavaim_paev` | järgmise 7 päeva odavaim päev |
| `sensor.elektri_uhenduste_katkestused` | eelseisvate katkestuste arv; atribuut `kirjeldus` |
| `binary_sensor.elektrihind_ee_odav_tund` | sees, kui praegu on päeva 6 odavama tunni seas |

---

## 3. EMHASS

Mall annab EMHASS-ile hinnad kujul **ajatempel → €/kWh**
(`{"2026-10-16T09:00:00+00:00": 0.1734, ...}`). EMHASS loeb iga väärtuse kehtivaks
kuni järgmise ajatemplini, seega sobib see **15-, 30- ja 60-minutilise**
`optimization_time_step`-iga ning algab alati praegusest tunnist.

| Makro | Mis |
|---|---|
| `volt_import(N)` | ostuhind N tunniks: börs + marginaal + võrgutasu + tasud, × km |
| `volt_export(N)` | müügihind N tunniks: börs − müügimarginaal |
| `volt_import_raw(N)` | ostuhind `[{"start","end","value"}]` kujul anduri ühikus (graafikute jaoks) |

**Kontroll** — Arendaja tööriistad → Mall:
```jinja
{% from 'volt_ee.jinja' import volt_import %}{{ volt_import(48) }}
```

**Näide** (`configuration.yaml` või pakett):
```yaml
rest_command:
  emhass_mpc:
    url: http://EMHASS_IP:5000/action/naive-mpc-optim
    method: post
    content_type: application/json
    timeout: 120
    payload: >
      {% from 'volt_ee.jinja' import volt_import, volt_export %}
      {
        "prediction_horizon": 96,
        "load_cost_forecast": {{ volt_import(48) }},
        "prod_price_forecast": {{ volt_export(48) }}
      }
```

`prediction_horizon` on **sammudes**: 30 min sammuga 96 = 48 h, 60 min sammuga 48 = 48 h.
Hoia `volt_import(N)` tundide arv vähemalt sama pikk kui horisont tundides — muidu
EMHASS lükkab liiga lühikese prognoosi tagasi. Lisa ülejäänud parameetrid
(`soc_init`, PV- ja koormusprognoos jne) nagu sinu EMHASS-i seadistus nõuab.

Kinnitatud (täna/homme) ja prognoositud tunnid tulevad samast allikast; MPC arvutab plaani
iga käivitusega uuesti, nii et kui homsed hinnad saabuvad, kasutatakse kohe neid.

---

## 4. Mudel ja andmed

| Allikas | Mis | Võti |
|---|---|---|
| [Elering](https://dashboard.elering.ee) | EE/FI/LV börsihinnad, EE↔FI/LV ülekandevõimsus | ei |
| [Open-Meteo](https://open-meteo.com) | tuul 100 m, temperatuur, päikesekiirgus (arhiiv + prognoos) | ei |
| [Sähkövatkain](https://sahkovatkain.web.app) | Soome hinnaprognoos | ei |
| [Nord Pool UMM](https://umm.nordpoolgroup.com) | ülekandeühenduste ja jaamade katkestused | ei |

* **Mudel:** XGBoost kvantiilregressioon (p10, p50, p90), treenitakse iga käivitusega uuesti.
  Uuemaid andmeid kaalutakse rohkem (poolestusaeg 180 päeva), sest turg muutus pärast
  Balti sünkroniseerimist (02/2025) ja 15-min hindu (10/2025).
* **Tunnused:** kellaaeg, nädalapäev, hooaeg, Eesti riigipühad, tuul (EE/LV/FI + 6 h keskmine),
  temperatuur (EE/FI), päike, **Soome hind** (treenimisel tegelik, ennustamisel Sähkövatkaini
  prognoos; kus see ei ulatu, kasutatakse baasmudelit — JSON-is `"model": "base"`).
* **Ülekandeühendused** (`cap_fi`, `cap_lv`, MW): ajalugu ja homne päev Eleringist;
  kaugemale tavavõimsus (30 p mediaan) miinus UMM-teadetes teada olevad katkestused.
  Sama üksuse kattuvaid teateid ei liideta; summa piiratakse füüsilise võimsusega.
* **Eesti jaamad** (`gen_out_ee`): UMM-teadetest. *Vt piiranguid allpool.*
* **Automaatne tunnuste valik:** valikulised tunnused, millel treeningandmetes sisu pole
  (alla 1% erinevaid väärtusi), jäetakse välja. Kasutatud tunnused: `meta.holdout.features`.
* Üle 60 päeva kestvad "katkestused" (konserveeritud jaamad) jäetakse välja (`MAX_OUTAGE_DAYS`).

---

## 5. JSON-i formaat

```jsonc
{
  "meta": {
    "generated_at_utc": "2026-10-15T12:12:39Z",
    "unit": "EUR/MWh (ilma km-ta, börsihind)",
    "holdout": {                      // viimase 30 päeva test, mida mudel ei näinud
      "full": {"mae": 21.2, "coverage_p10_p90": 0.69, "naive_week_ago_mae": 63.1},
      "base": {"mae": 29.6, "coverage_p10_p90": 0.74, "naive_week_ago_mae": 63.1},
      "features": ["hour", "...", "cap_fi", "cap_lv", "fi_price"]
    },
    "live_mae_by_day_ahead": {"1": 25.3, "2": 38.0},  // päris ennustuste viga päevade kaupa
    "outages": [{"border": "EE-FI", "name": "Estlink 2", "start": "2026-10-18T03:00",
                 "stop": "2026-10-20T03:00", "unavailable_mw": 650, "type": "planeeritud"}]
  },
  "hourly": [
    {"ts_utc": "2026-10-17T09:00Z", "local": "2026-10-17T12:00",
     "p10": 18.4, "p50": 33.6, "p90": 44.9,
     "actual": null, "confirmed": false, "price": 33.6, "model": "full",
     "cap_fi": 1016, "cap_lv": 1250, "gen_out_ee": 0}
  ],
  "daily": [{"date": "2026-10-17", "mean": 51.0, "min": 11.6, "max": 107.4,
             "cheapest_hour": "03:00", "confirmed": false}]
}
```

`price` = kinnitatud hind, kui see on teada, muidu p50. Kõik hinnad €/MWh, ilma km-ta.

---

## 6. Täpsus ja diagnostika

* **`meta.holdout`** — viga viimasel 30 päeval. Võrdle `naive_week_ago_mae`-ga
  ("sama tund nädal tagasi"); mudel peab olema selgelt parem.
  NB: holdout kasutab Soome *tegelikku* hinda, seega on päris viga suurem.
* **`meta.live_mae_by_day_ahead`** — iga käivituse ennustused logitakse
  (`data/cache/prediction_log.csv`) ja võrreldakse hiljem päris hindadega.
  **Ausaim mõõdik**; täitub mõne päevaga. Võti = mitu päeva ette (1 = homme).
* **`coverage_p10_p90`** peaks olema ~0,8.

Diagnostika (UMM-teated, katkestuste statistika, eelseisvad katkestused):
```bash
docker compose exec forecaster python -m voltee.diag
```

Käsitsi käivitus:
```bash
docker compose exec forecaster python -m voltee.run
```

---

## 7. Teadaolevad piirangud

* **Hinnapiigid** (äkilised rikked, tuulevaikus + pakane) jäävad sageli mööda. Suund
  ("reede odav, esmaspäev kallis") tuleb tavaliselt hästi välja.
* **Eesti jaamad (Auvere, Narva plokid):** Nord Pooli UMM-is on neid teateid vaid 2025. aasta
  alguseni, seega tunnus `gen_out_ee` jääb praegu automaatselt välja. Lahendus on
  ENTSO-E Transparency Platform (vt edasiarendus).
* **Kaugemate päevade vahemik** võib olla liiga kitsas, sest mudel usaldab Soome prognoosi.
* Ülekandeühenduste suuri katkestusi on ajaloos vähe — mõju võib olla alahinnatud.

---

## 8. Seaded (keskkonnamuutujad)

| Muutuja | Vaikimisi | Tähendus |
|---|---|---|
| `RUN_TIMES` | `06:15,14:15,20:15` | uuendamise kellaajad (Eesti aeg) |
| `HORIZON_DAYS` | `7` | mitu päeva ette |
| `TRAIN_START` | `2025-02-09` | treeningandmete algus |
| `MAX_OUTAGE_DAYS` | `60` | pikemad UMM-katkestused jäetakse välja |
| `FI_FORECAST_URL` | Sähkövatkaini `prediction.json` | Soome prognoos |
| `DATA_DIR` | `/data` (Dockeris) | andmete ja puhvri kaust |

Puhvri saab julgelt kustutada (`rm -rf data/cache`); see laetakse uuesti.

---

## 9. Tõrkeotsing

| Probleem | Lahendus |
|---|---|
| `no configuration file provided` | oled vales kaustas — `cd ~/volt-ee` |
| HA andur on `unknown` | ava brauseris `http://SERVERI_IP:8088/forecast.json`; kontrolli IP-d ja porti |
| homne päev pole `confirmed` | Nord Pool / Elering hilines; järgmine ajakava käivitus võtab selle |
| `Ülekandevõimsusi ei saanud` / `UMM-teateid ei saanud` | ajutine võrguviga; mudel töötab edasi ilma nende tunnusteta |
| `volt_import` tagastab `{}` | `sensor.elektrihind_ee_prognoos` ei tööta või mallid laadimata |

Ilma Dockerita / testimiseks:
```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python -m voltee.run --demo   # sünteetilised andmed, võrku pole vaja
```

---

## 10. Edasiarendus

* **ENTSO-E Transparency Platform** (tasuta token): Eesti jaamade katkestused, tarbimis- ja
  tuuletoodangu prognoos.
* Soome hinna müra treenimisel, et kaugemate päevade vahemik oleks realistlikum.
* Läti/Leedu hinnapiirkonnad.

Koodi struktuur: `voltee/config.py` (seaded, ilmapunktid), `fetch.py` (andmeallikad),
`model.py` (tunnused, mudel), `run.py` (käivitus, JSON, ajakava), `diag.py` (diagnostika),
`demo.py` (sünteetilised testandmed).

---

## Allikad ja tänud

* [Sähkövatkain / nordpool-predict-fi](https://github.com/vividfog/nordpool-predict-fi) — idee ja Soome prognoos
* [Elering](https://dashboard.elering.ee) — hinnad ja ülekandevõimsused
* [Open-Meteo](https://open-meteo.com) — ilmaandmed (CC BY 4.0; tasuta mitteäriliseks kasutuseks)
* [Nord Pool UMM](https://umm.nordpoolgroup.com) — turuteated

Prognoos on mudeli väljund, mitte finantsnõuanne.
