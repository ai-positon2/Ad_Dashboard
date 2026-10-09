"""Email a notification for each new comment.

Env vars (set on the Railway service; never commit them)
    NOTIFY_TO    comma-separated recipients, e.g. a team alias. Unset = no emails.
    SMTP_USER    the sending account, e.g. a Gmail address
    SMTP_PASS    its app password (Gmail: Google Account > Security > 2-Step Verification > App passwords)
    SMTP_HOST    default smtp.gmail.com
    SMTP_PORT    default 587 (STARTTLS)
    NOTIFY_FROM  optional display address, default SMTP_USER
"""
import os
import smtplib
import ssl
import threading
import traceback
from email.message import EmailMessage

TO = [a.strip() for a in os.environ.get("NOTIFY_TO", "").split(",") if a.strip()]
USER = os.environ.get("SMTP_USER", "")
PASS = os.environ.get("SMTP_PASS", "")
HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
PORT = int(os.environ.get("SMTP_PORT", "587"))
FROM = os.environ.get("NOTIFY_FROM", "") or USER
ENABLED = bool(TO and USER and PASS)


def describe():
    if ENABLED:
        return f"emails to {', '.join(TO)}"
    missing = [n for n, v in (("NOTIFY_TO", TO), ("SMTP_USER", USER), ("SMTP_PASS", PASS)) if not v]
    return "emails off" + (f" (missing {', '.join(missing)})" if TO or USER or PASS else "")


def comment_added(client, ad, name, text, link):
    """Send in the background so posting a comment never waits on the mail server."""
    if ENABLED:
        threading.Thread(target=_send, args=(client, ad, name, text, link), daemon=True).start()


def _send(client, ad, name, text, link):
    where = " › ".join(p for p in (ad.get("account"), ad.get("campaign"), ad.get("adGroup")) if p)
    msg = EmailMessage()
    msg["Subject"] = f"[{client} Ad Previews] {name} commented on {ad.get('adGroup') or ad.get('campaign') or 'an ad'}"
    msg["From"] = FROM
    msg["To"] = ", ".join(TO)
    msg.set_content(f"{name} left a comment on the {client} ad previews dashboard.\n\n"
                    f"Ad: {where}\nType: {ad.get('kind', '')}  ·  Ad ID: {ad.get('id', '')}\n\n"
                    f"{text}\n\nOpen the ad: {link}\n")
    try:
        with smtplib.SMTP(HOST, PORT, timeout=30) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(USER, PASS)
            s.send_message(msg)
        print(f"comment email sent to {', '.join(TO)}", flush=True)
    except Exception:
        print("comment email failed:", flush=True)
        traceback.print_exc()
