"""Google Ads API source for the dashboard (replaces the Google Ads Scripts + Supermetrics tabs).

`fetch_client(client)` returns the same tables the scripts wrote into the sheet ("RSA Combinations",
"PMax Combinations", "Live Assets", "Campaign Performance") as DataFrames with the same columns, so
build.py reads them exactly as it reads the sheet. It also returns ad strength and PMax search themes
(which used to come from Supermetrics).

Credentials come from env vars (Railway) or ~/.gads_creds.json (local); never commit them:
    GADS_CLIENT_ID  GADS_CLIENT_SECRET  GADS_DEVELOPER_TOKEN  GADS_REFRESH_TOKEN  GADS_LOGIN_CUSTOMER_ID

Explorer access allows ~2,880 operations a day (one search request = one operation), so a full pull is
cached on disk (GADS_CACHE_HOURS, default 12) and shared by every rebuild in that window.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pandas as pd

API = "https://googleads.googleapis.com/v24"
PREVIEW_API = "https://googleads.googleapis.com/v23"  # v24 changed the shareable-preview request shape; v23 is documented and works
DATE_RANGE = "LAST_30_DAYS"
MAX_RSA_COMBOS = 20
MAX_PMAX_COMBOS = 20
ASSET_BATCH = 400
WORKERS = 4  # accounts pulled in parallel; kept low to stay well inside Google's rate limits
ROOT = Path(__file__).parent
# Keep the cache on a Railway volume when one is attached, so a redeploy doesn't throw it away and re-spend the quota.
_VOLUME = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
CACHE_DIR = Path(os.environ.get("GADS_CACHE_DIR") or (Path(_VOLUME) / "gads_cache" if _VOLUME else ROOT / "cache"))
CACHE_HOURS = float(os.environ.get("GADS_CACHE_HOURS", "12"))
CACHE_VERSION = 2  # bump when the pulled data gains a field, so older caches are discarded instead of reused
MIN_REFRESH_MINUTES = float(os.environ.get("GADS_MIN_REFRESH_MINUTES", "10"))  # floor between manual refreshes
FORCE = threading.Event()  # set by the dashboard's "Refresh" link: pull again even inside the cache window

RSA_HEADER = ["Account", "Customer ID", "Campaign", "Ad group", "Ad ID", "Rank", "Impressions", "Enabled",
              "Final URL", "Path 1", "Path 2", "Headline 1", "Headline 2", "Headline 3", "Description 1",
              "Description 2", "Assets JSON", "Date range", "Exported at"]
PMAX_HEADER = ["Account", "Customer ID", "Campaign", "Asset group", "Asset group ID", "Category", "Rank",
               "Final URL", "Path 1", "Path 2", "Headlines", "Long headline", "Descriptions", "Business name",
               "Call to action", "Images", "Logos", "Videos", "Assets JSON", "Date range", "Exported at"]
LIVE_HEADER = ["Account", "Customer ID", "Level", "Campaign", "Channel", "Ad group / asset group", "Owner ID",
               "Field", "Asset type", "Text", "Lines", "Image URL", "Video ID", "Pinned", "Final URL", "Path 1",
               "Path 2", "Exported at"]
PERF_HEADER = ["Account", "Customer ID", "Currency", "Campaign", "Channel", "Status", "Live ads", "Cost 30d",
               "Clicks 30d", "Impressions 30d", "Conversions 30d", "Conv. value 30d", "Cost MTD", "Clicks MTD",
               "Impressions MTD", "Conversions MTD", "Conv. value MTD", "Exported at"]


# ----------------------------------------------------------------- credentials
def _creds():
    clean = lambda v: v.strip().strip("\"'").strip()  # pasted values often carry spaces, newlines or quotes
    env = {k: clean(os.environ.get(f"GADS_{k.upper()}", "")) for k in
           ("client_id", "client_secret", "developer_token", "refresh_token", "login_customer_id")}
    if all(env[k] for k in ("client_id", "client_secret", "developer_token", "refresh_token")):
        return env
    p = Path.home() / ".gads_creds.json"
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if all(d.get(k) for k in ("client_id", "client_secret", "developer_token", "refresh_token")):
                return {**d, "login_customer_id": env["login_customer_id"] or d.get("login_customer_id", "")}
        except (OSError, ValueError):
            pass
    return None


def available():
    return _creds() is not None


class Api:
    """Thin REST client: token refresh, pagination, retry on rate limits, request counter."""

    def __init__(self, login_customer_id=""):
        self.c = _creds()
        if not self.c:
            raise RuntimeError("Google Ads API credentials not set (GADS_* env vars or ~/.gads_creds.json)")
        self.login = re.sub(r"\D", "", login_customer_id or self.c.get("login_customer_id", ""))
        self.token, self.token_at, self.lock, self.requests = "", 0.0, threading.Lock(), 0

    def _access_token(self):
        with self.lock:
            if self.token and time.time() - self.token_at < 2700:
                return self.token
            data = urllib.parse.urlencode({
                "client_id": self.c["client_id"], "client_secret": self.c["client_secret"],
                "refresh_token": self.c["refresh_token"], "grant_type": "refresh_token"}).encode()
            try:
                self.token = json.load(urllib.request.urlopen("https://oauth2.googleapis.com/token", data, timeout=30))["access_token"]
            except urllib.error.HTTPError as e:  # say why Google refused (error name only, never the credentials)
                try:
                    body = json.loads(e.read().decode("utf-8", "replace"))
                    why = f'{body.get("error", "")}: {body.get("error_description", "")}'.strip(": ")
                except ValueError:
                    why = ""
                raise RuntimeError(f"Google OAuth token refresh rejected (HTTP {e.code}) {why} - check GADS_CLIENT_ID, "
                                   "GADS_CLIENT_SECRET and GADS_REFRESH_TOKEN") from None
            self.token_at = time.time()
            return self.token

    def _post(self, cid, body, path="/googleAds:search", api=API):
        headers = {"Authorization": "Bearer " + self._access_token(), "developer-token": self.c["developer_token"],
                   "Content-Type": "application/json"}
        if self.login:
            headers["login-customer-id"] = self.login
        for attempt in range(5):
            req = urllib.request.Request(f"{api}/customers/{cid}{path}", json.dumps(body).encode(),
                                         headers, method="POST")
            try:
                self.requests += 1
                return json.load(urllib.request.urlopen(req, timeout=120))
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                if e.code == 429:
                    wait = re.search(r"[Rr]etry in (\d+) seconds", text)
                    if wait and int(wait.group(1)) > 120:  # daily quota used up: waiting won't help, fail fast
                        raise ApiError(429, text) from None
                if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise ApiError(e.code, text) from None
            except (urllib.error.URLError, TimeoutError):
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)

    def search(self, cid, query):
        """All rows of a GAQL query (follows pagination)."""
        out, body = [], {"query": query}
        while True:
            r = self._post(cid, body)
            out += r.get("results", [])
            if not r.get("nextPageToken"):
                return out
            body = {"query": query, "pageToken": r["nextPageToken"]}


class ApiError(Exception):
    def __init__(self, code, text):
        msgs = re.findall(r'"message": "([^"]+)"', text)
        super().__init__(f"HTTP {code}: {msgs[-1] if msgs else text[:200]}")
        self.code, self.text = code, text


def g(d, *path, default=""):
    """d['a']['b'] without KeyErrors."""
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return default
        d = d[p]
    return d if d is not None else default


def fmt_cid(cid):
    s = re.sub(r"\D", "", str(cid))
    return f"{s[:3]}-{s[3:6]}-{s[6:]}" if len(s) == 10 else s


# -------------------------------------------------------------------- accounts
def list_accounts(api, mcc_id):
    """Enabled, non-manager accounts under an MCC (any depth)."""
    rows = api.search(re.sub(r"\D", "", mcc_id),
                      "SELECT customer_client.id, customer_client.descriptive_name, customer_client.currency_code, "
                      "customer_client.time_zone FROM customer_client "
                      "WHERE customer_client.manager = FALSE AND customer_client.status = 'ENABLED'")
    return [{"id": g(r, "customerClient", "id"), "name": g(r, "customerClient", "descriptiveName"),
             "currency": g(r, "customerClient", "currencyCode"), "tz": g(r, "customerClient", "timeZone")}
            for r in rows]


# ---------------------------------------------------------------- asset lookup
def load_assets(api, cid, names, log):
    """resource name -> {type, text, url, video, cta, lines}. Only the referenced assets are fetched."""
    names = sorted(set(names))
    out = {}

    def batches(extra_where, fields, handle, label, subset=None):
        todo = subset if subset is not None else names
        try:
            for i in range(0, len(todo), ASSET_BATCH):
                in_list = ", ".join(f"'{n}'" for n in todo[i:i + ASSET_BATCH])
                for r in api.search(cid, f"SELECT asset.resource_name, asset.type, {fields} FROM asset "
                                         f"WHERE asset.resource_name IN ({in_list}){extra_where}"):
                    handle(r["asset"])
        except ApiError as e:
            log(f'asset lookup "{label}" failed (continuing without it): {e}')

    def base(a):
        out[a["resourceName"]] = {
            "type": a.get("type", ""), "text": g(a, "textAsset", "text"),
            "url": g(a, "imageAsset", "fullSize", "url"), "video": g(a, "youtubeVideoAsset", "youtubeVideoId"),
            "cta": g(a, "callToActionAsset", "callToAction")}
    batches("", "asset.text_asset.text, asset.image_asset.full_size.url, asset.youtube_video_asset.youtube_video_id, "
                "asset.call_to_action_asset.call_to_action", base, "base")

    def of_type(t):
        return [n for n in names if out.get(n, {}).get("type") == t]

    def entry(a):
        return out.setdefault(a["resourceName"], {"type": a.get("type", "")})

    def sitelink(a):
        s, e = a.get("sitelinkAsset", {}), entry(a)
        e["text"], e["lines"] = s.get("linkText", ""), [x for x in (s.get("description1"), s.get("description2")) if x]

    def callout(a):
        entry(a)["text"] = g(a, "calloutAsset", "calloutText")

    def snippet(a):
        s = a.get("structuredSnippetAsset", {})
        entry(a)["text"] = f'{s["header"]}: {", ".join(s.get("values", []))}' if s.get("header") else ""

    for t, fields, handle in (
            ("SITELINK", "asset.sitelink_asset.link_text, asset.sitelink_asset.description1, asset.sitelink_asset.description2", sitelink),
            ("CALLOUT", "asset.callout_asset.callout_text", callout),
            ("STRUCTURED_SNIPPET", "asset.structured_snippet_asset.header, asset.structured_snippet_asset.values", snippet)):
        sub = of_type(t)
        if sub:
            batches("", fields, handle, t.lower(), sub)
    return out


def resolve(usages, assets):
    out = []
    for u in usages or []:
        a = assets.get(u.get("asset"), {})
        o = {"field": u.get("servedAssetFieldType", ""), "type": a.get("type", ""),
             "text": a.get("text") or a.get("cta") or "", "url": a.get("url", ""), "video": a.get("video", "")}
        if a.get("lines"):
            o["lines"] = a["lines"]
        out.append(o)
    return out


# ------------------------------------------------------------- combinations
def combinations(api, acct, now, log):
    cid, name = acct["id"], acct["name"]
    rsa, pmax = [], []
    by_ad = {}
    try:
        for r in api.search(cid, "SELECT campaign.name, ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.final_urls, "
                                 "ad_group_ad.ad.responsive_search_ad.path1, ad_group_ad.ad.responsive_search_ad.path2, "
                                 "ad_group_ad_asset_combination_view.served_assets, ad_group_ad_asset_combination_view.enabled, "
                                 "metrics.impressions FROM ad_group_ad_asset_combination_view "
                                 f"WHERE segments.date DURING {DATE_RANGE} AND metrics.impressions > 0 "
                                 "AND campaign.advertising_channel_type = 'SEARCH' AND campaign.status = 'ENABLED' "
                                 "AND ad_group.status = 'ENABLED' AND ad_group_ad.status = 'ENABLED'"):
            ad, view = r["adGroupAd"]["ad"], r["adGroupAdAssetCombinationView"]
            rs = ad.get("responsiveSearchAd", {})
            by_ad.setdefault(ad["id"], []).append({
                "campaign": r["campaign"]["name"], "adGroup": r["adGroup"]["name"], "adId": ad["id"],
                "impressions": int(g(r, "metrics", "impressions", default=0) or 0), "enabled": view.get("enabled", False),
                "finalUrl": (ad.get("finalUrls") or [""])[0], "path1": rs.get("path1", ""), "path2": rs.get("path2", ""),
                "usages": view.get("servedAssets", [])})
    except ApiError as e:
        log(f"RSA combination query failed: {e}")
    for rows in by_ad.values():
        rows.sort(key=lambda c: -c["impressions"])
        for i, c in enumerate(rows[:MAX_RSA_COMBOS], 1):
            c["rank"] = i
            rsa.append(c)
    try:
        for p in api.search(cid, "SELECT campaign.name, asset_group.id, asset_group.name, asset_group.final_urls, "
                                 "asset_group.path1, asset_group.path2, asset_group_top_combination_view.resource_name, "
                                 "asset_group_top_combination_view.asset_group_top_combinations "
                                 f"FROM asset_group_top_combination_view WHERE segments.date DURING {DATE_RANGE} "
                                 "AND campaign.status = 'ENABLED' AND asset_group.status = 'ENABLED'"):
            ag, tv = p["assetGroup"], p["assetGroupTopCombinationView"]
            category = str(tv.get("resourceName", "")).split("~")[-1]
            for i, combo in enumerate(tv.get("assetGroupTopCombinations", [])[:MAX_PMAX_COMBOS], 1):
                pmax.append({"campaign": p["campaign"]["name"], "assetGroup": ag["name"], "assetGroupId": ag["id"],
                             "category": category, "rank": i, "finalUrl": (ag.get("finalUrls") or [""])[0],
                             "path1": ag.get("path1", ""), "path2": ag.get("path2", ""),
                             "usages": combo.get("assetCombinationServedAssets", [])})
    except ApiError as e:
        log(f"PMax top-combination query failed: {e}")

    needed = {u["asset"] for c in rsa + pmax for u in c["usages"] if u.get("asset")}
    assets = load_assets(api, cid, needed, log) if needed else {}
    cname, cfid = name, fmt_cid(cid)
    rsa_rows, pmax_rows = [], []
    for c in rsa:
        served = resolve(c["usages"], assets)
        pick = lambda f: next((s["text"] for s in served if s["field"] == f), "")
        rsa_rows.append([cname, cfid, c["campaign"], c["adGroup"], c["adId"], c["rank"], c["impressions"], c["enabled"],
                         c["finalUrl"], c["path1"], c["path2"], pick("HEADLINE_1"), pick("HEADLINE_2"), pick("HEADLINE_3"),
                         pick("DESCRIPTION_1"), pick("DESCRIPTION_2"), json.dumps(served), DATE_RANGE, now])
    for c in pmax:
        served = resolve(c["usages"], assets)
        lst = lambda rx: " | ".join(s["text"] or s["url"] or s["video"] or "" for s in served if re.search(rx, s["field"]))
        pmax_rows.append([cname, cfid, c["campaign"], c["assetGroup"], c["assetGroupId"], c["category"], c["rank"],
                          c["finalUrl"], c["path1"], c["path2"], lst(r"^HEADLINE"), lst(r"^LONG_HEADLINE"),
                          lst(r"^DESCRIPTION"), lst(r"^BUSINESS_NAME"), lst(r"^CALL_TO_ACTION"), lst(r"IMAGE$"),
                          lst(r"LOGO"), lst(r"VIDEO"), json.dumps(served), DATE_RANGE, now])
    return rsa_rows, pmax_rows


# -------------------------------------------------------------- live assets
LIVE = " AND campaign.status = 'ENABLED' AND ad_group.status = 'ENABLED' AND ad_group_ad.status = 'ENABLED'"
EXT_TYPES = ("TEXT", "IMAGE", "SITELINK", "CALLOUT", "STRUCTURED_SNIPPET", "PRICE", "PROMOTION", "CALL", "MOBILE_APP")


def _pretty(s):
    return str(s or "").lower().replace("_", " ")


def _money(m):
    return f'{int(m["amountMicros"]) / 1e6:,.0f} {m.get("currencyCode", "")}'.strip() if m and m.get("amountMicros") else ""


def _ext_fields(a):
    """asset -> (text, lines, img) for each extension asset type."""
    t = a.get("type")
    if t in ("TEXT", "IMAGE"):
        return g(a, "textAsset", "text"), [], g(a, "imageAsset", "fullSize", "url")
    if t == "SITELINK":
        s = a.get("sitelinkAsset", {})
        return s.get("linkText", ""), [s.get("description1"), s.get("description2")], ""
    if t == "CALLOUT":
        return g(a, "calloutAsset", "calloutText"), [], ""
    if t == "STRUCTURED_SNIPPET":
        s = a.get("structuredSnippetAsset", {})
        return (f'{s["header"]}: {", ".join(s.get("values", []))}' if s.get("header") else ""), [], ""
    if t == "PRICE":
        p, lines = a.get("priceAsset", {}), []
        for o in p.get("priceOfferings", []):
            unit = o.get("unit")
            unit = " " + _pretty(unit).replace("per ", "/ ") if unit and unit != "UNSPECIFIED" else ""
            lines.append(f'{o.get("header", "")}: {_money(o.get("price"))}{unit}' + (f' — {o["description"]}' if o.get("description") else ""))
        return _pretty(p.get("type")) or "prices", lines, ""
    if t == "PROMOTION":
        p = a.get("promotionAsset", {})
        off = f'{int(p["percentOff"]) / 10000:g}% off' if p.get("percentOff") else ""
        occ = _pretty(p["occasion"]) if p.get("occasion") not in (None, "UNSPECIFIED") else ""
        return " ".join(x for x in (off, p.get("promotionTarget")) if x), [f'Code {p["promotionCode"]}' if p.get("promotionCode") else "", occ], ""
    if t == "CALL":
        c = a.get("callAsset", {})
        return c.get("phoneNumber", ""), [c.get("countryCode", "")], ""
    if t == "MOBILE_APP":
        m = a.get("mobileAppAsset", {})
        return m.get("linkText", ""), [m.get("appId", "")], ""
    return "", [], ""


ASSET_SELECT = ("asset.type, asset.text_asset.text, asset.image_asset.full_size.url, asset.sitelink_asset.link_text, "
                "asset.sitelink_asset.description1, asset.sitelink_asset.description2, asset.callout_asset.callout_text, "
                "asset.structured_snippet_asset.header, asset.structured_snippet_asset.values, asset.price_asset.type, "
                "asset.price_asset.price_offerings, asset.promotion_asset.promotion_target, "
                "asset.promotion_asset.percent_off, asset.promotion_asset.promotion_code, asset.promotion_asset.occasion, "
                "asset.call_asset.phone_number, asset.call_asset.country_code, asset.mobile_app_asset.link_text, "
                "asset.mobile_app_asset.app_id")


def live_assets(api, acct, now, log):
    cid, name = acct["id"], acct["name"]
    cfid, rows, pending = fmt_cid(cid), [], []

    def row(level="", campaign="", channel="", group="", owner="", field="", type_="", text="", lines=(), img="",
            video="", pinned="", url="", p1="", p2=""):
        r = [name, cfid, level, campaign, channel, group, str(owner), field, type_, text or "",
             " | ".join(x for x in lines if x), img or "", video or "", pinned or "", url or "", p1 or "", p2 or "", now]
        rows.append(r)
        return r

    def guarded(label, fn):
        try:
            fn()
        except ApiError as e:
            log(f"{label} query failed (continuing): {e}")

    def ad_base(r):
        ad = r["adGroupAd"]["ad"]
        return dict(level="AD", campaign=r["campaign"]["name"], channel=r["campaign"].get("advertisingChannelType", ""),
                    group=r["adGroup"]["name"], owner=ad["id"], url=(ad.get("finalUrls") or [""])[0])

    def texts(base, lst, field):
        for t in lst or []:
            if t.get("text"):
                row(**base, field=field, type_="TEXT", text=t["text"])

    def refs(base, lst, field):
        for x in lst or []:
            if x.get("asset"):
                pending.append((x["asset"], row(**base, field=field, type_="YOUTUBE_VIDEO" if field == "YOUTUBE_VIDEO" else "IMAGE")))

    def rsa():
        for r in api.search(cid, "SELECT campaign.name, campaign.advertising_channel_type, ad_group.name, ad_group_ad.ad.id, "
                                 "ad_group_ad.ad.final_urls, ad_group_ad.ad.responsive_search_ad.headlines, "
                                 "ad_group_ad.ad.responsive_search_ad.descriptions, ad_group_ad.ad.responsive_search_ad.path1, "
                                 "ad_group_ad.ad.responsive_search_ad.path2 FROM ad_group_ad "
                                 "WHERE ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'" + LIVE):
            rs, base = r["adGroupAd"]["ad"].get("responsiveSearchAd", {}), ad_base(r)
            base.update(p1=rs.get("path1", ""), p2=rs.get("path2", ""))
            for h in rs.get("headlines", []):
                row(**base, field="HEADLINE", type_="TEXT", text=h.get("text"), pinned=h.get("pinnedField"))
            for d in rs.get("descriptions", []):
                row(**base, field="DESCRIPTION", type_="TEXT", text=d.get("text"), pinned=d.get("pinnedField"))

    def dg_image():
        for r in api.search(cid, "SELECT campaign.name, campaign.advertising_channel_type, ad_group.name, ad_group_ad.ad.id, "
                                 "ad_group_ad.ad.final_urls, ad_group_ad.ad.demand_gen_multi_asset_ad.headlines, "
                                 "ad_group_ad.ad.demand_gen_multi_asset_ad.descriptions, "
                                 "ad_group_ad.ad.demand_gen_multi_asset_ad.marketing_images, "
                                 "ad_group_ad.ad.demand_gen_multi_asset_ad.square_marketing_images, "
                                 "ad_group_ad.ad.demand_gen_multi_asset_ad.portrait_marketing_images, "
                                 "ad_group_ad.ad.demand_gen_multi_asset_ad.logo_images, "
                                 "ad_group_ad.ad.demand_gen_multi_asset_ad.business_name, "
                                 "ad_group_ad.ad.demand_gen_multi_asset_ad.call_to_action_text FROM ad_group_ad "
                                 "WHERE ad_group_ad.ad.type = 'DEMAND_GEN_MULTI_ASSET_AD'" + LIVE):
            dg, base = r["adGroupAd"]["ad"].get("demandGenMultiAssetAd", {}), ad_base(r)
            texts(base, dg.get("headlines"), "HEADLINE")
            texts(base, dg.get("descriptions"), "DESCRIPTION")
            refs(base, dg.get("marketingImages"), "MARKETING_IMAGE")
            refs(base, dg.get("squareMarketingImages"), "SQUARE_MARKETING_IMAGE")
            refs(base, dg.get("portraitMarketingImages"), "PORTRAIT_MARKETING_IMAGE")
            refs(base, dg.get("logoImages"), "LOGO")
            if dg.get("businessName"):
                row(**base, field="BUSINESS_NAME", type_="TEXT", text=dg["businessName"])
            if dg.get("callToActionText"):
                row(**base, field="CALL_TO_ACTION", type_="TEXT", text=dg["callToActionText"])

    def dg_video():
        for r in api.search(cid, "SELECT campaign.name, campaign.advertising_channel_type, ad_group.name, ad_group_ad.ad.id, "
                                 "ad_group_ad.ad.final_urls, ad_group_ad.ad.demand_gen_video_responsive_ad.headlines, "
                                 "ad_group_ad.ad.demand_gen_video_responsive_ad.long_headlines, "
                                 "ad_group_ad.ad.demand_gen_video_responsive_ad.descriptions, "
                                 "ad_group_ad.ad.demand_gen_video_responsive_ad.videos, "
                                 "ad_group_ad.ad.demand_gen_video_responsive_ad.logo_images, "
                                 "ad_group_ad.ad.demand_gen_video_responsive_ad.business_name FROM ad_group_ad "
                                 "WHERE ad_group_ad.ad.type = 'DEMAND_GEN_VIDEO_RESPONSIVE_AD'" + LIVE):
            dv, base = r["adGroupAd"]["ad"].get("demandGenVideoResponsiveAd", {}), ad_base(r)
            texts(base, dv.get("headlines"), "HEADLINE")
            texts(base, dv.get("longHeadlines"), "LONG_HEADLINE")
            texts(base, dv.get("descriptions"), "DESCRIPTION")
            refs(base, dv.get("videos"), "YOUTUBE_VIDEO")
            refs(base, dv.get("logoImages"), "LOGO")
            if g(dv, "businessName", "text"):
                row(**base, field="BUSINESS_NAME", type_="TEXT", text=dv["businessName"]["text"])

    def pmax():
        for r in api.search(cid, "SELECT campaign.name, campaign.advertising_channel_type, asset_group.id, asset_group.name, "
                                 "asset_group.final_urls, asset_group.path1, asset_group.path2, asset_group_asset.field_type, "
                                 "asset.type, asset.text_asset.text, asset.image_asset.full_size.url, "
                                 "asset.youtube_video_asset.youtube_video_id, asset.call_to_action_asset.call_to_action "
                                 "FROM asset_group_asset WHERE asset_group_asset.status = 'ENABLED' "
                                 "AND asset_group.status = 'ENABLED' AND campaign.status = 'ENABLED'"):
            ag, a = r["assetGroup"], r["asset"]
            row(level="ASSET_GROUP", campaign=r["campaign"]["name"], channel=r["campaign"].get("advertisingChannelType", ""),
                group=ag["name"], owner=ag["id"], field=r["assetGroupAsset"].get("fieldType", ""), type_=a.get("type", ""),
                text=g(a, "textAsset", "text") or g(a, "callToActionAsset", "callToAction"),
                img=g(a, "imageAsset", "fullSize", "url"), video=g(a, "youtubeVideoAsset", "youtubeVideoId"),
                url=(ag.get("finalUrls") or [""])[0], p1=ag.get("path1", ""), p2=ag.get("path2", ""))

    guarded("RSA assets", rsa)
    guarded("Demand Gen image ads", dg_image)
    guarded("Demand Gen video ads", dg_video)
    guarded("PMax assets", pmax)

    # extensions + business logo / name at account, campaign and ad-group level: one query per level
    types = ", ".join(f"'{t}'" for t in EXT_TYPES)
    for level, res, sel, where in (
            ("ACCOUNT", "customer_asset", "", ""),
            ("CAMPAIGN", "campaign_asset", "campaign.name, campaign.status, campaign.advertising_channel_type, ",
             " AND campaign.status = 'ENABLED'"),
            ("AD_GROUP", "ad_group_asset", "campaign.name, campaign.status, campaign.advertising_channel_type, "
                                           "ad_group.name, ad_group.status, ",
             " AND campaign.status = 'ENABLED' AND ad_group.status = 'ENABLED'")):
        camel = re.sub(r"_([a-z])", lambda m: m.group(1).upper(), res)

        def ext(level=level, res=res, sel=sel, where=where, camel=camel):
            for r in api.search(cid, f"SELECT {sel}{res}.field_type, {ASSET_SELECT} FROM {res} "
                                     f"WHERE {res}.status = 'ENABLED'{where} AND asset.type IN ({types})"):
                text, lines, img = _ext_fields(r["asset"])
                row(level=level, campaign=g(r, "campaign", "name"), channel=g(r, "campaign", "advertisingChannelType"),
                    group=g(r, "adGroup", "name"), field=r[camel].get("fieldType", ""), type_=r["asset"].get("type", ""),
                    text=text, lines=lines, img=img)
        guarded(f"{level.lower()} extensions", ext)

    if pending:  # Demand Gen images / videos arrive as references
        refs_ = sorted({p[0] for p in pending})
        found = {}

        def lookup():
            for i in range(0, len(refs_), ASSET_BATCH):
                in_list = ", ".join(f"'{n}'" for n in refs_[i:i + ASSET_BATCH])
                for r in api.search(cid, "SELECT asset.resource_name, asset.image_asset.full_size.url, "
                                         "asset.youtube_video_asset.youtube_video_id FROM asset "
                                         f"WHERE asset.resource_name IN ({in_list})"):
                    a = r["asset"]
                    found[a["resourceName"]] = (g(a, "imageAsset", "fullSize", "url"), g(a, "youtubeVideoAsset", "youtubeVideoId"))
        guarded("image lookup", lookup)
        for res_name, r in pending:
            r[11], r[12] = found.get(res_name, ("", ""))
    return rows


# -------------------------------------------------------------- performance
def performance(api, acct, now, log):
    cid, name = acct["id"], acct["name"]
    camps = {}

    def get(n):
        return camps.setdefault(n, {"channel": "", "status": "", "ads": 0, "d30": [0] * 5, "mtd": [0] * 5})

    def guarded(label, fn):
        try:
            fn()
        except ApiError as e:
            log(f"{label} query failed (continuing): {e}")

    def listing():
        for r in api.search(cid, "SELECT campaign.name, campaign.status, campaign.advertising_channel_type FROM campaign "
                                 "WHERE campaign.status = 'ENABLED'"):
            c = get(r["campaign"]["name"])
            c["status"], c["channel"] = r["campaign"].get("status", ""), r["campaign"].get("advertisingChannelType", "")

    def metrics(key, rng):
        def run():
            for r in api.search(cid, "SELECT campaign.name, campaign.status, campaign.advertising_channel_type, "
                                     "metrics.cost_micros, metrics.clicks, metrics.impressions, metrics.conversions, "
                                     f"metrics.conversions_value FROM campaign WHERE segments.date DURING {rng} "
                                     "AND metrics.impressions > 0"):
                c, m = get(r["campaign"]["name"]), r.get("metrics", {})
                c["status"] = c["status"] or r["campaign"].get("status", "")
                c["channel"] = c["channel"] or r["campaign"].get("advertisingChannelType", "")
                c[key] = [float(m.get("costMicros", 0) or 0) / 1e6, float(m.get("clicks", 0) or 0),
                          float(m.get("impressions", 0) or 0), float(m.get("conversions", 0) or 0),
                          float(m.get("conversionsValue", 0) or 0)]
        return run

    def ads():
        for r in api.search(cid, "SELECT campaign.name, ad_group_ad.ad.id FROM ad_group_ad WHERE ad_group_ad.status = 'ENABLED' "
                                 "AND ad_group.status = 'ENABLED' AND campaign.status = 'ENABLED'"):
            get(r["campaign"]["name"])["ads"] += 1

    def groups():
        for r in api.search(cid, "SELECT campaign.name, asset_group.id FROM asset_group WHERE asset_group.status = 'ENABLED' "
                                 "AND campaign.status = 'ENABLED'"):
            get(r["campaign"]["name"])["ads"] += 1

    guarded("campaign list", listing)
    guarded("metrics 30d", metrics("d30", DATE_RANGE))
    guarded("metrics MTD", metrics("mtd", "THIS_MONTH"))
    guarded("live ads", ads)
    guarded("live asset groups", groups)
    return [[name, fmt_cid(cid), acct["currency"], n, c["channel"], c["status"], c["ads"]] + c["d30"] + c["mtd"] + [now]
            for n, c in camps.items()]


# ------------------------------------------- ad strength + PMax search themes
def strengths(api, acct, log):
    """{'ad': {ad id: strength}, 'ag': {asset group id: strength}, 'themes': {asset group id: [text]}}"""
    cid, out = acct["id"], {"ad": {}, "ag": {}, "themes": {}}
    try:
        for r in api.search(cid, "SELECT ad_group_ad.ad.id, ad_group_ad.ad_strength FROM ad_group_ad "
                                 "WHERE ad_group_ad.status = 'ENABLED' AND ad_group.status = 'ENABLED' AND campaign.status = 'ENABLED'"):
            out["ad"][r["adGroupAd"]["ad"]["id"]] = r["adGroupAd"].get("adStrength", "")
    except ApiError as e:
        log(f"ad strength query failed (continuing): {e}")
    try:
        for r in api.search(cid, "SELECT asset_group.id, asset_group.ad_strength FROM asset_group "
                                 "WHERE asset_group.status = 'ENABLED' AND campaign.status = 'ENABLED'"):
            out["ag"][r["assetGroup"]["id"]] = r["assetGroup"].get("adStrength", "")
    except ApiError as e:
        log(f"asset group strength query failed (continuing): {e}")
    try:
        for r in api.search(cid, "SELECT asset_group.id, asset_group_signal.search_theme.text FROM asset_group_signal "
                                 "WHERE asset_group.status = 'ENABLED' AND campaign.status = 'ENABLED'"):
            t = g(r, "assetGroupSignal", "searchTheme", "text")
            if t:
                out["themes"].setdefault(r["assetGroup"]["id"], []).append(t)
    except ApiError as e:
        log(f"search theme query failed (continuing): {e}")
    return out


# ------------------------------------------------------ Google's preview links
def previews(api, acct, groups, now, log):
    """Google's own shareable preview link for each PMax asset group. Google supports no other ad type
    here (Demand Gen / RSA return UNSUPPORTED_AD_TYPE). Links last 14 days; every pull makes fresh ones."""
    out, ids = [], sorted(groups)
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        try:
            r = api._post(acct["id"], {"shareablePreviews": [{"assetGroupIdentifier": {"assetGroupId": x}} for x in batch]},
                          ":generateShareablePreviews", PREVIEW_API)
        except ApiError as e:
            log(f"preview links failed (continuing without them): {e}")
            continue
        for x in r.get("responses", []):
            res, gid = x.get("shareablePreviewResult"), g(x, "assetGroupIdentifier", "assetGroupId")
            if res and res.get("shareablePreviewUrl") and gid in groups:
                out.append({"id": gid, "campaign": groups[gid][0], "group": groups[gid][1],
                            "url": res["shareablePreviewUrl"], "expires": str(res.get("expirationDateTime", ""))[:10],
                            "created": now[:10]})
            else:  # Google declines groups without enough assets, and Demand Gen / RSA outright
                log(f'no preview link for asset group {gid}: {g(x, "partialFailureError", "message") or "none returned (too few assets?)"}')
    return out


# ---------------------------------------------------------------- entry point
def _pull_account(api, acct, now):
    msgs = []
    log = lambda m: msgs.append(f"[{acct['name']}] {m}")
    rsa, pmax = combinations(api, acct, now, log)
    live = live_assets(api, acct, now, log)
    perf = performance(api, acct, now, log)
    st = strengths(api, acct, log)
    groups = {r[6]: (r[3], r[5]) for r in live if r[2] == "ASSET_GROUP"}  # owner id -> (campaign, asset group)
    return {"rsa": rsa, "pmax": pmax, "live": live, "perf": perf, "strengths": st,
            "previews": previews(api, acct, groups, now, log), "log": msgs}


def pull(client):
    """Query the API for every account of a client; returns a JSON-able dict (what gets cached)."""
    api = Api(client.get("login_customer_id", ""))  # the manager account the OAuth user signs in through
    mcc = client.get("mcc_id") or api.login          # the MCC whose client accounts we list
    accounts = list_accounts(api, mcc)
    only = {re.sub(r"\D", "", x) for x in client.get("customer_ids", [])}
    if only:
        accounts = [a for a in accounts if a["id"] in only]
    if not accounts:
        raise RuntimeError(f"No enabled accounts found under MCC {mcc}")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    t0 = time.time()
    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(lambda a: _pull_account(api, a, now), accounts))
    out = {"version": CACHE_VERSION, "pulledAt": now, "pulledTs": time.time(), "accounts": [a["name"] for a in accounts], "log": [],
           "rsa": [], "pmax": [], "live": [], "perf": [], "previews": [], "strengths": {"ad": {}, "ag": {}, "themes": {}}}
    for r in results:
        for k in ("rsa", "pmax", "live", "perf", "previews", "log"):
            out[k] += r[k]
        for k in ("ad", "ag", "themes"):
            out["strengths"][k].update(r["strengths"][k])
    out["requests"] = api.requests
    print(f"Google Ads API: {len(accounts)} accounts, {api.requests} requests, {time.time() - t0:.0f}s")
    for m in out["log"]:
        print("  !", m)
    return out


def load_or_pull(key, client):
    """Cached pull (GADS_CACHE_HOURS). A failed refresh falls back to the last good cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{key}.json"
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
    if cached and cached.get("version") != CACHE_VERSION:
        print("Google Ads API: cache is from an older version, pulling fresh data")
        cached = None
    age = time.time() - cached.get("pulledTs", 0) if cached else None
    forced = FORCE.is_set()
    FORCE.clear()
    if cached and age < (MIN_REFRESH_MINUTES * 60 if forced else CACHE_HOURS * 3600):
        why = f"{MIN_REFRESH_MINUTES:g} min refresh floor" if forced else f"{CACHE_HOURS:g}h window"
        print(f"Google Ads API: using cache from {cached['pulledAt']} ({why})")
        return cached
    try:
        data = pull(client)
    except Exception as e:
        if cached:
            print(f"Google Ads API refresh failed ({e}); using cache from {cached['pulledAt']}")
            return cached
        raise
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return data


def frames(data):
    """Cached pull -> the same tab DataFrames build.py reads from the sheet."""
    def df(header, rows):
        out = pd.DataFrame([[None if v == "" else str(v) for v in r] for r in rows], columns=header, dtype=object)
        return out
    return {"RSA Combinations": df(RSA_HEADER, data["rsa"]), "PMax Combinations": df(PMAX_HEADER, data["pmax"]),
            "Live Assets": df(LIVE_HEADER, data["live"]), "Campaign Performance": df(PERF_HEADER, data["perf"])}
