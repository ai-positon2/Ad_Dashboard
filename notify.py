"""Email a notification for each new comment, through a Google Apps Script web app.

The script (google-apps-script/comment_notifier.gs) runs in the team's own Google account and emails the alias set
inside it, so the dashboard never holds an email password.

Env vars (set on the Railway service; never commit them)
    NOTIFY_WEBHOOK  the Apps Script web app URL (ends in /exec). Unset = no emails.
    NOTIFY_SECRET   the same secret word set as SECRET in the script
"""
import json
import os
import threading
import traceback
import urllib.request

WEBHOOK = os.environ.get("NOTIFY_WEBHOOK", "").strip()
SECRET = os.environ.get("NOTIFY_SECRET", "").strip()
ENABLED = bool(WEBHOOK and SECRET)


def describe():
    if ENABLED:
        return "comment emails on"
    if WEBHOOK or SECRET:
        return "comment emails off (set both NOTIFY_WEBHOOK and NOTIFY_SECRET)"
    return "comment emails off"


def comment_added(client, ad, name, text, link):
    """Send in the background so posting a comment never waits on Google."""
    if ENABLED:
        threading.Thread(target=_send, args=(client, ad, name, text, link), daemon=True).start()


def _send(client, ad, name, text, link):
    where = " › ".join(p for p in (ad.get("account"), ad.get("campaign"), ad.get("adGroup")) if p)
    payload = {
        "secret": SECRET,
        "subject": f"[{client} Ad Previews] {name} commented on {ad.get('adGroup') or ad.get('campaign') or 'an ad'}",
        "body": (f"{name} left a comment on the {client} ad previews dashboard.\n\n"
                 f"Ad: {where}\nType: {ad.get('kind', '')}  ·  Ad ID: {ad.get('id', '')}\n\n"
                 f"{text}\n\nOpen the ad: {link}\n"),
    }
    req = urllib.request.Request(WEBHOOK, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # Apps Script answers via a redirect; urllib follows it
            reply = r.read().decode("utf-8", "ignore")
        print(f"comment email: {reply[:120]}", flush=True)
    except Exception:
        print("comment email failed:", flush=True)
        traceback.print_exc()
