"""meta_api.py without a network: creatives in the shapes the Marketing API returns them, and a whole pull against a fake Graph API.

    python tests/test_meta_api.py      (or pytest)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("GADS_CACHE_DIR", tempfile.mkdtemp())

import meta_api  # noqa: E402

IMAGE = {"object_story_spec": {"page_id": "111", "instagram_user_id": "222", "link_data": {
    "message": "Glowing skin starts here.\n\nBook your first facial today.", "name": "First facial, 20% off", "description": "Soteri Skin, Denver",
    "link": "https://soteriskin.com/facials?utm_source=meta", "image_hash": "h1", "call_to_action": {"type": "BOOK_TRAVEL", "value": {"link": "https://soteriskin.com/book"}}}}}
VIDEO = {"object_type": "VIDEO", "object_story_spec": {"page_id": "111", "video_data": {"video_id": "900", "message": "Meet our team", "title": "Watch", "image_url": "https://x/thumb.jpg",
                                                                                      "call_to_action": {"type": "LEARN_MORE", "value": {"link": "https://soteriskin.com/team"}}}}}
CAROUSEL = {"object_story_spec": {"page_id": "111", "link_data": {"message": "Three treatments", "link": "https://soteriskin.com", "child_attachments": [
    {"name": "Facials", "description": "From $99", "link": "https://soteriskin.com/f", "image_hash": "h2", "call_to_action": {"type": "SHOP_NOW"}},
    {"name": "Peels", "link": "https://soteriskin.com/p", "picture": "https://x/p.jpg"},
    {"name": "Clip", "link": "https://soteriskin.com/c", "video_id": "901", "picture": "https://x/c.jpg"}]}}}
FLEX = {"object_story_spec": {"page_id": "111"}, "asset_feed_spec": {"bodies": [{"text": "Body A"}, {"text": "Body B"}], "titles": [{"text": "Title A"}], "descriptions": [{"text": "Desc"}],
                                                                       "images": [{"hash": "h3"}, {"hash": "h4"}], "videos": [{"video_id": "902", "thumbnail_url": "https://x/v.jpg"}],
                                                                       "call_to_action_types": ["SIGN_UP"], "link_urls": [{"website_url": "https://soteriskin.com/join"}], "ad_formats": ["AUTOMATIC_FORMAT"]}}
# a single image with a 9:16 version for Stories and Reels and two text options, as Ads Manager sets it up (asset feed + placement rules)
SINGLE = {"object_story_spec": {"page_id": "111"}, "asset_feed_spec": {
    "ad_formats": ["SINGLE_IMAGE"], "bodies": [{"text": "Text one"}, {"text": "Text two"}], "titles": [{"text": "Clinically proven"}],
    "images": [{"hash": "sq", "adlabels": [{"name": "feed_img"}]}, {"hash": "tall", "adlabels": [{"name": "story_img"}]}],
    "asset_customization_rules": [
        {"customization_spec": {"publisher_platforms": ["facebook", "instagram"], "facebook_positions": ["story", "facebook_reels"], "instagram_positions": ["story", "reels"]}, "image_label": {"name": "story_img"}, "priority": 1},
        {"customization_spec": {"publisher_platforms": ["facebook", "instagram"], "facebook_positions": ["feed"], "instagram_positions": ["stream"]}, "image_label": {"name": "feed_img"}, "priority": 2}]},
    "degrees_of_freedom_spec": {"creative_features_spec": {"image_touchups": {"enroll_status": "OPT_IN"}, "inline_comment": {"enroll_status": "OPT_IN"}, "image_templates": {"enroll_status": "OPT_IN"},
                                                          "enhance_cta": {"enroll_status": "OPT_OUT"}, "image_brightness_and_contrast": {"enroll_status": "OPT_OUT"}, "brand_new_thing": {"enroll_status": "OPT_OUT"},
                                                          "site_extensions": {"enroll_status": "OPT_OUT"}, "music": {"enroll_status": "OPT_OUT"}, "audio": {"enroll_status": "OPT_IN"}}},
    "contextual_multi_ads": {"enroll_status": "OPT_OUT"}}


def test_an_image_ad_keeps_its_words_line_breaks_picture_button_and_link():
    p = meta_api.creative_parts(IMAGE)
    assert p["format"] == "image" and p["primary"] == ["Glowing skin starts here.\n\nBook your first facial today."] and p["headline"] == ["First facial, 20% off"]
    assert p["description"] == ["Soteri Skin, Denver"] and p["images"] == [{"hash": "h1"}] and p["cta"] == "BOOK_TRAVEL" and p["link"] == "https://soteriskin.com/book"
    assert p["page_id"] == "111" and p["ig_id"] == "222"


def test_a_video_a_carousel_and_a_flexible_ad_are_told_apart():
    v = meta_api.creative_parts(VIDEO)
    assert v["format"] == "video" and v["videos"] == [{"id": "900", "thumb": "https://x/thumb.jpg"}] and v["cta"] == "LEARN_MORE" and v["link"] == "https://soteriskin.com/team"
    c = meta_api.creative_parts(CAROUSEL)
    assert c["format"] == "carousel" and [k["headline"] for k in c["cards"]] == ["Facials", "Peels", "Clip"] and c["cards"][0]["image"] == {"hash": "h2"}
    assert c["cards"][1]["image"] == {"url": "https://x/p.jpg"} and c["cards"][2]["video"] == {"id": "901", "thumb": "https://x/c.jpg"} and c["cards"][0]["cta"] == "SHOP_NOW"
    f = meta_api.creative_parts(FLEX)
    assert f["format"] == "flexible" and f["primary"] == ["Body A", "Body B"] and f["images"] == [{"hash": "h3"}, {"hash": "h4"}] and f["cta"] == "SIGN_UP" and f["link"] == "https://soteriskin.com/join"
    assert meta_api.creative_parts(dict(FLEX, asset_feed_spec=dict(FLEX["asset_feed_spec"], ad_formats=[])))["format"] == "image"
    assert meta_api.creative_parts({"video_id": "5", "thumbnail_url": "t"})["format"] == "video" and meta_api.creative_parts({})["format"] == "image"


def test_a_single_image_with_a_stories_version_is_a_single_image_with_its_placement_pictures():
    p = meta_api.creative_parts(SINGLE)
    assert p["format"] == "image" and p["images"] == [{"hash": "sq"}, {"hash": "tall"}] and p["primary"] == ["Text one", "Text two"]
    assert p["slots"] == {"story": {"kind": "image", "i": 1}, "feed": {"kind": "image", "i": 0}}


def test_a_duplicated_ad_does_not_show_the_first_ads_leftover_picture():
    # a copy keeps the first ad's picture in its story spec; Meta shows only the asset feed's pictures
    left = {"page_id": "111", "link_data": {"image_hash": "old", "link": "https://soteriskin.com"}}
    f = meta_api.creative_parts(dict(FLEX, object_story_spec=left))
    assert f["images"] == [{"hash": "h3"}, {"hash": "h4"}]
    s = meta_api.creative_parts(dict(SINGLE, object_story_spec=left))
    assert s["images"] == [{"hash": "sq"}, {"hash": "tall"}] and s["slots"] == {"story": {"kind": "image", "i": 1}, "feed": {"kind": "image", "i": 0}}
    # an unlabelled picture in the feed that no placement rule names is not shown by Meta either
    extra = dict(SINGLE["asset_feed_spec"], images=[{"hash": "old"}] + SINGLE["asset_feed_spec"]["images"])
    e = meta_api.creative_parts(dict(SINGLE, asset_feed_spec=extra))
    assert e["images"] == [{"hash": "sq"}, {"hash": "tall"}] and e["slots"] == {"story": {"kind": "image", "i": 1}, "feed": {"kind": "image", "i": 0}}
    # with no placement rules (Meta mixes them all), every picture stays
    assert len(meta_api.creative_parts(FLEX)["images"]) == 2
    # with no asset feed pictures or videos, the story spec's picture is the ad's picture
    assert meta_api.creative_parts({"object_story_spec": left, "asset_feed_spec": {"bodies": [{"text": "x"}]}})["images"] == [{"hash": "old"}]


def test_advantage_plus_enhancements_and_delivery_settings_are_in_ads_managers_words():
    e = meta_api.enhancements(SINGLE)
    assert e["on"] == ["Add music", "Add overlays", "Relevant comments", "Visual touch-ups"] and e["multiAdvertiser"] == "off"
    assert e["off"] == ["Add music", "Add site links", "Adjust brightness and contrast", "Brand new thing", "Enhance CTA"]
    # under Ads Manager's headings, a switch sent under two names (music, audio) once and on
    assert [(i["group"], i["label"], i["on"]) for i in e["items"]] == [
        ("setup", "Site links", False), ("creative", "Add music", True), ("creative", "Add overlays", True), ("creative", "Relevant comments", True),
        ("creative", "Visual touch-ups", True), ("creative", "Adjust brightness and contrast", False), ("creative", "Enhance CTA", False), ("other", "Brand new thing", False)]
    assert meta_api.enhancements({}) == {"on": [], "off": [], "items": [], "multiAdvertiser": ""}
    o = meta_api.optimization({"adset": {"optimization_goal": "OFFSITE_CONVERSIONS", "bid_strategy": "COST_CAP", "targeting": {"targeting_automation": {"advantage_audience": 1}}},
                               "campaign": {"daily_budget": "5000", "smart_promotion_type": "GUIDED_CREATION"}})
    assert o == {"goal": "Conversions", "bid": "Cost per result goal", "campaignBudget": True, "audience": "on", "shopping": False}
    assert meta_api.optimization({"campaign": {"bid_strategy": "LOWEST_COST_WITHOUT_CAP"}})["bid"] == "Highest volume"


def test_placements_chosen_or_automatic():
    assert meta_api.placements({"targeting": {"publisher_platforms": ["instagram"], "instagram_positions": ["story"]}}) == {"automatic": False, "networks": ["instagram"], "positions": ["ig:story"]}
    assert meta_api.placements({})["automatic"] is True and "facebook" in meta_api.placements(None)["networks"]


class FakeGraph:
    """Answers each Graph path the pull asks for; records the asks."""

    def __init__(self, refuse=()):
        self.asked, self.refuse, self.requests = [], set(refuse), 0

    def get(self, path, params=None):
        self.asked.append(path)
        self.requests += 1
        for r in self.refuse:
            if r in path:
                raise RuntimeError("Meta answered 400: (#10) not allowed")
        if path == "act_261215873024315":
            return {"name": "Soteri Skin", "currency": "USD"}
        if path.endswith("/previews"):
            return {"data": [{"body": '<iframe src="https://www.facebook.com/ads/api/preview_iframe.php?d=AQ&amp;t=AQ" width="540"></iframe>'}]}
        if path == "900":
            return {"picture": "https://x/small.jpg", "thumbnails": {"data": [{"uri": "https://x/big.jpg", "is_preferred": True, "width": 1080}]}, "length": 14.5, "permalink_url": "/soteri/videos/900/"}
        if path == "111":
            return {"name": "Soteri Skin", "picture": {"data": {"url": "https://x/page.jpg"}}, "instagram_business_account": {"username": "soteriskin", "profile_picture_url": "https://x/ig.jpg"}}
        return {}

    def pages(self, path, params, limit=meta_api.MAX_ADS):
        self.asked.append(path)
        if path.endswith("/ads"):
            assert "ACTIVE" in json.loads(params["effective_status"]) and "CAMPAIGN_PAUSED" in json.loads(params["effective_status"]) and "ARCHIVED" not in json.loads(params["effective_status"])
            return [{"id": "1", "name": "Facial offer", "effective_status": "ACTIVE", "created_time": "2026-10-01T10:00:00+0000", "campaign": {"name": "Prospecting", "objective": "OUTCOME_LEADS"},
                     "adset": {"name": "Denver 25-54", "targeting": {}}, "creative": IMAGE},
                    {"id": "2", "name": "Team video", "effective_status": "ACTIVE", "campaign": {"name": "Prospecting"}, "adset": {"name": "IG only", "targeting": {"publisher_platforms": ["instagram"]}}, "creative": VIDEO}]
        if path.endswith("/adimages"):
            return [{"hash": "h1", "url": "https://scontent.x/h1.jpg"}]
        return []


def test_a_pull_reads_ads_pictures_videos_page_and_previews(monkeypatch=None):
    fake = FakeGraph()
    orig = meta_api.Api
    meta_api.Api = lambda: fake
    try:
        out = meta_api.pull({"meta_account_id": "261215873024315"})
    finally:
        meta_api.Api = orig
    assert out["account"] == {"id": "act_261215873024315", "name": "Soteri Skin", "currency": "USD"} and [a["id"] for a in out["ads"]] == ["1", "2"] and out["log"] == []
    img, vid = out["ads"]
    assert img["images"] == ["https://scontent.x/h1.jpg"] and img["ctaText"] == "Book now" and img["page"]["ig"] == "soteriskin" and img["placements"]["automatic"] is True
    assert img["previews"][0] == {"format": "MOBILE_FEED_STANDARD", "label": "Facebook feed", "url": "https://www.facebook.com/ads/api/preview_iframe.php?d=AQ&t=AQ"} and len(img["previews"]) == 3
    assert vid["ctaText"] == "Learn more"
    assert vid["videos"] == [{"id": "900", "thumb": "https://x/big.jpg", "url": "https://www.facebook.com/soteri/videos/900/", "length": 14.5}] and vid["placements"]["networks"] == ["instagram"]


def test_a_part_meta_refuses_is_logged_and_the_rest_still_reads():
    fake = FakeGraph(refuse=("/previews", "111"))
    orig = meta_api.Api
    meta_api.Api = lambda: fake
    try:
        out = meta_api.pull({"meta_account_id": "act_261215873024315", "name": "Soteri Skin"})
    finally:
        meta_api.Api = orig
    assert len(out["ads"]) == 2 and all(a["previews"] == [] for a in out["ads"]) and out["ads"][0]["page"]["name"] == "Soteri Skin" and out["ads"][0]["page"]["picture"] == ""
    assert any("preview links" in m for m in out["log"]) and any("Facebook page 111" in m for m in out["log"])


def test_when_meta_refuses_the_extra_fields_the_ads_are_read_without_them():
    class Picky(FakeGraph):
        def pages(self, path, params, limit=meta_api.MAX_ADS):
            if path.endswith("/ads") and "degrees_of_freedom_spec" in params["fields"]:
                raise RuntimeError("Meta answered 400: (#100) Tried accessing nonexisting field (contextual_multi_ads)")
            return super().pages(path, params, limit)
    fake = Picky()
    orig = meta_api.Api
    meta_api.Api = lambda: fake
    try:
        out = meta_api.pull({"meta_account_id": "261215873024315"})
    finally:
        meta_api.Api = orig
    assert len(out["ads"]) == 2 and out["ads"][0]["enhancements"] == {"on": [], "off": [], "items": [], "multiAdvertiser": ""} and "Advantage+" in out["log"][0]


def test_without_a_token_or_an_account_nothing_is_read():
    old = os.environ.pop("META_ADS_ACCESS_TOKEN", None)
    try:
        assert meta_api.available() is False
    finally:
        if old is not None:
            os.environ["META_ADS_ACCESS_TOKEN"] = old
    try:
        meta_api.pull({})
        raise AssertionError("should refuse")
    except RuntimeError as e:
        assert "meta_account_id" in str(e)
    assert meta_api.act_of("261215873024315") == meta_api.act_of("act_261215873024315") == "act_261215873024315"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
