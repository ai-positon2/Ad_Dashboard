"""Web service for Railway: builds the dashboard, serves it, and rebuilds on a timer.

    python main.py

Env vars
    PORT            port to listen on (Railway sets this)
    REBUILD_HOURS   hours between rebuilds from the sheet (default 3)
    DASH_PASSWORD   optional; when set, the site asks for a password (any username)
    OPENAI_API_KEY  enables the AI-written account summary (/api/summary). Never commit it.
    OPENAI_MODEL    optional, default gpt-4o-mini
"""
import base64
import http.server
import json
import os
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime
from functools import partial

import build

PORT = int(os.environ.get("PORT", "8000"))
REBUILD_HOURS = float(os.environ.get("REBUILD_HOURS", "3"))
PASSWORD = os.environ.get("DASH_PASSWORD", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
DIST = build.DIST
status = {"last_ok": None, "last_error": None}
summary_cache = {}  # (build time, account) -> text: at most one OpenAI call per build per account
summary_lock = threading.Lock()

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


# ------------------------------------------------------------ account summary
def account_facts(data, account):
    """Every number the summary may quote, computed here so the model never does arithmetic."""
    ads = [a for a in data["ads"] if account in ("All", a["account"])]
    perf = [p for p in data.get("performance", []) if account in ("All", p["account"])]
    rsa = [a for a in ads if a["kind"] == "RSA"]
    served = [a for a in rsa if a["served"]]
    facts = {
        "client": data["client"],
        "scope": "all accounts" if account == "All" else account,
        "data_as_of": data.get("perfExportedAt") or data.get("exportedAt"),
        "search_ads_with_impressions_30d": len(served),
        "search_ads_without_impressions_30d": len(rsa) - len(served),
        "search_impressions_30d_from_served_combinations": sum(a["impressions"] for a in served),
        "pmax_asset_groups": sum(1 for a in ads if a["kind"] == "PMAX"),
        "demand_gen_ads_in_sheet": sum(1 for a in ads if a["kind"] == "DG"),
        "ad_strength_counts": {s: sum(1 for a in ads if a.get("strength") == s)
                               for s in ("Poor", "Average", "Good", "Excellent")},
    }
    heads = {}
    for a in served:
        for c in a["combos"]:
            for h in {p["x"] for p in c["parts"] if p["f"].startswith("HEADLINE_") and p.get("x")}:
                heads[h] = heads.get(h, 0) + c["imp"]
    facts["top_headlines_by_impressions"] = [
        {"headline": h, "impressions": v} for h, v in sorted(heads.items(), key=lambda kv: -kv[1])[:3]]

    if not perf:
        facts["spend_data"] = "not available yet (the Campaign Performance export has not run)"
        return facts

    def tot(period, key):
        return round(sum(p[period][key] for p in perf), 2)

    cost, conv, clicks, impr = tot("d30", "cost"), tot("d30", "conv"), tot("d30", "clicks"), tot("d30", "impr")
    by_channel = {}
    for p in perf:
        by_channel[p["channel"]] = by_channel.get(p["channel"], 0) + p["liveAds"]
    facts.update({
        "currency": perf[0]["currency"],
        "enabled_campaigns": len(perf),
        "live_ads_total": sum(p["liveAds"] for p in perf),
        "live_ads_by_channel": by_channel,
        "last_30_days": {
            "spend": cost, "clicks": clicks, "impressions": impr, "conversions": round(conv, 1),
            "ctr_pct": round(clicks / impr * 100, 2) if impr else None,
            "cost_per_conversion": round(cost / conv, 2) if conv else None,
        },
        "month_to_date": {"spend": tot("mtd", "cost"), "clicks": tot("mtd", "clicks"),
                          "conversions": round(tot("mtd", "conv"), 1)},
        "top_campaigns_by_spend_30d": [
            {"campaign": p["campaign"], "account": p["account"], "spend": round(p["d30"]["cost"], 2),
             "conversions": round(p["d30"]["conv"], 1)}
            for p in sorted(perf, key=lambda p: -p["d30"]["cost"])[:5]],
    })
    return facts


def write_summary(facts):
    prompt = (
        "Write a short account summary for the client's marketing team, based ONLY on the JSON facts below. "
        "Quote numbers exactly as given (format money with the currency and thousands separators); never "
        "estimate, extrapolate or add numbers that are not in the facts. If spend data is not available, say so "
        "plainly. Format: one opening sentence, then 4-6 lines that each start with '- ', then one closing line "
        "starting 'Worth watching:'. Plain text, no markdown headings, no bold. Under 170 words.\n\nFacts:\n"
        + json.dumps(facts, ensure_ascii=False))
    body = json.dumps({
        "model": OPENAI_MODEL, "temperature": 0.3, "max_tokens": 450,
        "messages": [
            {"role": "system", "content": "You are a precise paid-search account manager at a digital agency."},
            {"role": "user", "content": prompt},
        ],
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".html": "text/html; charset=utf-8", ".json": "application/json; charset=utf-8",
                      ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, f"ok last_build={status['last_ok']} error={status['last_error']}".encode(),
                              "text/plain")
        if PASSWORD and not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Ad previews"')
            self.end_headers()
            return
        if self.path.startswith("/api/summary"):
            return self._summary()
        if not (DIST / "index.html").exists():
            return self._send(503, BUILDING_PAGE, "text/html; charset=utf-8")
        return super().do_GET()

    def _summary(self):
        def reply(code, obj):
            self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

        if not OPENAI_KEY:
            return reply(503, {"error": "The AI summary isn't switched on yet: OPENAI_API_KEY isn't set on the server."})
        try:
            data = json.loads((DIST / "data.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return reply(503, {"error": "The dashboard is still building. Try again in a minute."})
        account = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("account", ["All"])[0]
        key = (data["builtAt"], account)
        with summary_lock:
            if key not in summary_cache:
                try:
                    summary_cache[key] = write_summary(account_facts(data, account))
                except Exception as e:
                    traceback.print_exc()
                    return reply(502, {"error": f"The summary service didn't respond ({type(e).__name__}). "
                                                "Try again shortly."})
        reply(200, {"text": summary_cache[key], "builtAt": data["builtAt"], "model": OPENAI_MODEL})

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
        if self.path in ("/", "/index.html") or self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # keep Railway logs to build output


if __name__ == "__main__":
    DIST.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=rebuild_loop, daemon=True).start()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), partial(Handler, directory=str(DIST)))
    print(f"Serving on :{PORT}, rebuilding every {REBUILD_HOURS}h, "
          f"AI summary {'on' if OPENAI_KEY else 'off'}", flush=True)
    server.serve_forever()
