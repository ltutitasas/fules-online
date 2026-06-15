from flask import Flask, request, jsonify, Response
import json, os, sys, time, requests as _req, hashlib, feedparser
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup as _BS4
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime

# Bendra saitų konfigūracija – api/_sites_config.py (pabraukimas = Vercel
# neeksponuoja kaip atskiros funkcijos, bet įtraukia į bundle).
# try/except – kad importo bėda negriautų viso app; klaida matoma /api/health
_IMPORT_ERR = ""
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _sites_config import SITES as _SITES, slim_art as _slim_art
except Exception as _e:
    _IMPORT_ERR = f"{type(_e).__name__}: {_e}"
    _SITES = []
    _SLIM_FIELDS = ("site","sport","title","url","date","image","source","id","text_selector")
    def _slim_art(a):
        return {k: a[k] for k in _SLIM_FIELDS if a.get(k)}

# lxml ~5-10x greitesnis; fallback į html.parser jei Vercel lxml neturi
try:
    import lxml  # noqa: F401
    _PARSER = "lxml"
except Exception:
    _PARSER = "html.parser"

app = Flask(__name__)

KV_URL        = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN      = os.environ.get("KV_REST_API_TOKEN", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SPORTAS_USER  = os.environ.get("SPORTAS_USER", "")
SPORTAS_PASS  = os.environ.get("SPORTAS_PASS", "")
TG_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT       = os.environ.get("TELEGRAM_CHAT_ID", "")
APP_TOKEN     = os.environ.get("APP_TOKEN", "")

def _auth_ok():
    """Publikavimo/admin endpointų apsauga. Kol APP_TOKEN env nenustatytas – atvira
    (atgalinis suderinamumas). Nustačius: header X-App-Token arba ?token=..."""
    if not APP_TOKEN:
        return True
    tok = request.headers.get("X-App-Token", "") or request.args.get("token", "")
    return tok == APP_TOKEN

def _auth_fail():
    return jsonify({"ok": False, "error": "Neautorizuota – reikia APP_TOKEN"}), 401

def tg_send(text: str) -> dict:
    if not TG_TOKEN or not TG_CHAT:
        return {"ok": False, "error": f"token={'set' if TG_TOKEN else 'MISSING'}, chat={'set' if TG_CHAT else 'MISSING'}"}
    try:
        r = _req.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

_SPORT_CATS = {"krepšinis": [22, 6], "futbolas": [103, 7],
               "ledo ritulys": [10, 99], "kitas sportas": [72, 89]}
# Kategorijų prioriteto override: {sport: {cat_id: priority}}
_SPORT_PRIORITIES = {"ledo ritulys": {10: "1"}, "kitas sportas": {72: "1"}}
_BASE = "https://www.sportas.lt/Admin/Load/UArticles"
_UA   = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ── Vercel KV ──────────────────────────────────────────────────────
def _kv_get(key):
    if not KV_URL: return None
    r = _req.get(f"{KV_URL}/get/{key}",
                 headers={"Authorization": f"Bearer {KV_TOKEN}"}, timeout=5)
    result = r.json().get("result")
    return json.loads(result) if result else None

def _kv_set(key, value, ex=86400):
    _req.post(f"{KV_URL}/pipeline",
              headers={"Authorization": f"Bearer {KV_TOKEN}"},
              json=[["SET", key, json.dumps(value, ensure_ascii=False), "EX", ex]],
              timeout=5)

def _kv_smembers(key):
    if not KV_URL: return set()
    r = _req.get(f"{KV_URL}/smembers/{key}",
                 headers={"Authorization": f"Bearer {KV_TOKEN}"}, timeout=5)
    return set(r.json().get("result") or [])

def _kv_sadd(key, *members):
    if not KV_URL or not members: return
    _req.post(f"{KV_URL}/pipeline",
              headers={"Authorization": f"Bearer {KV_TOKEN}"},
              json=[["SADD", key] + list(members), ["EXPIRE", key, 86400 * 30]],
              timeout=5)

def _kv_pipeline(cmds, timeout=8):
    """Daug Redis komandų vienu HTTP request'u – taupo round-trip'us (svarbu 10s limite)."""
    if not KV_URL or not cmds: return [None] * len(cmds)
    r = _req.post(f"{KV_URL}/pipeline",
                  headers={"Authorization": f"Bearer {KV_TOKEN}"},
                  json=cmds, timeout=timeout)
    r.raise_for_status()   # 4xx/5xx → exception
    data = r.json()
    if not isinstance(data, list):   # Upstash klaida = dict {"error":...}
        raise RuntimeError(f"KV pipeline klaida: {str(data)[:200]}")
    # Upstash grąžina HTTP 200 net kai komanda atmesta (pvz. "max requests limit
    # exceeded") – klaida slypi item viduje. Nebetylim: keliam exception.
    for item in data:
        if isinstance(item, dict) and item.get("error"):
            raise RuntimeError(f"KV komandos klaida: {str(item['error'])[:200]}")
    return [item.get("result") if isinstance(item, dict) else None for item in data]

def _kv_json(raw, default):
    """Pipeline GET rezultatas (string arba None) → Python objektas."""
    if not raw: return default
    try: return json.loads(raw)
    except Exception: return default


# ── AI praturtinimas ───────────────────────────────────────────────
def _ai_enrich(title, text):
    # AI IŠJUNGTAS – pajungti kai bus ANTHROPIC_API_KEY Vercel aplinkoje
    return {"tags": "", "text": text}
    # try:
    #     import anthropic, re as _re
    #     client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    #     prompt = (
    #         f"Sporto straipsnis (lietuvių k.):\nPavadinimas: {title}\n\n"
    #         f"Tekstas:\n{text[:3000]}\n\n"
    #         "1. Sugeneruok 1–4 temas (tagus): asmenų vardai/pavardės, klubų pavadinimai.\n"
    #         "2. Tekste paboldink PIRMĄ kiekvieno asmens vardo paminėjimą "
    #         "(<strong>Vardas Pavardė</strong>).\n\n"
    #         "Atsakyk TIKSLIAI šiuo formatu:\nTAGS: Tag1, Tag2\nTEXT:\npilnas tekstas"
    #     )
    #     resp = client.messages.create(
    #         model="claude-haiku-4-5-20251001", max_tokens=3000,
    #         messages=[{"role": "user", "content": prompt}]
    #     )
    #     raw = resp.content[0].text.strip()
    #     tags, enriched = "", text
    #     m = _re.search(r'^TAGS:\s*(.+)$', raw, _re.MULTILINE)
    #     if m: tags = m.group(1).strip()
    #     m = _re.search(r'^TEXT:\s*\n(.*)', raw, _re.MULTILINE | _re.DOTALL)
    #     if m: enriched = m.group(1).strip()
    #     return {"tags": tags, "text": enriched}
    # except Exception as e:
    #     return {"tags": "", "text": text}


# ── Sportas.lt postinimas ──────────────────────────────────────────
def _match_source(sources, site_name):
    sl = site_name.lower()
    for name, sid in sources.items():
        if name.lower() == sl: return sid
    for name, sid in sources.items():
        if sl in name.lower(): return sid
    words = set(sl.split())
    for name, sid in sources.items():
        if len(name) >= 3 and words & set(name.lower().split()): return sid
    return "1"

def _session():
    from bs4 import BeautifulSoup
    sess = _req.Session()
    sess.headers.update({"User-Agent": _UA})
    if not SPORTAS_USER:
        return sess

    # Bandome atkurti išsaugotas cookies
    cached = _kv_get("sportas_cookies")
    if cached:
        for k, v in cached.items():
            sess.cookies.set(k, v, domain="www.sportas.lt")
        # Patikriname ar sesija dar galioja
        try:
            test = sess.get(f"{_BASE}/editArticle/",
                            allow_redirects=False, timeout=8)
            if test.status_code == 200:
                return sess  # Sesija galioja
        except:
            pass

    # Naujas login
    form_data = {
        "Username": SPORTAS_USER,
        "Password": SPORTAS_PASS,
        "return": "",
        "closeWindow": "",
        "referer": "/Admin/login",
    }
    sess.post("https://www.sportas.lt/Admin/check/",
              data=form_data, allow_redirects=True, timeout=15)
    _kv_set("sportas_cookies", dict(sess.cookies), ex=86400)
    return sess

def _sources(sess):
    cached = _kv_get("sportas_sources")
    if cached: return cached
    from bs4 import BeautifulSoup
    r = sess.get(f"{_BASE}/editArticle/", timeout=15)
    soup = BeautifulSoup(r.text, _PARSER)
    sel  = soup.find("select", {"id": "sourceSelect"})
    result = {}
    if sel:
        for opt in sel.find_all("option"):
            v, t = opt.get("value",""), opt.get_text(strip=True)
            if v and t: result[t] = v
    _kv_set("sportas_sources", result)
    return result

def _html_to_sportas(html_content):
    """Konvertuoja RSS HTML į sportas.lt formatą: <b>→<strong>, <i>→<em>, išlaiko <p> struktūrą.
    Pašalina hyperlinks (<a>→tekstas) ir WordPress footer artefaktus."""
    if not html_content: return ""
    import unicodedata
    html_content = unicodedata.normalize("NFC", html_content)
    soup = _BS4(html_content, _PARSER)
    for t in soup(["script","style","nav","footer","header","aside","iframe","noscript","form","button"]):
        t.decompose()
    # <a> → tik tekstas (pašaliname hyperlinks)
    for tag in soup.find_all("a"):
        tag.replace_with(tag.get_text())
    for tag in soup.find_all("b"): tag.name = "strong"
    for tag in soup.find_all("i"): tag.name = "em"
    parts = []
    for el in soup.find_all(["p","h2","h3","h4","li"]):
        inner = el.decode_contents().strip()
        plain = el.get_text(strip=True)
        if len(plain) > 4:
            parts.append(inner)
    result = "".join(f"<p>{p}</p>" for p in parts[:60])
    # Pašaliname WordPress footer: "The post X appeared first on Y."
    import re as _re
    result = _re.sub(r'<p>\s*The post\s+.+?appeared first on\s+.+?\.?\s*</p>', '', result, flags=_re.DOTALL | _re.IGNORECASE).strip()
    return result

def _do_post(article, photo_path="", photo_title="", photo_tags=""):
    sport    = article.get("sport", "")
    site     = article.get("site", "")
    cat_ids  = _SITE_CATS_OVERRIDE.get(site) or _SPORT_CATS.get(sport, [])
    import unicodedata as _ud
    _nfc = lambda s: _ud.normalize("NFC", s) if s else s
    title        = _nfc(article.get("title", ""))
    text         = _nfc(article.get("text", ""))
    # RSS HTML saugomas atskirame rakte html:{id} (articles sąrašas – slim).
    # Jei rakto nebėra (TTL) – žemiau suveiks fallback iš straipsnio URL.
    html_content = _nfc(article.get("html_content", "") or
                        _kv_get(f"html:{article.get('id','')}") or "")
    photo_title  = _nfc(photo_title)
    photo_tags   = _nfc(photo_tags)

    # Jei yra originalus HTML iš RSS – naudojame jį išsaugant formatavimą
    if html_content:
        html_body = _html_to_sportas(html_content)
        # AI enrichment plain tekstui (tagams)
        enriched = _ai_enrich(title, text or _html_to_text(html_content))
        ai_tags  = enriched.get("tags", "")
    else:
        html_body = ""   # būtina inicializuoti – jei fetch žemiau nepavyks/neras
                         # konteinerio, kitaip UnboundLocalError (buvo Top Lygos bug'as)
        # Jei tekstas tuščias – bandome gauti iš straipsnio URL
        if not text and article.get("url"):
            try:
                r = _req.get(article["url"], headers={"User-Agent": _UA}, timeout=8)
                from bs4 import BeautifulSoup as _BSt
                soup = _BSt(r.text, _PARSER)
                for tag in soup(["script","style","nav","footer","header","aside","iframe","noscript","form","button"]):
                    tag.decompose()
                # Pirma bandome site-specific text_selector (iš article dict arba _SITES config)
                _site_cfg = next((s for s in _SITES if s["name"] == site), {})
                txt_sel = article.get("text_selector", "") or _site_cfg.get("text_selector", "")
                main = (soup.select_one(txt_sel) if txt_sel else None) or \
                       soup.select_one('[class*="post-content"]') or \
                       soup.select_one('[class*="article-content"]') or \
                       soup.select_one('[class*="entry-content"]') or \
                       soup.select_one('[class*="article-body"]') or \
                       soup.select_one('[class*="prose"]') or \
                       soup.select_one('.fck') or \
                       soup.select_one('article') or \
                       soup.select_one('main')
                if main:
                    # Naudojame _html_to_sportas – išsaugo <strong>, <em> formatavimą
                    html_body = _html_to_sportas(str(main))
                    # Plain tekstas tik AI tagams
                    paras_plain = [" ".join(el.get_text(" ", strip=True).split())
                                   for el in main.find_all(["p","h2","h3","h4"])
                                   if len(el.get_text(strip=True)) > 10]
                    text = "\n\n".join(paras_plain[:60])
            except:
                pass

        if not html_body:
            enriched  = _ai_enrich(title, text)
            ai_tags   = enriched.get("tags", "")
            rich_text = enriched.get("text", text)
            paras     = [p.strip() for p in rich_text.split("\n\n") if p.strip()]
            html_body = "".join(f"<p>{p}</p>" for p in paras)
        else:
            enriched = _ai_enrich(title, text)
            ai_tags  = enriched.get("tags", "")
    # Be teksto nepublikuojame – aiški žinutė vietoj tuščio straipsnio sportas.lt
    if not html_body:
        return False, ("Straipsnis dar be teksto (šaltinis paskelbė tik antraštę) – "
                       "pabandykite vėliau, kai tekstas atsiras")
    # Suliejame AI tags + photo modal gaires.
    # sportas.lt tagų su kabutėmis neišsaugo – jas šaliname („Žalgiris“ → Žalgiris).
    # Nuotraukos pavadinime (leadPhoto[title]/photo_title) kabutės lieka kaip įvesta.
    import re as _re_t
    def _clean_tag(t):
        return _re_t.sub(r'[„“”"«»]', '', t).strip()
    combined = ", ".join(filter(None, [ai_tags, photo_tags]))
    tags_list = list(dict.fromkeys(ct for t in combined.split(",") if (ct := _clean_tag(t))))

    if not SPORTAS_USER:
        return False, f"SPORTAS_USER env var nenustatytas | photo_tags={repr(photo_tags)} | ai_tags={repr(ai_tags)} | tags_list={tags_list}"

    sess      = _session()
    # Sportas.lt šaltinio ID: pirma žiūrim rankinį priskyrimą, tada fuzzy match
    explicit = _SITE_SOURCE_OVERRIDE.get(site, "")
    if explicit and explicit.isdigit():
        # Tiesioginis ID
        source_id = explicit
        sources   = {}
    else:
        sources   = _sources(sess)
        if explicit:
            # Tikslus pavadinimas nurodytas – ieškome tik jo
            source_id = sources.get(explicit, _match_source(sources, site))
        else:
            source_id = _match_source(sources, site)

    # Gauname edit puslapį pirma – jo formoje yra lietuviškas laikas
    from bs4 import BeautifulSoup as _BS
    edit_r = sess.get(f"{_BASE}/editArticle/", allow_redirects=True, timeout=15)
    if edit_r.url and "login" in edit_r.url.lower():
        return False, f"Login nepavyko | cookies: {list(sess.cookies.keys())} | redirect: {edit_r.url}"
    if edit_r.status_code != 200:
        return False, f"editArticle grąžino {edit_r.status_code} | url: {edit_r.url}"
    edit_soup = _BS(edit_r.text, _PARSER)

    # Ištraukiame lietuvišką datą/laiką iš sportas.lt serverio formos (jis rodo LT laiką)
    now = datetime.now(timezone(timedelta(hours=3)))  # fallback UTC+3
    start_date_el = edit_soup.find("input", {"id": "publishStartDate"})
    start_time_el = edit_soup.find("input", {"id": "publishStartTime"})
    pub_date = start_date_el.get("value", now.strftime("%Y-%m-%d")) if start_date_el else now.strftime("%Y-%m-%d")
    pub_time = start_time_el.get("value", now.strftime("%H:%M")) if start_time_el else now.strftime("%H:%M")

    # Visi kategorijų ID iš formos (reikia siųsti priority[] visiems)
    _ALL_CAT_IDS = [
        6,167,194,137,20,29,22,47,50,155,49,111,121,122,123,124,
        168,169,172,173,190,191,195,196,198,199,7,175,179,181,182,
        183,184,185,189,192,193,197,32,51,165,103,188,52,53,55,54,
        56,57,176,161,8,23,24,25,26,102,9,34,180,97,35,10,98,99,
        100,101,15,16,18,19,108,91,92,93,94,95,96,156,72,88,76,75,
        77,153,90,89,11,39,40,106,109,134,131,132,170,171,158,159
    ]
    main_cat = str(cat_ids[0]) if cat_ids else "6"

    data = [
        ("id",""),("returnId","-1"),("smartyNow",str(int(time.time()))),
        ("titleSlug",""),("title",title),("extraTitle",""),("facebookTitle",""),
        ("generatedTV3Title",""),("intro",""),("text",html_body),
        ("leadPhoto[path]", photo_path),("leadPhoto[title]", photo_title),("leadPhoto[size]","l"),
        ("cropSize","l"),("leadLiveVideoTime[endDate]",""),("leadLiveVideoTime[endTime]",""),
        ("leadPlayVideo[code]",""),("leadPlayVideo[playId]",""),("leadVideo[url]",""),
        ("attachCustomJs[]",""),("attachFbPost[]",""),
        ("mainCategory", main_cat),
    ]
    for cid in cat_ids: data.append(("categories[]", str(cid)))
    # Siunčiame priority[] visiems kategorijų ID (kaip realus naršyklė)
    for cid in _ALL_CAT_IDS: data.append((f"priority[{cid}]", "1000"))
    # Prioriteto override pagal sportą (pvz. kiti → Ledo ritulys cat 10 = 1)
    for cat_id, priority in _SPORT_PRIORITIES.get(sport, {}).items():
        data.append((f"priority[{cat_id}]", priority))
    data += [
        ("source",str(source_id)),("realSource","0"),("realSource","0"),
        ("disableComments","0"),("commentsForUsers","0"),("isLiveNews","0"),("tags",""),
    ]
    for tag in tags_list: data.append(("tags[]", tag))
    data += [
        ("n18","0"),("sensitive","0"),("top10","0"),("useSpecNews","0"),
        ("orderedArticle","0"),("leftBlocks","0"),("cacheKey",""),
        ("status","1"),("exportArticle","1"),
        ("publish[StartDate]", pub_date),
        ("publish[StartTime]", pub_time),
        ("publish[EndDate]","2030-01-01"),("publish[EndTime]","00:00"),
        ("titlePage","0"),("titlePagePriority","1000"),
    ]

    for inp in edit_soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name", "")
        value = inp.get("value", "")
        if name and not any(k == name for k, v in data):
            data.append((name, value))
    form = edit_soup.find("form")
    action = form.get("action", "") if form else ""
    if action:
        save_url = action if action.startswith("http") else "https://www.sportas.lt" + action
    else:
        save_url = f"{_BASE}/saveArticle"
    save_url = save_url.rstrip("/")
    # Priverstinai koduojame kaip UTF-8 (requests kartais naudoja latin-1 pagal sportas.lt atsakymą)
    import unicodedata as _ud2
    from urllib.parse import urlencode as _ue
    data_nfc = [(k, _ud2.normalize("NFC", v) if isinstance(v, str) else v) for k, v in data]
    data_bytes = _ue(data_nfc, doseq=True).encode("utf-8")
    r = sess.post(save_url, data=data_bytes,
                  allow_redirects=False, timeout=30,
                  headers={"Referer": f"{_BASE}/editArticle/",
                           "Origin": "https://www.sportas.lt",
                           "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    location = r.headers.get("Location", "")
    if r.status_code == 302:
        if "login" in location.lower() or "check" in location.lower():
            return False, f"Login nepavyko – redirect į: {location}"
        _kv_set("sportas_cookies", dict(sess.cookies))
        # Sekame redirect ir tikriname ar nėra klaidos pranešimo
        try:
            follow = sess.get(
                location if location.startswith("http") else "https://www.sportas.lt" + location,
                allow_redirects=True, timeout=10)
            from bs4 import BeautifulSoup as _BSf
            fsoup = _BSf(follow.text, _PARSER)
            # Ieškome klaidos arba sėkmės pranešimo
            alert = fsoup.select_one(".alert, .error, .success, .flash, [class*='message']")
            msg = alert.get_text(strip=True)[:200] if alert else ""
            # Tikriname ar atsirado naujas straipsnis (articleList rodo naujausius)
            first_row = fsoup.select_one("table tr:nth-child(2) td, .list-item:first-child")
            first_title = first_row.get_text(strip=True)[:80] if first_row else ""
            return True, f"redirect:{location} | source:{source_id} | tags:{tags_list} | msg:{msg} | first:{first_title}"
        except Exception as ex:
            return True, f"redirect:{location} | source:{source_id} | tags:{tags_list} | follow_err:{ex}"
    body = r.text[:500].replace("\n", " ").replace("\r", "")
    return False, f"HTTP {r.status_code} | save_url={save_url[:80]} | location={location} | source:{source_id} | body:{body[:200]}"


# ── Inline scraperis (naudojamas /api/refresh) ─────────────────────
_UA  = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_MAX = 10

# _SITES importuojamas iš sites_config.py (bendras su scraper/run_http.py)

# Greitas peržvalgos žodynas: mūsų svetainės pavadinimas → sportas_source
_SITE_SOURCE_OVERRIDE = {s["name"]: s["sportas_source"] for s in _SITES if s.get("sportas_source")}
# Kategorijų override (kai saitui reikia kitų kategorijų nei numatyta pagal sportą)
_SITE_CATS_OVERRIDE = {
    "BC Kibirkštis":      [6, 49],    # Krepšinis + Moterų krepšinis (ne LKL)
    "Lengvoji atletika":  [72, 88],    # Kitas sportas + Lengvoji atletika
    "Lietuva Basketball": [6, 137],   # Krepšinis + Lietuvos rinktinės
    "LTU Aquatics":       [15, 16],   # Vandens sportas + Plaukimas
}

def _art_id(url, title):
    return hashlib.md5(f"{url}{title}".encode()).hexdigest()

def _strip_wp_footer(text: str) -> str:
    """Pašalina WordPress RSS artefaktą: 'The post X appeared first on Y.'"""
    import re as _re
    return _re.sub(r'\s*The post\s+.+?\s+appeared first on\s+.+?\.?\s*$', '', text, flags=_re.DOTALL | _re.IGNORECASE).strip()

def _html_to_text(html):
    if not html: return ""
    soup = _BS4(html, _PARSER)
    for t in soup(["script","style","nav","footer","header","aside"]): t.decompose()
    parts = []
    for el in soup.find_all(["p","h2","h3","h4","li"]):
        txt = " ".join(el.get_text(" ", strip=True).split())
        if len(txt) > 20:
            parts.append(txt)
    result = "\n\n".join(parts) if parts else " ".join(soup.get_text(" ", strip=True).split())
    return _strip_wp_footer(result)

def _fetch_rss(site):
    try:
        # requests su timeout (feedparser.parse(url) timeout neturi – pakibęs
        # feedas kabintų visą funkciją iki Vercel 10s kill)
        rr = _req.get(site["rss"], timeout=6,
                      headers={"User-Agent": _UA, "Accept-Encoding": "gzip, deflate"})
        feed = feedparser.parse(rr.content)
        arts = []
        for e in feed.entries[:_MAX]:
            title = e.get("title","").strip()
            url   = e.get("link","").strip()
            date  = e.get("published", e.get("updated",""))
            image = None
            if hasattr(e,"media_thumbnail") and e.media_thumbnail:
                image = e.media_thumbnail[0].get("url")
            elif hasattr(e,"media_content") and e.media_content:
                for mc in e.media_content:
                    if mc.get("medium") == "image" or mc.get("type","").startswith("image"):
                        image = mc.get("url",""); break
                if not image:
                    image = e.media_content[0].get("url","") or None
            elif hasattr(e,"enclosures") and e.enclosures:
                enc = e.enclosures[0]
                if enc.get("type","").startswith("image"):
                    image = enc.get("href")
            raw_html = ""
            if hasattr(e,"content") and e.content:
                raw_html = e.content[0].get("value","")
            if not raw_html and e.get("summary"):
                raw_html = e.get("summary","")
            # Fallback: pirma <img> iš HTML (data-src pirmiau dėl lazy load)
            if not image and raw_html:
                _si = _BS4(raw_html, _PARSER)
                _it = _si.find("img")
                if _it:
                    src = _it.get("data-src","") or _it.get("src","")
                    if src and src.startswith("http"):
                        image = src
            text = _html_to_text(raw_html) if raw_html else ""
            if not text and e.get("summary"):
                raw_html = raw_html or e.get("summary","")
                text = _html_to_text(e.get("summary",""))
            if title and url:
                arts.append({"site":site["name"],"sport":site.get("sport",""),
                    "title":title,"url":url,"date":date,"image":image,
                    "text":text,"html_content":raw_html,"source":"RSS","id":_art_id(url,title)})
        return arts
    except: return []

def _fetch_http(site):
    _HTTP_HEADERS = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "lt-LT,lt;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",  # br (brotli) pašalintas – requests nemoka atpakuoti
    }
    try:
        r = _req.get(site["url"], headers=_HTTP_HEADERS, timeout=8)
        if r.status_code != 200: return []
        soup = _BS4(r.text, _PARSER)
        base = site.get("base_url","")
        arts = []
        if "link_pattern" in site or "link_patterns" in site or "link_pattern_re" in site:
            import re as _re
            pat_re = _re.compile(site["link_pattern_re"]) if "link_pattern_re" in site else None
            patterns = site.get("link_patterns") or ([site.get("link_pattern","")] if "link_pattern" in site else [])
            seen = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if pat_re:
                    if not pat_re.search(href): continue
                elif not any(p in href for p in patterns): continue
                if "#" in href: continue
                url = href if href.startswith("http") else base + (href if href.startswith("/") else "/" + href)
                # title_selector – specifinis elementas su titulu (pvz. ltok.lt)
                title_sel = site.get("title_selector", "")
                if title_sel:
                    tel = a.select_one(title_sel)
                    title = tel.get_text(strip=True) if tel else ""
                else:
                    # Pirma bandome title atributą (švaresnis, be komentarų skaičiaus "(0)")
                    title = a.get("title", "").strip() or a.get_text(strip=True)
                if not title or len(title) < 5:
                    h = a.find(["h2","h3","h4"])
                    if h: title = h.get_text(strip=True)
                if not title or len(title) < 5:
                    img_in_a = a.find("img")
                    if img_in_a and img_in_a.get("alt"):
                        title = img_in_a["alt"].strip()
                # Pašaliname komentarų skaičių " (N)" iš pavadinimo pabaigos
                title = _re.sub(r'\s*\(\d+\)\s*$', '', title).strip()
                if not title or len(title) < 5 or url in seen: continue
                seen.add(url)
                img = a.find("img")
                image = None
                if img:
                    src = img.get("src") or img.get("data-src","")
                    image = (base+src if src.startswith("/") else src) or None
                arts.append({"site":site["name"],"sport":site.get("sport",""),
                    "title":title,"url":url,"date":"","image":image,
                    "text":"","source":"HTTP","id":_art_id(url,title),
                    "text_selector":site.get("text_selector","")})
                if len(arts) >= _MAX: break
        elif "selectors" in site:
            sel = site["selectors"]
            for container in soup.select(sel["articles"]):
                t_el = container.select_one(sel["title"])
                title = t_el.get_text(strip=True) if t_el else ""
                l_el  = container.select_one(sel["link"])
                href  = l_el.get("href","") if l_el else ""
                url   = href if href.startswith("http") else (base + (href if href.startswith("/") else "/" + href) if href else "")
                i_el  = container.select_one(sel.get("image","img"))
                image = None
                if i_el:
                    src = i_el.get("src") or i_el.get("data-src","")
                    image = (base+src if src.startswith("/") else src) or None
                if title and url:
                    arts.append({"site":site["name"],"sport":site.get("sport",""),
                        "title":title,"url":url,"date":"","image":image,
                        "text":"","source":"HTTP","id":_art_id(url,title)})
                if len(arts) >= _MAX: break
        return arts
    except: return []

def _sort_key(art):
    d = art.get("date","")
    if not d: return datetime.min
    try: return parsedate_to_datetime(d).replace(tzinfo=None)
    except:
        try: return datetime.fromisoformat(d).replace(tzinfo=None)
        except: return datetime.min

def run_scraper(mode="all"):
    """mode: 'all' | 'rss' (tik RSS, greita, <10s) | 'http' (tik HTTP saitai)"""
    now_iso0 = datetime.now(timezone.utc).isoformat()
    # Visi pradiniai KV skaitymai vienu pipeline request'u (vietoj 4-5 round-trip'ų)
    pre = _kv_pipeline([["GET", "dates_cache"], ["GET", "first_seen"], ["GET", "scrape_status"]])
    dates_cache   = _kv_json(pre[0], {})
    first_seen    = _kv_json(pre[1], {})   # {id: iso} – kada MES pirmą kartą pamatėme
    scrape_status = _kv_json(pre[2], {})   # {site: {"ok": iso, "n": count}} – saitų sveikata
    rss_sites  = [s for s in _SITES if "rss" in s]   if mode != "http" else []
    # rss režime papildomai imami HTTP saitai su also_vercel (pvz. LKL, kurį
    # lkl.lt blokuoja GitHub Actions IP) – fetch vyksta lygiagrečiai, telpa į 10s
    if mode == "rss":
        http_sites = [s for s in _SITES if s.get("method") == "http" and s.get("also_vercel")]
    else:
        http_sites = [s for s in _SITES if s.get("method") == "http"]
    all_arts   = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(_fetch_rss, s): s for s in rss_sites}
        futs.update({ex.submit(_fetch_http, s): s for s in http_sites})
        for fut in as_completed(futs):
            site = futs[fut]
            try:
                arts = fut.result()
                all_arts.extend(arts)
                if arts:
                    scrape_status[site["name"]] = {"ok": now_iso0, "n": len(arts)}
            except: pass
    # Naujumo patikra: SMISMEMBER tik kandidatams (vietoj viso seen_ids/seen_urls
    # seto siuntimosi – setai per 30 d. užauga iki tūkstančių narių)
    ids  = [a["id"] for a in all_arts]
    urls = [a.get("url", "") for a in all_arts]
    chk_cmds = [["SCARD", "seen_ids"], ["GET", "articles"], ["GET", "recent_ids"]]
    if ids:
        chk_cmds.append(["SMISMEMBER", "seen_ids"] + ids)
        chk_cmds.append(["SMISMEMBER", "seen_urls"] + urls)
    chk_cmds.append(["GET", "articles_hash"])   # bandwidth dedup – paskutinis
    chk = _kv_pipeline(chk_cmds)
    prev_articles_hash = chk[-1]
    seen_count = int(chk[0] or 0)
    existing   = _kv_json(chk[1], [])
    raw_recent = _kv_json(chk[2], {})
    # SAUGIKLIS nuo nenuoseklaus KV skaitymo (tuščia/bloga replika): seen_ids ir
    # articles turi būti suderinti. Jei seen_ids "tuščias", o articles pilnas –
    # skaitymas pataikė į blogą backend'ą; praleidžiam runą be rašymo/Telegram,
    # kitaip viskas atrodytų "nauja" (spam + articles perrašymas tuščiu).
    if seen_count == 0 and len(existing) > 10:
        return len(existing), 0
    # Duomenų higiena: išmetame įrašus su blogai suformuotu URL (pvz. buvęs
    # run_http bug'as "toplyga.ltrungtynes-..." be /) – domenas turi būti mūsų saitų
    from urllib.parse import urlparse as _up
    _hosts = _allowed_hosts()
    def _url_ok(u):
        if not u: return True
        h = _up(u).netloc.lower()
        return (h[4:] if h.startswith("www.") else h) in _hosts
    existing = [a for a in existing if _url_ok(a.get("url", ""))]
    id_seen  = (chk[3] if len(chk) > 3 else None) or [0] * len(ids)
    url_seen = (chk[4] if len(chk) > 4 else None) or [0] * len(ids)
    # Naujas = nematytas id IR nematytas url (apsauga nuo redaguotų pavadinimų).
    # Saitams su renotify_on_rename pakanka naujo id – pervadinimas = nauja naujiena
    _RENAME_OK = {s["name"] for s in _SITES if s.get("renotify_on_rename")}
    new_ids = {a["id"] for a, s1, s2 in zip(all_arts, id_seen, url_seen)
               if not int(s1 or 0) and (not int(s2 or 0) or a["site"] in _RENAME_OK)}

    # og:image fallback lygiagrečiai (LFF ir kt. saituose su og_image_fallback=True)
    import re as _re
    _OG_FALLBACK_SITES = {s["name"] for s in _SITES if s.get("og_image_fallback")}
    _SITE_IMG_SEL = {s["name"]: s["image_selector"] for s in _SITES if s.get("image_selector")}
    _OG_PATS = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](https?://[^"\']+)',
        r'<meta[^>]+content=["\'](https?://[^"\']+)[^>]+property=["\']og:image["\']',
        r'<meta[^>]+itemprop=["\']image["\'][^>]+content=["\'](https?://[^"\']+)',
        r'<meta[^>]+content=["\'](https?://[^"\']+)[^>]+itemprop=["\']image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\'](https?://[^"\']+)',
    ]
    def _fetch_og_image(art):
        try:
            r = _req.get(art["url"], headers={"User-Agent": _UA}, timeout=2 if mode=="rss" else 4)
            html = r.text
            sel = _SITE_IMG_SEL.get(art["site"], "")
            # 1. CSS selektorius pirmas (kai nurodytas) – tikslus herojinis paveikslas
            if sel:
                el = _BS4(html, _PARSER).select_one(sel)
                if el:
                    src = el.get("data-src") or el.get("src", "")
                    if src and src.startswith("http"): return art["id"], src
            # 2. og:image / itemprop / twitter:image meta (kai nėra selektoriaus)
            for pat in _OG_PATS:
                m = _re.search(pat, html)
                if m: return art["id"], m.group(1)
        except: pass
        return art["id"], None
    # Saitams su image_selector – visada naudojame selektorių (ne RSS kūno atsitiktinį paveikslą)
    _IMG_SEL_SITES = {s["name"] for s in _SITES if s.get("og_image_fallback") and s.get("image_selector")}
    # Jau išspręstus paveikslus imame iš KV (kitaip kiekvieną runą iš naujo
    # siųstumės dešimtis straipsnių puslapių – netelpa į Vercel 10s)
    _existing_img = {a["id"]: a.get("image") for a in existing if a.get("image")}
    for a in all_arts:
        if a["id"] in _existing_img and (not a.get("image") or a["site"] in _IMG_SEL_SITES):
            a["image"] = _existing_img[a["id"]]
    arts_no_img = [a for a in all_arts if a.get("url") and a["site"] in _OG_FALLBACK_SITES and
                   (not a.get("image") or (a["site"] in _IMG_SEL_SITES and a["id"] not in _existing_img))]
    if arts_no_img:
        with ThreadPoolExecutor(max_workers=6) as ex:
            for aid, img in ex.map(_fetch_og_image, arts_no_img):
                if img:
                    for a in all_arts:
                        if a["id"] == aid: a["image"] = img; break

    # Datos: naujoms be datos – fiksuojame dabar; senoms – atkuriame iš cache
    now_iso = datetime.now(timezone.utc).isoformat()
    dates_changed = False
    first_seen_changed = False
    for art in all_arts:
        aid = art["id"]
        if not art.get("date"):
            if aid in dates_cache:
                art["date"] = dates_cache[aid]
            else:
                art["date"] = now_iso
                dates_cache[aid] = now_iso
                dates_changed = True
        # first_seen – visada fiksuojame kada pirmą kartą pamatėme (nepriklausomai nuo RSS datos)
        if aid in new_ids and aid not in first_seen:
            first_seen[aid] = now_iso
            first_seen_changed = True

    all_arts.sort(key=_sort_key, reverse=True)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=3)
    recent_ids = []
    for art in all_arts:
        if art["id"] not in new_ids: continue
        try:
            if parsedate_to_datetime(art.get("date","")) >= cutoff:
                recent_ids.append(art["id"])
        except:
            try:
                dt = datetime.fromisoformat(art["date"].replace("Z",""))
                if dt.replace(tzinfo=None) >= cutoff.replace(tzinfo=None):
                    recent_ids.append(art["id"])
            except:
                # Naudojame first_seen – tik jei MES pamatėme neseniai (per 10 min)
                fs = first_seen.get(art["id"], "")
                if fs:
                    try:
                        if datetime.fromisoformat(fs.replace("Z","+00:00")) >= datetime.now(timezone.utc) - timedelta(minutes=10):
                            recent_ids.append(art["id"])
                    except: pass
    sorted_arts = ([a for a in all_arts if a["id"] in recent_ids] +
                   [a for a in all_arts if a["id"] not in recent_ids])
    # Merge su esamais KV straipsniais (ne overwrite) – kad RSS ir HTTP scraperiai netrukdytų vienas kitam.
    # Dedup ir pagal URL – pervadintas straipsnis (tas pats URL, kitas title/id)
    # PAKEIČIA seną įrašą, o ne sukuria dublikatą
    _fetched_ids  = {x["id"] for x in sorted_arts}
    _fetched_urls = {x["url"] for x in sorted_arts if x.get("url")}
    merged = sorted_arts + [a for a in existing
                            if a["id"] not in _fetched_ids
                            and a.get("url", "") not in _fetched_urls]
    merged.sort(key=_sort_key, reverse=True)  # persortavimas po merge (recent_ids viršuje per API /articles)
    # Slim saugojimas – be html_content/text (jie dideli; html atskirai raktuose html:{id})
    slim = [_slim_art(a) for a in merged[:300]]
    slim_json = json.dumps(slim, ensure_ascii=False)
    new_articles_hash = hashlib.md5(slim_json.encode()).hexdigest()

    # recent_ids – KAUPIAMAS dict {id: iso}, ne perrašomas (kitaip "NAUJA" badge
    # dingsta per 1-2 min, o ne po 3h; senas formatas list konvertuojamas)
    if isinstance(raw_recent, list):
        raw_recent = {i: now_iso for i in raw_recent}
    cutoff3 = datetime.now(timezone.utc) - timedelta(hours=3)
    def _fresh3(iso):
        try: return datetime.fromisoformat(str(iso).replace("Z", "+00:00")) >= cutoff3
        except: return False
    rec_map = {i: t for i, t in raw_recent.items() if _fresh3(t)}
    for rid in recent_ids:
        rec_map.setdefault(rid, now_iso)

    write_cmds = [
        ["SET", "recent_ids", json.dumps(rec_map), "EX", 3600*3],
        ["SET", "scrape_status", json.dumps(scrape_status, ensure_ascii=False), "EX", 86400*7],
    ]
    # articles perrašome tik kai skaitymas nuoseklus. Jei existing tuščias, bet
    # seen_ids egzistuoja (seen_count>0) – skaitymas pataikė į blogą backend'ą,
    # neperrašome archyvo tuščiu/sutrumpintu sąrašu.
    _read_consistent = not (not existing and seen_count > 0)
    if _read_consistent and new_articles_hash != prev_articles_hash:
        # Turinys pasikeitė → perrašom (~100KB). BANDWIDTH dedup: jei nepasikeitė,
        # nerašom viso bloko, tik atnaujinam TTL pigia EXPIRE komanda (žemiau).
        write_cmds.append(["SET", "articles", slim_json, "EX", 86400*2])
        write_cmds.append(["SET", "articles_hash", new_articles_hash, "EX", 86400*2])
        write_cmds.append(["SET", "articles_meta", json.dumps({"count": len(slim),
                                             "first": slim[0]["id"] if slim else "",
                                             "ts": now_iso}), "EX", 86400*2])
    elif _read_consistent:
        # Niekas nepasikeitė – tik pratęsiam TTL (kad articles neišsitrintų po 2 d.),
        # nerašydami viso ~100KB bloko (taupom 10GB/mėn bandwidth).
        write_cmds.append(["EXPIRE", "articles", 86400*2])
        write_cmds.append(["EXPIRE", "articles_hash", 86400*2])
        write_cmds.append(["EXPIRE", "articles_meta", 86400*2])
    if new_ids:
        write_cmds.append(["SADD", "seen_ids"] + list(new_ids))
        write_cmds.append(["EXPIRE", "seen_ids", 86400*30])
        new_urls = [a["url"] for a in all_arts if a["id"] in new_ids and a.get("url")]
        if new_urls:
            write_cmds.append(["SADD", "seen_urls"] + new_urls)
            write_cmds.append(["EXPIRE", "seen_urls", 86400*30])
    if dates_changed:
        write_cmds.append(["SET", "dates_cache", json.dumps(dates_cache), "EX", 86400*30])
    if first_seen_changed:
        write_cmds.append(["SET", "first_seen", json.dumps(first_seen), "EX", 86400*30])
    _kv_pipeline(write_cmds)

    # RSS HTML – atskiruose raktuose html:{id}, kad articles sąrašas liktų mažas.
    # EXISTS patikra + SET tik trūkstamiems; ribojam iki 60/run (likę – kitam runui).
    html_arts = [a for a in all_arts if a.get("html_content")]
    if html_arts:
        ex_res = _kv_pipeline([["EXISTS", f"html:{a['id']}"] for a in html_arts])
        to_store = [a for a, e in zip(html_arts, ex_res) if not int(e or 0)][:60]
        for i in range(0, len(to_store), 20):   # dalimis – Upstash request dydžio limitas
            _kv_pipeline([["SET", f"html:{a['id']}",
                           json.dumps(a["html_content"], ensure_ascii=False), "EX", 86400*2]
                          for a in to_store[i:i+20]])

    # Telegram – tik nauji IR sistemos pirmo pamatymo laikas ne senesnis nei 24h
    if new_ids and seen_count:
        tg_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        def _is_fresh(art):
            # Naudojame first_seen (kada MES pamatėme), ne RSS datą
            d = first_seen.get(art["id"], "")
            if not d: return True   # pirmas kartas – tikrai naujas
            try: return datetime.fromisoformat(d.replace("Z","+00:00")) >= tg_cutoff
            except: return True
        fresh_cands = [a for a in sorted_arts if a["id"] in new_ids and _is_fresh(a)]
        # Atominis vartas nuo dublikatų: SADD notified_ids grąžina 1 tik tam, kurį
        # ŠIS runas pirmas pažymi. Pakartotiniai/lygiagretūs runai gauna 0 → nebesiunčia.
        notify_arts = []
        if fresh_cands:
            sadd_res = _kv_pipeline([["SADD", "notified_ids", a["id"]] for a in fresh_cands])
            _kv_pipeline([["EXPIRE", "notified_ids", 86400*2]])
            notify_arts = [a for a, r in zip(fresh_cands, sadd_res) if int(r or 0) == 1]
        new_arts = notify_arts[:5]
        if new_arts:
            lines = ["🏆 <b>Naujos sporto naujienos!</b>\n"]
            for a in new_arts:
                sport_icon = {"futbolas":"⚽","krepšinis":"🏀","ledo ritulys":"🏒","kitas sportas":"🏅"}
                icon = sport_icon.get(a.get("sport",""), "🏆")
                lines.append(f'{icon} <a href="{a["url"]}">{a["title"]}</a>')
            extra = len(notify_arts) - 5
            if extra > 0:
                lines.append(f"\n+{extra} daugiau naujienų")
            lines.append("\n🔗 fules-online.vercel.app")
            tg_send("\n".join(lines))

    return len(sorted_arts), len(new_ids)


_INDEX_HTML = """<!DOCTYPE html>
<html lang="lt"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🏆 Sporto naujienos</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}
h1{text-align:center;color:#e2e8f0;margin-bottom:8px;font-size:1.8em}
.meta-bar{text-align:center;color:#94a3b8;margin-bottom:20px;font-size:.85em}
.sport-tabs{display:flex;justify-content:center;gap:0;margin-bottom:24px;border-bottom:2px solid #334155}
.sport-tab{padding:10px 28px;cursor:pointer;font-weight:600;font-size:.95em;color:#64748b;border-bottom:3px solid transparent;margin-bottom:-2px;transition:.2s;background:none;border-top:none;border-left:none;border-right:none}
.sport-tab:hover{color:#e2e8f0}
.sport-tab.active-all{color:#e2e8f0;border-bottom-color:#e2e8f0}
.sport-tab.active-futbolas{color:#22c55e;border-bottom-color:#22c55e}
.sport-tab.active-krepsinys{color:#f97316;border-bottom-color:#f97316}
.stats{display:flex;gap:12px;justify-content:center;margin-bottom:20px;flex-wrap:wrap}
.stat{background:#1e293b;border-radius:10px;padding:10px 20px;text-align:center;min-width:80px}
.stat strong{display:block;font-size:1.6em}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:20px;justify-content:center}
.filter-btn{background:#1e293b;border:1px solid #334155;color:#94a3b8;padding:4px 12px;border-radius:20px;cursor:pointer;font-size:.8em;transition:.2s}
.filter-btn:hover,.filter-btn.active{background:#33415522;border-color:#94a3b8;color:#e2e8f0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:18px}
.card{background:#1e293b;border-radius:10px;overflow:hidden;border:1px solid #334155;transition:transform .2s}
.card:hover{transform:translateY(-2px)}
.card.recent{background:#2d0a0a;border-color:#ef4444;box-shadow:0 0 20px rgba(239,68,68,.25)}
.card img{width:100%;height:160px;object-fit:cover}
.card-body{padding:14px}
.badge{font-size:.72em;padding:2px 9px;border-radius:99px;font-weight:700;background:#ef4444;color:#fff}
.meta{display:flex;gap:6px;align-items:center;margin:8px 0 4px;flex-wrap:wrap}
.sport-tag{font-size:.75em;font-weight:600}
.site{color:#64748b;font-size:.78em}
.method{font-size:.7em;padding:1px 6px;border-radius:99px;font-weight:600}
.card h3{font-size:.95em;line-height:1.4}
.card h3 a{color:#e2e8f0;text-decoration:none}
.card h3 a:hover{color:#94a3b8}
.date{color:#475569;font-size:.78em;margin-top:6px}
.copy-btn{margin-top:10px;width:100%;padding:7px;background:#1e3a5f;border:1px solid #2563eb44;color:#93c5fd;border-radius:6px;cursor:pointer;font-size:.82em;transition:.2s}
.copy-btn:hover{background:#1d4ed8;color:#fff}
.copy-btn.copied{background:#14532d;border-color:#22c55e44;color:#86efac}
.post-btn{margin-top:6px;width:100%;padding:7px;background:#1a2a1a;border:1px solid #22c55e44;color:#86efac;border-radius:6px;cursor:pointer;font-size:.82em;transition:.2s}
.post-btn:hover{background:#166534;color:#fff}
.post-btn.posted{background:#166534;border-color:#22c55e;color:#fff;cursor:default}
.post-btn:disabled{opacity:.5;cursor:not-allowed}
.loading{text-align:center;padding:60px;color:#64748b;font-size:1.1em}
.refresh-btn{display:block;margin:0 auto 20px;padding:8px 20px;background:#1e293b;border:1px solid #334155;color:#94a3b8;border-radius:8px;cursor:pointer;font-size:.85em}
.refresh-btn:hover{border-color:#64748b;color:#e2e8f0}
.photo-modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.85);z-index:1000;padding:16px;overflow-y:auto}
.photo-modal.open{display:flex;align-items:flex-start;justify-content:center}
.photo-box{background:#1e293b;border-radius:12px;width:100%;max-width:920px;padding:20px;margin-top:20px}
.photo-box h2{color:#e2e8f0;margin-bottom:14px;font-size:1.1em}
.photo-search{display:flex;gap:8px;margin-bottom:14px}
.photo-search input{flex:1;padding:8px 12px;background:#0f172a;border:1px solid #334155;color:#e2e8f0;border-radius:6px;font-size:.9em}
.photo-search button{padding:8px 16px;background:#1d4ed8;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:.9em}
.photo-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;max-height:420px;overflow-y:auto;margin-bottom:12px}
.photo-item{cursor:pointer;border:2px solid #334155;border-radius:6px;overflow:hidden;transition:.15s;background:#0f172a}
.photo-item:hover{border-color:#3b82f6;transform:scale(1.03)}
.photo-item img{width:100%;height:90px;object-fit:cover;display:block}
.photo-item span{display:block;font-size:.68em;color:#94a3b8;padding:3px 5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.photo-skip{width:100%;padding:8px;background:#334155;color:#94a3b8;border:none;border-radius:6px;cursor:pointer;font-size:.85em}
.photo-skip:hover{background:#475569;color:#e2e8f0}
.photo-status{text-align:center;padding:40px;color:#64748b}
.art-img-section{background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px;margin-bottom:14px;display:none}
.art-img-section .lbl{font-size:.75em;color:#64748b;margin-bottom:8px}
.art-img-row{display:flex;align-items:center;gap:12px}
.art-img-row img{height:80px;width:120px;object-fit:cover;border-radius:4px;background:#1e293b}
.art-img-row .upload-btn{padding:8px 14px;background:#059669;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:.85em;white-space:nowrap}
.art-img-row .upload-btn:disabled{background:#334155;color:#64748b;cursor:not-allowed}
</style>
</head><body>
<h1>🏆 Sporto naujienos</h1>
<div class="meta-bar" id="meta">Kraunama...</div>
<div class="sport-tabs">
  <button class="sport-tab active-all" onclick="setSport('all',this)">Visos</button>
  <button class="sport-tab" onclick="setSport('futbolas',this)">⚽ Futbolas</button>
  <button class="sport-tab" onclick="setSport('krepsinys',this)">🏀 Krepšinis</button>
  <button class="sport-tab" onclick="setSport('kiti',this)">🏒 Kiti</button>
</div>
<div class="stats" id="stats"></div>
<div style="display:flex;gap:8px;justify-content:center;margin-bottom:20px">
  <button class="refresh-btn" id="refreshBtn" onclick="manualRefresh()" style="margin:0">🔄 Atnaujinti</button>
  <button class="refresh-btn" id="notifBtn" onclick="askNotif()" style="margin:0">🔔 Pranešimai</button>
</div>
<div class="filters" id="filters"></div>
<div class="grid" id="grid"><div class="loading">⏳ Kraunamos naujienos...</div></div>

<div class="photo-modal" id="photoModal" onclick="if(event.target===this)closePhotoModal()">
  <div class="photo-box">
    <h2>🖼️ Pasirinkite vedančiąją nuotrauką</h2>
    <div style="margin-bottom:14px">
      <div style="font-size:.75em;color:#64748b;margin-bottom:5px">📝 Straipsnio pavadinimas (galite pakoreguoti prieš dedant):</div>
      <input type="text" id="postTitle"
             style="width:100%;padding:8px 12px;background:#0f172a;border:1px solid #334155;color:#e2e8f0;border-radius:6px;font-size:.95em;box-sizing:border-box">
    </div>
    <div class="art-img-section" id="artImgSection">
      <div class="lbl">📷 Straipsnio nuotrauka:</div>
      <div class="art-img-row">
        <img id="artImg" src="" alt="" onerror="document.getElementById('artImgSection').style.display='none'">
        <div style="flex:1">
          <input type="text" id="photoTags" placeholder="Gairės (kableliu atskirtos)..."
                 style="width:100%;padding:6px 10px;background:#0f172a;border:1px solid #334155;color:#e2e8f0;border-radius:6px;font-size:.85em;margin-bottom:7px;box-sizing:border-box">
          <button class="upload-btn" id="uploadOwnBtn" onclick="uploadArticlePhoto()">⬆️ Įkelti šią nuotrauką</button>
          <div id="uploadStatus" style="font-size:.75em;color:#94a3b8;margin-top:4px"></div>
        </div>
      </div>
    </div>
    <div class="photo-search">
      <input type="text" id="photoSearchInput" placeholder="Paieška sportas.lt galerijoje..."
             onkeydown="if(event.key==='Enter')searchPhotos()">
      <button onclick="searchPhotos()">🔍 Ieškoti</button>
    </div>
    <div class="photo-grid" id="photoGrid">
      <div class="photo-status">Įveskite paieškos frazę ir spauskite 🔍</div>
    </div>
    <button class="photo-skip" onclick="selectPhoto('','')">⏭️ Įdėti be nuotraukos</button>
  </div>
</div>

<script>
let ALL = [], RECENT = new Set(), curSport = 'all', curSite = 'all';
// HTML escape – scrape'intų saitų pavadinimai negali injectinti HTML/JS (XSS)
function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
// fetch su APP_TOKEN headeriu; gavus 401 – paprašo rakto ir bando dar kartą
async function authFetch(url, opts) {
  opts = opts || {};
  opts.headers = Object.assign({}, opts.headers,
    {'X-App-Token': localStorage.getItem('appToken') || ''});
  let r = await fetch(url, opts);
  if (r.status === 401) {
    const t = prompt('Įveskite prieigos raktą (APP_TOKEN iš Vercel env):');
    if (t) {
      localStorage.setItem('appToken', t.trim());
      opts.headers['X-App-Token'] = t.trim();
      r = await fetch(url, opts);
    }
  }
  return r;
}
function fmtDate(d) {
  if (!d) return '';
  try {
    const dt = new Date(d);
    if (isNaN(dt)) return d;
    return dt.toLocaleString('lt-LT', {timeZone:'Europe/Vilnius',
      year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
  } catch { return d; }
}
const _SPORT_COLORS = {'futbolas':'#22c55e','krepšinis':'#f97316','ledo ritulys':'#38bdf8','kitas sportas':'#a78bfa'};
const _SPORT_ICONS  = {'futbolas':'⚽','krepšinis':'🏀','ledo ritulys':'🏒','kitas sportas':'🏅'};
const _KITI_SPORTS  = new Set(['ledo ritulys','kitas sportas']);
// Konvertuojame URL-friendly sport key į tikrą sport reikšmę
const _SPORT_MAP = {'futbolas':'futbolas','krepsinys':'krepšinis','kiti':'kiti','all':'all'};
function _matchSport(artSport) {
  if (curSport === 'all') return true;
  if (curSport === 'kiti') return _KITI_SPORTS.has(artSport);
  const mapped = _SPORT_MAP[curSport] || curSport;
  return artSport === mapped;
}
function renderCards() {
  const grid = document.getElementById('grid');
  if (!ALL.length) { grid.innerHTML = '<div class="loading">Straipsnių nerasta</div>'; return; }
  let html = '';
  for (const art of ALL) {
    const sportOk = _matchSport(art.sport);
    const siteOk  = curSite === 'all' || art.site === curSite;
    if (!sportOk || !siteOk) continue;
    const isR  = RECENT.has(art.id);
    const sCol = _SPORT_COLORS[art.sport] || '#94a3b8';
    const mCol = art.source === 'RSS' ? '#64748b' : (art.source === 'HTTP' ? '#3b82f6' : sCol);
    const sIcon = _SPORT_ICONS[art.sport] || '🏆';
    const badge = isR ? '<span class="badge">🆕 NAUJA</span>' : '';
    const img   = art.image ? `<img src="${esc(art.image)}" loading="lazy" onerror="this.style.display='none'">` : '';
    const id    = art.id;
    html += `<div class="card ${isR?'recent':''}" data-sport="${esc(art.sport)}" data-site="${esc(art.site)}">
  ${img}<div class="card-body">${badge}
    <div class="meta"><span class="sport-tag" style="color:${sCol}">${sIcon} ${esc(art.sport)}</span>
      <span class="site">${esc(art.site)}</span>
      <span class="method" style="background:${mCol}22;color:${mCol}">${esc(art.source)}</span></div>
    <h3><a href="${esc(art.url)}" target="_blank" rel="noopener">${esc(art.title)}</a></h3>
    <div class="date">${fmtDate(art.date)}</div>
    <button class="copy-btn" onclick="copyArt('${id}',this)">📋 Kopijuoti</button>
    <button class="post-btn" onclick="postArt('${id}',this)">📤 Įdėti</button>
  </div></div>`;
  }
  grid.innerHTML = html || '<div class="loading">Nėra straipsnių šiai kategorijai</div>';
  const f = document.getElementById('stats');
  const fc = ALL.filter(a=>a.sport==='futbolas').length;
  const kc = ALL.filter(a=>a.sport==='krepšinis').length;
  const oc = ALL.filter(a=>_KITI_SPORTS.has(a.sport)).length;
  f.innerHTML = `<div class="stat"><strong>${ALL.length}</strong>Iš viso</div>
    <div class="stat"><strong style="color:#ef4444">${RECENT.size}</strong>Naujų</div>
    <div class="stat"><strong style="color:#22c55e">${fc}</strong>⚽</div>
    <div class="stat"><strong style="color:#f97316">${kc}</strong>🏀</div>
    ${oc ? `<div class="stat"><strong style="color:#06b6d4">${oc}</strong>🏒🏅</div>` : ''}`;
}
function renderFilters() {
  const sites = [...new Set(ALL.filter(a => _matchSport(a.sport)).map(a=>a.site))].sort();
  let h = '<button class="filter-btn active" onclick="setSite(\\'all\\',this)">Visos</button>';
  sites.forEach(s => { h += `<button class="filter-btn" onclick="setSite('${s}',this)">${s}</button>`; });
  document.getElementById('filters').innerHTML = h;
}
function setSport(sport, btn) {
  document.querySelectorAll('.sport-tab').forEach(b => b.className = 'sport-tab');
  btn.className = 'sport-tab active-' + sport;
  curSport = sport; curSite = 'all';
  renderFilters(); renderCards();
}
function setSite(site, btn) {
  curSite = site;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderCards();
}
async function loadData() {
  document.getElementById('meta').textContent = 'Kraunama...';
  try {
    const r = await fetch('/api/articles');
    const d = await r.json();
    ALL    = d.articles || [];
    RECENT = new Set(d.recent_ids || []);
    const now = new Date().toLocaleString('lt-LT', {timeZone:'Europe/Vilnius'});
    document.getElementById('meta').textContent = 'Atnaujinta: ' + now + ' | ' + ALL.length + ' straipsnių';
    renderFilters(); renderCards();
  } catch(e) {
    document.getElementById('meta').textContent = '❌ Klaida: ' + e.message;
  }
}
function _doCopy(txt, btn) {
  // execCommand visada veikia (veikia ir po await), clipboard API kartais blokuojama
  const ta = document.createElement('textarea');
  ta.value = txt;
  ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.focus(); ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
  btn.textContent = '✅ Nukopijuota!'; btn.classList.add('copied');
  setTimeout(() => { btn.textContent = '📋 Kopijuoti'; btn.classList.remove('copied'); }, 2500);
}
async function copyArt(id, btn) {
  const art = ALL.find(a => a.id === id);
  if (!art) return;
  btn.textContent = '⏳ Kraunama...'; btn.disabled = true;
  try {
    const r = await fetch('/api/article-text?url=' + encodeURIComponent(art.url));
    const d = await r.json();
    if (d.text) art.text = d.text;
  } catch(e) { /* naudosim tai, ką turim */ }
  _doCopy(art.title + (art.text ? '\\n\\n' + art.text : ''), btn);
  btn.disabled = false;
}
let _pendingPost = null;
let _pendingImageUrl = null;
async function postArt(id, btn) {
  _pendingPost = {id, btn};
  const art = ALL.find(a => a.id === id);
  // Pilnas pavadinimas redagavimui (į sportas.lt eis būtent šis tekstas)
  document.getElementById('postTitle').value = art ? art.title : '';
  // Straipsnio nuotrauka
  _pendingImageUrl = (art && art.image) ? art.image : null;
  const sec = document.getElementById('artImgSection');
  const upBtn = document.getElementById('uploadOwnBtn');
  document.getElementById('uploadStatus').textContent = '';
  upBtn.textContent = '⬆️ Įkelti šią nuotrauką'; upBtn.disabled = false;
  if (_pendingImageUrl) {
    document.getElementById('artImg').src = _pendingImageUrl;
    sec.style.display = 'block';
  } else {
    sec.style.display = 'none';
  }
  // Paieškos laukas paliekamas tuščias – pagal title pradžią galerijoje nieko nerandama
  document.getElementById('photoSearchInput').value = '';
  document.getElementById('photoGrid').innerHTML = '<div class="photo-status">Įveskite paieškos frazę ir spauskite 🔍</div>';
  document.getElementById('photoModal').classList.add('open');
}
function closePhotoModal() {
  document.getElementById('photoModal').classList.remove('open');
  if (_pendingPost) { _pendingPost.btn.disabled = false; _pendingPost = null; }
  _pendingImageUrl = null;
}
async function uploadArticlePhoto() {
  if (!_pendingImageUrl) return;
  const btn = document.getElementById('uploadOwnBtn');
  const status = document.getElementById('uploadStatus');
  const tags = document.getElementById('photoTags').value.trim();
  btn.textContent = '⏳ Įkeliama...'; btn.disabled = true;
  status.textContent = '';
  const art = _pendingPost ? ALL.find(a => a.id === _pendingPost.id) : null;
  const sport = art ? art.sport : '';
  const site  = art ? art.site  : '';
  try {
    const r = await authFetch('/api/upload-photo', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: _pendingImageUrl, tags, sport, site})
    });
    const d = await r.json();
    if (d.ok && d.path) {
      const sourceName = d.source_name || 'Organizatorių nuotr.';
      const photoTitle = (tags ? tags + ' | ' : '') + sourceName;
      selectPhoto(d.path, photoTitle);
    } else {
      status.textContent = '❌ ' + (d.error || 'Nepavyko įkelti');
      btn.textContent = '⬆️ Įkelti šią nuotrauką'; btn.disabled = false;
    }
  } catch(e) {
    status.textContent = '❌ ' + e.message;
    btn.textContent = '⬆️ Įkelti šią nuotrauką'; btn.disabled = false;
  }
}
async function searchPhotos() {
  const q = document.getElementById('photoSearchInput').value.trim();
  const grid = document.getElementById('photoGrid');
  if (!q) { grid.innerHTML = '<div class="photo-status">Įveskite paieškos frazę</div>'; return; }
  grid.innerHTML = '<div class="photo-status">⏳ Ieškoma...</div>';
  try {
    const r = await authFetch('/api/photos?q=' + encodeURIComponent(q));
    const d = await r.json();
    if (!d.photos || !d.photos.length) {
      grid.innerHTML = '<div class="photo-status">Nuotraukų nerasta – pabandykite kitą frazę</div>';
      return;
    }
    grid.innerHTML = d.photos.map(p => {
      const thumb = p.thumb.startsWith('http') ? p.thumb : 'https://www.sportas.lt' + p.thumb;
      const safeT = (p.title||'').replace(/"/g,'&quot;');
      return '<div class="photo-item" data-path="' + p.path + '" data-title="' + safeT + '" onclick="selectPhoto(this.dataset.path,this.dataset.title)">'
        + '<img src="' + thumb + '" loading="lazy" onerror="this.style.display=\\'none\\'">'
        + '<span>' + (p.title || p.path.split('/').pop()) + '</span></div>';
    }).join('');
  } catch(e) {
    grid.innerHTML = '<div class="photo-status">❌ Klaida: ' + e.message + '</div>';
  }
}
async function selectPhoto(path, title) {
  document.getElementById('photoModal').classList.remove('open');
  if (!_pendingPost) return;
  const {id, btn} = _pendingPost;
  const photo_tags = document.getElementById('photoTags')?.value.trim() || '';
  const custom_title = document.getElementById('postTitle').value.trim();
  _pendingPost = null;
  btn.textContent = '⏳ Įdedama...'; btn.disabled = true;
  try {
    const r = await authFetch('/api/post', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id, photo_path: path, photo_title: title, photo_tags, title: custom_title})});
    const d = await r.json();
    if (d.ok) { btn.textContent = '✅ Įdėta! ' + (d.message||''); btn.classList.add('posted'); }
    else { btn.textContent = '❌ ' + (d.error || d.message || 'Klaida'); btn.disabled = false;
           console.error('Post error:', d); }
  } catch(e) { btn.textContent = '❌ Klaida'; btn.disabled = false; }
}
let _lastRecent = '';
let _swReg = null;

// Service Worker registracija
async function _initSW() {
  if (!('serviceWorker' in navigator)) return;
  try {
    _swReg = await navigator.serviceWorker.register('/sw.js', {scope: '/'});
    // Laukiame kol SW aktyvus
    await navigator.serviceWorker.ready;
    const sw = _swReg.active;
    if (sw) sw.postMessage({type: 'INIT', key: _lastRecent});
    // Kas 30s žadiname SW pollingui (veikia net kai tab fone).
    setInterval(() => {
      const s = _swReg?.active;
      if (s) s.postMessage({type: 'POLL'});
    }, 30000);
  } catch(e) { console.log('SW klaida:', e); }
}

// Siunčia notification per SW (veikia ir fone)
function _notify(title, body, url) {
  if (Notification.permission !== 'granted') return;
  const sw = _swReg?.active;
  if (sw) { sw.postMessage({type: 'POLL'}); }  // SW pats aptiks ir parodys
  else { new Notification(title, {body}); }
}

async function _checkNew() {
  try {
    // Lengvas /api/version polling'as (~100 B); pilnas sąrašas – tik pasikeitus
    const r = await fetch('/api/version');
    const d = await r.json();
    const recentIds = d.recent_ids || [];
    const recentKey = recentIds.slice().sort().join(',');
    const fullKey   = recentKey + '|' + (d.count||0) + '|' + (d.first||'');
    const now = new Date().toLocaleString('lt-LT', {timeZone:'Europe/Vilnius'});
    if (fullKey !== _lastRecent && _lastRecent !== '') {
      const prevSet = new Set((_lastRecent.split('|')[0] || '').split(',').filter(Boolean));
      const r2 = await fetch('/api/articles');
      const d2 = await r2.json();
      ALL = d2.articles || []; RECENT = new Set(d2.recent_ids || recentIds);
      document.getElementById('meta').textContent = '🆕 Nauja naujiena! ' + now + ' | ' + ALL.length + ' straipsnių';
      renderFilters(); renderCards();
      // Notification tik naujai ATSIRADUSIEMS recent ID – ne kai sąrašas
      // susitraukia (pasibaigus 3h langui) ar persirikiuoja
      const added = recentIds.filter(id => !prevSet.has(id));
      if (added.length) {
        // notifiedIds localStorage – multi-tab ir pakartotinių notificationų apsauga
        const notified = new Set(JSON.parse(localStorage.getItem('notifiedIds') || '[]'));
        const toNotify = added.filter(id => !notified.has(id));
        added.forEach(id => notified.add(id));
        localStorage.setItem('notifiedIds', JSON.stringify([...notified].slice(-100)));
        if (toNotify.length) {
          const notifArt = ALL.find(a => toNotify.includes(a.id));
          if (notifArt) {
            const icon = {'futbolas':'⚽','krepšinis':'🏀','ledo ritulys':'🏒'}[notifArt.sport] || '🏆';
            _notify('🏆 Sporto naujienos', icon + ' ' + notifArt.title, notifArt.url || '/');
          }
        }
      }
    }
    _lastRecent = fullKey;
    // Sinchronizuojame "matyta" būseną – kitaip refresh'as po ilgai atidaryto
    // tab'o rodo "rasta naujų nuo paskutinio apsilankymo" apie jau matytas
    localStorage.setItem('lastSeenRecent', fullKey);
  } catch {}
}
async function manualRefresh() {
  const btn = document.getElementById('refreshBtn');
  btn.textContent = '⏳ Tikrinama...'; btn.disabled = true;
  document.getElementById('meta').textContent = '⏳ Kreipiamasi į svetaines, palaukite...';
  try {
    const r = await authFetch('/api/refresh', {method:'POST'});
    const d = await r.json();
    ALL    = d.articles || [];
    RECENT = new Set(d.recent_ids || []);
    const now = new Date().toLocaleString('lt-LT', {timeZone:'Europe/Vilnius'});
    const msg = d.new_count > 0
      ? `✅ Atnaujinta: ${now} | Rasta ${d.new_count} naujų | ${ALL.length} iš viso`
      : `✅ Atnaujinta: ${now} | Naujų nėra | ${ALL.length} straipsnių`;
    document.getElementById('meta').textContent = msg;
    renderFilters(); renderCards();
  } catch(e) {
    document.getElementById('meta').textContent = '❌ Klaida: ' + e.message;
  }
  btn.textContent = '🔄 Atnaujinti'; btn.disabled = false;
}
async function askNotif() {
  const btn = document.getElementById('notifBtn');
  if (!('Notification' in window)) { btn.textContent = '❌ Nepalaikoma'; return; }
  if (Notification.permission === 'granted') { btn.textContent = '✅ Įjungta'; return; }
  const perm = await Notification.requestPermission();
  if (perm === 'granted') {
    btn.textContent = '✅ Įjungta';
    await _initSW();
    _notify('🏆 Sporto naujienos', 'Pranešimai įjungti! Gausite žinutę kai atsiras naujienų.', '/');
  } else {
    btn.textContent = '🚫 Užblokuota';
  }
}
function _updateNotifBtn() {
  const btn = document.getElementById('notifBtn');
  if (!btn) return;
  if (!('Notification' in window)) { btn.textContent = '🔔 Nepalaikoma'; btn.disabled = true; return; }
  if (Notification.permission === 'granted') btn.textContent = '✅ Įjungta';
  else if (Notification.permission === 'denied') { btn.textContent = '🚫 Užblokuota'; btn.disabled = true; }
  else btn.textContent = '🔔 Pranešimai';
}
loadData().then(() => {
  _lastRecent = [...RECENT].sort().join(',') + '|' + ALL.length + '|' + (ALL[0]?.id||'');
  // Praleistos notifikacijos: tik ID, kurių NEBUVO paskutinio matymo momentu
  // (ne visas RECENT sąrašas – jis apima 3h langą, kurį galbūt jau matėme)
  const stored = localStorage.getItem('lastSeenRecent') || '';
  const storedSet = new Set((stored.split('|')[0] || '').split(',').filter(Boolean));
  const missed = [...RECENT].filter(id => !storedSet.has(id));
  if (stored && missed.length > 0) {
    const now = new Date().toLocaleString('lt-LT', {timeZone:'Europe/Vilnius'});
    document.getElementById('meta').textContent = '🆕 Nauja naujiena! ' + now + ' | ' + ALL.length + ' straipsnių';
    _notify('🏆 Sporto naujienos', `Rasta ${missed.length} nauja(-ų) nuo paskutinio apsilankymo`, '/');
  }
  localStorage.setItem('lastSeenRecent', _lastRecent);
  _updateNotifBtn();
});
setInterval(_checkNew, 10000);   // 10s – greitas naujienų matymas (version mažytis)
// Grįžus į tab'ą – tikriname IŠKART (fone naršyklė taimerius užmigdo,
// be šito atsinaujinimas matomas tik po ~10s ar paspaudus mygtuką)
document.addEventListener('visibilitychange', () => { if (!document.hidden) _checkNew(); });
window.addEventListener('focus', _checkNew);
</script>
</body></html>"""

# ── Service Worker ─────────────────────────────────────────────────
_SW_JS = """
// Sporto naujienų Service Worker
let _swPrevRecent = null;        // Set – žinomi recent ID
let _swNotified   = new Set();   // jau parodytų notificationų ID

async function swPoll() {
  try {
    // Lengvas /api/version; pilnas /api/articles tik kai atsirado naujų ID
    const r = await fetch('/api/version?_sw=' + Date.now(), {cache: 'no-store'});
    const d = await r.json();
    const recentIds = d.recent_ids || [];
    if (_swPrevRecent !== null) {
      const added = recentIds.filter(id => !_swPrevRecent.has(id) && !_swNotified.has(id));
      if (added.length) {
        added.forEach(id => _swNotified.add(id));
        const r2 = await fetch('/api/articles?_sw=' + Date.now(), {cache: 'no-store'});
        const d2 = await r2.json();
        const notifArt = (d2.articles || []).find(a => added.includes(a.id));
        if (notifArt) {
          const icon = {'futbolas':'⚽','krepšinis':'🏀','ledo ritulys':'🏒'}[notifArt.sport] || '🏆';
          await self.registration.showNotification('🏆 Sporto naujienos', {
            body:     icon + ' ' + notifArt.title,
            tag:      'sporto-naujienos',
            renotify: true,
            data:     { url: notifArt.url || 'https://fules-online.vercel.app/' },
          });
        }
      }
    }
    _swPrevRecent = new Set(recentIds);
  } catch(e) {}
}

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('message', e => {
  if (!e.data) return;
  if (e.data.type === 'POLL') swPoll();
  if (e.data.type === 'INIT') {
    // key formatas: "id1,id2|count|firstId" – paimame tik recent ID dalį
    const ids = String(e.data.key || '').split('|')[0].split(',').filter(Boolean);
    _swPrevRecent = new Set(ids);
    ids.forEach(id => _swNotified.add(id));
    swPoll();
  }
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = e.notification.data?.url || 'https://fules-online.vercel.app/';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cs => {
      for (const c of cs) {
        if (c.url.startsWith('https://fules-online.vercel.app') && 'focus' in c)
          return c.focus();
      }
      return self.clients.openWindow(url);
    })
  );
});
"""

# ── Routes ─────────────────────────────────────────────────────────
@app.after_request
def _no_cache(resp):
    # Be šito mobilios naršyklės heuristiškai cache'ina /api/articles ir
    # šviežiai atidarytas puslapis rodo senas naujienas
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/sw.js")
def service_worker():
    return Response(_SW_JS, content_type="application/javascript",
                    headers={"Service-Worker-Allowed": "/"})

@app.route("/")
def serve_index():
    return Response(_INDEX_HTML, content_type="text/html; charset=utf-8")

def _recent_list(raw):
    """recent_ids KV formatas: dict {id: iso} (naujas) arba list (senas)."""
    if isinstance(raw, dict): return list(raw.keys())
    return raw or []

# Serverinis cache polling endpointams – tas pats šiltas Vercel instance'as
# kelis greitus pollus aptarnauja iš atminties, nebadydamas KV (taupo Upstash
# komandų kvotą). Be to – jei KV grąžina klaidą (kvota), atiduodam paskutinį gerą.
_RESP_CACHE = {}   # {raktas: (ts, data)}
def _cached_kv(key, ttl, fetch):
    now = time.time()
    hit = _RESP_CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        data = fetch()
        _RESP_CACHE[key] = (now, data)
        return data
    except Exception:
        if hit: return hit[1]   # KV klaida (pvz. kvota) – atiduodam paskutinį gerą
        raise

@app.route("/api/articles", methods=["GET"])
def articles():
    def _fetch():
        res    = _kv_pipeline([["GET", "articles"], ["GET", "recent_ids"]])
        data   = _kv_json(res[0], [])
        recent = _recent_list(_kv_json(res[1], {}))
        slim = [{k: v for k, v in a.items() if k not in ("html_content", "text")} for a in data]
        return {"articles": slim, "recent_ids": recent}
    try:
        return jsonify(_cached_kv("articles", 5, _fetch))
    except Exception:
        return jsonify({"articles": [], "recent_ids": []})

@app.route("/api/version", methods=["GET"])
def version():
    """Mažytis atsakymas polling'ui – frontend pilną /api/articles siunčiasi
    tik kai čia kažkas pasikeičia (vietoj pilno sąrašo kas 10s)."""
    def _fetch():
        res    = _kv_pipeline([["GET", "articles_meta"], ["GET", "recent_ids"]])
        meta   = _kv_json(res[0], {})
        recent = _recent_list(_kv_json(res[1], {}))
        return {"recent_ids": recent, "count": meta.get("count", 0),
                "first": meta.get("first", "")}
    try:
        return jsonify(_cached_kv("version", 2, _fetch))
    except Exception:
        return jsonify({"recent_ids": [], "count": 0, "first": ""})

@app.route("/api/health", methods=["GET"])
def health():
    """Diagnostika: ar modulis pilnai užsikrovė, koks parseris, kiek saitų."""
    return jsonify({"ok": not _IMPORT_ERR, "import_error": _IMPORT_ERR,
                    "sites": len(_SITES), "parser": _PARSER,
                    "python": sys.version.split()[0],
                    "kv": bool(KV_URL), "app_token_set": bool(APP_TOKEN)})

@app.route("/api/scrape-status", methods=["GET"])
def scrape_status_view():
    """Saitų sveikata: kada kiekvienas saitas paskutinį kartą grąžino straipsnių."""
    return jsonify(_kv_get("scrape_status") or {})

def _allowed_hosts():
    from urllib.parse import urlparse
    hosts = set()
    for s in _SITES:
        for u in (s.get("url", ""), s.get("rss", ""), s.get("base_url", "")):
            if u:
                h = urlparse(u).netloc.lower()
                hosts.add(h[4:] if h.startswith("www.") else h)
    return hosts

@app.route("/api/article-text", methods=["GET"])
def article_text():
    url = request.args.get("url", "")
    if not url:
        return jsonify({"text": ""}), 400
    # Tik mūsų saitų domenai – kitaip endpoint'as veiktų kaip atviras proxy
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    if host.startswith("www."): host = host[4:]
    if host not in _allowed_hosts():
        return jsonify({"text": "", "error": "Domenas neleidžiamas"}), 403
    try:
        r = _req.get(url, headers={"User-Agent": _UA}, timeout=8)
        soup = _BS4(r.text, _PARSER)
        for tag in soup(["script","style","nav","footer","header","aside",
                         "iframe","noscript","form","button"]):
            tag.decompose()
        # Ieškome site-specific text_selector iš _SITES konfigūracijos
        site_cfg = next((s for s in _SITES
                         if s.get("base_url") and url.startswith(s["base_url"])), {})
        txt_sel = site_cfg.get("text_selector", "")
        # Ieškome turinio konteinerio - nuo konkretesnių iki bendresnių
        main = (soup.select_one(txt_sel) if txt_sel else None) or \
               soup.select_one('[class*="post-content"]') or \
               soup.select_one('[class*="article-content"]') or \
               soup.select_one('[class*="entry-content"]') or \
               soup.select_one('[class*="article-body"]') or \
               soup.select_one('[class*="prose"]') or \
               soup.select_one('.fck') or \
               soup.select_one('article') or \
               soup.select_one('main')
        paragraphs = []
        seen_txts = set()
        if main:
            # Jei text_selector rasta – įtraukiame ir div (pvz. hockey.lt naudoja div)
            tags = ["p","h2","h3","h4","li","div"] if txt_sel else ["p","h2","h3","h4","li"]
            for el in main.find_all(tags):
                txt = " ".join(el.get_text(" ", strip=True).split())
                if len(txt) > 10 and txt not in seen_txts:
                    seen_txts.add(txt)
                    paragraphs.append(txt)
        if not paragraphs:
            # Paskutinis fallback: body su p tagais (be div, vengiam footer)
            for el in (soup.body or soup).find_all(["p","h2","h3","h4","li"]):
                txt = " ".join(el.get_text(" ", strip=True).split())
                if len(txt) > 10 and txt not in seen_txts:
                    seen_txts.add(txt)
                    paragraphs.append(txt)
        text = _strip_wp_footer("\n\n".join(paragraphs))
        return jsonify({"text": text[:30000]})
    except Exception as e:
        return jsonify({"text": "", "error": str(e)})

@app.route("/api/refresh", methods=["POST"])
def refresh():
    if not _auth_ok(): return _auth_fail()
    import traceback
    try:
        total, new_count = run_scraper()
        res    = _kv_pipeline([["GET", "articles"], ["GET", "recent_ids"]])
        data   = _kv_json(res[0], [])
        recent = _recent_list(_kv_json(res[1], {}))
        slim = [{k: v for k, v in a.items() if k not in ("html_content", "text")} for a in data]
        return jsonify({"articles": slim, "recent_ids": recent,
                        "total": total, "new_count": new_count})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e),
                        "trace": traceback.format_exc()[-1000:]}), 500

@app.route("/api/cron", methods=["GET", "POST"])
def cron():
    try:
        total, new_count = run_scraper()
        return jsonify({"ok": True, "total": total, "new": new_count})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/cron-rss", methods=["GET", "POST"])
def cron_rss():
    """Tik RSS saitai – greita (<10s), skirta cron-job.org kas 2 min"""
    try:
        total, new_count = run_scraper(mode="rss")
        return jsonify({"ok": True, "total": total, "new": new_count, "mode": "rss"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/post", methods=["POST", "OPTIONS"])
def post():
    if request.method == "OPTIONS":
        return Response("", headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-App-Token",
        })
    if not _auth_ok(): return _auth_fail()
    import unicodedata as _ud
    _nfc = lambda s: _ud.normalize("NFC", s) if s else s
    payload     = request.get_json() or {}
    aid         = payload.get("id", "")
    photo_path  = payload.get("photo_path", "")
    photo_title = _nfc(payload.get("photo_title", ""))
    photo_tags  = _nfc(payload.get("photo_tags", ""))
    articles = _kv_get("articles") or []
    article  = next((a for a in articles if a["id"] == aid), None)
    if not article:
        return jsonify({"ok": False, "error": "Nerasta"}), 404
    # Vartotojo pakoreguotas pavadinimas iš modalo (jei pateiktas)
    custom_title = _nfc(payload.get("title", "").strip())
    if custom_title:
        article = dict(article, title=custom_title)
    try:
        ok, msg = _do_post(article, photo_path=photo_path, photo_title=photo_title, photo_tags=photo_tags)
        return jsonify({"ok": ok, "message": msg, "_debug_photo_tags": photo_tags})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "traceback": traceback.format_exc()})

@app.route("/api/photos", methods=["GET"])
def get_photos():
    """Proxy: sportas.lt galerijos paieška. Grąžina photo sąrašą su path ir thumb."""
    if not _auth_ok(): return _auth_fail()
    q = request.args.get("q", "")
    try:
        sess = _session()
        if not SPORTAS_USER:
            return jsonify({"ok": False, "error": "SPORTAS_USER nenustatytas"}), 400
        # Paieškos parametras yra "query", ne "search"
        r = sess.get("https://www.sportas.lt/Admin/LoadPopup/UGallery/choicePhoto",
                     params={"query": q} if q else {}, timeout=12)
        soup = _BS4(r.text, _PARSER)
        import re as _re
        photos = []
        seen = set()
        # HTML struktūra: <div class="item"><a onclick="choicePhoto(id, 'url', 'title')">
        # Realus formatas: choicePhoto("/Uploads/UGallery/photos/...", "https://static.../thumb.jpg", "Pavadinimas")
        for div in soup.select("div.item"):
            a = div.find("a", onclick=True)
            if not a: continue
            oc = a.get("onclick", "")
            m = _re.search(
                r'choicePhoto\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]*)"',
                oc)
            if not m: continue
            path, thumb_url, title = m.group(1), m.group(2), m.group(3)
            if path in seen: continue
            seen.add(path)
            photos.append({"path": path, "thumb": thumb_url, "title": title})
        return jsonify({"ok": True, "photos": photos[:60]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/upload-photo", methods=["POST"])
def upload_photo():
    """Parsisiunčia nuotrauką iš URL ir įkelia į sportas.lt galeriją per submitPhotos."""
    if not _auth_ok(): return _auth_fail()
    import unicodedata as _ud
    payload = request.get_json() or {}
    image_url = payload.get("url", "")
    tags      = _ud.normalize("NFC", payload.get("tags", ""))
    sport     = payload.get("sport", "")
    site      = payload.get("site", "")

    # Šaltinių žemėlapis: site pavadinimas → (source_id, source_name)
    _SOURCE_MAP = {
        "LKL":              ("2642", 'LKL, kurią remia „Betsson" nuotr.'),
        "FK Žalgiris":      ("11",   "fkzalgiris.lt nuotr."),
        "Žalgiris":         ("57",   "zalgiris.lt nuotr."),
        "Žalgiris futbolas":("57",   "zalgiris.lt nuotr."),
        "Hockey Lietuva":   ("18",   "hockey.lt nuotr."),
        "LTOK":             ("40",   "LTOK nuotr."),
        "BC Kibirkštis":    ("2641", 'BC „Kibirkštis" nuotr.'),
        "LTU Aquatics":     ("2730", "ltuswimming.com nuotr."),
    }
    source_id, source_name = _SOURCE_MAP.get(site, ("3", "Organizatorių nuotr."))
    if not image_url:
        return jsonify({"ok": False, "error": "Nenurodytas image URL"}), 400
    try:
        sess = _session()
        if not SPORTAS_USER:
            return jsonify({"ok": False, "error": "SPORTAS_USER nenustatytas"}), 400

        # 1. Parsisiunčiame nuotrauką iš originalaus šaltinio
        img_r = _req.get(image_url, headers={"User-Agent": _UA,
                         "Referer": image_url.split("/")[0] + "//" + image_url.split("/")[2]},
                         timeout=15, allow_redirects=True)
        if img_r.status_code != 200:
            return jsonify({"ok": False, "error": f"Nepavyko parsisiųsti: HTTP {img_r.status_code}"}), 400

        ct = img_r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        ext_map = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
                   "image/gif": ".gif", "image/webp": ".jpg"}
        ext = ext_map.get(ct, ".jpg")
        raw_name = image_url.split("?")[0].rstrip("/").split("/")[-1]
        filename = raw_name if any(raw_name.lower().endswith(e) for e in [".jpg",".jpeg",".png",".gif",".webp"]) \
                   else (raw_name or "photo") + ext
        # HTTP headeriai turi būti ASCII – pašaliname ne-ASCII simbolius iš failvardo
        import unicodedata as _ud3
        filename_ascii = _ud3.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
        if not filename_ascii or filename_ascii in (".", ""):
            filename_ascii = "photo" + ext

        # 2. Įkeliame į sportas.lt kaip raw octet-stream (Fine Uploader stilius)
        upload_url = (
            f"https://www.sportas.lt/Admin/Load/UGallery/submitPhotos"
            f"?cfDontBugMe=please&qqfile={filename_ascii}&noCacheCF=1"
        )
        up_r = sess.post(
            upload_url,
            data=img_r.content,
            headers={
                "Content-Type": "application/octet-stream",
                "X-File-Name":  filename_ascii,
                "X-Mime-Type":  ct,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://www.sportas.lt/Admin/Load/UGallery/addPhotos/",
                "Origin":  "https://www.sportas.lt",
            },
            timeout=20,
        )

        # 3. Parseriname atsakymą — tikimasi {"success": true, "id": 1546210}
        try:
            resp = up_r.json()
        except Exception:
            return jsonify({"ok": False, "error": f"Netikėtas atsakymas: {up_r.text[:200]}"}), 400

        if not resp.get("success"):
            return jsonify({"ok": False, "error": f"Įkėlimo klaida: {resp}"}), 400

        photo_id = resp.get("id") or resp.get("photoId") or resp.get("uuid") or ""

        # 4. Išsaugome metaduomenis
        cat_id = "128" if "krep" in sport.lower() else "129"
        save_data = {
            "file":    "",
            "source":  source_id,
            "category": cat_id,
            "tags":    tags,
            "action":  "1",
            "galleryAutocomplete": "",
        }
        if photo_id:
            save_data[f"photoName[{photo_id}]"] = tags or ""
        import unicodedata as _ud2
        from urllib.parse import urlencode as _ue
        save_data_nfc = {k: (_ud2.normalize("NFC", v) if isinstance(v, str) else v) for k, v in save_data.items()}
        sess.post("https://www.sportas.lt/Admin/Load/UGallery/savePhotos",
                  data=_ue(save_data_nfc).encode("utf-8"),
                  headers={"Referer": "https://www.sportas.lt/Admin/Load/UGallery/addPhotos/",
                           "Origin": "https://www.sportas.lt",
                           "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                  timeout=10)

        # 5. Bandome rasti kelią
        import re as _re
        path = ""
        chk_status = "?"

        # A) MD5 iš įkeltų baitų — content-addressed storage
        h = hashlib.md5(img_r.content).hexdigest()
        path_md5 = f"/Uploads/UGallery/photos/{h[0:2]}/{h[2:4]}/{h[4:6]}/{h[6:8]}/{h}{ext}"
        try:
            chk = _req.head(f"https://www.sportas.lt{path_md5}", timeout=5)
            chk_status = chk.status_code
            if chk.status_code in (200, 301, 302):
                path = path_md5
        except Exception:
            pass

        # B) Fetch editPhoto/{id} — ieškome PIRMO /Uploads/UGallery/photos/ kelio
        #    (tai yra pati nuotrauka edit formoje, ne listing'o sidebar)
        if not path and photo_id:
            lst_r = sess.get(
                f"https://www.sportas.lt/Admin/Load/UGallery/editPhoto/{photo_id}",
                timeout=10)
            # Tiesiog pirmasis /Uploads/... kelias puslapyje — tai edit forma
            m = _re.search(
                r'(/Uploads/UGallery/photos/[^\s\'"<>?]+)',
                lst_r.text)
            if m:
                p = m.group(1)
                # Patikriname kad tai nuotrauka (ne thumbnails suffix)
                if any(p.lower().endswith(e) for e in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
                    path = p

        if not path:
            return jsonify({"ok": False, "error": f"Nuotrauka įkelta (id={photo_id}), MD5 path={path_md5}, HEAD={chk_status}"}), 400

        return jsonify({"ok": True, "path": path, "photo_id": photo_id, "source_name": source_name})

    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "traceback": traceback.format_exc()}), 500

@app.route("/api/photo-debug/<int:photo_id>", methods=["GET"])
def photo_debug(photo_id):
    """Randa photo kelią pagal ID – žiūri listing'e prieš/po mūsų ID."""
    if not _auth_ok(): return _auth_fail()
    try:
        sess = _session()
        import re as _re

        # Listing puslapis – ieškome mūsų photo pagal ID
        r = sess.get("https://www.sportas.lt/Admin/Load/UGallery/editPhoto/",
                     timeout=10)
        html = r.text

        # Pattern: &f={PATH}') href="...editPhoto/{ID}?
        listing = {}
        for m in _re.finditer(
            r'[&?](?:amp;)?f=(/Uploads/UGallery/photos/[^\'"]+)[\'"][^"]*"[^"]*editPhoto/(\d+)',
            html):
            listing[int(m.group(2))] = m.group(1)

        our_path = listing.get(photo_id)

        # Pirmasis ID sąraše
        first_ids = sorted(listing.keys(), reverse=True)[:5]

        # Taip pat pažiūrime viršutinę dalį (3000–7000 chars) – ten gali būti forma
        mid_html = html[2000:7000]

        return jsonify({
            "found": our_path is not None,
            "our_path": our_path,
            "listing_ids_found": first_ids,
            "listing_count": len(listing),
            "mid_html": mid_html,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/rss-debug", methods=["GET"])
def rss_debug():
    """Rodo ką feedparser gauna iš RSS. ?url=https://..."""
    if not _auth_ok(): return _auth_fail()
    rss_url = request.args.get("url", "https://www.lff.lt/feed/")
    try:
        feed = feedparser.parse(rss_url, request_headers={"User-Agent": _UA})
        results = []
        for e in feed.entries[:5]:
            info = {
                "title": e.get("title","")[:60],
                "link":  e.get("link",""),
                "media_thumbnail": (e.media_thumbnail if hasattr(e,"media_thumbnail") and e.media_thumbnail else None),
                "media_content":   (e.media_content   if hasattr(e,"media_content")   and e.media_content   else None),
                "enclosures":      (e.enclosures       if hasattr(e,"enclosures")       and e.enclosures       else None),
                "has_content_img": None,
            }
            raw_html = ""
            if hasattr(e,"content") and e.content:
                raw_html = e.content[0].get("value","")
            if not raw_html and e.get("summary"):
                raw_html = e.get("summary","")
            if raw_html:
                pg = _BS4(raw_html, _PARSER)
                it = pg.find("img")
                info["has_content_img"] = (it.get("src") or it.get("data-src")) if it else None
            results.append(info)
        return jsonify({"entries": results})
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500

@app.route("/api/img-debug", methods=["GET"])
def img_debug():
    """Testuoja og:image paėmimą iš URL. ?url=https://..."""
    if not _auth_ok(): return _auth_fail()
    import re as _re
    url = request.args.get("url", "")
    if not url:
        return jsonify({"error": "Reikia ?url=..."}), 400
    try:
        r = _req.get(url, headers={"User-Agent": _UA}, timeout=8)
        full_html = r.text
        status = r.status_code
        patterns = {
            "og:image":      r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](https?://[^"\']+)',
            "og:image(rev)": r'<meta[^>]+content=["\'](https?://[^"\']+)[^>]+property=["\']og:image["\']',
            "itemprop":      r'<meta[^>]+itemprop=["\']image["\'][^>]+content=["\'](https?://[^"\']+)',
            "itemprop(rev)": r'<meta[^>]+content=["\'](https?://[^"\']+)[^>]+itemprop=["\']image["\']',
            "twitter":       r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\'](https?://[^"\']+)',
        }
        results = {}
        for name, pat in patterns.items():
            m = _re.search(pat, full_html)
            results[name] = m.group(1) if m else None
        # Pirma didelė img
        pg = _BS4(full_html, _PARSER)
        first_img = None
        for img in pg.find_all("img"):
            src = img.get("src","")
            if not src or not src.startswith("http"): continue
            try: w = int(img.get("width","0"))
            except: w = 0
            if w and w < 200: continue
            if any(x in src for x in ["logo","icon","avatar","thumb","sprite"]): continue
            first_img = src; break
        head_snippet = full_html[:3000]
        return jsonify({"status": status, "patterns": results,
                        "first_body_img": first_img, "head_snippet": head_snippet})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/tg-test", methods=["GET"])
def tg_test():
    if not _auth_ok(): return _auth_fail()
    result = tg_send("🧪 Testas – Telegram pranešimai veikia!")
    return jsonify({"telegram_response": result,
                    "token_set": bool(TG_TOKEN),
                    "chat_set": bool(TG_CHAT)})

@app.route("/api/debug-fetch", methods=["GET"])
def debug_fetch():
    """Testuoja _fetch_http konkrečiai svetainei ir grąžina rezultatus arba klaidą."""
    if not _auth_ok(): return _auth_fail()
    import traceback
    name = request.args.get("site", "LTOK")
    site = next((s for s in _SITES if s["name"] == name), None)
    if not site:
        return jsonify({"error": f"Site '{name}' not found", "sites": [s["name"] for s in _SITES]}), 404
    try:
        r = _req.get(site["url"], headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "lt-LT,lt;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        }, timeout=10)
        status = r.status_code
        html_len = len(r.text)
        from bs4 import BeautifulSoup as _BSd
        import re as _red
        soup = _BSd(r.text, _PARSER)
        base = site.get("base_url", "")
        pat_re = _red.compile(site["link_pattern_re"]) if "link_pattern_re" in site else None
        title_sel = site.get("title_selector", "")
        matches = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if pat_re and not pat_re.search(href): continue
            if "#" in href: continue
            url = href if href.startswith("http") else base + (href if href.startswith("/") else "/" + href)
            tel = a.select_one(title_sel) if title_sel else None
            title = tel.get_text(strip=True) if tel else a.get_text(strip=True)[:60]
            matches.append({"url": url, "title": title[:80]})
            if len(matches) >= 15: break
        return jsonify({"site": name, "http_status": status, "html_len": html_len,
                        "matches": matches, "match_count": len(matches),
                        "html_preview": r.text[:600]})
    except Exception as e:
        return jsonify({"site": name, "error": str(e), "trace": traceback.format_exc()[-800:]}), 500

@app.route("/api/sources", methods=["GET"])
def get_sources():
    """Grąžina sportas.lt šaltinių sąrašą su ID ir mūsų svetainių priskyrimą."""
    if not _auth_ok(): return _auth_fail()
    try:
        sess = _session()
        if not SPORTAS_USER:
            return jsonify({"ok": False, "error": "SPORTAS_USER nenustatytas"}), 400
        sources = _sources(sess)
        # Surikiuojame pagal pavadinimą
        sorted_sources = sorted(sources.items(), key=lambda x: x[0].lower())
        # Parodome ir mūsų svetainių priskyrimus
        our_sites = [{"name": s["name"], "sport": s["sport"],
                      "sportas_source": s.get("sportas_source", ""),
                      "auto_match": _match_source(sources, s["name"]) if sources else "?"}
                     for s in _SITES]
        return jsonify({
            "sportas_sources": [{"name": n, "id": sid} for n, sid in sorted_sources],
            "our_sites": our_sites,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
