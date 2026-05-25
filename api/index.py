from flask import Flask, request, jsonify, Response
import json, os, time, requests as _req
from datetime import datetime

app = Flask(__name__)

KV_URL        = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN      = os.environ.get("KV_REST_API_TOKEN", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SPORTAS_USER  = os.environ.get("SPORTAS_USER", "")
SPORTAS_PASS  = os.environ.get("SPORTAS_PASS", "")

_SPORT_CATS = {"krepšinis": [6, 22], "futbolas": [7, 103]}
_BASE = "https://www.sportas.lt/Admin/Load/UArticles"
_UA   = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ── Vercel KV ──────────────────────────────────────────────────────
def _kv_get(key):
    if not KV_URL: return None
    r = _req.get(f"{KV_URL}/get/{key}",
                 headers={"Authorization": f"Bearer {KV_TOKEN}"}, timeout=5)
    result = r.json().get("result")
    return json.loads(result) if result else None

def _kv_set(key, value):
    _req.post(f"{KV_URL}/pipeline",
              headers={"Authorization": f"Bearer {KV_TOKEN}"},
              json=[["SET", key, json.dumps(value, ensure_ascii=False), "EX", 86400]],
              timeout=5)


# ── AI praturtinimas ───────────────────────────────────────────────
def _ai_enrich(title, text):
    try:
        import anthropic, re as _re
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        prompt = (
            f"Sporto straipsnis (lietuvių k.):\nPavadinimas: {title}\n\n"
            f"Tekstas:\n{text[:3000]}\n\n"
            "1. Sugeneruok 1–4 temas (tagus): asmenų vardai/pavardės, klubų pavadinimai.\n"
            "2. Tekste paboldink PIRMĄ kiekvieno asmens vardo paminėjimą "
            "(<strong>Vardas Pavardė</strong>).\n\n"
            "Atsakyk TIKSLIAI šiuo formatu:\nTAGS: Tag1, Tag2\nTEXT:\npilnas tekstas"
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=3000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        tags, enriched = "", text
        m = _re.search(r'^TAGS:\s*(.+)$', raw, _re.MULTILINE)
        if m: tags = m.group(1).strip()
        m = _re.search(r'^TEXT:\s*\n(.*)', raw, _re.MULTILINE | _re.DOTALL)
        if m: enriched = m.group(1).strip()
        return {"tags": tags, "text": enriched}
    except Exception as e:
        return {"tags": "", "text": text}


# ── Sportas.lt postinimas ──────────────────────────────────────────
def _match_source(sources, site_name):
    sl = site_name.lower()
    for name, sid in sources.items():
        if name.lower() == sl: return sid
    for name, sid in sources.items():
        if sl in name.lower(): return sid
    words = set(sl.split())
    for name, sid in sources.items():
        if len(name) >= 3 and words & set(name.lower().split()): return sid
    return "1"

def _session():
    sess = _req.Session()
    sess.headers.update({"User-Agent": _UA})
    cookies = _kv_get("sportas_cookies")
    if cookies:
        for k, v in cookies.items():
            sess.cookies.set(k, v, domain="www.sportas.lt")
    r = sess.get(f"{_BASE}/editArticle/", allow_redirects=False, timeout=15)
    if r.status_code in (301, 302):
        sess.post("https://www.sportas.lt/Admin/login",
                  data={"Loginas": SPORTAS_USER, "Password": SPORTAS_PASS},
                  allow_redirects=True, timeout=15)
        _kv_set("sportas_cookies", dict(sess.cookies))
    return sess

def _sources(sess):
    cached = _kv_get("sportas_sources")
    if cached: return cached
    from bs4 import BeautifulSoup
    r = sess.get(f"{_BASE}/editArticle/", timeout=15)
    soup = BeautifulSoup(r.text, "html.parser")
    sel  = soup.find("select", {"id": "sourceSelect"})
    result = {}
    if sel:
        for opt in sel.find_all("option"):
            v, t = opt.get("value",""), opt.get_text(strip=True)
            if v and t: result[t] = v
    _kv_set("sportas_sources", result)
    return result

def _do_post(article):
    sport    = article.get("sport", "")
    cat_ids  = _SPORT_CATS.get(sport, [])
    title    = article.get("title", "")
    text     = article.get("text", "")
    site     = article.get("site", "")

    enriched  = _ai_enrich(title, text)
    ai_tags   = enriched.get("tags", "")
    rich_text = enriched.get("text", text)
    paras     = [p.strip() for p in rich_text.split("\n\n") if p.strip()]
    html_body = "".join(f"<p>{p}</p>" for p in paras)
    tags_list = [t.strip() for t in ai_tags.split(",") if t.strip()] if ai_tags else []

    sess      = _session()
    sources   = _sources(sess)
    source_id = _match_source(sources, site)
    now       = datetime.now()

    data = [
        ("id","0"),("returnId","-1"),("smartyNow",str(int(time.time()))),
        ("titleSlug",""),("title",title),("extraTitle",""),("facebookTitle",""),
        ("generatedTV3Title",""),("intro",""),("Pastabos",""),("text",html_body),
        ("leadPhoto[path]",""),("leadPhoto[title]",""),("leadPhoto[size]","l"),
        ("cropSize","l"),("leadLiveVideoTime[endDate]",""),("leadLiveVideoTime[endTime]",""),
        ("leadPlayVideo[code]",""),("leadPlayVideo[playId]",""),("leadVideo[url]",""),
        ("attachCustomJs[]",""),("attachFbPost[]",""),
        ("mainCategory", str(cat_ids[0]) if cat_ids else "20"),
    ]
    for cid in cat_ids: data.append(("categories[]", str(cid)))
    for cid in cat_ids: data.append((f"priority[{cid}]", "4"))
    data += [
        ("source",str(source_id)),("realSource","0"),("realSource","0"),
        ("disableComments","0"),("commentsForUsers","0"),("isLiveNews","0"),("tags",""),
    ]
    for tag in tags_list: data.append(("tags[]", tag))
    data += [
        ("n18","0"),("sensitive","0"),("top10","0"),("useSpecNews","0"),
        ("orderedArticle","0"),("leftBlocks","0"),("cacheKey",""),
        ("status","0"),("status","1"),("exportArticle","1"),
        ("publish[StartDate]", now.strftime("%Y-%m-%d")),
        ("publish[StartTime]", now.strftime("%H:%M")),
        ("publish[EndDate]","2030-01-01"),("publish[EndTime]","00:00"),
        ("titlePage","0"),("titlePagePriority","6"),
    ]

    r = sess.post(f"{_BASE}/saveArticle", data=data, allow_redirects=False, timeout=30)
    if r.status_code == 302:
        _kv_set("sportas_cookies", dict(sess.cookies))
        return True, "OK"
    return False, f"HTTP {r.status_code}"


# ── Routes ─────────────────────────────────────────────────────────
@app.route("/")
def serve_index():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in [
        os.path.join(base, "public", "index.html"),
        os.path.join(os.path.dirname(__file__), "..", "public", "index.html"),
        "public/index.html",
    ]:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return Response(f.read(), content_type="text/html; charset=utf-8")
    return "Not found", 404

@app.route("/api/articles", methods=["GET"])
def articles():
    data   = _kv_get("articles")   or []
    recent = _kv_get("recent_ids") or []
    return jsonify({"articles": data, "recent_ids": recent})

@app.route("/api/post", methods=["POST", "OPTIONS"])
def post():
    if request.method == "OPTIONS":
        return Response("", headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })
    payload  = request.get_json() or {}
    aid      = payload.get("id", "")
    articles = _kv_get("articles") or []
    article  = next((a for a in articles if a["id"] == aid), None)
    if not article:
        return jsonify({"ok": False, "error": "Nerasta"}), 404
    ok, msg = _do_post(article)
    return jsonify({"ok": ok, "message": msg})
