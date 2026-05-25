from http.server import BaseHTTPRequestHandler
import json, os, requests as _req

KV_URL   = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN = os.environ.get("KV_REST_API_TOKEN", "")

def _kv_get(key: str):
    if not KV_URL:
        return None
    r = _req.get(f"{KV_URL}/get/{key}",
                 headers={"Authorization": f"Bearer {KV_TOKEN}"}, timeout=5)
    result = r.json().get("result")
    return json.loads(result) if result else None


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        articles   = _kv_get("articles")   or []
        recent_ids = _kv_get("recent_ids") or []
        body = json.dumps(
            {"articles": articles, "recent_ids": recent_ids},
            ensure_ascii=False
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass
