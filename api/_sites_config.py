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
    # wp_featured_api: fksuduva.lt HTML atsako per 3-4s (netelpa į rss 2s img
    # timeout – nuotraukos niekada neužsikraudavo); WP REST JSON ~1.5s
    {"name":"FK Sūduva",         "sport":"futbolas",  "sportas_source":"118",  "rss":"https://fksuduva.lt/feed/", "og_image_fallback": True, "wp_featured_api": True, "image_selector": ".post-featured-image img"},
    {"name":"FK TransINVEST",    "sport":"futbolas",  "sportas_source":"1",    "rss":"https://fktransinvest.lt/feed/"},
    {"name":"FA Šiauliai",       "sport":"futbolas",  "sportas_source":"1",    "rss":"https://siauliufa.lt/feed/", "og_image_fallback": True, "image_selector": ".elementor-post__thumbnail img"},
    {"name":"FK Žalgiris",       "sport":"futbolas",  "sportas_source":"302",  "rss":"https://fkzalgiris.lt/feed/", "og_image_fallback": True, "image_selector": ".little-thumb-single"},
    {"name":"LFF",               "sport":"futbolas",  "sportas_source":"13",   "rss":"https://www.lff.lt/feed/", "og_image_fallback": True},
    {"name":"BC Kibirkštis",     "sport":"krepšinis", "sportas_source":"54",   "rss":"https://bckibirkstis.lt/feed/", "og_image_fallback": True, "text_selector":".entry-summary"},
    {"name":"BC Neptūnas",       "sport":"krepšinis", "sportas_source":"131",  "rss":"https://bcneptunas.lt/feed/", "og_image_fallback": True, "image_selector": ".single-hero-img"},
    {"name":"BC Lietkabelis",    "sport":"krepšinis", "sportas_source":"38",   "rss":"https://www.kklietkabelis.lt/feed/", "og_image_fallback": True},
    {"name":"BC Šiauliai",       "sport":"krepšinis", "sportas_source":"143",  "rss":"https://bcsiauliai.lt/feed/", "og_image_fallback": True},
    # Feed'as be content:encoded (tik excerpt su "Skaityti toliau") ir be nuotraukų;
    # puslapis NETURI og:image, todėl nuotrauka tik per image_selector (+srcset max)
    {"name":"Utenos Juventus",   "sport":"krepšinis", "sportas_source":"138",  "rss":"https://utenosjuventus.lt/feed/",
     "og_image_fallback": True, "image_selector": ".entry-content img",
     "base_url":"https://utenosjuventus.lt"},
    {"name":"Lietuva Basketball","sport":"krepšinis", "sportas_source":"1034", "rss":"https://lietuva.basketball/feed/"},
    # base_url + text_selector: /api/article-text pagal juos randa turinio konteinerį
    # (puslapio class "single-post__content" neatitinka generinių [class*="post-content"]
    # paieškų, o straipsniai iš Word įkelti div.s3/div.s8 blokais – ne <p>)
    {"name":"BC Rytas",          "sport":"krepšinis", "sportas_source":"411",  "rss":"https://rytasvilnius.lt/feed/", "og_image_fallback": True, "image_selector": ".article .image img",
     "base_url":"https://rytasvilnius.lt", "text_selector":".single-post__content"},
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
     # sąrašo kortelė rodo -medium (pixeliuota, ~13KB); -featured ~5x didesnė, geresnė
     "img_replace":["-medium.jpg", "-featured.jpg"],
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
    # also_vercel: lkl.lt periodiškai blokuoja GitHub Actions IP (Cloudflare),
    # todėl LKL scrape'ina IR Vercel /api/cron-rss (Vercel IP atsako normaliai)
    {"name":"LKL", "sport":"krepšinis", "sportas_source":"30", "method":"http", "also_vercel": True,
     "url":"https://lkl.lt/straipsniai",
     "link_pattern_re": r"/straipsniai/\d+/",
     # Sąraše dalis thumb'ų maži (160x208) – keičiam į 1214x726 (og:image dydis,
     # serveris generuoja visoms nuotraukoms; patikrinta HEAD=200)
     "img_replace_re": [r"/media/articles/\d+x\d+/", "/media/articles/1214x726/"],
     "base_url":"https://lkl.lt"},
    {"name":"KK Nevėžis", "sport":"krepšinis", "sportas_source":"195", "method":"http",
     "url":"https://www.kknevezis.lt/naujienos",
     "link_pattern":"/naujienos/", "base_url":"https://www.kknevezis.lt"},
    {"name":"BC Jonava", "sport":"krepšinis", "sportas_source":"1043", "method":"http",
     "url":"https://bcjonavahipocredit.lt/naujienos/",
     "selectors":{"articles":"div.news-list-post","title":"h4 a","link":"h4 a","image":"img"},
     "base_url":"https://bcjonavahipocredit.lt",
     # Straipsnio tekstas .post-txt bloke; bendri selektoriai (article/main/
     # post-content...) šioje temoje neegzistuoja – be šito publikavimas
     # grąžindavo „Straipsnis dar be teksto"
     "text_selector":".post-txt"},
    # ── Kiti ────────────────────────────────────────────────────────
    {"name":"Hockey Lietuva", "sport":"ledo ritulys", "sportas_source":"27", "method":"http",
     "url":"https://www.hockey.lt/index.php/naujienos/17",
     "link_pattern_re": r"/index\.php/naujienos/[^/]+/\d+",
     "base_url":"https://www.hockey.lt",
     "og_image_fallback": True, "image_selector":".news_item_img img",
     "text_selector":".short_text"},
    # LTOK – Cloudflare 403 "Just a moment..." blokuoja Vercel ir GitHub Actions IP
    # (patikrinta 2026-07-22: visi keliai, įskaitant sitemap ir _payload.json).
    # method:"local" – scrape'ina TIK lokalus Mac scraperis scraper/run_ltok_local.py
    # (namų IP Cloudflare praleidžia). Įrašas čia reikalingas _allowed_hosts,
    # kategorijoms ir sportas_source publikavimui.
    {"name":"LTOK", "sport":"kitas sportas", "sportas_source":"33", "method":"local",
     "url":"https://ltok.lt/naujienos", "base_url":"https://ltok.lt"},
]

RSS_SITES  = [s for s in SITES if "rss" in s]
HTTP_SITES = [s for s in SITES if s.get("method") == "http"]

def fix_img(site, image):
    """Per-saito nuotraukos URL korekcijos, taikomos ABIEJUOSE scraperiuose:
    img_replace = [kas, kuo] (paprastas replace), img_replace_re = [regex, kuo].
    LKL atvejis: sąrašo 160x208 thumb'ai → 1214x726 originalai."""
    if not image:
        return image
    rep = site.get("img_replace")
    if rep:
        image = image.replace(rep[0], rep[1])
    rre = site.get("img_replace_re")
    if rre:
        import re as _re_fx
        image = _re_fx.sub(rre[0], rre[1], image)
    return image

def norm_url(u):
    """URL normalizavimas dedup'ui. toplyga.lt gyvų rungtynių puslapis
    /rungtynes-gyvai/... pasibaigus mačui tampa /rungtynes/... (tas pats ID gale) –
    tai TAS PATS straipsnis: ataskaita turi PAKEISTI seną GYVAI įrašą, kurio
    nuotrauką toplyga.lt po mačo ištrina (liktų kortelė be foto + dublikatas)."""
    return (u or "").replace("/rungtynes-gyvai/", "/rungtynes/")

# Laukai, kurie saugomi KV "articles" sąraše (slim – be html_content/text,
# kad /api/articles atsakymas ir KV srautas būtų maži)
SLIM_FIELDS = ("site", "sport", "title", "url", "date", "image", "source", "id", "text_selector")

def slim_art(a):
    return {k: a[k] for k in SLIM_FIELDS if a.get(k)}
