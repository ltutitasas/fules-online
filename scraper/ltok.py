#!/usr/bin/env python3
"""Atskiras LTOK (ltok.lt) scraperis – veikia iš GitHub Actions (Vercel IP blokuojamas Cloudflare)"""

import json, os, hashlib, re, requests as _req
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone

KV_URL   = os.environ["KV_REST_API_URL"]
KV_TOKEN = os.environ["KV_REST_API_TOKEN"]
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")
_HDR     = {"Authorization": f"Bearer {KV_TOKEN}"}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_HTTP_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

SITE = {
    "name": "LTOK",
    "sport": "kitas sportas",
    "url": "https://ltok.lt/naujienos",
    "base_url": "https://ltok.lt",
    "link_pattern_re": r"/naujienos/[a-z]",
    "title_selector": "[class*='text-ellipsis']",
    "text_selector": "[class*='prose']",
}

def art_id(url, title):
    return hashlib.md5(f"{url}{title}".encode()).hexdigest()

def kv_get(key):
    r = _req.get(f"{KV_URL}/get/{key}", headers=_HDR, timeout=10)
    result = r.json().get("result")
    return json.loads(result) if result else None

def kv_set(key, value, ex=86400*2):
    _req.post(f"{KV_URL}/pipeline", headers=_HDR, timeout=10,
              json=[["SET", key, json.dumps(value, ensure_ascii=False), "EX", ex]])

def kv_smembers(key):
    r = _req.get(f"{KV_URL}/smembers/{key}", headers=_HDR, timeout=10)
    return set(r.json().get("result") or [])

def kv_sadd(key, *members):
    if members:
        _req.post(f"{KV_URL}/pipeline", headers=_HDR, timeout=10,
                  json=[["SADD", key] + list(members), ["EXPIRE", key, 86400*7]])

def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        r = _req.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=10)
        print(f"  Telegram: {r.json().get('ok')}")
    except Exception as e:
        print(f"  Telegram klaida: {e}")

def fetch_ltok():
    r = _req.get(SITE["url"], headers=_HTTP_HEADERS, timeout=15)
    if r.status_code != 200:
        print(f"  ⚠️  HTTP {r.status_code} – praleidžiama")
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    pat = re.compile(SITE["link_pattern_re"])
    seen_urls_local = set()
    arts = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not pat.search(href): continue
        if "#" in href: continue
        url = href if href.startswith("http") else SITE["base_url"] + href
        if url in seen_urls_local: continue
        tel = a.select_one(SITE["title_selector"])
        title = tel.get_text(strip=True) if tel else ""
        if not title or len(title) < 5: continue
        img = a.find("img")
        image = None
        if img:
            src = img.get("src") or img.get("data-src", "")
            image = (SITE["base_url"] + src if src.startswith("/") else src) or None
        seen_urls_local.add(url)
        arts.append({"site": SITE["name"], "sport": SITE["sport"],
                     "title": title, "url": url, "date": "", "image": image,
                     "text": "", "source": "HTTP", "id": art_id(url, title)})
        if len(arts) >= 10: break
    return arts

def main():
    print(f"🏅 LTOK scraper – {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")

    seen_ids  = kv_smembers("seen_ids")
    seen_urls = kv_smembers("seen_urls")
    first_seen = kv_get("first_seen") or {}

    arts = fetch_ltok()
    print(f"  Rasta: {len(arts)} straipsnių")

    new_arts = [a for a in arts
                if a["id"] not in seen_ids and a.get("url","") not in seen_urls]
    print(f"  Nauji: {len(new_arts)}")

    # Atnaujiname KV articles (pridedame prie esamo sąrašo arba tiesiog atnaujinome TTL)
    existing = kv_get("articles") or []
    merged = new_arts + [a for a in existing if a["id"] not in {x["id"] for x in new_arts}]
    kv_set("articles", merged[:300], ex=86400*2)  # visada rašome – TTL atsinaujina

    if not new_arts:
        print("  Nieko naujo (articles TTL atnaujintas).")
        return

    # Išsaugome first_seen
    now_iso = datetime.now(timezone.utc).isoformat()
    changed = False
    for a in new_arts:
        if a["id"] not in first_seen:
            first_seen[a["id"]] = now_iso
            changed = True
    if changed:
        kv_set("first_seen", first_seen, ex=86400*30)

    # Žymime kaip matytus
    kv_sadd("seen_ids",  *[a["id"]  for a in new_arts])
    kv_sadd("seen_urls", *[a["url"] for a in new_arts if a.get("url")])

    # Telegram
    tg_cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    fresh = [a for a in new_arts
             if first_seen.get(a["id"],"") == now_iso or
             (first_seen.get(a["id"],"") and
              datetime.fromisoformat(first_seen[a["id"]].replace("Z","+00:00")) >= tg_cutoff)]
    if fresh:
        lines = ["🏆 <b>Naujos sporto naujienos!</b>\n"]
        for a in fresh[:5]:
            lines.append(f'🏅 <a href="{a["url"]}">{a["title"]}</a>')
        lines.append("\n🔗 fules-online.vercel.app")
        tg_send("\n".join(lines))

    print("  ✅ Atnaujinta.")

if __name__ == "__main__":
    main()
