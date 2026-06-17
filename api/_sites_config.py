# -*- coding: utf-8 -*-
"""Bendra saitų konfigūracija – VIENINTELĖ vieta, kur aprašomi scrape'inami saitai.
Naudoja: api/index.py (Vercel, RSS + HTTP) ir scraper/run_http.py (GitHub Actions, tik HTTP).

Saito dict raktai:
- rss               – RSS feed URL (WordPress beveik visada turi /feed/)
- method:"http"     – HTTP scrape (su selectors ARBA link_pattern/link_pattern_re)
- sportas_source    – sportas.lt straipsnio šaltinio ID ("" = automatinis fuzzy match)
- og_image_fallback – lankytis straipsnio puslapyje paveikslui rasti
- image_selector    – CSS selektorius herojiniam paveikslui (PIRMENYBĖ prieš og:image!)
- text_selector     – CSS selektorius tekstui
"""

SITES = [
    # ── RSS saitai (scrape'ina Vercel /api/cron-rss) ────────────────
    {"name":"FK Banga",          "sport":"futbolas",  "sportas_source":"276",  "rss":"https://www.fkbanga.lt/feed/", "og_image_fallback": True, "image_selector": ".single-feat img"},
    {"name":"FC Džiugas",        "sport":"futbolas",  "sportas_source":"1",    "rss":"https://www.fcdziugas.lt/feed/"},
    {"name":"FC Hegelmann",      "sport":"futbolas",  "sportas_source":"1",    "rss":"https://fchegelmann.com/feed/", "og_image_fallback": True},
    {"name":"FK Panevėžys",      "sport":"futbolas",  "sportas_source":"1029", "rss":"https://fk-panevezys.lt/feed/", "og_image_fallback": True},
    {"name":"FK Sūduva",         "sport":"futbolas",  "sportas_source":"118",  "rss":"https://fksuduva.lt/feed/", "og_image_fallback": True, "image_selector": ".post-featured-image img"},
    {"name":"FK TransINVEST",    "sport":"futbolas",  "sportas_source":"1",    "rss":"https://fktransinvest.lt/feed/"},
    {"name":"FA Šiauliai",       "sport":"futbolas",  "sportas_source":"1",    "rss":"https://siauliufa.lt/feed/", "og_image_fallback": True, "image_selector": ".elementor-post__thumbnail img"},
    {"name":"FK Žalgiris",       "sport":"futbolas",  "sportas_source":"302",  "rss":"https://fkzalgiris.lt/feed/", "og_image_fallback": True, "image_selector": ".little-thumb-single"},
    {"name":"LFF",               "sport":"futbolas",  "sportas_source":"13",   "rss":"https://www.lff.lt/feed/", "og_image_fallback": True},
    {"name":"BC Kibirkštis",     "sport":"krepšinis", "sportas_source":"54",   "rss":"https://bckibirkstis.lt/feed/", "og_image_fallback": True, "text_selector":".entry-summary"},
    {"name":"BC Neptūnas",       "sport":"krepšinis", "sportas_source":"131",  "rss":"https://bcneptunas.lt/feed/", "og_image_fallback": True, "image_selector": ".single-hero-img"},
    {"name":"BC Lietkabelis",    "sport":"krepšinis", "sportas_source":"38",   "rss":"https://www.kklietkabelis.lt/feed/", "og_image_fallback": True},
    {"name":"BC Šiauliai",       "sport":"krepšinis", "sportas_source":"143",  "rss":"https://bcsiauliai.lt/feed/", "og_image_fallback": True},
    {"name":"Utenos Juventus",   "sport":"krepšinis", "sportas_source":"138",  "rss":"https://utenosjuventus.lt/feed/"},
    {"name":"Lietuva Basketball","sport":"krepšinis", "sportas_source":"1034", "rss":"https://lietuva.basketball/feed/"},
    {"name":"BC Rytas",          "sport":"krepšinis", "sportas_source":"411",  "rss":"https://rytasvilnius.lt/feed/", "og_image_fallback": True, "image_selector": ".article .image img"},
    {"name":"Lengvoji atletika", "sport":"kitas sportas", "sportas_source":"17",   "rss":"https://lengvoji.lt/feed/", "og_image_fallback": True},
    {"name":"LTU Aquatics",      "sport":"kitas sportas", "sportas_source":"56",   "rss":"https://ltuaquatics.com/feed/", "og_image_fallback": True},
    # ── HTTP saitai (scrape'ina GitHub Actions run_http.py) ─────────
    # renotify_on_rename: toplyga.lt rungtynių kortelę "X – Y (GYVAI)" pasibaigus
    # rungtynėms PERVADINA į rezultatą tuo pačiu URL – pervadinimas čia yra nauja
    # naujiena, todėl seen_urls apsauga šiam saitui netaikoma
    # renotify_on_text: toplyga.lt turi anonsą prieš mačą ir ataskaitą po mačo.
    # Į id įtraukiamas teksto TURINIO parašas – turiniui pasikeitus (be teksto →
    # anonsas → ataskaita) suveikia naujas pranešimas tai pačiai naujienai.
    {"name":"Top Lyga", "sport":"futbolas", "sportas_source":"1056", "method":"http",
     "renotify_on_rename": True, "renotify_on_text": True,
     "url":"https://toplyga.lt/naujienos",
     "selectors":{"articles":"div.new","title":"a.title","link":"a.title","image":"img"},
     "base_url":"https://toplyga.lt"},
    {"name":"Žalgiris futbolas", "sport":"futbolas", "sportas_source":"382", "method":"http",
     "url":"https://zalgiris.lt/naujienos?category=futbolas",
     "selectors":{"articles":"article",
                  "title":"div.font-semibold a",
                  "link":"div.font-semibold a",
                  "image":"figure img"},
     "base_url":"https://zalgiris.lt"},
    {"name":"Žalgiris", "sport":"krepšinis", "sportas_source":"8", "method":"http",
     "url":"https://zalgiris.lt/naujienos?category=zalgiris",
     "selectors":{"articles":"article",
                  "title":"div.font-semibold a",
                  "link":"div.font-semibold a",
                  "image":"figure img"},
     "base_url":"https://zalgiris.lt"},
    {"name":"FK Riteriai", "sport":"futbolas", "sportas_source":"1019", "method":"http",
     "url":"https://www.fkriteriai.lt/naujienos",
     "link_pattern":"/post/", "base_url":"https://www.fkriteriai.lt"},
    # also_vercel: lkl.lt periodiškai blokuoja GitHub Actions IP (Cloudflare),
    # todėl LKL scrape'ina IR Vercel /api/cron-rss (Vercel IP atsako normaliai)
    {"name":"LKL", "sport":"krepšinis", "sportas_source":"30", "method":"http", "also_vercel": True,
     "url":"https://lkl.lt/straipsniai",
     "link_pattern_re": r"/straipsniai/\d+/",
     "base_url":"https://lkl.lt"},
    {"name":"KK Nevėžis", "sport":"krepšinis", "sportas_source":"195", "method":"http",
     "url":"https://www.kknevezis.lt/naujienos",
     "link_pattern":"/naujienos/", "base_url":"https://www.kknevezis.lt"},
    {"name":"BC Jonava", "sport":"krepšinis", "sportas_source":"1043", "method":"http",
     "url":"https://bcjonavahipocredit.lt/naujienos/",
     "selectors":{"articles":"div.news-list-post","title":"h4 a","link":"h4 a","image":"img"},
     "base_url":"https://bcjonavahipocredit.lt"},
    # ── Kiti ────────────────────────────────────────────────────────
    {"name":"Hockey Lietuva", "sport":"ledo ritulys", "sportas_source":"27", "method":"http",
     "url":"https://www.hockey.lt/index.php/naujienos/17",
     "link_pattern_re": r"/index\.php/naujienos/[^/]+/\d+",
     "base_url":"https://www.hockey.lt",
     "og_image_fallback": True, "image_selector":".news_item_img img",
     "text_selector":".short_text"},
    # LTOK – Cloudflare 403 "Just a moment..." blokuoja Vercel ir GitHub Actions IP
    # {"name":"LTOK", "sport":"kitas sportas", "sportas_source":"33", "method":"http",
    #  "url":"https://ltok.lt/naujienos", "link_pattern_re": r"/naujienos/[a-z]",
    #  "base_url":"https://ltok.lt", "title_selector": "[class*='text-ellipsis']",
    #  "text_selector": "[class*='prose']"},
]

RSS_SITES  = [s for s in SITES if "rss" in s]
HTTP_SITES = [s for s in SITES if s.get("method") == "http"]

# Laukai, kurie saugomi KV "articles" sąraše (slim – be html_content/text,
# kad /api/articles atsakymas ir KV srautas būtų maži)
SLIM_FIELDS = ("site", "sport", "title", "url", "date", "image", "source", "id", "text_selector")

def slim_art(a):
    return {k: a[k] for k in SLIM_FIELDS if a.get(k)}
