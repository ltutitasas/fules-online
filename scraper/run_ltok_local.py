#!/usr/bin/env python3
"""LTOK lokalus scraperis – leidžiamas iš NAMŲ tinklo (Mac), nes ltok.lt
Cloudflare blokuoja Vercel ir GitHub Actions data centrų IP (403 „Just a moment",
patikrinta 2026-07-22 – visiems keliams, įskaitant sitemap ir _payload.json).

Veikimas:
  1. https://ltok.lt/naujienos SSR sąrašas → naujausi straipsnių URL
  2. Naujiems (seen_urls dedup) siunčiamas {url}/_payload.json (Nuxt „devalue"
     formatas) → title, date_created, image UUID, ProseMirror turinys
  3. Turinys → HTML → KV html:{id} (+html_ids indeksas) – publikavimas veikia
     iš KV, be URL fallback (Vercel ltok.lt nepasiekia!)
  4. Nuotraukos baitai → KV img:{md5(image_url)} – upload_photo ima iš KV
  5. Merge į articles (ta pati strategija kaip run_http) + Telegram

Paleidimas:
  KV_REST_API_URL=... KV_REST_API_TOKEN=... python3 scraper/run_ltok_local.py
  --dry-run – tik fetch+parse, be KV (testavimui, env nereikia)
Automatizavimas per launchd – žr. scraper/lt.fules.ltok.plist.example
"""
import base64, hashlib, json, os, re, sys, time
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "api"))

DRY = "--dry-run" in sys.argv
if DRY:
    os.environ.setdefault("KV_REST_API_URL", "http://dry-run.invalid")
    os.environ.setdefault("KV_REST_API_TOKEN", "dry")

import requests as _req
from run_http import kv_pipeline, kv_json, tg_send, art_id   # noqa: E402
from _sites_config import SITES, slim_art, norm_url          # noqa: E402

SITE = next(s for s in SITES if s["name"] == "LTOK")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
H = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate",
     "Accept-Language": "lt-LT,lt;q=0.9,en;q=0.8"}
MAX_LIST = 10   # kiek sąrašo URL tikrinam
MAX_NEW  = 4    # kiek naujų straipsnių apdorojam per runą (payload+img fetch)


# ── Nuxt „devalue" payload parsinimas ─────────────────────────────
def _resolve(data, x, depth=0):
    """devalue: objektų reikšmės – indeksai į bendrą masyvą. Išskleidžiam."""
    if depth > 40 or isinstance(x, bool):
        return x
    if isinstance(x, dict):
        return {k: _resolve(data, i, depth + 1) for k, i in x.items()}
    if isinstance(x, list):
        return [_resolve(data, i, depth + 1) for i in x]
    if isinstance(x, int) and 0 <= x < len(data):
        v = data[x]
        if isinstance(v, (dict, list)):
            return _resolve(data, v, depth + 1)
        return v
    return x


def _find_article(payload):
    """Straipsnio dict payload'e: turi title+date_created+blocks raktus."""
    for v in payload:
        if isinstance(v, dict) and {"title", "date_created", "blocks"} <= set(v.keys()):
            return _resolve(payload, v)
    return None


# ── ProseMirror doc → HTML (sportas.lt draugiškas: p/h3/li + strong/em) ──
def _pm_text(node):
    t = node.get("text", "") or ""
    for m in node.get("marks") or []:
        mt = m.get("type", "") if isinstance(m, dict) else ""
        if mt == "bold":
            t = f"<strong>{t}</strong>"
        elif mt == "italic":
            t = f"<em>{t}</em>"
    return t


def _pm_inline(node):
    out = []
    for c in node.get("content") or []:
        if not isinstance(c, dict):
            continue
        out.append(_pm_text(c) if c.get("type") == "text" else _pm_inline(c))
    return "".join(out)


def _pm_html(doc):
    parts = []
    for n in doc.get("content") or []:
        t, txt = n.get("type", ""), ""
        if t in ("paragraph", "heading", "blockquote"):
            txt = _pm_inline(n).strip()
            if not txt:
                continue
            if t == "heading":
                parts.append(f"<h3>{txt}</h3>")
            elif t == "blockquote":
                parts.append(f"<p><em>{txt}</em></p>")
            else:
                parts.append(f"<p>{txt}</p>")
        elif t in ("bulletList", "orderedList"):
            for li in n.get("content") or []:
                litxt = _pm_inline(li).strip()
                if litxt:
                    parts.append(f"<li>{litxt}</li>")
    return "".join(parts)


def _find_docs(x, out):
    """blocks gali būti vienas doc arba įdėtas giliau – surandam visus."""
    if isinstance(x, dict):
        if x.get("type") == "doc" and "content" in x:
            out.append(x)
            return
        for v in x.values():
            _find_docs(v, out)
    elif isinstance(x, list):
        for v in x:
            _find_docs(v, out)


# ── LTOK fetch ────────────────────────────────────────────────────
def fetch_listing():
    r = _req.get(SITE["url"], headers=H, timeout=20)
    if r.status_code != 200 or "Just a moment" in r.text[:3000]:
        raise RuntimeError(f"ltok.lt sąrašas neprieinamas (HTTP {r.status_code}) – "
                           "Cloudflare? Šis scraperis veikia tik iš namų IP")
    links = list(dict.fromkeys(re.findall(r'href="(/naujienos/[a-z0-9\-]+)"', r.text)))
    return [SITE["base_url"] + l for l in links[:MAX_LIST]]


def fetch_article(url):
    """Straipsnio duomenys iš Nuxt _payload.json. Grąžina dict arba None."""
    r = _req.get(url.rstrip("/") + "/_payload.json", headers=H, timeout=20)
    if r.status_code != 200:
        return None
    art = _find_article(json.loads(r.text))
    if not art or not art.get("title"):
        return None
    docs = []
    _find_docs(art.get("blocks"), docs)
    html = "".join(_pm_html(d) for d in docs)
    img_id = (art.get("image") or {}).get("id", "")
    image = f"{SITE['base_url']}/api/assets/{img_id}" if img_id else None
    return {"site": SITE["name"], "sport": SITE.get("sport", ""),
            "title": str(art["title"]).strip(), "url": url,
            "date": str(art.get("date_created", "")), "image": image,
            "text": "", "html_content": html, "source": "LTOK-local",
            "id": art_id(url, str(art["title"]).strip())}


def fetch_image_b64(image_url):
    """Nuotraukos baitai KV'ui – Vercel ltok.lt nepasiekia, ims iš KV."""
    r = _req.get(image_url, headers=H, timeout=25)
    if r.status_code != 200 or not r.content:
        return None
    ct = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    return {"b64": base64.b64encode(r.content).decode(), "ct": ct}


# ── Pagrindinis ciklas (run_http merge strategijos kopija) ───────
def main():
    print(f"\n🏅 LTOK LOCAL  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    now_iso = datetime.now(timezone.utc).isoformat()
    urls = fetch_listing()
    print(f"  sąraše: {len(urls)} straipsnių")

    if DRY:
        for u in urls[:2]:
            a = fetch_article(u)
            if a:
                print(f"\n  ✅ {a['title']}\n     data: {a['date']} | img: {a['image']}")
                print(f"     html ({len(a['html_content'])} simb.): {a['html_content'][:200]}...")
            else:
                print(f"  ❌ nepavyko: {u}")
        print("\n(dry-run – KV neliestas)")
        return

    # VISI skaitymai vienu pipeline (MGET = 1 komanda; žr. KV taupymo režimą)
    chk_cmds = [["MGET", "dates_cache", "first_seen", "scrape_status",
                 "articles", "recent_ids", "articles_hash", "html_ids"],
                ["SCARD", "seen_ids"],
                ["SMISMEMBER", "seen_urls"] + urls]
    chk = kv_pipeline(chk_cmds)
    mg = chk[0] or [None] * 7
    dates_cache   = kv_json(mg[0], {})
    first_seen    = kv_json(mg[1], {})
    scrape_status = kv_json(mg[2], {})
    existing      = kv_json(mg[3], [])
    raw_recent    = kv_json(mg[4], {})
    prev_hash     = mg[5]
    html_ids      = kv_json(mg[6], {})
    seen_count    = int(chk[1] or 0)
    url_seen      = chk[2] or [0] * len(urls)
    if seen_count == 0 and len(existing) > 10:
        print("⚠️  Nenuoseklus KV skaitymas – praleidžiam runą")
        return

    new_urls = [u for u, s in zip(urls, url_seen) if not int(s or 0)][:MAX_NEW]
    print(f"  naujų: {len(new_urls)}")
    if not new_urls:
        return   # 3 komandos ir baigta – jokių rašymų

    new_arts = []
    for u in new_urls:
        a = fetch_article(u)
        if a:
            new_arts.append(a)
            print(f"  ✅ {a['title'][:60]}")
        time.sleep(1)   # mandagumo pauzė
    if not new_arts:
        return

    # Datos + first_seen
    for a in new_arts:
        if not a.get("date"):
            a["date"] = dates_cache.get(a["id"], now_iso)
        dates_cache.setdefault(a["id"], a["date"])
        first_seen.setdefault(a["id"], now_iso)

    # Merge (LTOK straipsniai nauji – seni LTOK įrašai lieka per existing)
    fetched_ids  = {a["id"] for a in new_arts}
    fetched_urls = {norm_url(a["url"]) for a in new_arts}
    merged = new_arts + [a for a in existing
                         if a["id"] not in fetched_ids
                         and norm_url(a.get("url", "")) not in fetched_urls]
    merged.sort(key=_sort_key, reverse=True)
    slim = [slim_art(a) for a in merged[:300]]
    slim_json = json.dumps(slim, ensure_ascii=False)
    new_hash = hashlib.md5(slim_json.encode()).hexdigest()

    # recent_ids (kaupiamas dict, žr. CLAUDE.md)
    if isinstance(raw_recent, list):
        raw_recent = {i: now_iso for i in raw_recent}
    cutoff3 = datetime.now(timezone.utc) - timedelta(hours=3)
    def _fresh3(iso):
        try:
            return datetime.fromisoformat(str(iso).replace("Z", "+00:00")) >= cutoff3
        except Exception:
            return False
    rec_map = {i: t for i, t in raw_recent.items() if _fresh3(t)}
    for a in new_arts:
        rec_map.setdefault(a["id"], now_iso)

    scrape_status["LTOK"] = {"ok": now_iso, "n": len(urls)}

    write_cmds = [
        ["SET", "recent_ids", json.dumps(rec_map), "EX", 3600 * 3],
        ["SET", "scrape_status", json.dumps(scrape_status, ensure_ascii=False), "EX", 86400 * 7],
        ["SADD", "seen_ids"] + [a["id"] for a in new_arts],
        ["EXPIRE", "seen_ids", 86400 * 30],
        ["SADD", "seen_urls"] + [a["url"] for a in new_arts],
        ["EXPIRE", "seen_urls", 86400 * 30],
        ["SET", "dates_cache", json.dumps(dates_cache), "EX", 86400 * 30],
        ["SET", "first_seen", json.dumps(first_seen), "EX", 86400 * 30],
    ]
    if new_hash != prev_hash and not (not existing and seen_count > 0):
        write_cmds += [
            ["SET", "articles", slim_json, "EX", 86400 * 2],
            ["SET", "articles_hash", new_hash, "EX", 86400 * 2],
            ["SET", "articles_meta", json.dumps({"count": len(slim),
                                                 "first": slim[0]["id"] if slim else "",
                                                 "ts": now_iso}), "EX", 86400 * 2],
        ]
    # html:{id} publikavimui + html_ids indeksas
    for a in new_arts:
        if a.get("html_content"):
            write_cmds.append(["SET", f"html:{a['id']}",
                               json.dumps(a["html_content"], ensure_ascii=False), "EX", 86400 * 2])
            html_ids[a["id"]] = now_iso
    write_cmds.append(["SET", "html_ids", json.dumps(html_ids), "EX", 86400 * 2])
    kv_pipeline(write_cmds)

    # Nuotraukų baitai – atskirai (dideli; po vieną SET per pipeline)
    for a in new_arts:
        if a.get("image"):
            blob = fetch_image_b64(a["image"])
            if blob:
                key = "img:" + hashlib.md5(a["image"].encode()).hexdigest()
                kv_pipeline([["SET", key, json.dumps(blob), "EX", 86400 * 2]])
                print(f"  🖼️  {key} ({len(blob['b64']) // 1024} KB b64)")

    print(f"✅ Išsaugota: {len(new_arts)} naujų")

    # Telegram (ta pati pirmo paleidimo apsauga). Siunčiam tik šviežius pagal
    # straipsnio SAVĄ datą – kad seen_ids/first_seen bėda nepaverstų senų
    # straipsnių „naujienomis" (žr. fchegelmann.com atvejį api/index.py)
    pub_cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).replace(tzinfo=None)
    tg_arts = [a for a in new_arts
               if _sort_key(a) == datetime.min or _sort_key(a) >= pub_cutoff]
    if seen_count and tg_arts:
        lines = []
        for a in tg_arts[:5]:
            lines.append(f'🏅 <a href="{a["url"]}">{a["title"]}</a>')
        lines.append("\n🔗 fules-online2.vercel.app")
        tg_send("\n".join(lines))


def _sort_key(art):
    d = str(art.get("date", ""))
    if not d:
        return datetime.min
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(d).replace(tzinfo=None)
    except Exception:
        try:
            return datetime.fromisoformat(d.replace("Z", "")).replace(tzinfo=None)
        except Exception:
            return datetime.min


if __name__ == "__main__":
    main()
