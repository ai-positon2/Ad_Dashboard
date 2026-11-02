"""Reddit ads (Reddit Ads API v3) for the Reddit view: the account's ads as they are set up now, with their post and ad group settings.

Read only (scope adsread). The app's REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET plus REDDIT_REFRESH_TOKEN (from tools/reddit_token.py)
are exchanged for an hour-long access token. The ad account is clients.json `reddit_account_id`; without one, the accounts the
token can see are listed and the one named like the client is used. What is read:
  POST www.reddit.com/api/v1/access_token (refresh), GET /me/businesses, /businesses/{id}/ad_accounts, /ad_accounts/{id},
  /ad_accounts/{id}/campaigns, /ad_groups, /ads, and /posts/{post_id} for each ad's creative.
Archived and deleted ads are left out; paused ones are kept and labelled.
"""
import base64
import concurrent.futures
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import gads_api

BASE = os.environ.get("REDDIT_ADS_API_BASE", "https://ads-api.reddit.com/api/v3").rstrip("/")
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
UA = "ad-preview-dashboard/1.0 (reddit ads read)"
CACHE_VERSION = 1
MAX_ADS = 400
ENV = ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_REFRESH_TOKEN")
OBJECTIVE = {"APP_INSTALLS": "App installs", "BRAND_AWARENESS": "Brand awareness", "CATALOG_SALES": "Catalog sales", "CLICKS": "Traffic",
             "CONVERSIONS": "Conversions", "IMPRESSIONS": "Reach", "LEAD_GENERATION": "Lead generation", "SALES": "Sales",
             "VIDEO_VIEWABLE_IMPRESSIONS": "Video views"}
STRATEGY = {"BIDLESS": "Automatic", "MAXIMIZE_VOLUME": "Lowest cost", "TARGET_CPX": "Cost cap", "MANUAL_BIDDING": "Manual bid"}
GONE = ("ARCHIVED", "DELETED")


def _env(name):
    return os.environ.get(name, "").strip().strip("\"'")


def missing():
    """The Railway variables that are not set, in order."""
    return [n for n in ENV if not _env(n)]


def enabled(client):
    """A client is set up for Reddit ads when clients.json says so; the variables are checked separately so a missing one is reported."""
    c = client or {}
    return bool(c.get("reddit_ads") or c.get("reddit_account_id"))


class Api:
    """Ads API calls with an access token from the refresh token, paging by next_url, and a retry on rate limits. Errors never name a secret."""

    def __init__(self):
        if missing():
            raise RuntimeError(", ".join(missing()) + " not set")
        self.requests, self.lock, self.tok = 0, threading.Lock(), self._access_token()

    def _open(self, req):
        with self.lock:
            self.requests += 1
        return urllib.request.urlopen(req, timeout=60)

    def _access_token(self):
        basic = base64.b64encode((_env("REDDIT_CLIENT_ID") + ":" + _env("REDDIT_CLIENT_SECRET")).encode()).decode()
        req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": _env("REDDIT_REFRESH_TOKEN")}).encode(),
                                     headers={"Authorization": "Basic " + basic, "User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self._open(req) as r:
                got = json.load(r)
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            hint = " (Reddit blocked this server's network)" if "blocked" in text.lower() else ""
            raise RuntimeError("Reddit refused the refresh token: HTTP %s%s" % (e.code, hint)) from None
        except urllib.error.URLError as e:
            raise RuntimeError("Reddit could not be reached (%s)" % e.reason) from None
        except ValueError:
            raise RuntimeError("Reddit answered the token request with a page, not JSON (it may block this server's network)") from None
        if not got.get("access_token"):
            raise RuntimeError("Reddit gave no access token: %s" % str(got.get("error") or "no reason given")[:120])
        return got["access_token"]

    def call(self, path, params=None):
        url = path if path.startswith("https://") else BASE + "/" + path.lstrip("/") + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.tok, "Accept": "application/json", "User-Agent": UA})
        for attempt in range(4):
            try:
                with self._open(req) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503) and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                try:
                    body = json.loads(e.read().decode("utf-8", "replace"))
                    err = body.get("error") if isinstance(body.get("error"), dict) else body
                    msg = err.get("message") or err.get("explanation") or err.get("reason") or ""
                except (ValueError, AttributeError):
                    msg = ""
                raise RuntimeError("Reddit answered %s: %s" % (e.code, str(msg)[:200] or "no reason given")) from None
            except urllib.error.URLError as e:
                if attempt == 3:
                    raise RuntimeError("Reddit Ads could not be reached (%s)" % e.reason) from None
                time.sleep(3 * (attempt + 1))
        raise RuntimeError("Reddit kept refusing the request (rate limit)")

    def pages(self, path, params=None, limit=5000):
        out, got = [], self.call(path, dict(params or {}, **{"page.size": 1000}))
        while True:
            out += got.get("data") or []
            nxt = (got.get("pagination") or {}).get("next_url")
            if not nxt or len(out) >= limit:
                return out[:limit]
            got = self.call(nxt)


def _txt(v):
    return " ".join(str(v or "").split())


def _day(iso):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(iso or ""))
    return time.strftime("%d %b %Y", time.strptime(m.group(0), "%Y-%m-%d")) if m else ""


def _ts(iso):
    try:
        return time.mktime(time.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except (TypeError, ValueError):
        return 0


def money(micros, currency=""):
    if micros in (None, ""):
        return ""
    v = int(micros) / 1e6
    sym = {"USD": "$", "GBP": "£", "EUR": "€", "INR": "₹", "AUD": "A$", "CAD": "C$"}.get(currency, "")
    return (sym or (currency + " " if currency else "")) + (f"{v:,.2f}" if v < 100 else f"{v:,.0f}")


def find_account(api, client):
    """clients.json reddit_account_id, else the one ad account the token sees, else the one named like the client."""
    want = str(client.get("reddit_account_id") or "").strip()
    if want:
        return (api.call("ad_accounts/" + urllib.parse.quote(want)) or {}).get("data") or {"id": want}
    accounts = []
    for b in api.pages("me/businesses"):
        accounts += api.pages("businesses/%s/ad_accounts" % urllib.parse.quote(b["id"]))
    accounts = [a for a in accounts if a.get("id")]
    if len(accounts) == 1:
        return accounts[0]
    name = re.sub(r"[^a-z0-9]", "", str(client.get("name") or "").lower())
    named = [a for a in accounts if name and name in re.sub(r"[^a-z0-9]", "", str(a.get("name") or "").lower())]
    if len(named) == 1:
        return named[0]
    seen = "; ".join("%s (%s)" % (a.get("name"), a["id"]) for a in accounts[:12]) or "none"
    raise RuntimeError("set reddit_account_id in clients.json; the Reddit sign-in sees these ad accounts: " + seen)


def post_parts(post, ad):
    """format (image | video | carousel | text | product), headline, body, cta, link, display url, images, cards, video."""
    post = post or {}
    content = [c for c in post.get("content") or [] if isinstance(c, dict)]
    first = content[0] if content else {}
    kind = str(post.get("type") or "").lower() or ("product" if ad.get("shopping_creative") else "text")
    shop = ad.get("shopping_creative") or {}
    dest = ad.get("destination") or {}
    link = ad.get("click_url") or first.get("destination_url") or dest.get("url") or shop.get("destination_url") or ""
    out = {"format": kind, "headline": _txt(post.get("headline") or shop.get("headline")), "body": _txt(post.get("body")),
           "cta": _txt(first.get("call_to_action") or shop.get("call_to_action")), "link": link,
           "displayUrl": _txt(first.get("display_url") or dest.get("display_url")), "images": [], "cards": [], "video": None,
           "allowComments": bool(post.get("allow_comments", shop.get("allow_comments", True)))}
    if kind == "carousel":
        out["cards"] = [{"image": c.get("media_url") or "", "caption": _txt(c.get("caption")), "link": c.get("destination_url") or "",
                         "cta": _txt(c.get("call_to_action")), "displayUrl": _txt(c.get("display_url"))} for c in content]
        out["images"] = [c["image"] for c in out["cards"] if c["image"]]
    elif kind == "video":
        out["video"] = {"url": first.get("media_url") or "", "thumb": post.get("thumbnail_url") or ""}
        out["images"] = [post["thumbnail_url"]] if post.get("thumbnail_url") else []
    elif kind == "image":
        out["images"] = [c.get("media_url") for c in content if c.get("media_url")][:1]
    return out


def settings(campaign, group, currency):
    """Objective, bid and budget in Ads Manager's words, the schedule, and what the ad group targets."""
    t = (group or {}).get("targeting") or {}
    cbo = bool(campaign.get("is_campaign_budget_optimization"))
    owner = campaign if cbo else group
    bid_src = campaign if cbo and campaign.get("bid_strategy") else group
    strat, bid_type = bid_src.get("bid_strategy"), bid_src.get("bid_type") or ""
    bid = money(bid_src.get("bid_value"), currency)
    goal = {"DAILY_SPEND": " daily", "LIFETIME_SPEND": " lifetime"}.get(owner.get("goal_type") or "", "")
    start, end = _day(group.get("start_time") or campaign.get("start_time")), _day(group.get("end_time") or campaign.get("end_time"))
    devices = sorted({str(d["type"]).capitalize() + (" " + {"IOS": "iOS", "ANDROID": "Android"}.get(d.get("os"), "") if d.get("os") else "")
                      for d in t.get("devices") or [] if isinstance(d, dict) and d.get("type")})
    lst = lambda k: [_txt(x.get("name") if isinstance(x, dict) else x) for x in t.get(k) or [] if _txt(x.get("name") if isinstance(x, dict) else x)]  # noqa: E731
    return {"objective": OBJECTIVE.get(campaign.get("objective"), _txt(campaign.get("objective")).capitalize()),
            "strategy": STRATEGY.get(strat, _txt(strat).replace("_", " ").capitalize()),
            "bid": (bid + " " + bid_type) if bid and strat not in ("BIDLESS", "MAXIMIZE_VOLUME") else "",
            "budget": (money(owner.get("goal_value"), currency) + goal) if owner.get("goal_value") else "",
            "budgetLevel": "campaign" if cbo else "ad group",
            "spendCap": money(campaign.get("spend_cap"), currency),
            "schedule": (start + " to " + end) if start and end else ("From " + start) if start else ("Until " + end) if end else "No end date",
            "goal": _txt(group.get("optimization_goal") or campaign.get("optimization_goal")).replace("_", " ").capitalize(),
            "communities": lst("communities"), "keywords": lst("keywords"), "interests": len(t.get("interests") or []),
            "locations": lst("geolocations") or lst("locations"), "devices": devices,
            "audiences": len(t.get("custom_audience_ids") or []), "excluded": len(t.get("excluded_custom_audience_ids") or []),
            "expand": bool(t.get("expand_targeting"))}


def status_of(camp, grp, ad):
    """active when Reddit says the ad is delivering; otherwise its own status, in words."""
    s = str(ad.get("effective_status") or ad.get("configured_status") or "").upper()
    if s == "ACTIVE" and all(str(o.get("configured_status") or "ACTIVE").upper() == "ACTIVE" for o in (camp, grp)):
        return "active"
    return {"CAMPAIGN_PAUSED": "campaign paused", "AD_GROUP_PAUSED": "ad group paused", "PENDING_APPROVAL": "in review"}.get(s, s.replace("_", " ").lower() or "paused")


def pull(client):
    api, log = Api(), []
    account = find_account(api, client)
    acct = account["id"]
    currency = account.get("currency") or ""
    base = "ad_accounts/" + urllib.parse.quote(acct)
    camps = {c["id"]: c for c in api.pages(base + "/campaigns") if str(c.get("configured_status") or "").upper() not in GONE}
    groups = {g["id"]: g for g in api.pages(base + "/ad_groups") if str(g.get("configured_status") or "").upper() not in GONE and g.get("campaign_id") in camps}
    raw = [a for a in api.pages(base + "/ads") if str(a.get("configured_status") or "").upper() not in GONE and a.get("ad_group_id") in groups]
    if len(raw) > MAX_ADS:
        log.append("Only the first %d ads are shown" % MAX_ADS)
        raw = raw[:MAX_ADS]
    posts, failed = {}, []

    def read_post(pid):
        try:
            posts[pid] = (api.call("posts/" + urllib.parse.quote(pid)) or {}).get("data") or {}
        except RuntimeError as e:
            failed.append(str(e))

    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        list(ex.map(read_post, sorted({a["post_id"] for a in raw if a.get("post_id")})))
    if failed:
        log.append("%d ad posts could not be read (%s)" % (len(failed), failed[0][:120]))
    out, now = [], time.time()
    for ad in raw:
        grp = groups[ad["ad_group_id"]]
        camp = camps.get(ad.get("campaign_id")) or camps[grp["campaign_id"]]
        p = post_parts(posts.get(ad.get("post_id")), ad)
        if not p["images"] and p["format"] in ("image", "carousel", "video") and not any("has no picture" in x for x in log):
            log.append("Ad %s has no picture in the API answer" % ad["id"])
        preview = ad.get("preview_url") if ad.get("preview_url") and _ts(ad.get("preview_expiry")) > now else ""
        out.append(dict(p, id=ad["id"], name=_txt(ad.get("name")), created=_day(ad.get("created_at")), status=status_of(camp, grp, ad),
                        rejection=_txt(ad.get("rejection_reason")).replace("_", " ").capitalize(), campaign=_txt(camp.get("name")) or camp["id"],
                        adset=_txt(grp.get("name")) or grp["id"], profile=_txt(ad.get("profile_username")), postUrl=ad.get("post_url") or "",
                        settings=settings(camp, grp, currency), preview=preview))
    for m in log:
        print("  !", m)
    return {"version": CACHE_VERSION, "pulledAt": time.strftime("%Y-%m-%d %H:%M"), "pulledTs": time.time(),
            "account": {"id": acct, "name": _txt(account.get("name")) or acct, "currency": currency},
            "ads": out, "log": log, "requests": api.requests}


def load_or_pull(key, client, forced=False):
    """Cached pull on the Google window. A failed refresh falls back to the last good cache."""
    gads_api.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = gads_api.CACHE_DIR / f"{key}.reddit.json"
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
    if cached and age < (gads_api.MIN_REFRESH_MINUTES * 60 if forced else gads_api.CACHE_HOURS * 3600):
        print(f"Reddit ads: using cache from {cached['pulledAt']}")
        return cached
    try:
        data = pull(client)
    except Exception as e:
        if cached:
            print(f"Reddit ads refresh failed ({e}); using cache from {cached['pulledAt']}")
            return dict(cached, log=list(cached.get("log") or []) + ["The latest read failed (%s), so this is the read of %s" % (str(e)[:160], cached["pulledAt"])])
        raise
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return data
