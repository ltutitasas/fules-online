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

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")

def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT:
        print(f"  ⚠️  Telegram: token={bool(TG_TOKEN)}, chat={bool(TG_CHAT)} – praleista")
        return
    try:
        r = _req.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        data = r.json()
        if data.get("ok"):
            print(f"  ✅ Telegram išsiųsta (msg_id={data['result']['message_id']})")
        else:
            print(f"  ❌ Telegram klaida: {data}")
    except Exception as e:
        print(f"  ⚠️  Telegram exception: {e}")

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
    {"name":"FK Banga",       "sport":"futbolas",  "rss":"https://www.fkbanga.lt/feed/", "og_image_fallback": True, "image_selector": ".single-feat img"},
    {"name":"FC Džiugas",     "sport":"futbolas",  "rss":"https://www.fcdziugas.lt/feed/"},
    {"name":"FC Hegelmann",   "sport":"futbolas",  "rss":"https://fchegelmann.com/feed/", "og_image_fallback": True},
    {"name":"FK Panevėžys",   "sport":"futbolas",  "rss":"https://fk-panevezys.lt/feed/", "og_image_fallback": True},
    {"name":"FK Sūduva",      "sport":"futbolas",  "rss":"https://fksuduva.lt/feed/"},
    {"name":"FK TransINVEST", "sport":"futbolas",  "rss":"https://fktransinvest.lt/feed/"},
    {"name":"FA Šiauliai",    "sport":"futbolas",  "rss":"https://siauliufa.lt/feed/"},
    {"name":"FK Žalgiris",    "sport":"futbolas",  "rss":"https://fkzalgiris.lt/feed/"},
    {"name":"LFF",            "sport":"futbolas",  "rss":"https://www.lff.lt/feed/", "og_image_fallback": True},
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
    {"name":"BC Neptūnas",       "sport":"krepšinis", "rss":"https://bcneptunas.lt/feed/", "og_image_fallback": True, "image_selector": ".single-hero-img"},
    {"name":"BC Lietkabelis",    "sport":"krepšinis", "rss":"https://www.kklietkabelis.lt/feed/", "og_image_fallback": True},
    {"name":"BC Šiauliai",       "sport":"krepšinis", "rss":"https://bcsiauliai.lt/feed/", "og_image_fallback": True},
    {"name":"Utenos Juventus",   "sport":"krepšinis", "rss":"https://utenosjuventus.lt/feed/"},
    {"name":"Lietuva Basketball","sport":"krepšinis", "rss":"https://lietuva.basketball/feed/"},
    {"name":"BC Rytas",          "sport":"krepšinis", "rss":"https://rytasvilnius.lt/feed/", "og_image_fallback": True, "image_selector": ".article .image img"},
    # 🏀 KREPŠINIS – HTTP
    {"name":"LKL", "sport":"krepšinis", "method":"http",
     "url":"https://lkl.lt/straipsniai",
     "link_pattern_re": r"/straipsniai/\d+/",
     "base_url":"https://lkl.lt"},
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
    # ── Kiti ────────────────────────────────────────────────────────
    {"name":"Hockey Lietuva", "sport":"ledo ritulys", "method":"http",
     "url":"https://www.hockey.lt/index.php/naujienos/17",
     "link_pattern_re": r"/index\.php/naujienos/[^/]+/\d+",
     "base_url":"https://www.hockey.lt",
     "og_image_fallback": True, "image_selector":".news_item_img img",
     "text_selector":".short_text"},
    {"name":"LTOK", "sport":"kitas sportas", "method":"http",
     "url":"https://ltok.lt/naujienos",
     "link_pattern_re": r"/naujienos/[a-z]",
     "base_url":"https://ltok.lt",
     "title_selector": "[class*='text-ellipsis']",
     "text_selector": "[class*='prose']"},
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
            # Fallback: pirma <img> iš HTML turinio
            raw_html = ""
            if hasattr(e,"content") and e.content:
                raw_html = e.content[0].get("value","")
            if not raw_html and e.get("summary"):
                raw_html = e.get("summary","")
            if not image and raw_html:
                soup_img = BeautifulSoup(raw_html, "html.parser")
                img_tag = soup_img.find("img")
                if img_tag:
                    # data-src pirmiau (lazy load), tada src
                    src = img_tag.get("data-src","") or img_tag.get("src","")
                    if src and src.startswith("http"):
                        image = src
            text = html_to_text(raw_html) if raw_html else ""
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
    _HTTP_HEADERS = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "lt-LT,lt;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
    }
    try:
        r = _req.get(site["url"], headers=_HTTP_HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"  ⚠️  {site['name']}: HTTP {r.status_code}")
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        base = site.get("base_url","")
        articles = []

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
                url = href if href.startswith("http") else base + href
                # title_selector – specifinis elementas (pvz. ltok.lt)
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
                    "text_selector":site.get("text_selector",""),
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
    seen_urls   = kv_smembers("seen_urls")   # URL deduplication – net jei pavadinimas pasikeitė
    dates_cache = kv_get("dates_cache") or {}   # {article_id: iso_date}
    first_seen  = kv_get("first_seen")  or {}   # {article_id: iso} – kada MES pirmą kartą pamatėme
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

    # Naujas = nematytas id IR nematytas url (apsauga nuo redaguotų pavadinimų)
    new_ids = {a["id"] for a in all_articles
               if a["id"] not in seen_ids and a.get("url","") not in seen_urls}
    print(f"\n📊 Iš viso: {len(all_articles)} | Naujų: {len(new_ids)}")

    # og:image fallback lygiagrečiai (LFF ir kt. saituose su og_image_fallback=True)
    import re as _re
    _OG_FALLBACK_SITES = {s["name"] for s in SITES if s.get("og_image_fallback")}
    _SITE_IMG_SEL = {s["name"]: s["image_selector"] for s in SITES if s.get("image_selector")}
    _OG_PATS = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](https?://[^"\']+)',
        r'<meta[^>]+content=["\'](https?://[^"\']+)[^>]+property=["\']og:image["\']',
        r'<meta[^>]+itemprop=["\']image["\'][^>]+content=["\'](https?://[^"\']+)',
        r'<meta[^>]+content=["\'](https?://[^"\']+)[^>]+itemprop=["\']image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\'](https?://[^"\']+)',
    ]
    def _fetch_og_image(art):
        try:
            r = _req.get(art["url"], headers={"User-Agent": UA}, timeout=4)
            html = r.text
            # 1. og:image / itemprop / twitter:image meta
            for pat in _OG_PATS:
                m = _re.search(pat, html)
                if m: return art["id"], m.group(1)
            # 2. CSS selektorius (saituose su image_selector)
            sel = _SITE_IMG_SEL.get(art["site"], "")
            if sel:
                el = BeautifulSoup(html, "html.parser").select_one(sel)
                if el:
                    src = el.get("data-src") or el.get("src", "")
                    if src and src.startswith("http"): return art["id"], src
        except: pass
        return art["id"], None
    arts_no_img = [a for a in all_articles if not a.get("image") and a.get("url")
                   and a["site"] in _OG_FALLBACK_SITES]
    if arts_no_img:
        print(f"🖼️  og:image fallback: {len(arts_no_img)} straipsnių...")
        with ThreadPoolExecutor(max_workers=6) as ex:
            for aid, img in ex.map(_fetch_og_image, arts_no_img):
                if img:
                    for a in all_articles:
                        if a["id"] == aid: a["image"] = img; break

    # Datos: naujoms be datos – fiksuojame dabar; senoms – atkuriame iš cache
    now_iso = datetime.now(timezone.utc).isoformat()
    dates_changed = False
    first_seen_changed = False
    for art in all_articles:
        aid = art["id"]
        if not art.get("date"):
            if aid in dates_cache:
                art["date"] = dates_cache[aid]      # atkuriame išsaugotą datą
            else:
                art["date"] = now_iso               # pirmas kartas – fiksuojame
                dates_cache[aid] = now_iso
                dates_changed = True
        # first_seen – visada fiksuojame kada pirmą kartą pamatėme (nepriklausomai nuo RSS datos)
        if aid in new_ids and aid not in first_seen:
            first_seen[aid] = now_iso
            first_seen_changed = True

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
        new_urls = [a["url"] for a in all_articles if a["id"] in new_ids and a.get("url")]
        if new_urls:
            kv_sadd("seen_urls", *new_urls)
    if dates_changed:
        kv_set("dates_cache", dates_cache, ex=86400 * 30)
    if first_seen_changed:
        kv_set("first_seen", first_seen, ex=86400 * 30)

    print(f"✅ Išsaugota {len(sorted_arts)} straipsnių, {len(new_ids)} naujų\n")

    # Telegram – tik nauji IR sistemos pirmo pamatymo laikas ne senesnis nei 24h
    if new_ids and seen_ids:
        tg_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        def _is_fresh(art):
            # Naudojame first_seen (kada MES pamatėme), ne RSS datą
            d = first_seen.get(art["id"], "")
            if not d: return True   # pirmas kartas – tikrai naujas
            try: return datetime.fromisoformat(d.replace("Z","+00:00")) >= tg_cutoff
            except: return True
        fresh_arts = [a for a in sorted_arts if a["id"] in new_ids and _is_fresh(a)]
        if fresh_arts:
            lines = ["🏆 <b>Naujos sporto naujienos!</b>\n"]
            for a in fresh_arts[:5]:
                sport_icon = {"futbolas":"⚽","krepšinis":"🏀","ledo ritulys":"🏒","kitas sportas":"🏅"}
                icon = sport_icon.get(a.get("sport",""), "🏆")
                lines.append(f'{icon} <a href="{a["url"]}">{a["title"]}</a>')
            if len(fresh_arts) > 5:
                lines.append(f"\n+{len(fresh_arts)-5} daugiau naujienų")
            lines.append("\n🔗 fules.online")
            tg_send("\n".join(lines))
            print(f"📨 Telegram pranešimas išsiųstas ({len(fresh_arts)} naujų)")


if __name__ == "__main__":
    main()
