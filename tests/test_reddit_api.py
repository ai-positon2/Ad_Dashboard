"""reddit_api.py without a network: a whole pull against a fake Reddit Ads API, in the shapes its OpenAPI reference documents.

    python tests/test_reddit_api.py      (or pytest)
"""
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reddit_api  # noqa: E402

ACCOUNTS = [{"id": "a2_other", "name": "Someone Else"}, {"id": "a2_tea", "name": "Tealium - Reddit", "currency": "USD"}]
CAMPAIGNS = [{"id": "c1", "name": "Awareness", "configured_status": "ACTIVE", "objective": "CLICKS", "spend_cap": 900000000, "start_time": "2026-10-01T00:00:00+00:00"},
             {"id": "c2", "name": "Old", "configured_status": "ARCHIVED"}]
GROUPS = [{"id": "g1", "campaign_id": "c1", "name": "Data subs", "configured_status": "ACTIVE", "bid_strategy": "MANUAL_BIDDING", "bid_type": "CPC", "bid_value": 1500000,
           "goal_type": "DAILY_SPEND", "goal_value": 50000000, "optimization_goal": "CLICKS",
           "targeting": {"communities": ["dataengineering", "marketing"], "keywords": ["cdp"], "geolocations": ["US"], "devices": [{"type": "MOBILE", "os": "IOS"}, {"type": "DESKTOP"}]}},
          {"id": "g2", "campaign_id": "c2", "name": "Gone", "configured_status": "ACTIVE"}]
ADS = [{"id": "ad1", "ad_group_id": "g1", "campaign_id": "c1", "name": "Image ad", "configured_status": "ACTIVE", "effective_status": "ACTIVE", "post_id": "t3_img",
        "click_url": "https://tealium.com/cdp", "profile_username": "Tealium", "post_url": "https://www.reddit.com/r/u_Tealium/comments/img/", "created_at": "2026-10-02T10:00:00+00:00",
        "preview_url": "https://www.reddit.com/preview/ad1", "preview_expiry": "2099-01-01T00:00:00+00:00"},
       {"id": "ad2", "ad_group_id": "g1", "name": "Carousel", "configured_status": "PAUSED", "effective_status": "PAUSED", "post_id": "t3_car",
        "preview_url": "https://www.reddit.com/preview/ad2", "preview_expiry": "2020-01-01T00:00:00+00:00"},
       {"id": "ad3", "ad_group_id": "g1", "name": "Deleted", "configured_status": "DELETED", "post_id": "t3_img"},
       {"id": "ad4", "ad_group_id": "g2", "name": "In archived campaign", "configured_status": "ACTIVE", "post_id": "t3_img"}]
POSTS = {"t3_img": {"id": "t3_img", "type": "IMAGE", "headline": "Unify your customer data", "allow_comments": False,
                    "content": [{"media_url": "https://i.redd.it/a.png", "call_to_action": "Learn More", "destination_url": "https://tealium.com/cdp", "display_url": "tealium.com"}]},
         "t3_car": {"id": "t3_car", "type": "CAROUSEL", "headline": "Three ways",
                    "content": [{"media_url": "https://i.redd.it/%d.png" % i, "caption": "Card %d" % i, "call_to_action": "Sign Up", "destination_url": "https://tealium.com/%d" % i} for i in range(3)]}}


class FakeApi(reddit_api.Api):
    def __init__(self):
        self.requests, self.lock, self.tok, self.calls = 0, threading.Lock(), "x", []

    def call(self, path, params=None):
        self.calls.append(path)
        if path.startswith("https://next/"):
            return {"data": ACCOUNTS[1:], "pagination": {"next_url": None}}
        page = lambda d: {"data": d, "pagination": {"next_url": None}}  # noqa: E731
        if path == "me/businesses":
            return page([{"id": "b1"}])
        if path == "businesses/b1/ad_accounts":
            return {"data": ACCOUNTS[:1], "pagination": {"next_url": "https://next/page2"}}
        if path.endswith("/campaigns"):
            return page(CAMPAIGNS)
        if path.endswith("/ad_groups"):
            return page(GROUPS)
        if path.endswith("/ads"):
            return page(ADS)
        if path.startswith("posts/"):
            return {"data": POSTS[path.split("/")[1]]}
        raise AssertionError(path)


def run(client):
    real = reddit_api.Api
    reddit_api.Api = FakeApi
    try:
        return reddit_api.pull(client)
    finally:
        reddit_api.Api = real


def test_pull_finds_the_account_and_reads_ads():
    d = run({"name": "Tealium", "reddit_ads": True})
    assert d["account"]["id"] == "a2_tea" and d["account"]["currency"] == "USD"          # found on the second page, by name
    assert [a["id"] for a in d["ads"]] == ["ad1", "ad2"]                                    # deleted ad and archived campaign left out
    a, c = d["ads"]
    assert a["format"] == "image" and a["headline"] == "Unify your customer data" and a["images"] == ["https://i.redd.it/a.png"]
    assert a["cta"] == "Learn More" and a["displayUrl"] == "tealium.com" and a["link"] == "https://tealium.com/cdp" and not a["allowComments"]
    assert a["status"] == "active" and a["profile"] == "Tealium" and a["preview"] == "https://www.reddit.com/preview/ad1" and a["created"] == "02 Oct 2026"
    s = a["settings"]
    assert s["objective"] == "Traffic" and s["strategy"] == "Manual bid" and s["bid"] == "$1.50 CPC" and s["budget"] == "$50.00 daily"
    assert s["spendCap"] == "$900" and s["communities"] == ["dataengineering", "marketing"] and s["keywords"] == ["cdp"]
    assert s["devices"] == ["Desktop", "Mobile iOS"] and s["locations"] == ["US"] and s["schedule"] == "From 01 Oct 2026"
    assert c["format"] == "carousel" and len(c["cards"]) == 3 and c["cards"][1]["caption"] == "Card 1" and c["status"] == "paused"
    assert c["preview"] == ""                                                               # an expired preview link is dropped


def test_account_from_clients_json_and_ambiguous():
    class One(FakeApi):
        def call(self, path, params=None):
            return {"data": {"id": "a2_set", "name": "Set", "currency": "EUR"}} if path == "ad_accounts/a2_set" else FakeApi.call(self, path, params)
    assert reddit_api.find_account(One(), {"reddit_account_id": "a2_set"})["currency"] == "EUR"
    try:
        reddit_api.find_account(FakeApi(), {"name": "Nobody"})
        raise AssertionError("expected an ambiguous account list to be refused")
    except RuntimeError as e:
        assert "reddit_account_id" in str(e) and "a2_tea" in str(e)


def test_not_set_up_or_missing_variables():
    assert not reddit_api.enabled({"name": "x"}) and reddit_api.enabled({"reddit_ads": True})
    saved = {k: os.environ.pop(k, None) for k in reddit_api.ENV}
    try:
        assert reddit_api.missing() == list(reddit_api.ENV)
        try:
            reddit_api.Api()
            raise AssertionError("expected missing variables to be refused")
        except RuntimeError as e:
            assert "REDDIT_REFRESH_TOKEN" in str(e)
    finally:
        os.environ.update({k: v for k, v in saved.items() if v})


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
