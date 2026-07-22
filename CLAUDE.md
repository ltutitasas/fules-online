# CLAUDE.md

Šis failas – išsamus projekto manualas Claude Code ateities sesijoms.
Atsakinėk vartotojui lietuvių kalba.

## Kas tai

Lietuvos sporto naujienų agregatorius. Scrape'ina LT sporto klubų/federacijų
svetaines, deduplikuoja, rodo dashboard'ą, siunčia Telegram + naršyklės
notificationus ir leidžia vienu paspaudimu publikuoti straipsnius į sportas.lt adminą.

- **Gyvas URL**: https://fules-online2.vercel.app (senas fules-online Vercel
  account'as užblokuotas – jo deploy status GitHube visada „failure", žiūrėti
  „Vercel – fules-online2" statusą)
- **GitHub repo**: https://github.com/ltutitasas/fules-online (deploy automatinis per Vercel push'inus į main)
- **Hostingas**: Vercel Hobby planas (⚠️ 10s serverless timeout limitas!)
- **Saugykla**: Vercel KV (Upstash Redis, REST API)

## Failų struktūra

| Failas | Paskirtis |
|---|---|
| `api/_sites_config.py` | **VIENINTELĖ saitų konfigūracijos vieta** (SITES, HTTP_SITES, slim_art). Importuoja ir api/index.py, ir run_http.py. ⚠️ Pabraukimas pavadinime BŪTINAS – be jo Vercel bandytų failą paversti atskira funkcija; šakninių failų Vercel į bundle neįtraukia (buvo FUNCTION_INVOCATION_FAILED) |
| `api/index.py` | Flask app, RSS+HTTP scraperiai, frontend HTML/JS, Service Worker, sportas.lt publikavimas. Vienas didelis failas. |
| `scraper/run_http.py` | Atskiras HTTP-only scraperis GitHub Actions'ui (be Vercel 10s limito) |
| `scraper/run.py` | Senas pilnas scraperis (GitHub Actions `scrape.yml`, retai naudojamas, turi SAVO seną saitų kopiją) |
| `.github/workflows/scrape-http.yml` | HTTP scraperio workflow (workflow_dispatch, palaiko `cycles` input) |
| `.github/workflows/scrape.yml` | Senas workflow su schedule (GitHub jį throttlina, nepatikimas) |

## Atsarginė kopija / rollback

Stabili versija prieš 2026-06-11 optimizaciją išsaugota:
- **Git tag**: `stabili-2026-06-11`
- **Šaka**: `backup-stabili` (GitHube)

Grįžimas nelaimės atveju:
```bash
git checkout main
git reset --hard stabili-2026-06-11
git push --force origin main        # Vercel automatiškai deploy'ins seną versiją
```
Arba be force (saugiau, istorija išlieka):
```bash
git revert --no-edit <blogo_commito_hash>..HEAD && git push origin main
```
KV suderinamumas grįžtant: senas kodas naujus raktus ignoruoja, o `recent_ids`
per 1–2 min (kitas cron runas) perrašys į savo seną formatą. Straipsniai be
`html_content` publikuojami per URL fallback – veikia abiejose versijose.

## Architektūra: dviejų scraperių sistema

Dėl Vercel 10s limito scrape'inimas padalintas:

```
cron-job.org Job 1 (kas ~1-2 min)
  → POST https://fules-online2.vercel.app/api/cron-rss
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
| `articles` | JSON list | 2 d. | Visi straipsniai (max 300), **SLIM** – be html_content/text, surūšiuoti pagal datą |
| `html:{id}` | JSON string | 2 d. | RSS straipsnio HTML turinys publikavimui (atskirai, kad articles būtų mažas) |
| `articles_meta` | JSON dict | 2 d. | {count, first, ts} – `/api/version` polling'ui |
| `seen_ids` | SET | 30 d. | Matytų straipsnių ID (md5(url+title)) – deduplication (tikrinama per SMISMEMBER) |
| `seen_urls` | SET | 30 d. | Matyti URL (apsauga nuo redaguotų pavadinimų) |
| `dates_cache` | JSON dict | 30 d. | {id: iso_data} – straipsniams be datos |
| `first_seen` | JSON dict | 30 d. | {id: iso} – kada MES pirmą kartą pamatėme |
| `recent_ids` | JSON **dict** | 3 h | {id: iso} – KAUPIAMAS (ne perrašomas!), įrašai senesni nei 3h išmetami kiekvieno runo metu |
| `scrape_status` | JSON dict | 7 d. | {site: {ok, n}} – kada saitas paskutinį kartą grąžino straipsnių (`/api/scrape-status`). „ok" atnaujinamas tik kas ~10 min (KV komandų taupymas) |
| `html_ids` | JSON dict | 2 d. | {id: iso kada įrašytas html:{id}} – indeksas vietoj EXISTS lavinos (buvo ~200 EXISTS/runą = 93% kvotos!). Įrašas galioja 46h, tada html perrašomas |
| `tl_hist` | JSON dict | 7 d. | {url: {title, full:[sakiniai], versions:[{ts,title,added,removed,title_from}]}} – Top Lyga (renotify_on_text saitų) versijų istorija „kas pasikeitė". Rašo TIK `run_http.py` ir TIK kai straipsnis pakito (id ∈ new_ids); rodo `/api/tl-history`. Max 25 URL / 15 versijų |

Visi KV skaitymai/rašymai scraperiuose eina per **pipeline** (`_kv_pipeline` /
`kv_pipeline`) – vienas HTTP request'as vietoj 5-6 round-trip'ų.

### ⚠️ KV komandų taupymo režimas (2026-07-18, Upstash Free 500K kom./mėn!)

Upstash Free planas – **500 000 komandų/mėn**; pipeline NETAUPO (kiekviena komanda
skaičiuojama atskirai), bet **MGET su N raktų = 1 komanda**. Kvota buvo išnaudota
3 kartus (06-19, 07-09, 07-18), todėl abu scraperiai optimizuoti – tipinis runas
be naujienų = **4 komandos** (MGET + SCARD + 2 SMISMEMBER, 0 rašymų):
- Visi GET sujungti į vieną MGET (skaitymas PO fetch'o, vienu pipeline su SMISMEMBER).
- `recent_ids`/`scrape_status` rašomi TIK pasikeitus (scrape_status „ok" – 10 min tikslumu).
- `EXPIRE` TTL pratęsimai – tik ~kas 30-tą runą (`random() < 0.033`), ne kas runą.
- `EXISTS html:{id}` lavina (~200 kom./runą!) pakeista `html_ids` indeksu (žr. lentelę).
- `/api/version` ir `/api/articles` – 1 MGET vietoj 2 GET.
NEGRĄŽINTI besąlyginių SET/EXPIRE kas runą ir nepridėti naujų per-straipsnį KV
komandų – kvota vėl baigsis per 1-2 savaites.

⚠️ `seen_ids` TTL buvo 7 d. – seni straipsniai "atgydavo" ir lipdavo į viršų.
Dabar 30 d. + merged sort pagal datą sprendžia šią problemą.

## Saitų konfigūracija (`api/_sites_config.py`)

### RSS saitai (scrape'ina Vercel kas ~1 min)
⚽ FK Banga, FC Džiugas, FC Hegelmann, FK Panevėžys, FK Sūduva, FK TransINVEST,
FA Šiauliai, FK Žalgiris, LFF
🏀 BC Kibirkštis, BC Neptūnas, BC Lietkabelis, BC Šiauliai, Utenos Juventus,
Lietuva Basketball, BC Rytas
🏅 Lengvoji atletika (lengvoji.lt), LTU Aquatics (ltuaquatics.com)

### HTTP saitai (scrape'ina GitHub Actions kas 2 min)
⚽ Top Lyga (toplyga.lt), Žalgiris futbolas (zalgiris.lt)
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
- `wp_featured_api: True` – nuotrauka per WP REST (`/wp-json/wp/v2/posts?slug=X&_embed`)
  vietoj HTML scrape. Lėtiems WP saitams (fksuduva.lt HTML ~3-4s netelpa į rss 2s
  timeout, REST ~1.5s); grąžina originalaus dydžio featured nuotrauką
- `text_selector` – CSS selektorius tekstui (pvz. BC Kibirkštis `.entry-summary`
  kad nedubliuotų title/autoriaus)
- `also_vercel: True` – HTTP saitą scrape'ina IR Vercel /api/cron-rss (LKL atvejis –
  lkl.lt blokuoja GitHub Actions IP)
- `renotify_on_rename: True` – pervadintas straipsnis (tas pats URL, naujas title)
  laikomas NAUJA naujiena (Top Lyga: "X – Y (GYVAI)" → rezultatas tuo pačiu URL).
  Be šio flag'o seen_urls apsauga pervadinimus nutyli. Merge visada dedup'ina pagal
  URL – pervadinimas pakeičia seną įrašą, dublikato nelieka.

### Naujo saito pridėjimas (dažniausias darbas!)
1. Į `SITES` (failas `api/_sites_config.py`) pridėti dict su `rss` (jei WordPress – beveik visada yra `/feed/`)
2. Vartotojas pateiks sportas.lt **šaltinio ID** (`<option value="X">pavadinimas</option>`) → `sportas_source`
3. Jei reikia specialių kategorijų → `_SITE_CATS_OVERRIDE` (žr. žemiau)
4. Jei vartotojas pateiks **foto šaltinio ID** → `_SOURCE_MAP` (funkcijoje `upload_photo`)
5. Commit + push → Vercel autodeploy

## sportas.lt integracija

⚠️ Nuo 2026-07 adminas pasiekiamas tik su `?ileisk=1` (be jo `/Admin/login` – 404).
`_session()` prideda `sess.params = {"ileisk": "1"}` prie visų admin užklausų.

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
- Puslapis polls **`/api/version`** kas 10s (`_checkNew()`) – mažytis atsakymas
  (~100 B); pilnas `/api/articles` siunčiamas TIK kai versija pasikeitė. SW žadinamas kas 30s.
- **Notification siunčiamas TIK naujai ATSIRADUSIEMS recent ID** (`added` =
  recentIds, kurių nebuvo ankstesniame poll'e). NE kai recent sąrašas susitraukia
  (3h langas baigėsi) ar persirikiuoja – kitaip būtų pakartotiniai notificationai.
- Multi-tab apsauga: `localStorage` raktas `notifiedIds` (JSON list, max 100 ID) –
  pirma kortelė įrašo, kitos mato ir nebesiunčia
- `_lastRecent` formatas: `"id1,id2|count|firstId"` – localStorage ir atmintyje
  formatai PRIVALO sutapti (buvo bug'as)
- SW turi savo in-memory `_swPrevRecent`/`_swNotified` – naršyklei nužudžius SW,
  baseline atsistato per INIT žinutę iš puslapio

### Auth (APP_TOKEN)
- Publikavimo/admin endpointai (`/api/post`, `/api/upload-photo`, `/api/photos`,
  `/api/sources`, `/api/refresh`, `/api/photo-debug`, `/api/tg-test`, `/api/tl-history`, visi debug)
  reikalauja `X-App-Token` headerio arba `?token=` (curl'ui), kai Vercel env
  nustatytas `APP_TOKEN`. **Kol APP_TOKEN nenustatytas – viskas atvira (kaip anksčiau).**
- Frontend tokeną laiko `localStorage.appToken`; gavęs 401 paprašo per `prompt()`.
- `/api/article-text` atviras, bet priima tik mūsų saitų domenus (ne atviras proxy).
- Atviri lieka: `/`, `/sw.js`, `/api/articles`, `/api/version`, `/api/scrape-status`,
  `/api/cron`, `/api/cron-rss` (juos kviečia cron-job.org be tokeno!).

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
8. **`articles` KV yra SLIM** – be `html_content`/`text`. Publikavimui HTML imamas iš
   `html:{id}` rakto, o jo nesant – fallback parsisiunčia iš straipsnio URL (`_do_post`).
9. **Paveikslai cache'inami** – og:image/image_selector fetch daromas tik straipsniams,
   kurių paveikslo dar nėra KV `articles` (kitaip kas runą siųstųsi dešimtys puslapių).
10. **`recent_ids` – dict, kaupiamas** – NEPERRAŠYTI plikų list'u, kitaip badge/notificationai
    vėl suges (senas bug'as: badge dingdavo per 1-2 min vietoj 3h).
11. **`anthropic` išimtas iš requirements.txt** (AI išjungtas) – įjungiant `_ai_enrich`,
    paketą reikia grąžinti.
12. **`lxml` su fallback** – Vercel'yje lxml realiai NEUŽSIKRAUNA (nors requirements.txt yra),
    kodas persijungia į `html.parser`. GitHub Actions ir lokaliai lxml veikia.
    Patikrinti: `/api/health` rodo aktyvų parserį.
13. **Vercel build/deploy būseną** galima patikrinti be Vercel CLI per GitHub API:
    `curl -s https://api.github.com/repos/ltutitasas/fules-online/commits/<sha>/status`
    ⚠️ Bendra `state` visada „failure", nes prie repo dar prikabintas užblokuotas
    senas `fules-online` account'as. Žiūrėti `statuses[]` įrašą su kontekstu
    **„Vercel – fules-online2"** – jo „success" = deploy gyvas; „failure" =
    build krito ir gyvas liko SENAS deploy!
14. **Iš Word įkeltas turinys (rytasvilnius.lt)** – tekstas ne `<p>`, o `div.s3`/`div.s8`
    blokuose, žodžiai suskaldyti `<span>` gabalais per vidurį. Todėl teksto ištraukimas
    renka ir „lapinius" div (be blokinių vaikų, žr. `_is_wrapper_div`), o plain tekstui
    naudojamas `_el_text` – get_text BE separatoriaus (get_text(" ") darydavo „20 17m.",
    „pasikeit ė"). `_html_to_sportas` span'us unwrap'ina.

## Debug įrankiai

- `/api/debug-fetch?site=SaitoVardas&token=...` – parodo HTTP statusą, HTML ilgį,
  selektorių matches, html_preview. Naudinga aiškinantis kodėl saitas negrąžina straipsnių.
- `/api/scrape-status` – kada kiekvienas saitas paskutinį kartą grąžino straipsnių
  (jei saito nėra arba `ok` senas – saitas tyliai miręs, tikrinti debug-fetch).
- `/api/version` – greitas patikrinimas ar scraperiai gyvi (`ts` articles_meta viduje).
- `/api/cron-rss` atsakymo `img_dbg` – og/REST nuotraukų eilės diagnostika:
  {cand, tried:[saitai], resolved}. Eilė ribota 6/runą (Vercel 10s apsauga), kandidatai
  tik KV sąraše esantys/nauji straipsniai (feed'ų seni įrašai, kuriuos merged[:300]
  nukerta, eilės neužima – buvo badavimo bug'as).
- `/api/article-text` atsakymo `up_status` – upstream HTTP statusas (WAF diagnostikai).
- `/api/tl-history` – Top Lyga versijų istorija: kiekvieno mačo laiko juosta su diff'u
  (➕ pridėti / ➖ pašalinti sakiniai, 📝 pavadinimo pakeitimai X→Y). Token'u apsaugotas.
  Duomenis kaupia `run_http.py` į `tl_hist` (tik pokyčio metu, 0 naujų HTTP – tekstas
  imamas iš jau atsisiųsto `fetch_text_sig` puslapio). Frontend mygtukas „📜 Top Lyga pakeitimai".
- GitHub Actions logs: https://github.com/ltutitasas/fules-online/actions
- cron-job.org dashboard – execution history (200 OK / 204 No Content = OK)

## Env kintamieji / Secrets

| Kintamasis | Kur |
|---|---|
| `KV_REST_API_URL`, `KV_REST_API_TOKEN` | Vercel env + GitHub secrets |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Vercel env + GitHub secrets |
| `APP_TOKEN` | Vercel env – publikavimo endpointų apsauga (nenustatytas = atvira) |
| GitHub PAT (`ghp_...`) | cron-job.org Job 2 header |
| `CYCLES` | GitHub Actions workflow input – scrape ciklų sk. viename rune (default 1) |

## Deployment

```bash
cd /Users/macbook/Downloads/fules-online
git add <failai> && git commit -m "..." && git push origin main
# Vercel deploy automatinis (~30-60s). GitHub Actions ima naują kodą kitame runs.
```
