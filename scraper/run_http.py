#!/usr/bin/env python3
"""HTTP-only scraper – lkl.lt, toplyga.lt, zalgiris.lt ir kt.
Paleidžiamas per GitHub Actions (workflow_dispatch) via cron-job.org.
Naudoja merge strategiją – neperrašo RSS straipsnių KV.
Saitų sąrašas – bendras sites_config.py (repo šaknyje)."""

import json, os, sys, hashlib, time, re as _re_mod, requests as _req
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))
from _sites_config import HTTP_SITES, slim_art, norm_url, fix_img

try:
    import lxml  # noqa: F401
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"

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

def kv_pipeline(cmds, timeout=15):
    """Daug Redis komandų vienu HTTP request'u."""
    if not cmds: return []
    def _do():
        r = _req.post(f"{KV_URL}/pipeline", headers=_HDR, timeout=timeout, json=cmds)
        r.raise_for_status()   # 4xx/5xx → retry, paskui exception
        data = r.json()
        if not isinstance(data, list):   # Upstash klaida = dict {"error":...}
            raise RuntimeError(f"KV pipeline klaida: {str(data)[:200]}")
        # HTTP 200 net kai komanda atmesta (pvz. "max requests limit exceeded") –
        # klaida slypi item viduje. Keliam exception (retry, paskui matomas fail).
        for item in data:
            if isinstance(item, dict) and item.get("error"):
                raise RuntimeError(f"KV komandos klaida: {str(item['error'])[:200]}")
        return [item.get("result") if isinstance(item, dict) else None for item in data]
    return _kv_retry(_do)

def kv_json(raw, default):
    if not raw: return default
    try: return json.loads(raw)
    except Exception: return default

def tg_send(text):
    if not TG_TOKEN or not TG_CHAT: return
    try:
        _req.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=10)
    except: pass

def art_id(url, title):
    return hashlib.md5(f"{url}{title}".encode()).hexdigest()

def fetch_text_sig(url):
    """Grąžina (parašas, pastraipos). Parašas – md5 8 simb. iš pastraipų >100 simb.
    Pasikeitus turiniui (be teksto → anonsas → ataskaita) parašas keičiasi → naujas
    id → renotify (naujas pranešimas). Tuščia/klaida → ('', []) (id lieka be žymės).
    `pastraipos` grąžinamos, kad NEPAPILDOMAI parsisiunčiant galėtume išsaugoti
    versijų istoriją (tl_hist) – diff'as „kas pasikeitė". Meniu/nav (<35 simb.) į
    parašą nepatenka; gyvas mačo komentaras lieka JS widget'uose (ne <p>/<li>), tad
    parašas gyvo mačo metu stabilus (= anonsas)."""
    try:
        r = _req.get(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}, timeout=8)
        soup = BeautifulSoup(r.text, PARSER)
        for tag in soup(["script","style","nav","footer","header","aside",
                         "iframe","noscript","form","button"]):
            tag.decompose()
        blocks = []
        for el in (soup.body or soup).find_all(["p","h2","h3","h4","li"]):
            txt = " ".join(el.get_text(" ", strip=True).split())
            if len(txt) > 100:
                blocks.append(txt)
        if not blocks:
            return "", []
        return hashlib.md5("\n".join(blocks).encode()).hexdigest()[:8], blocks
    except Exception:
        return "", []

def _sentences(blocks):
    """Pastraipas suskaido į sakinius (diff'ui). Trumpi (<12 simb.) atmetami."""
    txt = " ".join(" ".join(b.split()) for b in blocks)
    parts = _re_mod.split(r'(?<=[.!?…])\s+', txt)
    return [s.strip() for s in parts if len(s.strip()) >= 12]

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
        soup = BeautifulSoup(r.text, PARSER)
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
                    image = fix_img(site, (base + src if src.startswith("/") else src) or None)
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
                # santykiniam href be / pridedam / (toplyga.lt "rungtynes-gyvai/..." atvejis)
                url = href if href.startswith("http") else (base + (href if href.startswith("/") else "/" + href) if href else "")
                img_el = container.select_one(sel.get("image", "img"))
                image = None
                if img_el:
                    src = img_el.get("src") or img_el.get("data-src", "")
                    image = fix_img(site, (base + src if src.startswith("/") else src) or None)
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
            el = BeautifulSoup(html, PARSER).select_one(img_sel)
            if el:
                src = el.get("data-src") or el.get("src", "")
                if src and src.startswith("http"): return art["id"], src
    except: pass
    return art["id"], None

def _sort_key(art):
    d = art.get("date", "")
    if not d: return datetime.min
    try: return parsedate_to_datetime(d).replace(tzinfo=None)
    except:
        try: return datetime.fromisoformat(d.replace("Z", "")).replace(tzinfo=None)
        except: return datetime.min

def main():
    print(f"\n{'═'*50}")
    print(f"🌐 HTTP SCRAPER  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*50}\n")

    now_iso = datetime.now(timezone.utc).isoformat()

    all_arts = []
    _status_updates = {}   # merge'inam į scrape_status po KV skaitymo (žr. žemiau)
    print(f"🌐 HTTP ({len(HTTP_SITES)} saitų)...")
    with ThreadPoolExecutor(max_workers=len(HTTP_SITES)) as ex:
        futures = {ex.submit(fetch_http, s): s for s in HTTP_SITES}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                arts = fut.result()
                all_arts.extend(arts)
                if arts:
                    _status_updates[s["name"]] = {"ok": now_iso, "n": len(arts)}
                print(f"  ✅ {s['name']}: {len(arts)}")
            except Exception as e:
                print(f"  ❌ {s['name']}: {e}")

    # renotify_on_text (Top Lyga): į id įtraukiam straipsnio TURINIO parašą. Turiniui
    # pasikeitus (be teksto → anonsas → ataskaita) id keičiasi → renotify (naujas
    # pranešimas tai pačiai naujienai). Toplyga tekstas keičiasi retai, o gyvas
    # komentaras parašo neveikia, tad spam'o nebus. Be turinio id lieka kaip dabar.
    _RENOTEXT = {s["name"] for s in HTTP_SITES if s.get("renotify_on_text")}
    txt_arts = [a for a in all_arts if a["site"] in _RENOTEXT and a.get("url")]
    if txt_arts:
        print(f"📝 Teksto parašas ({len(txt_arts)} str.)...")
        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(lambda a: fetch_text_sig(a["url"]), txt_arts))
        n = 0
        for a, (sig, blocks) in zip(txt_arts, results):
            if sig:
                a["id"] = art_id(a["url"], a["title"] + "|" + sig)
                a["_blocks"] = blocks   # laikinas – versijų istorijai (tl_hist), neslim'inamas
                n += 1
        print(f"  📝 su turiniu: {n}/{len(txt_arts)}")

    # VISI KV skaitymai vienu pipeline: MGET Upstash apskaitoje = 1 KOMANDA
    # nepriklausomai nuo raktų kiekio (6 GET → 1 MGET; taupom 500K/mėn kvotą).
    # SMISMEMBER tik kandidatams (vietoj viso seen_ids/seen_urls seto siuntimosi)
    ids  = [a["id"] for a in all_arts]
    urls = [a.get("url", "") for a in all_arts]
    chk_cmds = [["MGET", "dates_cache", "first_seen", "scrape_status",
                 "articles", "recent_ids", "articles_hash"],
                ["SCARD", "seen_ids"]]
    if ids:
        chk_cmds.append(["SMISMEMBER", "seen_ids"] + ids)
        chk_cmds.append(["SMISMEMBER", "seen_urls"] + urls)
    chk = kv_pipeline(chk_cmds)
    mg = chk[0] or [None] * 6
    dates_cache   = kv_json(mg[0], {})
    first_seen    = kv_json(mg[1], {})
    scrape_status = kv_json(mg[2], {})
    existing      = kv_json(mg[3], [])
    raw_recent    = kv_json(mg[4], {})
    prev_articles_hash = mg[5]
    seen_count = int(chk[1] or 0)
    # SAUGIKLIS nuo nenuoseklaus KV skaitymo (tuščia/bloga replika): jei seen_ids
    # "tuščias", o articles pilnas – skaitymas pataikė į blogą backend'ą; praleidžiam
    # runą be rašymo/Telegram (kitaip viskas atrodytų nauja → spam).
    if seen_count == 0 and len(existing) > 10:
        print("⚠️  Nenuoseklus KV skaitymas (seen_ids=0, bet articles pilnas) – praleidžiam runą")
        return
    id_seen  = (chk[2] if len(chk) > 2 else None) or [0] * len(ids)
    url_seen = (chk[3] if len(chk) > 3 else None) or [0] * len(ids)
    # scrape_status atnaujinam TAUPIAI: "ok" keičiam tik jei senesnis nei 10 min
    # arba pasikeitė n – kitaip raktas būtų rašomas kas runą vien dėl timestamp'o
    _ss_changed = False
    _ss_fresh_cut = datetime.now(timezone.utc) - timedelta(minutes=10)
    for _sn, _st in _status_updates.items():
        _prev = scrape_status.get(_sn) or {}
        try:
            _prev_fresh = (datetime.fromisoformat(str(_prev.get("ok", "")).replace("Z", "+00:00"))
                           >= _ss_fresh_cut) and _prev.get("n") == _st["n"]
        except Exception:
            _prev_fresh = False
        if not _prev_fresh:
            scrape_status[_sn] = _st
            _ss_changed = True
    # Saitams su renotify_on_rename pakanka naujo id – pervadinimas = nauja naujiena
    _RENAME_OK = {s["name"] for s in HTTP_SITES if s.get("renotify_on_rename")}
    new_ids = {a["id"] for a, s1, s2 in zip(all_arts, id_seen, url_seen)
               if not int(s1 or 0) and (not int(s2 or 0) or a["site"] in _RENAME_OK)}
    print(f"\n📊 Iš viso: {len(all_arts)} | Naujų: {len(new_ids)}")

    # og:image fallback – tik straipsniams, kurių paveikslas dar neišspręstas KV
    _OG_FALLBACK = {s["name"] for s in HTTP_SITES if s.get("og_image_fallback")}
    _IMG_SEL     = {s["name"]: s.get("image_selector", "") for s in HTTP_SITES}
    existing_img = {a["id"]: a.get("image") for a in existing if a.get("image")}
    for a in all_arts:
        if not a.get("image") and a["id"] in existing_img:
            a["image"] = existing_img[a["id"]]
    arts_no_img = [a for a in all_arts if not a.get("image") and a.get("url") and a["site"] in _OG_FALLBACK]
    if arts_no_img:
        print(f"🖼️  og:image fallback: {len(arts_no_img)} straipsnių...")
        with ThreadPoolExecutor(max_workers=6) as ex:
            for aid, img in ex.map(lambda a: fetch_og_image(a, _IMG_SEL.get(a["site"], "")), arts_no_img):
                if img:
                    for a in all_arts:
                        if a["id"] == aid: a["image"] = img; break

    # Datos
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

    # Merge VISŲ atneštų straipsnių (ne tik naujų) – jei lygiagretus RSS runas
    # netyčia perrašytų mūsų įrašus, kitas runas juos atkurtų
    new_arts = [a for a in all_arts if a["id"] in new_ids]
    fetched_ids  = {a["id"] for a in all_arts}
    # Dedup ir pagal URL – pervadintas straipsnis pakeičia seną įrašą (ne dublikatas).
    # norm_url: /rungtynes-gyvai/ → /rungtynes/ (toplyga.lt keičia URL po mačo)
    fetched_urls = {norm_url(a["url"]) for a in all_arts if a.get("url")}
    merged = all_arts + [a for a in existing
                         if a["id"] not in fetched_ids
                         and norm_url(a.get("url", "")) not in fetched_urls]
    merged.sort(key=_sort_key, reverse=True)
    slim = [slim_art(a) for a in merged[:300]]
    slim_json = json.dumps(slim, ensure_ascii=False)
    new_articles_hash = hashlib.md5(slim_json.encode()).hexdigest()

    # recent_ids – KAUPIAMAS dict {id: iso} (badge + naršyklės notificationai,
    # dabar veikia ir HTTP saitams)
    if isinstance(raw_recent, list):
        raw_recent = {i: now_iso for i in raw_recent}
    cutoff3 = datetime.now(timezone.utc) - timedelta(hours=3)
    def _fresh3(iso):
        try: return datetime.fromisoformat(str(iso).replace("Z", "+00:00")) >= cutoff3
        except: return False
    rec_map = {i: t for i, t in raw_recent.items() if _fresh3(t)}
    for a in new_arts:
        rec_map.setdefault(a["id"], now_iso)

    print("\n💾 Merge į Vercel KV...")
    # Rašom TIK tai, kas realiai pasikeitė – kiekviena komanda kainuoja Upstash
    # 500K/mėn kvotą. Tipinis runas be naujienų: 0 rašymo komandų.
    write_cmds = []
    if rec_map != raw_recent:
        write_cmds.append(["SET", "recent_ids", json.dumps(rec_map), "EX", 3600*3])
    if _ss_changed:
        write_cmds.append(["SET", "scrape_status", json.dumps(scrape_status, ensure_ascii=False), "EX", 86400*7])
    # articles perrašome tik kai skaitymas nuoseklus. Jei existing tuščias, bet
    # seen_ids egzistuoja – skaitymas pataikė į blogą backend'ą, neperrašome archyvo.
    _read_consistent = not (not existing and seen_count > 0)
    # TTL priežiūra ~kas 30-tą runą (TTL 2 d., pratęsinėti kas 2 min beprasmiška)
    import random as _rnd
    _maint = _rnd.random() < 0.033
    if _read_consistent and new_articles_hash != prev_articles_hash:
        # Turinys pasikeitė → perrašom. Jei nepasikeitė – tik retkarčiais TTL pratęsiam.
        write_cmds.append(["SET", "articles", slim_json, "EX", 86400*2])
        write_cmds.append(["SET", "articles_hash", new_articles_hash, "EX", 86400*2])
        write_cmds.append(["SET", "articles_meta", json.dumps({"count": len(slim),
                                             "first": slim[0]["id"] if slim else "",
                                             "ts": now_iso}), "EX", 86400*2])
    elif _read_consistent and _maint:
        write_cmds.append(["EXPIRE", "articles", 86400*2])
        write_cmds.append(["EXPIRE", "articles_hash", 86400*2])
        write_cmds.append(["EXPIRE", "articles_meta", 86400*2])
    if new_ids:
        write_cmds.append(["SADD", "seen_ids"] + [a["id"] for a in new_arts])
        write_cmds.append(["EXPIRE", "seen_ids", 86400*30])
        new_urls = [a["url"] for a in new_arts if a.get("url")]
        if new_urls:
            write_cmds.append(["SADD", "seen_urls"] + new_urls)
            write_cmds.append(["EXPIRE", "seen_urls", 86400*30])
    if dates_changed:
        write_cmds.append(["SET", "dates_cache", json.dumps(dates_cache), "EX", 86400*30])
    if first_seen_changed:
        write_cmds.append(["SET", "first_seen", json.dumps(first_seen), "EX", 86400*30])

    # ── Versijų istorija (tl_hist) – „kas pasikeitė" diff'as ────────────────────
    # Rašom TIK kai renotify_on_text saito straipsnis pasikeitė (id ∈ new_ids).
    # Tekstą imam iš jau atsisiųsto puslapio (_blocks) → 0 naujų HTTP. tl_hist
    # skaitom (+1 GET) tik pokyčio runuose – nepasikeitus 0 papildomų komandų.
    changed_hist = [a for a in txt_arts if a["id"] in new_ids and a.get("_blocks")]
    if changed_hist:
        hist = kv_json(kv_pipeline([["GET", "tl_hist"]])[0], {})
        for a in changed_hist:
            url = a["url"]
            new_sents = _sentences(a["_blocks"])
            entry = hist.get(url) or {"title": a["title"], "full": [], "versions": []}
            prev_full = entry.get("full", [])
            prev_set, new_set = set(prev_full), set(new_sents)
            added   = [s for s in new_sents if s not in prev_set]
            removed = [s for s in prev_full  if s not in new_set]
            ver = {"ts": now_iso, "title": a["title"], "added": added, "removed": removed}
            if entry.get("title") and entry["title"] != a["title"]:
                ver["title_from"] = entry["title"]
            # Praleidžiam tuščią versiją (gali pasitaikyti, jei id pakito dėl ne-teksto)
            if added or removed or "title_from" in ver or not entry.get("versions"):
                entry["versions"] = (entry.get("versions", []) + [ver])[-15:]
            entry["title"] = a["title"]
            entry["full"]  = new_sents
            hist[url] = entry
        # Ribojam iki 25 vėliausių URL (pagal paskutinės versijos laiką)
        if len(hist) > 25:
            hist = dict(sorted(hist.items(),
                        key=lambda kv: (kv[1]["versions"][-1]["ts"] if kv[1].get("versions") else ""),
                        reverse=True)[:25])
        write_cmds.append(["SET", "tl_hist", json.dumps(hist, ensure_ascii=False), "EX", 86400*7])
        print(f"  📜 tl_hist: {len(changed_hist)} pokyčių įrašyta")

    kv_pipeline(write_cmds)
    print(f"✅ Išsaugota. Naujų: {len(new_ids)}")

    if not new_ids:
        return

    # Telegram – tik nauji IR pirmo pamatymo laikas ne senesnis nei 24h
    tg_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    def is_fresh(art):
        d = first_seen.get(art["id"], "")
        if not d: return True
        try: return datetime.fromisoformat(d.replace("Z", "+00:00")) >= tg_cutoff
        except: return True

    fresh = [a for a in new_arts if is_fresh(a)]
    if fresh and seen_count:  # seen_ids tuščias = pirmas paleidimas, nesiunčiame
        # Atominis vartas nuo dublikatų: SADD notified_ids grąžina 1 tik tam, kurį
        # ŠIS runas pirmas pažymi. Pakartotiniai/lygiagretūs runai gauna 0 → nesiunčia.
        sadd_res = kv_pipeline([["SADD", "notified_ids", a["id"]] for a in fresh])
        kv_pipeline([["EXPIRE", "notified_ids", 86400*2]])
        fresh = [a for a, r in zip(fresh, sadd_res) if int(r or 0) == 1]
    if fresh and seen_count:
        sport_icon = {"futbolas":"⚽","krepšinis":"🏀","ledo ritulys":"🏒","kitas sportas":"🏅"}
        # Be antraštės – title pirmoje eilutėje, kad matytųsi lock screen'e
        lines = []
        for a in fresh[:5]:
            lines.append(f'{sport_icon.get(a.get("sport",""),"🏆")} <a href="{a["url"]}">{a["title"]}</a>')
        if len(fresh) > 5: lines.append(f"\n+{len(fresh)-5} daugiau")
        lines.append("\n🔗 fules-online2.vercel.app")
        tg_send("\n".join(lines))
        print(f"📨 Telegram: {len(fresh)} naujos")

if __name__ == "__main__":
    # CYCLES > 1 leidžia vienam GitHub Actions runui scrape'inti kelis kartus
    # (mažiau runų – mažiau setup overhead). Naudoti kartu su retesniu cron'u!
    cycles = int(os.environ.get("CYCLES", "1") or "1")
    for i in range(cycles):
        if i: time.sleep(40)
        main()
