#!/usr/bin/env python3
"""HTTP-only scraper – lkl.lt, toplyga.lt, zalgiris.lt ir kt.
Paleidžiamas per GitHub Actions (workflow_dispatch) kas 2 min via cron-job.org.
Naudoja merge strategiją – neperrašo RSS straipsnių KV."""

import json, os, hashlib, time, requests as _req
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

KV_URL   = os.environ["KV_REST_API_URL"]
KV_TOKEN = os.environ["KV_REST_API_TOKEN"]
_HDR     = {"Authorization": f"Bearer {KV_TOKEN}"}
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
MAX = 10

def _kv_retry(fn, retries=3, delay=4):
    """KV užklausa su retry – tinklo laikini trukdžiai GitHub Actions"""
    last_err = None
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if i < retries - 1:
                print(f"  ⚠️  KV retry {i+1}/{retries-1}: {e}")
                time.sleep(delay)
    raise last_err

def kv_get(key):
    def _do():
        r = _req.get(f"{KV_URL}/get/{key}", headers=_HDR, timeout=10)
        result = r.json().get("result")
        return json.loads(result) if result else None
    return _kv_retry(_do)

def kv_set(key, value, ex=86400*2):
    def _do():
        _req.post(f"{KV_URL}/pipeline", headers=_HDR, timeout=10,
                  json=[["SET", key, json.dumps(value, ensure_ascii=False), "EX", ex]])
    _kv_retry(_do)

def kv_smembers(key):
    def _do():
        r = _req.get(f"{KV_URL}/smembers/{key}", headers=_HDR, timeout=10)
        return set(r.json().get("result") or [])
    return _kv_retry(_do)

def kv_sadd(key, *members):
    if not members: return
    def _do():
        _req.post(f"{KV_URL}/pipeline", headers=_HDR, timeout=10,
                  json=[["SADD", key] + list(members), ["EXPIRE", key, 86400*30]])
    _kv_retry(_do)

def tg_send(text):
    if not TG_TOKEN or not TG_CHAT: return
    try:
        _req.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=10)
    except: pass

def art_id(url, title):
    return hashlib.md5(f"{url}{title}".encode()).hexdigest()

# ── Tik HTTP saitai ────────────────────────────────────────────────
HTTP_SITES = [
    # ⚽ FUTBOLAS
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
    # 🏀 KREPŠINIS
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
    # 🏒 KITI
    {"name":"Hockey Lietuva", "sport":"ledo ritulys", "method":"http",
     "url":"https://www.hockey.lt/index.php/naujienos/17",
     "link_pattern_re": r"/index\.php/naujienos/[^/]+/\d+",
     "base_url":"https://www.hockey.lt",
     "og_image_fallback": True, "image_selector":".news_item_img img",
     "text_selector":".short_text"},
]

_HTTP_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "lt-LT,lt;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

def fetch_http(site):
    import re as _re
    try:
        r = _req.get(site["url"], headers=_HTTP_HEADERS, timeout=12)
        if r.status_code != 200:
            print(f"  ⚠️  {site['name']}: HTTP {r.status_code}")
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        base = site.get("base_url", "")
        articles = []

        if "link_pattern" in site or "link_patterns" in site or "link_pattern_re" in site:
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
                title_sel = site.get("title_selector", "")
                if title_sel:
                    tel = a.select_one(title_sel)
                    title = tel.get_text(strip=True) if tel else ""
                else:
                    title = a.get("title", "").strip() or a.get_text(strip=True)
                title = _re.sub(r'\s*\(\d+\)\s*$', '', title).strip()
                if not title or len(title) < 5: continue
                if url in seen: continue
                seen.add(url)
                img = a.find("img")
                image = None
                if img:
                    src = img.get("src") or img.get("data-src", "")
                    image = (base + src if src.startswith("/") else src) or None
                articles.append({
                    "site": site["name"], "sport": site.get("sport", ""),
                    "title": title, "url": url, "date": "", "image": image,
                    "text": "", "source": "HTTP", "id": art_id(url, title),
                    "text_selector": site.get("text_selector", ""),
                })
                if len(articles) >= MAX: break

        elif "selectors" in site:
            sel = site["selectors"]
            for container in soup.select(sel["articles"]):
                title_el = container.select_one(sel["title"])
                title = title_el.get_text(strip=True) if title_el else ""
                link_el = container.select_one(sel["link"])
                href = link_el.get("href", "") if link_el else ""
                url = href if href.startswith("http") else (base + href if href else "")
                img_el = container.select_one(sel.get("image", "img"))
                image = None
                if img_el:
                    src = img_el.get("src") or img_el.get("data-src", "")
                    image = (base + src if src.startswith("/") else src) or None
                if title and url:
                    articles.append({
                        "site": site["name"], "sport": site.get("sport", ""),
                        "title": title, "url": url, "date": "", "image": image,
                        "text": "", "source": "HTTP", "id": art_id(url, title),
                    })
                if len(articles) >= MAX: break

        return articles
    except Exception as e:
        print(f"  ❌ {site['name']}: {e}")
        return []

def fetch_og_image(art, img_sel):
    _OG_PATS = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](https?://[^"\']+)',
        r'<meta[^>]+content=["\'](https?://[^"\']+)[^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\'](https?://[^"\']+)',
    ]
    import re as _re
    try:
        r = _req.get(art["url"], headers={"User-Agent": UA}, timeout=5)
        html = r.text
        for pat in _OG_PATS:
            m = _re.search(pat, html)
            if m: return art["id"], m.group(1)
        if img_sel:
            el = BeautifulSoup(html, "html.parser").select_one(img_sel)
            if el:
                src = el.get("data-src") or el.get("src", "")
                if src and src.startswith("http"): return art["id"], src
    except: pass
    return art["id"], None

def main():
    print(f"\n{'═'*50}")
    print(f"🌐 HTTP SCRAPER  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*50}\n")

    seen_ids  = kv_smembers("seen_ids")
    seen_urls = kv_smembers("seen_urls")
    dates_cache = kv_get("dates_cache") or {}
    first_seen  = kv_get("first_seen") or {}

    all_arts = []
    print(f"🌐 HTTP ({len(HTTP_SITES)} saitų)...")
    with ThreadPoolExecutor(max_workers=len(HTTP_SITES)) as ex:
        futures = {ex.submit(fetch_http, s): s for s in HTTP_SITES}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                arts = fut.result()
                all_arts.extend(arts)
                print(f"  ✅ {s['name']}: {len(arts)}")
            except Exception as e:
                print(f"  ❌ {s['name']}: {e}")

    new_ids = {a["id"] for a in all_arts
               if a["id"] not in seen_ids and a.get("url","") not in seen_urls}
    print(f"\n📊 Iš viso: {len(all_arts)} | Naujų: {len(new_ids)}")

    # og:image fallback
    _OG_FALLBACK = {s["name"] for s in HTTP_SITES if s.get("og_image_fallback")}
    _IMG_SEL     = {s["name"]: s.get("image_selector","") for s in HTTP_SITES}
    arts_no_img  = [a for a in all_arts if not a.get("image") and a.get("url") and a["site"] in _OG_FALLBACK]
    if arts_no_img:
        print(f"🖼️  og:image fallback: {len(arts_no_img)} straipsnių...")
        with ThreadPoolExecutor(max_workers=6) as ex:
            for aid, img in ex.map(lambda a: fetch_og_image(a, _IMG_SEL.get(a["site"],"")), arts_no_img):
                if img:
                    for a in all_arts:
                        if a["id"] == aid: a["image"] = img; break

    # Datos
    now_iso = datetime.now(timezone.utc).isoformat()
    dates_changed = first_seen_changed = False
    for art in all_arts:
        aid = art["id"]
        if not art.get("date"):
            art["date"] = dates_cache.get(aid, now_iso)
            if aid not in dates_cache:
                dates_cache[aid] = now_iso
                dates_changed = True
        if aid in new_ids and aid not in first_seen:
            first_seen[aid] = now_iso
            first_seen_changed = True

    if not new_ids:
        print("  Nieko naujo.")
        # Vis tiek atnaujinti TTL
        existing = kv_get("articles") or []
        if existing:
            kv_set("articles", existing, ex=86400*2)
        return

    # Merge su esamais KV straipsniais (ne overwrite)
    print("\n💾 Merge į Vercel KV...")
    existing  = kv_get("articles") or []
    new_arts  = [a for a in all_arts if a["id"] in new_ids]
    merged    = new_arts + [a for a in existing if a["id"] not in new_ids]
    # Persortavimas pagal datą – seni straipsniai neatsiduria viršuje
    def _sort_key(art):
        d = art.get("date","")
        if not d: return datetime.min.replace(tzinfo=None)
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(d).replace(tzinfo=None)
        except:
            try: return datetime.fromisoformat(d.replace("Z","")).replace(tzinfo=None)
            except: return datetime.min.replace(tzinfo=None)
    merged.sort(key=_sort_key, reverse=True)
    kv_set("articles", merged[:300], ex=86400*2)

    kv_sadd("seen_ids",  *[a["id"]  for a in new_arts])
    kv_sadd("seen_urls", *[a["url"] for a in new_arts if a.get("url")])
    if dates_changed:  kv_set("dates_cache", dates_cache, ex=86400*30)
    if first_seen_changed: kv_set("first_seen", first_seen, ex=86400*30)
    print(f"✅ Išsaugota. Naujų: {len(new_ids)}")

    # Telegram
    tg_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    def is_fresh(art):
        d = first_seen.get(art["id"], "")
        if not d: return True
        try: return datetime.fromisoformat(d.replace("Z","+00:00")) >= tg_cutoff
        except: return True

    fresh = [a for a in new_arts if is_fresh(a)]
    if fresh and seen_ids:  # seen_ids tuščias = pirmas paleidimas, nesiunčiame
        sport_icon = {"futbolas":"⚽","krepšinis":"🏀","ledo ritulys":"🏒","kitas sportas":"🏅"}
        lines = ["🏆 <b>Naujos sporto naujienos!</b>\n"]
        for a in fresh[:5]:
            lines.append(f'{sport_icon.get(a.get("sport",""),"🏆")} <a href="{a["url"]}">{a["title"]}</a>')
        if len(fresh) > 5: lines.append(f"\n+{len(fresh)-5} daugiau")
        lines.append("\n🔗 fules-online.vercel.app")
        tg_send("\n".join(lines))
        print(f"📨 Telegram: {len(fresh)} naujos")

if __name__ == "__main__":
    main()
