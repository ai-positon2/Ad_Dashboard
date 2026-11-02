"""ChatGPT ads (OpenAI Ads API) for the ChatGPT view: the account's ads as they are set up now, with their campaign and ad group settings.

Read only. One ad account per key: the key named by clients.json `chatgpt_key_env` (OPENAI_ADS_API_KEY by default) is created in
ChatGPT Ads Manager under Settings and is scoped to that one account. What is read:
  GET /ad_account, /campaigns, /ad_groups?campaign_id=, /ads?ad_group_id=, and /ads/{id} for an ad with several images.
  POST /ads/{id}/preview, OpenAI's own preview (it changes nothing on the ad; the link lasts 24 hours). A refused preview drops the link only.
Archived ads are left out; paused ones are kept and labelled, since a new account often has little running.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import gads_api

BASE = os.environ.get("OPENAI_ADS_API_BASE", "https://api.ads.openai.com/v1").rstrip("/")
CACHE_VERSION = 2              # 2: pictures resolved from the ad itself, previews sent as JSON
MAX_ADS = 400
BIDDING = {"impressions": "Impressions (CPM)", "clicks": "Clicks (CPC)", "conversions": "Conversions (optimised CPC)"}
REVIEW = {"approved": "Approved", "in_review": "In review", "rejected": "Rejected"}


def key_env(client):
    return (client or {}).get("chatgpt_key_env") or "OPENAI_ADS_API_KEY"


def token(client):
    return os.environ.get(key_env(client), "").strip().strip("\"'")


def enabled(client):
    """A client is set up for ChatGPT ads when clients.json says so; the key is checked separately so a missing one is reported."""
    return bool((client or {}).get("chatgpt_ads"))


class Api:
    """Ads API calls with cursor paging and a retry on rate limits. Errors name OpenAI's own message, never the key."""

    def __init__(self, client):
        self.tok = token(client)
        if not self.tok:
            raise RuntimeError(key_env(client) + " is not set")
        self.requests, self.lock = 0, threading.Lock()

    def call(self, path, params=None, method="GET"):
        url = BASE + "/" + path.lstrip("/") + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"Authorization": "Bearer " + self.tok, "Accept": "application/json", "User-Agent": "ad-preview-dashboard"}
        if method == "POST":
            headers["Content-Type"] = "application/json"                          # the API only takes JSON bodies
        req = urllib.request.Request(url, method=method, data=b"{}" if method == "POST" else None, headers=headers)
        for attempt in range(4):
            with self.lock:
                self.requests += 1
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503) and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                try:
                    body = json.loads(e.read().decode("utf-8", "replace"))
                    err = body.get("error") if isinstance(body.get("error"), dict) else body
                    msg = err.get("message") or err.get("detail") or ""
                except (ValueError, AttributeError):
                    msg = ""
                raise RuntimeError("OpenAI answered %s: %s" % (e.code, str(msg)[:200] or "no reason given")) from None
            except urllib.error.URLError as e:
                if attempt == 3:
                    raise RuntimeError("OpenAI Ads could not be reached (%s)" % e.reason) from None
                time.sleep(3 * (attempt + 1))
        raise RuntimeError("OpenAI kept refusing the request (rate limit)")

    def pages(self, path, params=None, limit=MAX_ADS):
        out, q = [], dict(params or {}, limit=100)
        while True:
            got = self.call(path, q)
            out += got.get("data") or []
            if not got.get("has_more") or not got.get("last_id") or len(out) >= limit:
                return out[:limit]
            q["after"] = got["last_id"]


def _txt(v):
    return " ".join(str(v or "").split())


def _day(ts):
    try:
        return time.strftime("%d %b %Y", time.gmtime(int(ts))) if ts else ""
    except (TypeError, ValueError):
        return ""


def money(micros, currency=""):
    if micros in (None, ""):
        return ""
    v = int(micros) / 1e6
    sym = {"USD": "$", "GBP": "£", "EUR": "€", "INR": "₹", "AUD": "A$", "CAD": "C$"}.get(currency, "")
    return (sym or (currency + " " if currency else "")) + (f"{v:,.2f}" if v < 100 else f"{v:,.0f}")


def creative_parts(cr):
    """format (single | multi | product), title, body, price, images (urls in display order), link."""
    cr = cr or {}
    imgs = [i.get("image_url") for i in cr.get("images") or [] if isinstance(i, dict) and i.get("image_url")]
    if not imgs and cr.get("image_url"):
        imgs = [cr["image_url"]]
    kind = str(cr.get("type") or "")
    fmt = "product" if kind == "product_ad_template" else "multi" if len(cr.get("images") or cr.get("file_ids") or []) > 1 else "single"
    return {"format": fmt, "title": _txt(cr.get("title")), "body": _txt(cr.get("body")), "price": _txt(cr.get("price")), "images": imgs, "link": cr.get("target_url") or ""}


def bid_text(group, campaign, currency):
    """The ad group's bid in Ads Manager's terms: max_bid_micros is per event, so an impression bid is shown per thousand."""
    bc = (group or {}).get("bidding_config") or {}
    m, ev = bc.get("max_bid_micros"), str(bc.get("billing_event_type") or "")
    if m in (None, ""):
        return ""
    if ev == "impression":
        return money(int(m) * 1000, currency) + " max CPM"
    return money(m, currency) + (" target CPA" if (campaign or {}).get("bidding_type") == "conversions" else " max CPC")


def settings(campaign, group, currency):
    t = (campaign or {}).get("targeting") or {}
    platforms = ((t.get("platforms") or {}).get("included")) or []
    locs = ((t.get("locations") or {}).get("include")) or []
    aud = ((t.get("custom_audiences") or {}).get("ids")) or []
    excl = ((t.get("excluded_custom_audiences") or {}).get("ids")) or []
    start, end = _day(campaign.get("start_time")), _day(campaign.get("end_time"))
    return {"bidding": BIDDING.get(campaign.get("bidding_type") or "impressions", str(campaign.get("bidding_type") or "")),
            "bid": bid_text(group, campaign, currency),
            "budget": money(((campaign.get("budget") or {}).get("lifetime_spend_limit_micros")), currency),
            "schedule": (start + " to " + end) if start and end else ("From " + start) if start else ("Until " + end) if end else "No end date",
            "platforms": [str(p).replace("_", " ").capitalize() for p in platforms],
            "locations": len(locs), "audiences": len(aud), "excluded": len(excl),
            "productFeed": campaign.get("mode") == "product_feed",
            "hints": [_txt(h) for h in (group or {}).get("context_hints") or [] if _txt(h)],
            "groupNote": _txt((group or {}).get("description")), "campaignNote": _txt(campaign.get("description"))}


def preview_link(api, ad_id):
    """OpenAI's own preview: data[0].body is iframe HTML; its src is the link. Lasts 24 hours."""
    got = api.call("ads/%s/preview" % urllib.parse.quote(ad_id), method="POST")
    body = ((got.get("data") or [{}])[0] or {}).get("body") or ""
    m = re.search(r'src\s*=\s*"([^"]+)"', body) or re.search(r"src\s*=\s*'([^']+)'", body)
    return m.group(1).replace("&amp;", "&") if m and m.group(1).startswith("https://") else ""


def status_of(*objs):
    """active only when the campaign, the ad group and the ad all are; otherwise the first one that isn't."""
    for o in objs:
        s = str((o or {}).get("status") or "").lower()
        if s and s != "active":
            return s
    return "active"


def pull(client):
    api, log = Api(client), []
    account = api.call("ad_account")
    currency = account.get("currency_code") or ""
    out = []
    for camp in api.pages("campaigns"):
        if str(camp.get("status") or "").lower() == "archived":
            continue
        for grp in api.pages("ad_groups", {"campaign_id": camp["id"]}):
            if str(grp.get("status") or "").lower() == "archived":
                continue
            for ad in api.pages("ads", {"ad_group_id": grp["id"]}):
                if str(ad.get("status") or "").lower() == "archived" or len(out) >= MAX_ADS:
                    continue
                cr = ad.get("creative") or {}
                if (cr.get("file_ids") and not cr.get("images")) or (cr.get("file_id") and not cr.get("image_url")):  # the list can leave images unresolved; the ad itself has them
                    try:
                        cr = (api.call("ads/" + urllib.parse.quote(ad["id"])) or {}).get("creative") or cr
                    except RuntimeError as e:
                        log.append("The images of ad %s could not be read (%s)" % (ad["id"], str(e)[:120]))
                p = creative_parts(cr)
                if not p["images"] and p["format"] != "product" and not any("has no picture" in x for x in log):
                    log.append("Ad %s has no picture in the API answer (creative fields: %s)" % (ad["id"], ", ".join(sorted(cr)) or "none"))
                out.append(dict(p, id=ad["id"], name=_txt(ad.get("name")), created=_day(ad.get("created_at")),
                                status=status_of(camp, grp, ad), review=REVIEW.get(ad.get("review_status"), str(ad.get("review_status") or "")),
                                campaign=_txt(camp.get("name")) or camp["id"], adset=_txt(grp.get("name")) or grp["id"],
                                settings=settings(camp, grp, currency), preview=""))
    if len(out) >= MAX_ADS:
        log.append("Only the first %d ads are shown" % MAX_ADS)
    refused = 0
    for a in out:
        if a["status"] != "active" or refused >= 3:
            continue
        try:
            a["preview"] = preview_link(api, a["id"])
        except RuntimeError as e:
            refused += 1
            if refused == 3:
                log.append("OpenAI's own previews could not be made (%s), so only the drawn previews are shown" % str(e)[:160])
    now = time.strftime("%Y-%m-%d %H:%M")
    for m in log:
        print("  !", m)
    brand = _txt(account.get("brand_name")) or _txt(client.get("name")) or _txt(account.get("name"))
    return {"version": CACHE_VERSION, "pulledAt": now, "pulledTs": time.time(),
            "account": {"id": account.get("id") or "", "name": _txt(account.get("account_name")) or _txt(account.get("name")) or brand,
                        "brand": brand, "icon": account.get("preview_url") or "", "url": account.get("url") or "", "currency": currency},
            "ads": out, "log": log, "requests": api.requests}


def load_or_pull(key, client, forced=False):
    """Cached pull: the Google window, at most 12 hours so OpenAI's preview links stay valid. A failed refresh falls back to the last good cache."""
    gads_api.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = gads_api.CACHE_DIR / f"{key}.chatgpt.json"
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
    if cached and cached.get("version") != CACHE_VERSION:
        cached = None
    age = time.time() - cached.get("pulledTs", 0) if cached else None
    forced = forced or os.environ.get("GADS_FORCE") == "1"
    window = min(gads_api.CACHE_HOURS, float(os.environ.get("CHATGPT_PREVIEW_HOURS", "12"))) * 3600
    if cached and age < (gads_api.MIN_REFRESH_MINUTES * 60 if forced else window):
        print(f"ChatGPT ads: using cache from {cached['pulledAt']}")
        return cached
    try:
        data = pull(client)
    except Exception as e:
        if cached:
            print(f"ChatGPT ads refresh failed ({e}); using cache from {cached['pulledAt']}")
            return dict(cached, log=list(cached.get("log") or []) + ["The latest read failed (%s), so this is the read of %s" % (str(e)[:160], cached["pulledAt"])])
        raise
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return data
