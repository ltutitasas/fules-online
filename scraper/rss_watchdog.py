#!/usr/bin/env python3
"""RSS watchdog – paleidžia /api/cron-rss, jei RSS saitai per ilgai nescrape'inti.

Kodėl reikia: RSS scrape'inimą varo cron-job.org Job 1, kuris po mūsų svetainės
gedimų jau DU kartus automatiškai išsijungė (2026-08-05 Vercel maršrutų lūžis,
2026-10-26 Upstash kvota) – abu kartus RSS naujienos stovėjo paromis, kol
vartotojas pastebėjo. Job 2 (šis workflow) nenukenčia, nes kreipiasi į GitHub
API, ne į mūsų svetainę – tad jis tinka budėtojo vaidmeniui.

Normaliai nieko nedaro: 1 KV skaitymas per runą (~21K komandų/mėn).
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("APP_BASE_URL", "https://fules-online2.vercel.app")
# scrape_status „ok" rašomas tik kas ~10 min (KV taupymas), tad riba turi būti didesnė
STALE_MIN = float(os.environ.get("RSS_STALE_MIN", "15"))

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))
from _sites_config import RSS_SITES  # noqa: E402


def _get(url, method="GET", timeout=30):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def main():
    names = {s["name"] for s in RSS_SITES}
    try:
        status = _get(f"{BASE}/api/scrape-status")
    except Exception as e:
        print(f"⚠️  scrape-status nepasiekiamas: {e} – praleidžiam")
        return

    now = datetime.now(timezone.utc)
    ages = []
    for name in names:
        ok = (status.get(name) or {}).get("ok", "")
        try:
            ages.append((now - datetime.fromisoformat(str(ok).replace("Z", "+00:00"))).total_seconds() / 60)
        except Exception:
            pass

    if not ages:
        print("⚠️  nė vieno RSS saito scrape_status – paleidžiam cron-rss")
        freshest = None
    else:
        freshest = min(ages)
        if freshest <= STALE_MIN:
            print(f"✅ RSS šviežias ({freshest:.1f} min) – watchdog nieko nedaro")
            return
        print(f"🚨 RSS pasenęs ({freshest:.1f} min > {STALE_MIN}) – cron-job.org Job 1 miręs?")

    try:
        res = _get(f"{BASE}/api/cron-rss", method="POST", timeout=40)
        print(f"   paleista cron-rss: ok={res.get('ok')} total={res.get('total')} naujų={res.get('new')}")
        if res.get("error"):
            print(f"   klaida: {str(res['error'])[:200]}")
    except Exception as e:
        print(f"   ❌ nepavyko paleisti cron-rss: {e}")


if __name__ == "__main__":
    main()
