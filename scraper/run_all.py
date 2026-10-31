#!/usr/bin/env python3
"""Vieningas scraperis GitHub Actions'ui – RSS + HTTP saitai VIENU KV ciklu.

Kodėl (2026-10-31): RSS rinkimas Vercel'yje suvartodavo visą Hobby plano
4 val./mėn. CPU limitą (0,67 s CPU × 21 600 runų) – Vercel du kartus išjungė
projektą (402 DEPLOYMENT_DISABLED). GitHub Actions viešam repo – be limitų.

Kaip veikia:
- RSS saitai – api/index.py `_fetch_rss` (tas pats kodas kaip Vercel'yje)
- HTTP saitai – scraper/run_http.py `fetch_http` + Top Lyga teksto parašai
  (tas pats kodas kaip anksčiau Actions'e). Abu parsiuntimai – BŪTENT tie
  patys, todėl straipsnių ID identiški jau esantiems KV (kitaip seni
  straipsniai atrodytų nauji → Telegram spam'as).
- Merge, nuotraukos, KV, Telegram – vienas variklis `api/index.py:run_scraper`.
  Anksčiau RSS (Vercel) ir HTTP (Actions) darė DU atskirus KV ciklus; dabar
  vienas ciklas apima visus saitus → dvigubai dažnesnis tikrinimas už tą pačią
  Upstash kainą.

Ciklai: vienas runas atlieka CYCLES ciklų kas CYCLE_INTERVAL s (pradžia–pradžia).
Numatyta 2 × 55 s: cron-job.org Job 2 runą paleidžia kas 2 min, tad saitai
tikrinami ~kas minutę. Daugiau ciklų = mažesnė delsa, bet daugiau Upstash
komandų (4 komandos per ciklą) – žr. CLAUDE.md skaičiavimą prieš didinant.
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scraper"))

import run_http as rh          # noqa: E402  HTTP fetch + Top Lyga parašai
from api import index as idx   # noqa: E402  bendras variklis


def http_fetch_with_sig(site):
    """HTTP saitas TIKSLIAI kaip run_http.main: fetch_http, o renotify_on_text
    saitams (Top Lyga) į id įtraukiamas straipsnio turinio parašas."""
    arts = rh.fetch_http(site)
    if site.get("renotify_on_text"):
        txt = [a for a in arts if a.get("url")]
        if txt:
            with ThreadPoolExecutor(max_workers=6) as ex:
                res = list(ex.map(lambda a: rh.fetch_text_sig(a["url"]), txt))
            for a, (sig, blocks) in zip(txt, res):
                if sig:
                    a["id"] = rh.art_id(a["url"], a["title"] + "|" + sig)
                    a["_blocks"] = blocks   # versijų istorijai; slim_art jo nesaugo
    return arts


def tl_hist_writes(all_arts, new_ids, now_iso):
    """Top Lyga versijų istorija (tl_hist) – perkelta iš run_http.main be pakeitimų.
    tl_hist skaitomas (+1 KV GET) TIK kai straipsnis realiai pasikeitė."""
    changed = [a for a in all_arts if a["id"] in new_ids and a.get("_blocks")]
    if not changed:
        return []
    hist = idx._kv_json(idx._kv_pipeline([["GET", "tl_hist"]])[0], {})
    for a in changed:
        url = a["url"]
        new_sents = rh._sentences(a["_blocks"])
        entry = hist.get(url) or {"title": a["title"], "full": [], "versions": []}
        prev_full = entry.get("full", [])
        prev_set, new_set = set(prev_full), set(new_sents)
        added = [s for s in new_sents if s not in prev_set]
        removed = [s for s in prev_full if s not in new_set]
        ver = {"ts": now_iso, "title": a["title"], "added": added, "removed": removed}
        if entry.get("title") and entry["title"] != a["title"]:
            ver["title_from"] = entry["title"]
        if added or removed or "title_from" in ver or not entry.get("versions"):
            entry["versions"] = (entry.get("versions", []) + [ver])[-15:]
        entry["title"] = a["title"]
        entry["full"] = new_sents
        hist[url] = entry
    if len(hist) > 25:
        hist = dict(sorted(hist.items(),
                           key=lambda kv: (kv[1]["versions"][-1]["ts"] if kv[1].get("versions") else ""),
                           reverse=True)[:25])
    print(f"   📜 tl_hist: {len(changed)} pokyčių")
    return [["SET", "tl_hist", json.dumps(hist, ensure_ascii=False), "EX", 86400 * 7]]


def main():
    cycles = max(1, int(os.environ.get("CYCLES") or 2))
    interval = float(os.environ.get("CYCLE_INTERVAL") or 55)
    print(f"🔄 RSS + HTTP scraperis: {cycles} ciklai kas {interval:.0f} s")
    t0 = time.monotonic()
    ok = 0
    for i in range(cycles):
        wait = t0 + i * interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        c0 = time.monotonic()
        try:
            total, new = idx.run_scraper(mode="all", http_fetch=http_fetch_with_sig,
                                         extra_writes=tl_hist_writes)
            ok += 1
            img = idx._IMG_DBG
            print(f"[{i + 1}/{cycles}] {datetime.now(timezone.utc):%H:%M:%S} UTC  "
                  f"{time.monotonic() - c0:.1f} s | straipsnių {total} | naujų {new} | "
                  f"foto eilė {img.get('cand', 0)}, išspręsta {img.get('resolved', 0)}")
            for n in idx._NEW_DBG:
                print(f"      + {n['site']}: {n['title']}")
        except Exception as e:
            # Vienas nepavykęs ciklas (pvz. KV tinklo trikdis) nelaužo kitų –
            # kitas ciklas tą patį darbą padarys iš naujo (scrape idempotentiškas)
            print(f"[{i + 1}/{cycles}] ❌ {type(e).__name__}: {str(e)[:200]}")
    # Raudonas runas GitHub'e tik kai NĖ VIENAS ciklas nepavyko
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
