"""One-time helper: get a Reddit Ads refresh token (read only, scope adsread) for REDDIT_REFRESH_TOKEN.

Run it on your own computer:  python tools/reddit_token.py
1. Create a "web app" at https://www.reddit.com/prefs/apps with the redirect URI http://localhost:8080
   (signed in as a Reddit user who can see the ad account).
2. Paste the app's client ID and secret when asked (the secret is not shown as you type).
3. Your browser opens Reddit: click Allow. The refresh token is printed here; paste it into Railway, never into a chat.
Nothing is saved to disk.
"""
import base64
import getpass
import http.server
import json
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

REDIRECT = "http://localhost:8080"
UA = "ad-preview-dashboard/1.0 (reddit ads read)"


def main():
    cid = input("Reddit app client ID: ").strip()
    secret = getpass.getpass("Reddit app secret (hidden): ").strip()
    state = secrets.token_urlsafe(16)
    url = "https://www.reddit.com/api/v1/authorize?" + urllib.parse.urlencode(
        {"client_id": cid, "response_type": "code", "state": state, "redirect_uri": REDIRECT, "duration": "permanent", "scope": "adsread"})
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab and go back to the terminal.")

        def log_message(self, *a):
            pass

    if "--paste" in sys.argv:
        # Reddit blocks this network: open the link on another device (a phone on mobile data), click Allow, and paste back
        # the address it lands on (a localhost page that fails to load; the address bar still holds the code)
        print("\nOpen this link on another device or network, sign in and click Allow:\n\n" + url + "\n")
        landed = input("Then paste the full address the browser lands on (http://localhost:8080/?state=...&code=...): ").strip()
        got.update({k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(landed).query).items()})
    else:
        print("\nOpening Reddit in your browser. If it does not open, visit:\n" + url + "\n")
        webbrowser.open(url)
        srv = http.server.HTTPServer(("localhost", 8080), Handler)
        while "code" not in got and "error" not in got:
            srv.handle_request()
    if "code" not in got and "error" not in got:
        raise SystemExit("That address has no code in it; please try again.")
    if got.get("error"):
        raise SystemExit("Reddit said: " + got["error"])
    if got.get("state") != state:
        raise SystemExit("The sign-in answer did not match this run; please try again.")

    req = urllib.request.Request("https://www.reddit.com/api/v1/access_token",
                                 data=urllib.parse.urlencode({"grant_type": "authorization_code", "code": got["code"], "redirect_uri": REDIRECT}).encode(),
                                 headers={"Authorization": "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode(), "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.load(r)
    if not tok.get("refresh_token"):
        raise SystemExit("Reddit gave no refresh token: " + json.dumps({k: v for k, v in tok.items() if "token" not in k}))
    print("Scope granted:", tok.get("scope"))
    print("\nREDDIT_REFRESH_TOKEN=" + tok["refresh_token"])
    print("\nAdd that, REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET to the Ad_Dashboard variables on Railway.")


if __name__ == "__main__":
    main()
