from flask import Flask, request, jsonify, Response
import json, os, time, requests as _req, hashlib, feedparser
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup as _BS4
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime

app = Flask(__name__)

KV_URL        = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN      = os.environ.get("KV_REST_API_TOKEN", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SPORTAS_USER  = os.environ.get("SPORTAS_USER", "")
SPORTAS_PASS  = os.environ.get("SPORTAS_PASS", "")

_SPORT_CATS = {"krepšinis": [22, 6], "futbolas": [103, 7]}
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
              json=[["SADD", key] + list(members), ["EXPIRE", key, 86400 * 7]],
              timeout=5)


# ── AI praturtinimas ───────────────────────────────────────────────
def _ai_enrich(title, text):
    try:
        import anthropic, re as _re
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        prompt = (
            f"Sporto straipsnis (lietuvių k.):\nPavadinimas: {title}\n\n"
            f"Tekstas:\n{text[:3000]}\n\n"
            "1. Sugeneruok 1–4 temas (tagus): asmenų vardai/pavardės, klubų pavadinimai.\n"
            "2. Tekste paboldink PIRMĄ kiekvieno asmens vardo paminėjimą "
            "(<strong>Vardas Pavardė</strong>).\n\n"
            "Atsakyk TIKSLIAI šiuo formatu:\nTAGS: Tag1, Tag2\nTEXT:\npilnas tekstas"
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=3000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        tags, enriched = "", text
        m = _re.search(r'^TAGS:\s*(.+)$', raw, _re.MULTILINE)
        if m: tags = m.group(1).strip()
        m = _re.search(r'^TEXT:\s*\n(.*)', raw, _re.MULTILINE | _re.DOTALL)
        if m: enriched = m.group(1).strip()
        return {"tags": tags, "text": enriched}
    except Exception as e:
        return {"tags": "", "text": text}


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
    soup = BeautifulSoup(r.text, "html.parser")
    sel  = soup.find("select", {"id": "sourceSelect"})
    result = {}
    if sel:
        for opt in sel.find_all("option"):
            v, t = opt.get("value",""), opt.get_text(strip=True)
            if v and t: result[t] = v
    _kv_set("sportas_sources", result)
    return result

def _do_post(article):
    sport    = article.get("sport", "")
    cat_ids  = _SPORT_CATS.get(sport, [])
    title    = article.get("title", "")
    text     = article.get("text", "")
    site     = article.get("site", "")

    # Jei tekstas tuščias – bandome gauti iš straipsnio URL
    if not text and article.get("url"):
        try:
            r = _req.get(article["url"], headers={"User-Agent": _UA}, timeout=8)
            from bs4 import BeautifulSoup as _BSt
            soup = _BSt(r.text, "html.parser")
            for tag in soup(["script","style","nav","footer","header","aside","iframe","noscript","form","button"]):
                tag.decompose()
            main = (soup.select_one('[class*="post-content"]') or
                    soup.select_one('[class*="article-content"]') or
                    soup.select_one('[class*="entry-content"]') or
                    soup.select_one('[class*="article-body"]') or
                    soup.select_one('[class*="prose"]') or
                    soup.select_one('.fck') or
                    soup.select_one('article') or
                    soup.select_one('main'))
            if main:
                paras = [" ".join(el.get_text(" ", strip=True).split())
                         for el in main.find_all(["p","h2","h3","h4"])
                         if len(el.get_text(strip=True)) > 4]
                text = "\n\n".join(paras[:60])
        except:
            pass

    enriched  = _ai_enrich(title, text)
    ai_tags   = enriched.get("tags", "")
    rich_text = enriched.get("text", text)
    paras     = [p.strip() for p in rich_text.split("\n\n") if p.strip()]
    html_body = "".join(f"<p>{p}</p>" for p in paras)
    tags_list = [t.strip() for t in ai_tags.split(",") if t.strip()] if ai_tags else []

    if not SPORTAS_USER:
        return False, "SPORTAS_USER env var nenustatytas"

    sess      = _session()
    # Visada iš naujo gauname source sąrašą (ne iš cache)
    _kv_set("sportas_sources", {})
    sources   = _sources(sess)
    source_id = _match_source(sources, site)

    # Gauname edit puslapį pirma – jo formoje yra lietuviškas laikas
    from bs4 import BeautifulSoup as _BS
    edit_r = sess.get(f"{_BASE}/editArticle/", allow_redirects=True, timeout=15)
    if edit_r.url and "login" in edit_r.url.lower():
        return False, f"Login nepavyko | cookies: {list(sess.cookies.keys())} | redirect: {edit_r.url}"
    if edit_r.status_code != 200:
        return False, f"editArticle grąžino {edit_r.status_code} | url: {edit_r.url}"
    edit_soup = _BS(edit_r.text, "html.parser")

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
        ("leadPhoto[path]",""),("leadPhoto[title]",""),("leadPhoto[size]","l"),
        ("cropSize","l"),("leadLiveVideoTime[endDate]",""),("leadLiveVideoTime[endTime]",""),
        ("leadPlayVideo[code]",""),("leadPlayVideo[playId]",""),("leadVideo[url]",""),
        ("attachCustomJs[]",""),("attachFbPost[]",""),
        ("mainCategory", main_cat),
    ]
    for cid in cat_ids: data.append(("categories[]", str(cid)))
    # Siunčiame priority[] visiems kategorijų ID (kaip realus naršyklė)
    for cid in _ALL_CAT_IDS: data.append((f"priority[{cid}]", "1000"))
    data += [
        ("source",str(source_id)),("realSource","0"),("realSource","0"),
        ("disableComments","0"),("commentsForUsers","0"),("isLiveNews","0"),("tags",""),
    ]
    for tag in tags_list: data.append(("tags[]", tag))
    data += [
        ("n18","0"),("sensitive","0"),("top10","0"),("useSpecNews","0"),
        ("orderedArticle","0"),("leftBlocks","0"),("cacheKey",""),
        ("status","0"),("exportArticle","0"),
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
    r = sess.post(save_url, data=data,
                  allow_redirects=False, timeout=30,
                  headers={"Referer": f"{_BASE}/editArticle/",
                           "Origin": "https://www.sportas.lt"})
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
            fsoup = _BSf(follow.text, "html.parser")
            # Ieškome klaidos arba sėkmės pranešimo
            alert = fsoup.select_one(".alert, .error, .success, .flash, [class*='message']")
            msg = alert.get_text(strip=True)[:200] if alert else ""
            # Tikriname ar atsirado naujas straipsnis (articleList rodo naujausius)
            first_row = fsoup.select_one("table tr:nth-child(2) td, .list-item:first-child")
            first_title = first_row.get_text(strip=True)[:80] if first_row else ""
            return True, f"redirect:{location} | source:{source_id} | msg:{msg} | first:{first_title}"
        except Exception as ex:
            return True, f"redirect:{location} | source:{source_id} | follow_err:{ex}"
    body = r.text[:500].replace("\n", " ").replace("\r", "")
    return False, f"HTTP {r.status_code} | save_url={save_url[:80]} | location={location} | source:{source_id} | body:{body[:200]}"


# ── Inline scraperis (naudojamas /api/refresh) ─────────────────────
_UA  = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_MAX = 10

_SITES = [
    {"name":"FK Banga",       "sport":"futbolas",  "rss":"https://www.fkbanga.lt/feed/"},
    {"name":"FC Džiugas",     "sport":"futbolas",  "rss":"https://www.fcdziugas.lt/feed/"},
    {"name":"FC Hegelmann",   "sport":"futbolas",  "rss":"https://fchegelmann.com/feed/"},
    {"name":"FK Panevėžys",   "sport":"futbolas",  "rss":"https://fk-panevezys.lt/feed/"},
    {"name":"FK Sūduva",      "sport":"futbolas",  "rss":"https://fksuduva.lt/feed/"},
    {"name":"FK TransINVEST", "sport":"futbolas",  "rss":"https://fktransinvest.lt/feed/"},
    {"name":"FA Šiauliai",    "sport":"futbolas",  "rss":"https://siauliufa.lt/feed/"},
    {"name":"FK Žalgiris",    "sport":"futbolas",  "rss":"https://fkzalgiris.lt/feed/"},
    {"name":"LFF",            "sport":"futbolas",  "rss":"https://www.lff.lt/feed/"},
    {"name":"BC Neptūnas",    "sport":"krepšinis", "rss":"https://bcneptunas.lt/feed/"},
    {"name":"BC Lietkabelis", "sport":"krepšinis", "rss":"https://www.kklietkabelis.lt/feed/"},
    {"name":"BC Šiauliai",    "sport":"krepšinis", "rss":"https://bcsiauliai.lt/feed/"},
    {"name":"Utenos Juventus","sport":"krepšinis", "rss":"https://utenosjuventus.lt/feed/"},
    {"name":"Lietuva Basketball","sport":"krepšinis","rss":"https://lietuva.basketball/feed/"},
    {"name":"BC Rytas",       "sport":"krepšinis", "rss":"https://rytasvilnius.lt/feed/"},
    {"name":"Top Lyga", "sport":"futbolas", "method":"http",
     "url":"https://toplyga.lt/naujienos",
     "selectors":{"articles":"div.new","title":"a.title","link":"a.title","image":"img"},
     "base_url":"https://toplyga.lt"},
    {"name":"Žalgiris futbolas", "sport":"futbolas", "method":"http",
     "url":"https://zalgiris.lt/naujienos?category=futbolas",
     "selectors":{"articles":"article","title":"div.font-semibold a",
                  "link":"div.font-semibold a","image":"figure img"},
     "base_url":"https://zalgiris.lt"},
    {"name":"FK Riteriai", "sport":"futbolas", "method":"http",
     "url":"https://www.fkriteriai.lt/naujienos",
     "link_pattern":"/post/", "base_url":"https://www.fkriteriai.lt"},
    {"name":"LKL", "sport":"krepšinis", "method":"http",
     "url":"https://lkl.lt/straipsniai",
     "link_pattern_re": r"/straipsniai/\d+/",
     "base_url":"https://lkl.lt"},
    {"name":"Žalgiris", "sport":"krepšinis", "method":"http",
     "url":"https://zalgiris.lt/naujienos?category=zalgiris",
     "selectors":{"articles":"article","title":"div.font-semibold a",
                  "link":"div.font-semibold a","image":"figure img"},
     "base_url":"https://zalgiris.lt"},
    {"name":"KK Nevėžis", "sport":"krepšinis", "method":"http",
     "url":"https://www.kknevezis.lt/naujienos",
     "link_pattern":"/naujienos/", "base_url":"https://www.kknevezis.lt"},
    {"name":"BC Jonava", "sport":"krepšinis", "method":"http",
     "url":"https://bcjonavahipocredit.lt/naujienos/",
     "selectors":{"articles":"div.news-list-post","title":"h4 a","link":"h4 a","image":"img"},
     "base_url":"https://bcjonavahipocredit.lt"},
]

def _art_id(url, title):
    return hashlib.md5(f"{url}{title}".encode()).hexdigest()

def _strip_wp_footer(text: str) -> str:
    """Pašalina WordPress RSS artefaktą: 'The post X appeared first on Y.'"""
    import re as _re
    return _re.sub(r'\s*The post\s+.+?\s+appeared first on\s+.+?\.?\s*$', '', text, flags=_re.DOTALL | _re.IGNORECASE).strip()

def _html_to_text(html):
    if not html: return ""
    soup = _BS4(html, "html.parser")
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
        feed = feedparser.parse(site["rss"], request_headers={"User-Agent": _UA})
        arts = []
        for e in feed.entries[:_MAX]:
            title = e.get("title","").strip()
            url   = e.get("link","").strip()
            date  = e.get("published", e.get("updated",""))
            image = None
            if hasattr(e,"media_thumbnail") and e.media_thumbnail:
                image = e.media_thumbnail[0].get("url")
            elif hasattr(e,"enclosures") and e.enclosures:
                image = e.enclosures[0].get("href")
            text = ""
            if hasattr(e,"content") and e.content:
                text = _html_to_text(e.content[0].get("value",""))
            if not text and e.get("summary"):
                text = _html_to_text(e.get("summary",""))
            if title and url:
                arts.append({"site":site["name"],"sport":site.get("sport",""),
                    "title":title,"url":url,"date":date,"image":image,
                    "text":text,"source":"RSS","id":_art_id(url,title)})
        return arts
    except: return []

def _fetch_http(site):
    try:
        r = _req.get(site["url"], headers={"User-Agent":_UA}, timeout=5)
        if r.status_code != 200: return []
        soup = _BS4(r.text, "html.parser")
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
                title = a.get_text(strip=True)
                if not title or len(title) < 5:
                    h = a.find(["h2","h3","h4"])
                    if h: title = h.get_text(strip=True)
                if not title or len(title) < 5:
                    img_in_a = a.find("img")
                    if img_in_a and img_in_a.get("alt"):
                        title = img_in_a["alt"].strip()
                if not title or len(title) < 5 or url in seen: continue
                seen.add(url)
                img = a.find("img")
                image = None
                if img:
                    src = img.get("src") or img.get("data-src","")
                    image = (base+src if src.startswith("/") else src) or None
                arts.append({"site":site["name"],"sport":site.get("sport",""),
                    "title":title,"url":url,"date":"","image":image,
                    "text":"","source":"HTTP","id":_art_id(url,title)})
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

def run_scraper():
    seen_ids    = _kv_smembers("seen_ids")
    dates_cache = _kv_get("dates_cache") or {}
    rss_sites  = [s for s in _SITES if "rss" in s]
    http_sites = [s for s in _SITES if s.get("method") == "http"]
    all_arts   = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(_fetch_rss, s): s for s in rss_sites}
        futs.update({ex.submit(_fetch_http, s): s for s in http_sites})
        for fut in as_completed(futs):
            try: all_arts.extend(fut.result())
            except: pass
    new_ids = {a["id"] for a in all_arts if a["id"] not in seen_ids}

    # Datos: naujoms be datos – fiksuojame dabar; senoms – atkuriame iš cache
    now_iso = datetime.now(timezone.utc).isoformat()
    dates_changed = False
    for art in all_arts:
        aid = art["id"]
        if not art.get("date"):
            if aid in dates_cache:
                art["date"] = dates_cache[aid]
            else:
                art["date"] = now_iso        # pirmas kartas – fiksuojame (nauji ar seni)
                dates_cache[aid] = now_iso
                dates_changed = True

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
                if art["id"] in new_ids: recent_ids.append(art["id"])
    sorted_arts = ([a for a in all_arts if a["id"] in recent_ids] +
                   [a for a in all_arts if a["id"] not in recent_ids])
    _kv_set("articles",   sorted_arts, ex=86400*2)
    _kv_set("recent_ids", recent_ids,  ex=3600*3)
    if new_ids:
        _kv_sadd("seen_ids", *list(new_ids))
    if dates_changed:
        _kv_set("dates_cache", dates_cache, ex=86400*30)
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
</style>
</head><body>
<h1>🏆 Sporto naujienos</h1>
<div class="meta-bar" id="meta">Kraunama...</div>
<div class="sport-tabs">
  <button class="sport-tab active-all" onclick="setSport('all',this)">Visos</button>
  <button class="sport-tab" onclick="setSport('futbolas',this)">⚽ Futbolas</button>
  <button class="sport-tab" onclick="setSport('krepsinys',this)">🏀 Krepšinis</button>
</div>
<div class="stats" id="stats"></div>
<div style="display:flex;gap:8px;justify-content:center;margin-bottom:20px">
  <button class="refresh-btn" id="refreshBtn" onclick="manualRefresh()" style="margin:0">🔄 Atnaujinti</button>
  <button class="refresh-btn" id="notifBtn" onclick="askNotif()" style="margin:0">🔔 Pranešimai</button>
</div>
<div class="filters" id="filters"></div>
<div class="grid" id="grid"><div class="loading">⏳ Kraunamos naujienos...</div></div>
<script>
let ALL = [], RECENT = new Set(), curSport = 'all', curSite = 'all';
function fmtDate(d) {
  if (!d) return '';
  try {
    const dt = new Date(d);
    if (isNaN(dt)) return d;
    return dt.toLocaleString('lt-LT', {timeZone:'Europe/Vilnius',
      year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
  } catch { return d; }
}
function renderCards() {
  const sportMap = {futbolas:'futbolas', krepsinys:'krepšinis'};
  const matchSport = sportMap[curSport] || null;
  const grid = document.getElementById('grid');
  if (!ALL.length) { grid.innerHTML = '<div class="loading">Straipsnių nerasta</div>'; return; }
  let html = '';
  for (const art of ALL) {
    const sportOk = !matchSport || art.sport === matchSport;
    const siteOk  = curSite === 'all' || art.site === curSite;
    if (!sportOk || !siteOk) continue;
    const isR  = RECENT.has(art.id);
    const sCol = art.sport === 'futbolas' ? '#22c55e' : '#f97316';
    const mCol = art.source === 'RSS' ? '#64748b' : (art.source === 'HTTP' ? '#3b82f6' : sCol);
    const sIcon = art.sport === 'futbolas' ? '⚽' : '🏀';
    const badge = isR ? '<span class="badge">🆕 NAUJA</span>' : '';
    const img   = art.image ? `<img src="${art.image}" loading="lazy" onerror="this.style.display='none'">` : '';
    const id    = art.id;
    html += `<div class="card ${isR?'recent':''}" data-sport="${art.sport}" data-site="${art.site}">
  ${img}<div class="card-body">${badge}
    <div class="meta"><span class="sport-tag" style="color:${sCol}">${sIcon} ${art.sport}</span>
      <span class="site">${art.site}</span>
      <span class="method" style="background:${mCol}22;color:${mCol}">${art.source}</span></div>
    <h3><a href="${art.url}" target="_blank">${art.title}</a></h3>
    <div class="date">${fmtDate(art.date)}</div>
    <button class="copy-btn" onclick="copyArt('${id}',this)">📋 Kopijuoti</button>
    <button class="post-btn" onclick="postArt('${id}',this)">📤 Įdėti</button>
  </div></div>`;
  }
  grid.innerHTML = html || '<div class="loading">Nėra straipsnių šiai kategorijai</div>';
  const f = document.getElementById('stats');
  const fc = ALL.filter(a=>a.sport==='futbolas').length;
  const kc = ALL.filter(a=>a.sport==='krepšinis').length;
  f.innerHTML = `<div class="stat"><strong>${ALL.length}</strong>Iš viso</div>
    <div class="stat"><strong style="color:#ef4444">${RECENT.size}</strong>Naujų</div>
    <div class="stat"><strong style="color:#22c55e">${fc}</strong>⚽</div>
    <div class="stat"><strong style="color:#f97316">${kc}</strong>🏀</div>`;
}
function renderFilters() {
  const sportMap = {futbolas:'futbolas', krepsinys:'krepšinis'};
  const ms = sportMap[curSport] || null;
  const sites = [...new Set(ALL.filter(a => !ms || a.sport===ms).map(a=>a.site))].sort();
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
async function postArt(id, btn) {
  btn.textContent = '⏳ Įdedama...'; btn.disabled = true;
  try {
    const r = await fetch('/api/post', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({id})});
    const d = await r.json();
    if (d.ok) { btn.textContent = '✅ Įdėta! ' + (d.message||''); btn.classList.add('posted'); }
    else { btn.textContent = '❌ ' + (d.error || d.message || 'Klaida'); btn.disabled = false;
           console.error('Post error:', d); }
  } catch(e) { btn.textContent = '❌ Klaida'; btn.disabled = false; }
}
let _lastRecent = '';
async function _checkNew() {
  try {
    const r = await fetch('/api/articles');
    const d = await r.json();
    const newKey = (d.recent_ids||[]).sort().join(',');
    const now = new Date().toLocaleString('lt-LT', {timeZone:'Europe/Vilnius'});
    if (newKey !== _lastRecent && _lastRecent !== '') {
      ALL = d.articles || []; RECENT = new Set(d.recent_ids || []);
      document.getElementById('meta').textContent = '🆕 Nauja naujiena! ' + now + ' | ' + ALL.length + ' straipsnių';
      renderFilters(); renderCards();
      if (Notification.permission === 'granted' && d.recent_ids?.length)
        new Notification('🏆 Nauja naujiena!', {body: `Rasta ${d.recent_ids.length} nauja(-ų)`});
    }
    _lastRecent = newKey;
  } catch {}
}
async function manualRefresh() {
  const btn = document.getElementById('refreshBtn');
  btn.textContent = '⏳ Tikrinama...'; btn.disabled = true;
  document.getElementById('meta').textContent = '⏳ Kreipiamasi į svetaines, palaukite...';
  try {
    const r = await fetch('/api/refresh', {method:'POST'});
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
    new Notification('🏆 Sporto naujienos', {body: 'Pranešimai įjungti! Gausite žinutę kai atsiras naujienų.'});
  } else {
    btn.textContent = '🚫 Užblokuota';
  }
}
function _updateNotifBtn() {
  const btn = document.getElementById('notifBtn');
  if (!btn) return;
  if (!('Notification' in window)) { btn.textContent = '🔔 Nepalaikoma'; btn.disabled = true; return; }
  if (Notification.permission === 'granted') btn.textContent = '✅ Pranešimai';
  else if (Notification.permission === 'denied') { btn.textContent = '🚫 Užblokuota'; btn.disabled = true; }
  else btn.textContent = '🔔 Pranešimai';
}
loadData().then(() => { _lastRecent = [...RECENT].sort().join(','); _updateNotifBtn(); });
setInterval(_checkNew, 30000);
</script>
</body></html>"""

# ── Routes ─────────────────────────────────────────────────────────
@app.route("/")
def serve_index():
    return Response(_INDEX_HTML, content_type="text/html; charset=utf-8")

@app.route("/api/articles", methods=["GET"])
def articles():
    data   = _kv_get("articles")   or []
    recent = _kv_get("recent_ids") or []
    return jsonify({"articles": data, "recent_ids": recent})

@app.route("/api/article-text", methods=["GET"])
def article_text():
    url = request.args.get("url", "")
    if not url:
        return jsonify({"text": ""}), 400
    try:
        r = _req.get(url, headers={"User-Agent": _UA}, timeout=8)
        soup = _BS4(r.text, "html.parser")
        for tag in soup(["script","style","nav","footer","header","aside",
                         "iframe","noscript","form","button"]):
            tag.decompose()
        # Ieškome turinio konteinerio - nuo konkretesnių iki bendresnių
        main = (soup.select_one('[class*="post-content"]') or
                soup.select_one('[class*="article-content"]') or
                soup.select_one('[class*="entry-content"]') or
                soup.select_one('[class*="article-body"]') or
                soup.select_one('[class*="prose"]') or
                soup.select_one('.fck') or
                soup.select_one('article') or
                soup.select_one('main') or
                soup.body)
        paragraphs = []
        if main:
            for el in main.find_all(["p", "h2", "h3", "h4", "li"]):
                txt = el.get_text(" ", strip=True)
                txt = " ".join(txt.split())
                if len(txt) > 4:
                    paragraphs.append(txt)
        text = _strip_wp_footer("\n\n".join(paragraphs))
        return jsonify({"text": text[:30000]})
    except Exception as e:
        return jsonify({"text": "", "error": str(e)})

@app.route("/api/refresh", methods=["POST"])
def refresh():
    import traceback
    try:
        total, new_count = run_scraper()
        data   = _kv_get("articles")   or []
        recent = _kv_get("recent_ids") or []
        return jsonify({"articles": data, "recent_ids": recent,
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

@app.route("/api/post", methods=["POST", "OPTIONS"])
def post():
    if request.method == "OPTIONS":
        return Response("", headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })
    payload  = request.get_json() or {}
    aid      = payload.get("id", "")
    articles = _kv_get("articles") or []
    article  = next((a for a in articles if a["id"] == aid), None)
    if not article:
        return jsonify({"ok": False, "error": "Nerasta"}), 404
    ok, msg = _do_post(article)
    return jsonify({"ok": ok, "message": msg})
