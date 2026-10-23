"""Web service for Railway: builds the dashboard(s), serves them, and rebuilds on a timer.

    python main.py

Two modes
    default   one dashboard per service, chosen by the CLIENT env var, served at "/".
    MULTI=on  ONE service hosts every dashboard in clients.json, each at its own path:
              /tealium/  /oia/  /mchale/  /42north/ ...   (the path is the client's "slug" in clients.json)
              "/" and old root-level URLs (/api/comments/new, /assets/...) go to DEFAULT_CLIENT, so existing
              bookmarks and the comment notifier keep working.

Env vars
    PORT            port to listen on (Railway sets this)
    REBUILD_HOURS   hours between rebuilds (default 1); <slug>/api/rebuild rebuilds that dashboard now
    DASH_PASSWORD   optional; when set, the site asks for a password (any username).
                    MULTI: DASH_PASSWORD_<CLIENT KEY> (e.g. DASH_PASSWORD_TEALIUM) overrides it per dashboard
    OPENAI_API_KEY  enables the AI-written account summary (/api/summary). Never commit it.
    OPENAI_MODEL    optional, default gpt-4o-mini
    COMMENTS        "on" shows a comment box on each ad (/api/comments). Attach a Railway Volume to keep comments
                    across deploys; see comments.py.
    NOTIFY_SECRET   secret word shared with google-apps-script/comment_notifier.gs, which polls
                    <dashboard>/api/comments/new and emails the team about new comments
    MULTI           "on" = the one-service mode above
    SERVE_CLIENTS   MULTI only: comma-separated client keys to host (default: every client in clients.json)
    DEFAULT_CLIENT  MULTI only: what "/" opens (default tealium)
    DIST_ROOT       MULTI only: where builds are written (default: the Railway volume, else ./dist)
"""
import base64
import gzip
import hmac
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime
from functools import partial
from pathlib import Path

import build
import gads_api
import comments

ROOT = Path(__file__).parent
PORT = int(os.environ.get("PORT", "8000"))
REBUILD_HOURS = float(os.environ.get("REBUILD_HOURS", "1"))
PASSWORD = os.environ.get("DASH_PASSWORD", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
COMMENTS = os.environ.get("COMMENTS", "").strip().lower() in ("1", "on", "true", "yes")
NOTIFY_SECRET = os.environ.get("NOTIFY_SECRET", "").strip()  # lets the Apps Script notifier read new comments
MULTI = os.environ.get("MULTI", "").strip().lower() in ("1", "on", "true", "yes")
BUILD_TIMEOUT = int(os.environ.get("BUILD_TIMEOUT", "1800"))
_volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
DIST_ROOT = Path(os.environ.get("DIST_ROOT") or (Path(_volume) / "dist" if _volume else ROOT / "dist"))
wake = threading.Event()  # tells the rebuild loop a refresh was requested

BUILDING_PAGE = b"""<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="10">
<title>Ad Previews</title><body style="font:16px system-ui;padding:40px;color:#333">
<p>Building the dashboard. This page refreshes in a few seconds.</p>"""


def refresh_page(prefix):
    return (f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="75;url={prefix}">'
            '<title>Ad Previews</title><body style="font:16px system-ui;padding:40px;color:#333">'
            "<p>Refreshing the data. The dashboard reloads with the new data in about a minute.</p>"
            f'<p><a href="{prefix}">Back to the dashboard now</a></p>').encode()


class Site:
    """One dashboard: its own build output, status, summary cache, comments key and password."""

    def __init__(self, key, cfg, slug, dist):
        self.key, self.cfg, self.slug, self.dist = key, cfg, slug, Path(dist)
        self.prefix = f"/{slug}/" if slug else "/"
        self.status = {"last_ok": None, "last_error": None}
        self.summary_cache = {}  # (build time, account) -> text: at most one OpenAI call per build per account
        self.summary_lock = threading.Lock()
        self.gz_cache = {}  # page mtime -> gzipped page (several MB of inline JSON; gzip cuts it ~5x)
        self.rebuild_now = threading.Event()  # set by /api/rebuild to skip the wait
        self.force = False  # next build re-queries Google Ads instead of using the cache window
        self.password = os.environ.get(f"DASH_PASSWORD_{key.upper()}", PASSWORD)
        self.next_build = 0.0

    def schedule_first_build(self):
        """A build on disk (persistent volume) that is still fresh is served as is; otherwise build straight away."""
        page = self.dist / "index.html"
        self.next_build = page.stat().st_mtime + REBUILD_HOURS * 3600 if page.exists() else 0.0

    def run_build(self):
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] building {self.key}...", flush=True)
        try:
            if MULTI:  # a subprocess per dashboard: no shared globals, and builds never overlap
                env = {**os.environ, "DIST_ROOT": str(DIST_ROOT)}
                if self.force:
                    env["GADS_FORCE"] = "1"
                p = subprocess.run([sys.executable, str(ROOT / "build.py"), self.key], cwd=str(ROOT), env=env,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=BUILD_TIMEOUT)
                lines = [l for l in ((p.stdout or "") + (p.stderr or "")).strip().splitlines() if l.strip()]
                for line in lines[-25:]:
                    print(f"  [{self.key}] {line}", flush=True)
                if p.returncode != 0:
                    raise RuntimeError(lines[-1] if lines else f"build exited with code {p.returncode}")
            else:
                if self.force:
                    gads_api.FORCE.set()
                build.main()
            self.status["last_ok"], self.status["last_error"] = datetime.now().isoformat(timespec="seconds"), None
        except BaseException as e:  # keep serving the last good build
            self.status["last_error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            self.force = False


def make_sites():
    if not MULTI:
        return [Site(build.CLIENT_KEY, build.CLIENT, "", build.DIST)]
    keys = [k.strip().lower() for k in os.environ.get("SERVE_CLIENTS", "").split(",") if k.strip()] or list(build.CLIENTS)
    unknown = [k for k in keys if k not in build.CLIENTS]
    if unknown:
        raise SystemExit(f"SERVE_CLIENTS has unknown client(s): {', '.join(unknown)}. Known: {', '.join(build.CLIENTS)}")
    default = os.environ.get("DEFAULT_CLIENT", "tealium").strip().lower()
    if default not in keys:
        default = keys[0]
    keys.sort(key=lambda k: k != default)  # the default dashboard builds first
    return [Site(k, build.CLIENTS[k], build.CLIENTS[k].get("slug", k), DIST_ROOT / k) for k in keys]


SITES = make_sites()
DEFAULT_SITE = SITES[0]
BY_SLUG = {s.slug: s for s in SITES if s.slug}


def rebuild_loop():
    for s in SITES:
        s.schedule_first_build()
    while True:
        for s in SITES:
            if s.rebuild_now.is_set() or time.time() >= s.next_build:
                s.rebuild_now.clear()
                s.run_build()
                s.next_build = time.time() + REBUILD_HOURS * 3600
        wake.wait(30)
        wake.clear()


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
    site = DEFAULT_SITE

    # ------------------------------------------------------------- routing
    def _route(self):
        """Pick the dashboard from the first path segment and rewrite self.path to the path inside it.
        Returns False when it already answered (redirect / 404 / global health)."""
        path, _, query = self.path.partition("?")
        q = ("?" + query) if query else ""
        if not MULTI:
            self.site = DEFAULT_SITE
            return True
        if path == "/healthz":
            self._send(200, self._all_health().encode(), "text/plain")
            return False
        parts = path.split("/")  # "/oia/api/x" -> ["", "oia", "api", "x"]
        site = BY_SLUG.get(parts[1]) if len(parts) > 1 else None
        if site:
            if len(parts) == 2:  # "/oia" -> "/oia/" so the page's relative URLs resolve inside the dashboard
                return self._redirect(site.prefix + q)
            self.site = site
            self.path = "/" + "/".join(parts[2:]) + q
            return True
        if path in ("/", "/index.html"):
            return self._redirect(DEFAULT_SITE.prefix + q)
        self.site = DEFAULT_SITE  # legacy root-level URLs (old pages, the comment notifier) belong to the default dashboard
        return True

    def _redirect(self, where):
        self.send_response(302)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _all_health(self):
        ok = [s.status["last_ok"] for s in SITES if s.status["last_ok"]]
        errs = [f"{s.key}: {s.status['last_error']}" for s in SITES if s.status["last_error"]]
        per = ", ".join(f"{s.slug}={'ok' if s.status['last_ok'] else ('error' if s.status['last_error'] else 'building')}"
                        for s in SITES)
        return f"ok last_build={max(ok) if ok else None} error={'; '.join(errs) or None} sites[{per}]"

    def _alias_notes(self):
        """Some ad blockers / company filters drop any URL containing "comment": the page also talks to /api/notes."""
        if self.path.startswith("/api/notes"):
            self.path = "/api/comments" + self.path[len("/api/notes"):]

    # ------------------------------------------------------------- GET
    def do_GET(self):
        if not self._route():
            return
        site = self.site
        self._alias_notes()
        self.directory = str(site.dist)
        if self.path in ("/", "/index.html") or (self.path.startswith("/api/comments") and not self.path.startswith("/api/comments/new")):
            # one short line per page load / comment lookup, so a "the box isn't there" report can be checked against what reached the server
            kind = "page" if self.path in ("/", "/index.html") else "comments"
            print(f"[{kind}:{site.key}] {self.headers.get('User-Agent', '')[:100]}", flush=True)
        if self.path == "/healthz":
            return self._send(200, f"ok last_build={site.status['last_ok']} error={site.status['last_error']}".encode(),
                              "text/plain")
        if self.path.startswith("/api/comments/new"):  # the notifier authenticates with the secret, not the password
            return self._new_comments()
        if site.password and not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Ad Previews"')
            self.end_headers()
            return
        if self.path.startswith("/api/rebuild"):
            site.force = True  # re-query Google Ads (rate-limited), not just rebuild from the cache
            site.rebuild_now.set()
            wake.set()
            return self._send(202, refresh_page(site.prefix), "text/html; charset=utf-8")
        if self.path.startswith("/api/summary"):
            return self._summary()
        if self.path.startswith("/api/comments"):
            if not COMMENTS:
                return self._json(404, {"error": "Comments aren't switched on for this dashboard."})
            ad = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("ad", [""])[0]
            return self._json(200, {"comments": comments.list_for(site.key, ad)})
        if not (site.dist / "index.html").exists():
            return self._send(503, BUILDING_PAGE, "text/html; charset=utf-8")
        if self.path in ("/", "/index.html") and "gzip" in self.headers.get("Accept-Encoding", ""):
            return self._send_page_gzipped()
        return super().do_GET()

    def do_HEAD(self):
        if self._route():
            self.directory = str(self.site.dist)
            super().do_HEAD()

    # ------------------------------------------------------------- POST
    def do_POST(self):
        if not self._route():
            return
        site = self.site
        self._alias_notes()
        if self.path.startswith("/api/comments/delete"):  # admin clean-up, authenticated with NOTIFY_SECRET
            return self._delete_comments()
        if site.password and not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Ad Previews"')
            self.end_headers()
            return
        if not (COMMENTS and self.path.startswith("/api/comments")):
            return self._json(404, {"error": "Not found."})
        try:
            size = int(self.headers.get("Content-Length") or 0)
            if size > 16384:
                return self._json(413, {"error": "That comment is too long."})
            body = json.loads(self.rfile.read(size) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "Couldn't read the comment. Try again."})
        ad_id = str(body.get("ad") or "").strip()
        error = comments.add(site.key, ad_id, body.get("name"), body.get("text"))
        if error:
            return self._json(400, {"error": error})
        self._json(201, {"comments": comments.list_for(site.key, ad_id)})

    def _delete_comments(self):
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 16384)) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad request"})
        if not (COMMENTS and NOTIFY_SECRET) or not hmac.compare_digest(str(body.get("key", "")), NOTIFY_SECRET):
            return self._json(403, {"error": "forbidden"})
        try:
            n = comments.delete(self.site.key, body.get("ids") or [])
        except (TypeError, ValueError):
            return self._json(400, {"error": "ids must be a list of comment numbers"})
        self._json(200, {"deleted": n})

    def _new_comments(self):
        """Comments after ?after=<id>, with each ad's campaign and ad group, for the email notifier."""
        site = self.site
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if not (COMMENTS and NOTIFY_SECRET) or not hmac.compare_digest(q.get("key", [""])[0], NOTIFY_SECRET):
            return self._json(403, {"error": "forbidden"})
        try:
            after = int(q.get("after", ["0"])[0])
        except ValueError:
            after = 0
        try:
            ads = {a["id"]: a for a in json.loads((site.dist / "data.json").read_text(encoding="utf-8"))["ads"]}
        except (OSError, ValueError, KeyError):
            ads = {}
        new = comments.after(site.key, after)
        for c in new:
            a = ads.get(c["ad"], {})
            c["where"] = {k: a.get(k, "") for k in ("account", "campaign", "adGroup", "kind")}
        self._json(200, {"client": site.cfg["name"], "comments": new, "last": max([after] + [c["id"] for c in new])})

    # ------------------------------------------------------------- responses
    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def _send_page_gzipped(self):
        site = self.site
        page = site.dist / "index.html"
        mtime = page.stat().st_mtime
        if mtime not in site.gz_cache:
            site.gz_cache.clear()
            site.gz_cache[mtime] = gzip.compress(page.read_bytes(), 6)
        body = site.gz_cache[mtime]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _summary(self):
        site = self.site

        def reply(code, obj):
            self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

        if not OPENAI_KEY:
            return reply(503, {"error": "The AI summary isn't switched on yet: OPENAI_API_KEY isn't set on the server."})
        try:
            data = json.loads((site.dist / "data.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return reply(503, {"error": "The dashboard is still building. Try again in a minute."})
        account = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("account", ["All"])[0]
        key = (data["builtAt"], account)
        with site.summary_lock:
            if key not in site.summary_cache:
                try:
                    site.summary_cache[key] = write_summary(account_facts(data, account))
                except Exception as e:
                    traceback.print_exc()
                    return reply(502, {"error": f"The summary service didn't respond ({type(e).__name__}). "
                                                "Try again shortly."})
        reply(200, {"text": site.summary_cache[key], "builtAt": data["builtAt"], "model": OPENAI_MODEL})

    def _authorized(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            _, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
        except Exception:
            return False
        return hmac.compare_digest(pw, self.site.password)

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
    for s in SITES:
        s.dist.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=rebuild_loop, daemon=True).start()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), partial(Handler, directory=str(DEFAULT_SITE.dist)))
    where = (f"MULTI: {', '.join('/' + s.slug + '/' for s in SITES)} (default {DEFAULT_SITE.key}, builds in {DIST_ROOT})"
             if MULTI else f"single dashboard: {DEFAULT_SITE.key}")
    print(f"Serving on :{PORT}, {where}, rebuilding every {REBUILD_HOURS}h, "
          f"AI summary {'on' if OPENAI_KEY else 'off'}, comments "
          f"{('on, stored in ' + str(comments.DATA_DIR) + ('' if comments.PERSISTENT else ' (NOT persistent: attach a volume)')) if COMMENTS else 'off'}"
          f", comment emails {'on' if COMMENTS and NOTIFY_SECRET else 'off'}", flush=True)
    server.serve_forever()
