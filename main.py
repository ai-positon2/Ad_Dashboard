"""Web service for Railway: builds the dashboard, serves it, and rebuilds on a timer.

    python main.py

Env vars
    PORT            port to listen on (Railway sets this)
    REBUILD_HOURS   hours between rebuilds from the sheet (default 3)
    DASH_PASSWORD   optional; when set, the site asks for a password (any username)
"""
import base64
import http.server
import os
import threading
import time
import traceback
from datetime import datetime
from functools import partial

import build

PORT = int(os.environ.get("PORT", "8000"))
REBUILD_HOURS = float(os.environ.get("REBUILD_HOURS", "3"))
PASSWORD = os.environ.get("DASH_PASSWORD", "")
DIST = build.DIST
status = {"last_ok": None, "last_error": None}

BUILDING_PAGE = b"""<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="10">
<title>Ad previews</title><body style="font:16px system-ui;padding:40px;color:#333">
<p>Building the dashboard from the Google Sheet. This page refreshes in a few seconds.</p>"""


def rebuild_loop():
    while True:
        try:
            print(f"[{datetime.now():%Y-%m-%d %H:%M}] building...", flush=True)
            build.main()
            status["last_ok"], status["last_error"] = datetime.now().isoformat(timespec="seconds"), None
        except BaseException as e:  # build.main() uses SystemExit for a missing tab; keep serving the last good build
            status["last_error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        time.sleep(REBUILD_HOURS * 3600)


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, f"ok last_build={status['last_ok']} error={status['last_error']}".encode(), "text/plain")
        if PASSWORD and not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Ad previews"')
            self.end_headers()
            return
        if not (DIST / "index.html").exists():
            return self._send(503, BUILDING_PAGE, "text/html; charset=utf-8")
        return super().do_GET()

    def _authorized(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            _, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
        except Exception:
            return False
        return pw == PASSWORD

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if self.path in ("/", "/index.html"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # keep Railway logs to build output


if __name__ == "__main__":
    DIST.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=rebuild_loop, daemon=True).start()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), partial(Handler, directory=str(DIST)))
    print(f"Serving on :{PORT}, rebuilding every {REBUILD_HOURS}h", flush=True)
    server.serve_forever()
