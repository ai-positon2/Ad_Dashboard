"""Meta Marketing API reader: a client's active Meta ads, as the dashboard's Meta view draws them.

Read only. One ad account per client (clients.json `meta_account_id`), the token in META_ADS_ACCESS_TOKEN
(a System User token with ads_read on that account). What is read:

  * the account's name;
  * every ACTIVE ad with its campaign, its ad set's placements and its creative (the words, the pictures,
    the video, the carousel cards, the button and the link, wherever Meta keeps them);
  * the full-size pictures behind image hashes (/adimages), the cover of each video (/{video}), and the
    page's name and picture (/{page});
  * Meta's own preview of each ad in a few placements (/{ad}/previews): a link that works for about a day,
    so the hourly rebuild keeps it fresh. Optional: a refusal hides the buttons.

Cached like the Google pull (GADS_CACHE_HOURS, the same forced-refresh floor), next to it on the volume.
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
from html import unescape

import gads_api

GRAPH = "https://graph.facebook.com/" + os.environ.get("META_API_VERSION", "v23.0").strip()
CACHE_VERSION = 1
MAX_ADS = 300
PREVIEW_FORMATS = (("MOBILE_FEED_STANDARD", "Facebook feed"), ("INSTAGRAM_STANDARD", "Instagram feed"), ("INSTAGRAM_STORY", "Instagram Story"))
AD_FIELDS = ("id,name,effective_status,created_time,campaign{id,name,objective},adset{id,name,targeting},"
             "creative{id,name,object_type,title,body,image_url,image_hash,thumbnail_url,video_id,call_to_action_type,link_url,"
             "object_story_spec,asset_feed_spec,instagram_permalink_url,effective_object_story_id}")
CTA = {"LEARN_MORE": "Learn more", "SHOP_NOW": "Shop now", "SIGN_UP": "Sign up", "BOOK_NOW": "Book now", "BOOK_TRAVEL": "Book now", "CONTACT_US": "Contact us",
       "GET_QUOTE": "Get quote", "APPLY_NOW": "Apply now", "DOWNLOAD": "Download", "GET_OFFER": "Get offer", "SUBSCRIBE": "Subscribe", "CALL_NOW": "Call now",
       "MESSAGE_PAGE": "Send message", "SEND_MESSAGE": "Send message", "WHATSAPP_MESSAGE": "Send WhatsApp message", "WATCH_MORE": "Watch more", "ORDER_NOW": "Order now",
       "GET_STARTED": "Get started", "SEE_MORE": "See more", "REQUEST_TIME": "Request time", "SCHEDULE": "Schedule", "GET_DIRECTIONS": "Get directions",
       "SEE_DETAILS": "See details", "BUY_NOW": "Buy now", "INQUIRE_NOW": "Inquire now", "INSTALL_MOBILE_APP": "Install now", "NO_BUTTON": ""}
NETWORKS = {"facebook": "Facebook", "instagram": "Instagram", "messenger": "Messenger", "audience_network": "Audience Network", "threads": "Threads"}


def token():
    return os.environ.get("META_ADS_ACCESS_TOKEN", "").strip().strip("\"'")


def available():
    return bool(token())


def act_of(v):
    d = re.sub(r"\D", "", str(v or ""))
    return "act_" + d if d else ""


class Api:
    """Graph API GETs with paging, a retry on Meta's rate limits and a request counter. Errors name Meta's own message, never the token."""

    def __init__(self):
        self.tok = token()
        if not self.tok:
            raise RuntimeError("META_ADS_ACCESS_TOKEN is not set")
        self.requests, self.lock = 0, threading.Lock()

    def get(self, path, params=None):
        q = dict(params or {}, access_token=self.tok)
        url = path if path.startswith("https://") else "%s/%s?%s" % (GRAPH, path.lstrip("/"), urllib.parse.urlencode(q))
        for attempt in range(4):
            with self.lock:
                self.requests += 1
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ad-preview-dashboard"}), timeout=60) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                try:
                    err = json.loads(e.read().decode("utf-8", "replace")).get("error") or {}
                except ValueError:
                    err = {}
                code = err.get("code")
                if code in (4, 17, 32, 613, 80004) and attempt < 3:              # rate limits: wait and try again
                    time.sleep(10 * (attempt + 1))
                    continue
                raise RuntimeError("Meta answered %s: %s" % (e.code, err.get("message") or "no reason given")) from None
            except urllib.error.URLError as e:
                if attempt == 3:
                    raise RuntimeError("Meta could not be reached (%s)" % e.reason) from None
                time.sleep(3 * (attempt + 1))
        raise RuntimeError("Meta kept refusing the request (rate limit)")

    def pages(self, path, params, limit=MAX_ADS):
        out, got = [], self.get(path, params)
        while True:
            out += got.get("data") or []
            nxt = (got.get("paging") or {}).get("next")
            if not nxt or len(out) >= limit:
                return out[:limit]
            got = self.get(nxt)


# ── reading one creative ────────────────────────────────────────────────────

def _txt(v):
    return " ".join(str(v or "").split()) if v else ""


def _multiline(v):
    """Primary text keeps its line breaks (Meta shows them); spaces inside a line are tidied."""
    if not v:
        return ""
    return "\n".join(" ".join(line.split()) for line in str(v).strip().splitlines()).strip()


def _uniq(xs):
    seen, out = set(), []
    for x in xs:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def creative_parts(cr):
    """{format, primary[], headline[], description[], images[{hash|url}], videos[{id, thumb}], cards[], cta, link, page_id, ig_id} from one creative."""
    cr = cr or {}
    story, feed = cr.get("object_story_spec") or {}, cr.get("asset_feed_spec") or {}
    link, video, photo, tmpl = story.get("link_data") or {}, story.get("video_data") or {}, story.get("photo_data") or {}, story.get("template_data") or {}
    primary = [_multiline(cr.get("body")), _multiline(link.get("message")), _multiline(video.get("message")), _multiline(photo.get("caption")), _multiline(tmpl.get("message"))]
    headline = [_txt(cr.get("title")), _txt(link.get("name")), _txt(video.get("title")), _txt(tmpl.get("name"))]
    description = [_txt(link.get("description")), _txt(video.get("link_description")), _txt(tmpl.get("description"))]
    images, videos, cards = [], [], []
    for src in (link, photo):
        if src.get("image_hash"):
            images.append({"hash": src["image_hash"]})
        elif src.get("picture") or src.get("url"):
            images.append({"url": src.get("picture") or src.get("url")})
    if video.get("video_id"):
        videos.append({"id": str(video["video_id"]), "thumb": video.get("image_url") or ""})
    for c in link.get("child_attachments") or []:
        card = {"headline": _txt(c.get("name")), "description": _txt(c.get("description")), "link": c.get("link") or "",
                "cta": ((c.get("call_to_action") or {}).get("type") or "")}
        if c.get("video_id"):
            card["video"] = {"id": str(c["video_id"]), "thumb": c.get("picture") or ""}
        elif c.get("image_hash"):
            card["image"] = {"hash": c["image_hash"]}
        elif c.get("picture"):
            card["image"] = {"url": c["picture"]}
        cards.append(card)
    for key, out in (("bodies", primary), ("titles", headline), ("descriptions", description)):
        out += [(_multiline if key == "bodies" else _txt)(i.get("text") if isinstance(i, dict) else i) for i in feed.get(key) or []]
    images += [{"hash": i["hash"]} if i.get("hash") else {"url": i.get("url")} for i in feed.get("images") or [] if isinstance(i, dict) and (i.get("hash") or i.get("url"))]
    videos += [{"id": str(v["video_id"]), "thumb": v.get("thumbnail_url") or ""} for v in feed.get("videos") or [] if isinstance(v, dict) and v.get("video_id")]
    if not images and not videos and not cards:
        if cr.get("video_id"):
            videos.append({"id": str(cr["video_id"]), "thumb": cr.get("thumbnail_url") or ""})
        elif cr.get("image_hash"):
            images.append({"hash": cr["image_hash"]})
        elif cr.get("image_url") or cr.get("thumbnail_url"):
            images.append({"url": cr.get("image_url") or cr.get("thumbnail_url")})
    cta_types = feed.get("call_to_action_types") or []
    cta = ((link.get("call_to_action") or {}).get("type") or (video.get("call_to_action") or {}).get("type") or (tmpl.get("call_to_action") or {}).get("type")
           or cr.get("call_to_action_type") or (cta_types[0] if cta_types else ""))
    links = [(link.get("call_to_action") or {}).get("value", {}).get("link"), link.get("link"), (video.get("call_to_action") or {}).get("value", {}).get("link"),
             cr.get("link_url")] + [u.get("website_url") for u in feed.get("link_urls") or [] if isinstance(u, dict)]
    if feed:
        fmt = "flexible"
    elif tmpl:
        fmt = "catalog"
    elif len(cards) > 1:
        fmt = "carousel"
    elif videos or str(cr.get("object_type") or "").upper() == "VIDEO":
        fmt = "video"
    else:
        fmt = "image"
    return {"format": fmt, "primary": _uniq(primary), "headline": _uniq(headline), "description": _uniq(description), "images": images, "videos": videos, "cards": cards,
            "cta": str(cta or "").upper(), "link": next((u for u in links if u), ""), "page_id": str(story.get("page_id") or ""), "ig_id": str(story.get("instagram_user_id") or story.get("instagram_actor_id") or "")}


def placements(adset):
    t = (adset or {}).get("targeting") or {}
    chosen = [str(p).lower() for p in t.get("publisher_platforms") or []]
    return {"automatic": not chosen, "networks": chosen or ["facebook", "instagram", "messenger", "audience_network"],
            "positions": sorted(set(t.get("facebook_positions") or []) | {"ig:" + p for p in t.get("instagram_positions") or []})}


# ── the pull ────────────────────────────────────────────────────────────────

def _soft(log, what, fn, default):
    try:
        return fn()
    except Exception as e:                                                       # noqa: BLE001 (a part that cannot be read is said, never fatal)
        log.append("%s could not be read (%s)" % (what, str(e)[:160]))
        return default


def _preview_src(body):
    m = re.search(r'src="([^"]+)"', body or "")
    return unescape(m.group(1)) if m else ""


def pull(client):
    act = act_of(client.get("meta_account_id"))
    if not act:
        raise RuntimeError("no meta_account_id for this client")
    api, log, t0 = Api(), [], time.time()
    account = api.get(act, {"fields": "name,currency,account_status"})
    ads = api.pages(act + "/ads", {"fields": AD_FIELDS, "effective_status": json.dumps(["ACTIVE"]), "limit": 50})
    parsed = {a["id"]: creative_parts(a.get("creative")) for a in ads if a.get("id")}

    hashes = sorted({i["hash"] for p in parsed.values() for i in p["images"] + [c["image"] for c in p["cards"] if c.get("image")] if i.get("hash")})
    images = {}
    for k in range(0, len(hashes), 50):
        chunk = hashes[k:k + 50]
        rows = _soft(log, "Some pictures", lambda: api.pages(act + "/adimages", {"hashes": json.dumps(chunk), "fields": "hash,url,url_128,permalink_url,width,height"}, limit=1000), [])
        images.update({r["hash"]: r.get("url") or r.get("permalink_url") or r.get("url_128") or "" for r in rows if r.get("hash")})

    vids = sorted({v["id"] for p in parsed.values() for v in p["videos"] + [c["video"] for c in p["cards"] if c.get("video")]})

    def video(vid):
        got = _soft(log, "The video %s" % vid, lambda: api.get(vid, {"fields": "picture,thumbnails{uri,is_preferred,width},length,permalink_url"}), {})
        thumbs = sorted((got.get("thumbnails") or {}).get("data") or [], key=lambda t: (not t.get("is_preferred"), -(t.get("width") or 0)))
        return vid, {"thumb": (thumbs[0]["uri"] if thumbs else "") or got.get("picture") or "", "length": got.get("length"),
                     "url": ("https://www.facebook.com" + got["permalink_url"]) if str(got.get("permalink_url") or "").startswith("/") else (got.get("permalink_url") or "")}

    page_ids = sorted({p["page_id"] for p in parsed.values() if p["page_id"]})

    def page(pid):
        got = _soft(log, "The Facebook page %s" % pid, lambda: api.get(pid, {"fields": "name,picture.type(large){url},instagram_business_account{username,profile_picture_url}"}), {})
        ig = got.get("instagram_business_account") or {}
        return pid, {"name": got.get("name") or "", "picture": ((got.get("picture") or {}).get("data") or {}).get("url") or "", "ig": ig.get("username") or "", "igPicture": ig.get("profile_picture_url") or ""}

    def previews(ad_id):
        out = []
        for fmt, label in PREVIEW_FORMATS:
            try:
                rows = api.get(ad_id + "/previews", {"ad_format": fmt}).get("data") or []
                src = _preview_src(rows[0].get("body") if rows else "")
                if src.startswith("https://"):
                    out.append({"format": fmt, "label": label, "url": src})
            except Exception:                                                    # noqa: BLE001 (a placement the ad cannot show in has no preview)
                continue
        return ad_id, out

    with ThreadPoolExecutor(6) as pool:
        videos = dict(pool.map(video, vids))
        pages = dict(pool.map(page, page_ids))
        prevs = dict(pool.map(previews, list(parsed)))
    if parsed and not any(prevs.values()):
        log.append("Meta gave no preview links for these ads, so the 'Open Meta's preview' buttons are hidden")

    out_ads = []
    for a in ads:
        p = parsed.get(a.get("id"))
        if p is None:
            continue
        resolve = lambda i: images.get(i["hash"], "") if i.get("hash") else (i.get("url") or "")          # noqa: E731
        vthumb = lambda v: (videos.get(v["id"]) or {}).get("thumb") or v.get("thumb") or ""             # noqa: E731
        pg = pages.get(p["page_id"]) or {}
        out_ads.append({
            "id": a["id"], "name": a.get("name") or "", "status": a.get("effective_status") or "", "created": str(a.get("created_time") or "")[:10],
            "campaign": (a.get("campaign") or {}).get("name") or "", "objective": (a.get("campaign") or {}).get("objective") or "",
            "adset": (a.get("adset") or {}).get("name") or "", "placements": placements(a.get("adset")),
            "format": p["format"], "primary": p["primary"], "headline": p["headline"], "description": p["description"],
            "images": [u for u in (resolve(i) for i in p["images"]) if u], "videos": [{"id": v["id"], "thumb": vthumb(v), "url": (videos.get(v["id"]) or {}).get("url") or "",
                                                                                 "length": (videos.get(v["id"]) or {}).get("length")} for v in p["videos"]],
            "cards": [dict(c, image=resolve(c["image"]) if c.get("image") else "", video=dict(c["video"], thumb=vthumb(c["video"])) if c.get("video") else None,
                           ctaText=CTA.get(c["cta"], c["cta"].replace("_", " ").capitalize()) if c.get("cta") else "") for c in p["cards"]],
            "cta": p["cta"], "ctaText": CTA.get(p["cta"], p["cta"].replace("_", " ").capitalize()), "link": p["link"],
            "page": {"name": pg.get("name") or account.get("name") or "", "picture": pg.get("picture") or "", "ig": pg.get("ig") or "", "igPicture": pg.get("igPicture") or ""},
            "previews": prevs.get(a["id"]) or [], "igPermalink": (a.get("creative") or {}).get("instagram_permalink_url") or "",
        })
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"Meta API: {act}, {len(out_ads)} active ads, {api.requests} requests, {time.time() - t0:.0f}s")
    for m in log:
        print("  !", m)
    return {"version": CACHE_VERSION, "pulledAt": now, "pulledTs": time.time(), "account": {"id": act, "name": account.get("name") or act, "currency": account.get("currency") or ""},
            "ads": out_ads, "log": log, "requests": api.requests}


def load_or_pull(key, client, forced=False):
    """Cached pull, the same window and forced-refresh floor as the Google pull. A failed refresh falls back to the last good cache."""
    gads_api.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = gads_api.CACHE_DIR / f"{key}.meta.json"
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
    if cached and (cached.get("version") != CACHE_VERSION or cached.get("account", {}).get("id") != act_of(client.get("meta_account_id"))):
        cached = None
    age = time.time() - cached.get("pulledTs", 0) if cached else None
    forced = forced or os.environ.get("GADS_FORCE") == "1"
    # previews expire after about a day, so a cache older than PREVIEW_HOURS is read again even inside the Google window
    window = min(gads_api.CACHE_HOURS, float(os.environ.get("META_PREVIEW_HOURS", "12"))) * 3600
    if cached and age < (gads_api.MIN_REFRESH_MINUTES * 60 if forced else window):
        print(f"Meta API: using cache from {cached['pulledAt']}")
        return cached
    try:
        data = pull(client)
    except Exception as e:
        if cached:
            print(f"Meta API refresh failed ({e}); using cache from {cached['pulledAt']}")
            return dict(cached, log=list(cached.get("log") or []) + ["The latest Meta read failed (%s), so this is the read of %s" % (str(e)[:160], cached["pulledAt"])])
        raise
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return data
