#!/usr/bin/env python3
"""GitHub Actions scraper – RSS + HTTP, rašo į Vercel KV"""

import json, os, hashlib, time, feedparser, requests as _req
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime

# ── Vercel KV (Upstash Redis REST API) ────────────────────────────
KV_URL   = os.environ["KV_REST_API_URL"]
KV_TOKEN = os.environ["KV_REST_API_TOKEN"]
_HDR     = {"Authorization": f"Bearer {KV_TOKEN}"}

def kv_get(key: str):
    r = _req.get(f"{KV_URL}/get/{key}", headers=_HDR, timeout=10)
    result = r.json().get("result")
    return json.loads(result) if result else None

def kv_set(key: str, value, ex: int = 86400 * 2):
    _req.post(f"{KV_URL}/pipeline", headers=_HDR, timeout=10,
              json=[["SET", key, json.dumps(value, ensure_ascii=False), "EX", ex]])

def kv_smembers(key: str) -> set:
    r = _req.get(f"{KV_URL}/smembers/{key}", headers=_HDR, timeout=10)
    return set(r.json().get("result") or [])

def kv_sadd(key: str, *members):
    if members:
        _req.post(f"{KV_URL}/pipeline", headers=_HDR, timeout=10,
                  json=[["SADD", key] + list(members),
                        ["EXPIRE", key, 86400 * 7]])

# ── Bendros funkcijos ──────────────────────────────────────────────
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
MAX = 10

def art_id(url: str, title: str) -> str:
    return hashlib.md5(f"{url}{title}".encode()).hexdigest()

def html_to_text(html: str) -> str:
    if not html: return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script","style","nav","footer","header","aside"]):
        tag.decompose()
    parts = []
    for el in soup.find_all(["p","h2","h3","h4","li"]):
        txt = " ".join(el.get_text(" ", strip=True).split())
        if len(txt) > 20:
            parts.append(txt)
    return "\n\n".join(parts) if parts else " ".join(soup.get_text(" ", strip=True).split())

# ── Svetainių sąrašas ─────────────────────────────────────────────
SITES = [
    # ⚽ FUTBOLAS – RSS
    {"name":"FK Banga",       "sport":"futbolas",  "rss":"https://www.fkbanga.lt/feed/"},
    {"name":"FC Džiugas",     "sport":"futbolas",  "rss":"https://www.fcdziugas.lt/feed/"},
    {"name":"FC Hegelmann",   "sport":"futbolas",  "rss":"https://fchegelmann.com/feed/"},
    {"name":"FK Panevėžys",   "sport":"futbolas",  "rss":"https://fk-panevezys.lt/feed/"},
    {"name":"FK Sūduva",      "sport":"futbolas",  "rss":"https://fksuduva.lt/feed/"},
    {"name":"FK TransINVEST", "sport":"futbolas",  "rss":"https://fktransinvest.lt/feed/"},
    {"name":"FA Šiauliai",    "sport":"futbolas",  "rss":"https://siauliufa.lt/feed/"},
    {"name":"FK Žalgiris",    "sport":"futbolas",  "rss":"https://fkzalgiris.lt/feed/"},
    {"name":"LFF",            "sport":"futbolas",  "rss":"https://www.lff.lt/feed/"},
    # ⚽ FUTBOLAS – HTTP
    {"name":"Top Lyga", "sport":"futbolas", "method":"http",
     "url":"https://toplyga.lt/naujienos",
     "selectors":{"articles":"div.new","title":"a.title","link":"a.title","image":"img"},
     "base_url":"https://toplyga.lt"},
    {"name":"Žalgiris futbolas", "sport":"futbolas", "method":"http",
     "url":"https://zalgiris.lt/naujienos?category=futbolas",
     "selectors":{"articles":"article",
                  "title":"div.font-semibold a",
                  "link":"div.font-semibold a",
                  "image":"figure img"},
     "base_url":"https://zalgiris.lt"},
    {"name":"FK Riteriai", "sport":"futbolas", "method":"http",
     "url":"https://www.fkriteriai.lt/naujienos",
     "link_pattern":"/post/", "base_url":"https://www.fkriteriai.lt"},
    # 🏀 KREPŠINIS – RSS
    {"name":"BC Neptūnas",       "sport":"krepšinis", "rss":"https://bcneptunas.lt/feed/"},
    {"name":"BC Lietkabelis",    "sport":"krepšinis", "rss":"https://www.kklietkabelis.lt/feed/"},
    {"name":"BC Šiauliai",       "sport":"krepšinis", "rss":"https://bcsiauliai.lt/feed/"},
    {"name":"Utenos Juventus",   "sport":"krepšinis", "rss":"https://utenosjuventus.lt/feed/"},
    {"name":"Lietuva Basketball","sport":"krepšinis", "rss":"https://lietuva.basketball/feed/"},
    {"name":"BC Rytas",          "sport":"krepšinis", "rss":"https://rytasvilnius.lt/feed/"},
    # 🏀 KREPŠINIS – HTTP
    {"name":"Žalgiris", "sport":"krepšinis", "method":"http",
     "url":"https://zalgiris.lt/naujienos?category=zalgiris",
     "selectors":{"articles":"article",
                  "title":"div.font-semibold a",
                  "link":"div.font-semibold a",
                  "image":"figure img"},
     "base_url":"https://zalgiris.lt"},
    {"name":"KK Nevėžis", "sport":"krepšinis", "method":"http",
     "url":"https://www.kknevezis.lt/naujienos",
     "link_pattern":"/naujienos/", "base_url":"https://www.kknevezis.lt"},
    {"name":"BC Jonava", "sport":"krepšinis", "method":"http",
     "url":"https://bcjonavahipocredit.lt/naujienos/",
     "selectors":{"articles":"div.news-list-post","title":"h4 a",
                  "link":"h4 a","image":"img"},
     "base_url":"https://bcjonavahipocredit.lt"},
]

# ── Fetcher'iai ────────────────────────────────────────────────────
def fetch_rss(site: dict) -> list:
    try:
        feed = feedparser.parse(site["rss"])
        articles = []
        for e in feed.entries[:MAX]:
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
                text = html_to_text(e.content[0].get("value",""))
            if not text and e.get("summary"):
                text = html_to_text(e.get("summary",""))
            if title and url:
                articles.append({
                    "site":site["name"], "sport":site.get("sport",""),
                    "title":title, "url":url, "date":date,
                    "image":image, "text":text,
                    "source":"RSS", "id":art_id(url,title),
                })
        return articles
    except Exception as e:
        print(f"  ❌ RSS {site['name']}: {e}")
        return []


def fetch_http(site: dict) -> list:
    try:
        r = _req.get(site["url"], headers={"User-Agent": UA}, timeout=15)
        if r.status_code != 200:
            print(f"  ⚠️  {site['name']}: HTTP {r.status_code}")
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        base = site.get("base_url","")
        articles = []

        if "link_pattern" in site or "link_patterns" in site:
            patterns = site.get("link_patterns") or [site.get("link_pattern","")]
            seen = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if not any(p in href for p in patterns): continue
                if "#" in href: continue
                url = href if href.startswith("http") else base + href
                title = a.get_text(strip=True)
                if not title or len(title) < 5:
                    h = a.find(["h2","h3","h4"])
                    if h: title = h.get_text(strip=True)
                if not title or len(title) < 5: continue
                if url in seen: continue
                seen.add(url)
                img = a.find("img")
                image = None
                if img:
                    src = img.get("src") or img.get("data-src","")
                    image = (base+src if src.startswith("/") else src) or None
                articles.append({
                    "site":site["name"], "sport":site.get("sport",""),
                    "title":title, "url":url, "date":"", "image":image, "text":"",
                    "source":"HTTP", "id":art_id(url,title),
                })
                if len(articles) >= MAX: break

        elif "selectors" in site:
            sel = site["selectors"]
            for container in soup.select(sel["articles"]):
                title_el = container.select_one(sel["title"])
                title    = title_el.get_text(strip=True) if title_el else ""
                link_el  = container.select_one(sel["link"])
                href     = link_el.get("href","") if link_el else ""
                url      = href if href.startswith("http") else (base+href if href else "")
                img_el   = container.select_one(sel.get("image","img"))
                image    = None
                if img_el:
                    src   = img_el.get("src") or img_el.get("data-src","")
                    image = (base+src if src.startswith("/") else src) or None
                if title and url:
                    articles.append({
                        "site":site["name"], "sport":site.get("sport",""),
                        "title":title, "url":url, "date":"", "image":image, "text":"",
                        "source":"HTTP", "id":art_id(url,title),
                    })
                if len(articles) >= MAX: break

        return articles
    except Exception as e:
        print(f"  ❌ HTTP {site['name']}: {e}")
        return []

# ── Rikiavimas pagal datą ──────────────────────────────────────────
def _sort_key(art):
    d = art.get("date","")
    if not d: return datetime.min
    try:
        return parsedate_to_datetime(d).replace(tzinfo=None)
    except:
        try:
            return datetime.fromisoformat(d).replace(tzinfo=None)
        except:
            return datetime.min

# ── Pagrindinė funkcija ────────────────────────────────────────────
def main():
    print(f"\n{'═'*55}")
    print(f"🏆 SCRAPER  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*55}\n")

    seen_ids    = kv_smembers("seen_ids")
    dates_cache = kv_get("dates_cache") or {}   # {article_id: iso_date}
    print(f"📦 Žinomų straipsnių: {len(seen_ids)}, datos cache: {len(dates_cache)}\n")

    rss_sites  = [s for s in SITES if "rss" in s]
    http_sites = [s for s in SITES if s.get("method") == "http"]
    all_articles = []

    # RSS – lygiagrečiai
    print(f"📡 RSS ({len(rss_sites)} saitų)...")
    with ThreadPoolExecutor(max_workers=len(rss_sites)) as ex:
        futures = {ex.submit(fetch_rss, s): s for s in rss_sites}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                arts = fut.result()
                all_articles.extend(arts)
                print(f"  ✅ {s['name']}: {len(arts)}")
            except Exception as e:
                print(f"  ❌ {s['name']}: {e}")

    # HTTP – eilės tvarka
    print(f"\n🌐 HTTP ({len(http_sites)} saitų)...")
    for s in http_sites:
        icon = "⚽" if s.get("sport") == "futbolas" else "🏀"
        arts = fetch_http(s)
        all_articles.extend(arts)
        print(f"  {icon} {s['name']}: {len(arts)}")
        time.sleep(1)

    # Nauji straipsniai
    new_ids = {a["id"] for a in all_articles if a["id"] not in seen_ids}
    print(f"\n📊 Iš viso: {len(all_articles)} | Naujų: {len(new_ids)}")

    # Datos: naujoms be datos – fiksuojame dabar; senoms – atkuriame iš cache
    now_iso = datetime.now(timezone.utc).isoformat()
    dates_changed = False
    for art in all_articles:
        aid = art["id"]
        if not art.get("date"):
            if aid in dates_cache:
                art["date"] = dates_cache[aid]      # atkuriame išsaugotą datą
            else:
                art["date"] = now_iso               # pirmas kartas – fiksuojame
                dates_cache[aid] = now_iso
                dates_changed = True

    # Rikiuoti pagal datą
    all_articles.sort(key=_sort_key, reverse=True)

    # recent_ids – nauji IR publikuoti per paskutines 3h
    cutoff = datetime.now(timezone.utc) - timedelta(hours=3)
    recent_ids = []
    for art in all_articles:
        if art["id"] not in new_ids:
            continue
        try:
            dt = parsedate_to_datetime(art.get("date",""))
            if dt >= cutoff:
                recent_ids.append(art["id"])
        except:
            try:
                dt = datetime.fromisoformat(art["date"].replace("Z",""))
                if dt.replace(tzinfo=None) >= cutoff.replace(tzinfo=None):
                    recent_ids.append(art["id"])
            except:
                if art["id"] in new_ids:
                    recent_ids.append(art["id"])

    # NAUJA straipsniai – viršuje
    sorted_arts = (
        [a for a in all_articles if a["id"] in recent_ids] +
        [a for a in all_articles if a["id"] not in recent_ids]
    )

    # Išsaugoti į KV
    print("\n💾 Saugoma į Vercel KV...")
    kv_set("articles",   sorted_arts,  ex=86400 * 2)
    kv_set("recent_ids", recent_ids,   ex=3600 * 3)
    if new_ids:
        kv_sadd("seen_ids", *list(new_ids))
    if dates_changed:
        kv_set("dates_cache", dates_cache, ex=86400 * 30)  # 30 dienų

    print(f"✅ Išsaugota {len(sorted_arts)} straipsnių, {len(new_ids)} naujų\n")


if __name__ == "__main__":
    main()
