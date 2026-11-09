"""Meta Marketing API reader: a client's running and paused Meta ads, as the dashboard's Meta view draws them.

Read only. One ad account per client (clients.json `meta_account_id`), the token in META_ADS_ACCESS_TOKEN
(a System User token with ads_read on that account). What is read:

  * the account's name;
  * every ad that is running or paused (not archived or deleted) with its campaign, its ad set's placements and its creative (the words, the pictures,
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
CACHE_VERSION = 5              # 2: the real format, placement pictures, Advantage+ and optimization settings; 3: paused ads too; 4: enhancements grouped as in Ads Manager; 5: no leftover story picture
# the ads read: running, paused at any level, and waiting on review; archived and deleted ones are left out
STATUSES = ["ACTIVE", "PAUSED", "CAMPAIGN_PAUSED", "ADSET_PAUSED", "PENDING_REVIEW", "IN_PROCESS", "WITH_ISSUES", "DISAPPROVED", "PREAPPROVED", "PENDING_BILLING_INFO"]
MAX_ADS = 300
PREVIEW_FORMATS = (("MOBILE_FEED_STANDARD", "Facebook feed"), ("INSTAGRAM_STANDARD", "Instagram feed"), ("INSTAGRAM_STORY", "Instagram Story"))
AD_FIELDS = ("id,name,effective_status,created_time,campaign{id,name,objective},adset{id,name,targeting},"
             "creative{id,name,object_type,title,body,image_url,image_hash,thumbnail_url,video_id,call_to_action_type,link_url,"
             "object_story_spec,asset_feed_spec,instagram_permalink_url,effective_object_story_id}")
# the settings read on top: Advantage+ creative enhancements and multi-advertiser ads, the ad set's goal and bid, the campaign's budget. A field Meta refuses
# for an account drops these extras (the ads are read with AD_FIELDS) and says so, never the ads.
EXTRA_FIELDS = ("id,name,effective_status,created_time,campaign{id,name,objective,daily_budget,lifetime_budget,bid_strategy,smart_promotion_type},"
                "adset{id,name,targeting,optimization_goal,bid_strategy,billing_event,destination_type,daily_budget,lifetime_budget},"
                "creative{id,name,object_type,title,body,image_url,image_hash,thumbnail_url,video_id,call_to_action_type,link_url,"
                "object_story_spec,asset_feed_spec,instagram_permalink_url,effective_object_story_id,degrees_of_freedom_spec,contextual_multi_ads}")
# Meta's API names for the Advantage+ creative enhancements, in the words Ads Manager uses; a name not here is shown in plain words
FEATURES = {"standard_enhancements": "Standard enhancements", "image_touchups": "Visual touch-ups", "image_templates": "Add overlays", "add_text_overlay": "Add text overlays",
            "image_brightness_and_contrast": "Adjust brightness and contrast", "enhance_cta": "Enhance CTA", "inline_comment": "Relevant comments",
            "text_optimizations": "Text improvements", "text_generation": "Text generation", "product_extensions": "Add catalog items", "product_metadata_automation": "Add product tags",
            "image_uncrop": "Expand image", "image_animation": "Image animation", "image_background_gen": "Generate backgrounds", "video_auto_crop": "Video auto-crop",
            "adapt_to_placement": "Adapt to placement", "description_automation": "Add descriptions", "reveal_details_over_time": "Reveal details over time",
            "site_extensions": "Add site links", "music": "Add music", "audio": "Add music", "advantage_plus_creative": "Advantage+ creative", "show_summary": "Show summary",
            "show_destination_blurbs": "Show destination details", "cv_transformation": "Visual enhancements", "media_type_automation": "Dynamic media",
            "pac_relaxation": "Use assets across placements", "multi_photo_to_video": "Turn images into a video", "translate_text": "Translate text", "3d_animation": "3D animation",
            "creative_stickers": "Stickers", "biz_ai": "Business AI", "hide_price": "Hide price", "local_store_extension": "Show local store details",
            "add_stickers": "Add stickers", "ig_video_native_subtitle": "Instagram captions", "image_auto_crop": "Image auto-crop", "profile_card": "Profile card",
            "summary_sticker": "Summary sticker", "carousel_to_video": "Carousel to video", "dha_optimization": "Dynamic ad optimization", "text_translation": "Translate text"}
# where Ads Manager shows each one: "Creative setup" (site links, products) and "Advantage+ creative enhancements"; the rest are Meta's background switches
SETUP = {"site_extensions": "Site links", "product_extensions": "Products", "local_store_extension": "Local store details"}
CREATIVE = {"image_templates": "Add overlays", "add_text_overlay": "Add text overlays", "image_touchups": "Visual touch-ups", "music": "Add music", "audio": "Add music",
            "text_optimizations": "Optimize text per person", "enhance_cta": "Enhance CTA", "inline_comment": "Relevant comments", "image_animation": "Image animation",
            "image_brightness_and_contrast": "Adjust brightness and contrast", "reveal_details_over_time": "Reveal details over time", "show_summary": "Show summary",
            "show_destination_blurbs": "Show destination details", "image_uncrop": "Expand image", "image_background_gen": "Generate backgrounds",
            "video_auto_crop": "Video auto-crop", "image_auto_crop": "Image auto-crop", "description_automation": "Add descriptions", "multi_photo_to_video": "Turn images into a video",
            "translate_text": "Translate text", "text_translation": "Translate text", "adapt_to_placement": "Adapt to placement", "product_metadata_automation": "Add product tags",
            "add_stickers": "Add stickers", "creative_stickers": "Add stickers", "3d_animation": "3D animation", "standard_enhancements": "Standard enhancements"}
GOALS = {"OFFSITE_CONVERSIONS": "Conversions", "LINK_CLICKS": "Link clicks", "LANDING_PAGE_VIEWS": "Landing page views", "IMPRESSIONS": "Impressions", "REACH": "Reach",
         "THRUPLAY": "ThruPlay", "VALUE": "Conversion value", "LEAD_GENERATION": "Leads", "QUALITY_LEAD": "Quality leads", "POST_ENGAGEMENT": "Post engagement",
         "AD_RECALL_LIFT": "Ad recall lift", "CONVERSATIONS": "Conversations", "APP_INSTALLS": "App installs", "PAGE_LIKES": "Page likes", "TWO_SECOND_CONTINUOUS_VIDEO_VIEWS": "2-second video views"}
BIDS = {"LOWEST_COST_WITHOUT_CAP": "Highest volume", "LOWEST_COST_WITH_BID_CAP": "Bid cap", "COST_CAP": "Cost per result goal", "LOWEST_COST_WITH_MIN_ROAS": "ROAS goal",
        "TARGET_COST": "Target cost"}
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
    story_imgs, story_vids = len(images), len(videos)
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
    labelled = {}
    for i in feed.get("images") or []:
        if isinstance(i, dict) and (i.get("hash") or i.get("url")):
            images.append({"hash": i["hash"]} if i.get("hash") else {"url": i.get("url")})
            for lab in i.get("adlabels") or []:
                labelled[("image", lab.get("name"))] = ("image", len(images) - 1)
    for v in feed.get("videos") or []:
        if isinstance(v, dict) and v.get("video_id"):
            videos.append({"id": str(v["video_id"]), "thumb": v.get("thumbnail_url") or ""})
            for lab in v.get("adlabels") or []:
                labelled[("video", lab.get("name"))] = ("video", len(videos) - 1)
    # An ad with its own asset feed pictures or videos shows only those: a picture still in its story spec is a leftover (a duplicated ad keeps the
    # first ad's picture there) that Meta does not show, so it is dropped and the placement slots counted without it.
    if story_imgs and (len(images) > story_imgs or len(videos) > story_vids):
        images = images[story_imgs:]
        labelled = {k: (kind, i - story_imgs) if kind == "image" else (kind, i) for k, (kind, i) in labelled.items()}
    if story_vids and len(videos) > story_vids:
        videos = videos[story_vids:]
        labelled = {k: (kind, i - story_vids) if kind == "video" else (kind, i) for k, (kind, i) in labelled.items()}
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
    ad_formats = [str(x).upper() for x in feed.get("ad_formats") or []]
    if feed and "AUTOMATIC_FORMAT" in ad_formats:                               # Meta's flexible format: it picks the format and the mix itself
        fmt = "flexible"
    elif feed and any("CAROUSEL" in x for x in ad_formats):
        fmt = "carousel"
    elif feed and ("SINGLE_VIDEO" in ad_formats or (videos and not images)):
        fmt = "video"
    elif feed:
        fmt = "image"                                                          # SINGLE_IMAGE: one picture, perhaps a second for Stories and Reels
    elif tmpl:
        fmt = "catalog"
    elif len(cards) > 1:
        fmt = "carousel"
    elif videos or str(cr.get("object_type") or "").upper() == "VIDEO":
        fmt = "video"
    else:
        fmt = "image"
    return {"format": fmt, "slots": placement_media(feed, labelled), "primary": _uniq(primary), "headline": _uniq(headline), "description": _uniq(description), "images": images, "videos": videos, "cards": cards,
            "cta": str(cta or "").upper(), "link": next((u for u in links if u), ""), "page_id": str(story.get("page_id") or ""), "ig_id": str(story.get("instagram_user_id") or story.get("instagram_actor_id") or "")}


def placement_media(feed, labelled):
    """{feed|story: {kind, i}}: which picture or video an ad shows in feeds and in Stories and Reels, from its placement rules (a 9:16 version for Stories is the usual case)."""
    out = {}
    rules = sorted((r for r in feed.get("asset_customization_rules") or [] if isinstance(r, dict)), key=lambda r: r.get("priority") or 0)
    for r in rules:
        spec = r.get("customization_spec") or {}
        positions = [str(x).lower() for k in ("facebook_positions", "instagram_positions", "messenger_positions", "audience_network_positions") for x in spec.get(k) or []]
        slot = "story" if positions and all(("story" in x or "reels" in x) for x in positions) else "feed"
        for kind in ("image", "video"):
            hit = labelled.get((kind, (r.get(kind + "_label") or {}).get("name")))
            if hit and slot not in out:
                out[slot] = {"kind": hit[0], "i": hit[1]}
    return out


def enhancements(cr):
    """{on: [], off: [], items: [{label, group: setup|creative|other, on}], multiAdvertiser: on|off|""}: the Advantage+ creative enhancements of a creative,
    in Ads Manager's words and under its headings. A switch Meta sends under two names is listed once, on if either is on."""
    spec = ((cr or {}).get("degrees_of_freedom_spec") or {}).get("creative_features_spec") or {}
    on, off, items = [], [], {}
    for k, v in spec.items():
        status = str((v or {}).get("enroll_status") or "").upper() if isinstance(v, dict) else ""
        if status not in ("OPT_IN", "OPT_OUT"):
            continue
        group, label = ("setup", SETUP[k]) if k in SETUP else ("creative", CREATIVE[k]) if k in CREATIVE else ("other", FEATURES.get(k, k.replace("_", " ").capitalize()))
        (on if status == "OPT_IN" else off).append(FEATURES.get(k, label))
        prev = items.get((group, label))
        items[(group, label)] = {"label": label, "group": group, "on": status == "OPT_IN" or bool(prev and prev["on"])}
    multi = str(((cr or {}).get("contextual_multi_ads") or {}).get("enroll_status") or "").upper()
    order = {"setup": 0, "creative": 1, "other": 2}
    return {"on": _uniq(sorted(on)), "off": _uniq(sorted(off)), "items": sorted(items.values(), key=lambda i: (order[i["group"]], not i["on"], i["label"])),
            "multiAdvertiser": "on" if multi == "OPT_IN" else "off" if multi == "OPT_OUT" else ""}


def optimization(ad):
    """The delivery settings of an ad's ad set and campaign, in Ads Manager's words."""
    ads_, camp = ad.get("adset") or {}, ad.get("campaign") or {}
    auto = ((ads_.get("targeting") or {}).get("targeting_automation") or {}).get("advantage_audience")
    goal, bid = str(ads_.get("optimization_goal") or ""), str(ads_.get("bid_strategy") or camp.get("bid_strategy") or "")
    return {"goal": GOALS.get(goal, goal.replace("_", " ").capitalize()), "bid": BIDS.get(bid, bid.replace("_", " ").capitalize()),
            "campaignBudget": bool(camp.get("daily_budget") or camp.get("lifetime_budget")), "audience": "on" if auto == 1 else "off" if auto == 0 else "",
            "shopping": str(camp.get("smart_promotion_type") or "").upper() == "AUTOMATED_SHOPPING_ADS"}


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
    try:
        ads = api.pages(act + "/ads", {"fields": EXTRA_FIELDS, "effective_status": json.dumps(STATUSES), "limit": 50})
    except RuntimeError as e:
        log.append("The Advantage+ and optimization settings could not be read (%s), so they are not shown" % str(e)[:160])
        ads = api.pages(act + "/ads", {"fields": AD_FIELDS, "effective_status": json.dumps(STATUSES), "limit": 50})
    if not ads:  # say what the account does hold, so "no ads" is not mistaken for a broken read
        seen = _soft(log, "The ad statuses", lambda: api.pages(act + "/ads", {"fields": "effective_status", "limit": 200}, limit=2000), [])
        by = {}
        for a in seen:
            by[a.get("effective_status") or "UNKNOWN"] = by.get(a.get("effective_status") or "UNKNOWN", 0) + 1
        log.append("No running or paused ads in this account; this token sees %d ads in all%s" % (len(seen), (" (" + ", ".join("%d %s" % (n, k.lower().replace("_", " ")) for k, n in sorted(by.items())) + ")") if by else ""))
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
            "page": {"name": pg.get("name") or client.get("name") or account.get("name") or "", "picture": pg.get("picture") or "", "ig": pg.get("ig") or "", "igPicture": pg.get("igPicture") or ""},
            "slots": p["slots"], "enhancements": enhancements(a.get("creative")), "optimization": optimization(a),
            "previews": prevs.get(a["id"]) or [], "igPermalink": (a.get("creative") or {}).get("instagram_permalink_url") or "",
        })
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"Meta API: {act}, {len(out_ads)} ads ({sum(a['status'] == 'ACTIVE' for a in out_ads)} active), {api.requests} requests, {time.time() - t0:.0f}s")
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
