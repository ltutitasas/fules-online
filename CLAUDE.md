# CLAUDE.md

Šis failas – išsamus projekto manualas Claude Code ateities sesijoms.
Atsakinėk vartotojui lietuvių kalba.

## Kas tai

Lietuvos sporto naujienų agregatorius. Scrape'ina LT sporto klubų/federacijų
svetaines, deduplikuoja, rodo dashboard'ą, siunčia Telegram + naršyklės
notificationus ir leidžia vienu paspaudimu publikuoti straipsnius į sportas.lt adminą.

- **Gyvas URL**: https://fules-online.vercel.app
- **GitHub repo**: https://github.com/ltutitasas/fules-online (deploy automatinis per Vercel push'inus į main)
- **Hostingas**: Vercel Hobby planas (⚠️ 10s serverless timeout limitas!)
- **Saugykla**: Vercel KV (Upstash Redis, REST API)

## Failų struktūra

| Failas | Paskirtis |
|---|---|
| `api/index.py` | VISKAS: Flask app, RSS+HTTP scraperiai, frontend HTML/JS, Service Worker, sportas.lt publikavimas. Vienas didelis failas. |
| `scraper/run_http.py` | Atskiras HTTP-only scraperis GitHub Actions'ui (be Vercel 10s limito) |
| `scraper/run.py` | Senas pilnas scraperis (GitHub Actions `scrape.yml`, retai naudojamas) |
| `.github/workflows/scrape-http.yml` | HTTP scraperio workflow (workflow_dispatch) |
| `.github/workflows/scrape.yml` | Senas workflow su schedule (GitHub jį throttlina, nepatikimas) |

## Architektūra: dviejų scraperių sistema

Dėl Vercel 10s limito scrape'inimas padalintas:

```
cron-job.org Job 1 (kas ~1-2 min)
  → POST https://fules-online.vercel.app/api/cron-rss
  → run_scraper(mode="rss") – TIK RSS saitai, telpa į <10s

cron-job.org Job 2 (kas 2 min)
  → POST https://api.github.com/repos/ltutitasas/fules-online/actions/workflows/scrape-http.yml/dispatches
     Headers: Authorization: token ghp_xxx (GitHub PAT su repo+workflow teisėm)
     Body: {"ref":"main"}
  → GitHub Actions paleidžia scraper/run_http.py – HTTP saitai, ~20-25s
```

**SVARBU – merge strategija**: abu scraperiai rašo į tą patį KV raktą `articles`.
Niekada ne-overwrite'ina: `merged = nauji + [seni kurių id nesikartoja]`, tada
`merged.sort(key=_sort_key, reverse=True)` ir saugoma `merged[:300]`.
Be šito vienas scraperis ištrintų kito straipsnius.

**Kodėl ne GitHub Actions schedule**: free repo scheduled workflows GitHub
throttlina (vietoj kas 20 min realiai kas 8-12h). workflow_dispatch per API – patikimas.

## Vercel KV raktai

| Raktas | Tipas | TTL | Paskirtis |
|---|---|---|---|
| `articles` | JSON list | 2 d. | Visi straipsniai (max 300), surūšiuoti pagal datą |
| `seen_ids` | SET | 30 d. | Matytų straipsnių ID (md5(url+title)) – deduplication |
| `seen_urls` | SET | 30 d. | Matyti URL (apsauga nuo redaguotų pavadinimų) |
| `dates_cache` | JSON dict | 30 d. | {id: iso_data} – straipsniams be datos |
| `first_seen` | JSON dict | 30 d. | {id: iso} – kada MES pirmą kartą pamatėme |
| `recent_ids` | JSON list | 3 h | "Naujų" straipsnių ID – notificationams |

⚠️ `seen_ids` TTL buvo 7 d. – seni straipsniai "atgydavo" ir lipdavo į viršų.
Dabar 30 d. + merged sort pagal datą sprendžia šią problemą.

## Saitų konfigūracija (`_SITES` api/index.py)

### RSS saitai (scrape'ina Vercel kas ~1 min)
⚽ FK Banga, FC Džiugas, FC Hegelmann, FK Panevėžys, FK Sūduva, FK TransINVEST,
FA Šiauliai, FK Žalgiris, LFF
🏀 BC Kibirkštis, BC Neptūnas, BC Lietkabelis, BC Šiauliai, Utenos Juventus,
Lietuva Basketball, BC Rytas
🏅 Lengvoji atletika (lengvoji.lt), LTU Aquatics (ltuaquatics.com)

### HTTP saitai (scrape'ina GitHub Actions kas 2 min)
⚽ Top Lyga (toplyga.lt), Žalgiris futbolas (zalgiris.lt), FK Riteriai
🏀 LKL (lkl.lt), Žalgiris (zalgiris.lt), KK Nevėžis, BC Jonava
🏒 Hockey Lietuva (hockey.lt)

❌ **LTOK užkomentuotas** – Cloudflare JS challenge blokuoja ir Vercel, ir GitHub Actions IP.

### Site dict raktai
- `rss` – RSS feed URL (WordPress `/feed/`)
- `method: "http"` + `url` + `selectors` ARBA `link_pattern`/`link_pattern_re` – HTTP scrape
- `sportas_source` – sportas.lt straipsnio šaltinio ID (dropdown "Šaltinis" admine)
- `og_image_fallback: True` – lankytis straipsnio puslapyje paveikslui rasti
- `image_selector` – CSS selektorius herojiniam paveikslui. **PIRMENYBĖ prieš og:image!**
  Saitams su image_selector paveikslas VISADA imamas iš selektoriaus (RSS kūno
  pirma <img> nepatikima – ilguose straipsniuose paima vidinę nuotrauką)
- `text_selector` – CSS selektorius tekstui (pvz. BC Kibirkštis `.entry-summary`
  kad nedubliuotų title/autoriaus)

### Naujo saito pridėjimas (dažniausias darbas!)
1. Į `_SITES` pridėti dict su `rss` (jei WordPress – beveik visada yra `/feed/`)
2. Vartotojas pateiks sportas.lt **šaltinio ID** (`<option value="X">pavadinimas</option>`) → `sportas_source`
3. Jei reikia specialių kategorijų → `_SITE_CATS_OVERRIDE` (žr. žemiau)
4. Jei vartotojas pateiks **foto šaltinio ID** → `_SOURCE_MAP` (funkcijoje `upload_photo`)
5. Commit + push → Vercel autodeploy

## sportas.lt integracija

### Kategorijos (`_SPORT_CATS` + `_SITE_CATS_OVERRIDE`)
```python
_SPORT_CATS = {"krepšinis": [22, 6], "futbolas": [103, 7],
               "ledo ritulys": [10, 99], "kitas sportas": [72, 89]}
_SITE_CATS_OVERRIDE = {
    "BC Kibirkštis":      [6, 49],    # Krepšinis + Moterų krepšinis
    "Lengvoji atletika":  [72, 88],   # Kitas sportas + Lengvoji atletika
    "Lietuva Basketball": [6, 137],   # Krepšinis + Lietuvos rinktinės
    "LTU Aquatics":       [15, 16],   # Vandens sportas + Plaukimas
}
```
Override turi pirmenybę: `cat_ids = _SITE_CATS_OVERRIDE.get(site) or _SPORT_CATS.get(sport, [])`

### Foto šaltiniai (`_SOURCE_MAP` funkcijoje `upload_photo`)
`{"saito vardas": ("foto_šaltinio_id", "parašas nuotr.")}`. Default: `("3", "Organizatorių nuotr.")`

### Teksto formatavimas
- `_html_to_sportas()` konvertuoja HTML į sportas.lt formatą **išsaugant bold/em**
  (negalima naudoti get_text() – pradangina formatavimą; buvo LKL bug'as)
- `_ai_enrich()` – AI tagai ir teksto praturtinimas

## Frontend + notificationai

Frontend (HTML/JS) yra `_INDEX_HTML` stringe, Service Worker – `_SW_JS` stringe (api/index.py).

### Notification logika (daug kartų taisyta – atsargiai!)
- Puslapis polls `/api/articles` kas 10s (`_checkNew()`), SW žadinamas kas 30s
- **Notification siunčiamas TIK kai pasikeičia `recent_ids`** (recentKey dalis),
  NE kai pasikeičia arts.length/arts[0] – kitaip HTTP scraperio merge
  sukeldavo pakartotinį to paties straipsnio notificationą
- Multi-tab apsauga: `localStorage` raktas `lastNotifiedRecent` – pirma kortelė
  įrašo, kitos mato ir nebesiunčia
- `_lastRecent` formatas: `"id1,id2|count|firstId"` – localStorage ir atmintyje
  formatai PRIVALO sutapti (buvo bug'as)

### Telegram
- Siunčia ir Vercel (`run_scraper`), ir GitHub Actions (`run_http.py`) – kiekvienas už savo saitus
- Tik kai `new_ids` netuščias IR `seen_ids` netuščias (pirmo paleidimo apsauga nuo spam)
- Tik straipsniai, kurių `first_seen` < 24h
- Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (Vercel env + GitHub secrets)

## Žinomi spąstai (gotchas!)

1. **Brotli**: `requests` be `brotli` paketo grąžina binary šiukšles jei serveris
   siunčia `br`. VISUR `Accept-Encoding: "gzip, deflate"` (be br!). Buvo hockey.lt/lkl.lt bug'as.
2. **Vercel 10s**: `/api/cron-rss` og:image fallback timeout 2s (rss mode), kitur 4s.
   Nepridėti lėtų operacijų į RSS kelią.
3. **KV tinklo trukdžiai GitHub Actions**: `run_http.py` turi `_kv_retry` (3 bandymai po 4s).
4. **GitHub Actions schedule nepatikimas** – naudoti tik workflow_dispatch per API.
5. **LTOK = Cloudflare 403** – nebandyti įjungti be self-hosted runner ar panašaus sprendimo.
6. **`merged.sort()` būtinas** po merge – kitaip seni straipsniai atsiduria viršuje.
7. **image_selector > og:image** – og:image kartais rodo ne herojinę nuotrauką (FK Žalgiris atvejis).

## Debug įrankiai

- `/api/debug-fetch?name=SaitoVardas` – parodo HTTP statusą, HTML ilgį, selektorių
  matches, html_preview. Naudinga aiškinantis kodėl saitas negrąžina straipsnių.
- GitHub Actions logs: https://github.com/ltutitasas/fules-online/actions
- cron-job.org dashboard – execution history (200 OK / 204 No Content = OK)

## Env kintamieji / Secrets

| Kintamasis | Kur |
|---|---|
| `KV_REST_API_URL`, `KV_REST_API_TOKEN` | Vercel env + GitHub secrets |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Vercel env + GitHub secrets |
| GitHub PAT (`ghp_...`) | cron-job.org Job 2 header |

## Deployment

```bash
cd /Users/macbook/Downloads/fules-online
git add <failai> && git commit -m "..." && git push origin main
# Vercel deploy automatinis (~30-60s). GitHub Actions ima naują kodą kitame runs.
```
