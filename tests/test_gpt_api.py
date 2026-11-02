"""gpt_api.py without a network: a whole pull against a fake OpenAI Ads API, in the shapes its reference documents.

    python tests/test_gpt_api.py      (or pytest)
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gpt_api  # noqa: E402

ACCOUNT = {"id": "adacct_1", "name": "Acme LLC", "account_name": "Acme US", "brand_name": "Acme", "preview_url": "https://cdn.example/icon.png", "currency_code": "USD"}
CAMPAIGNS = [{"id": "cmpn_1", "name": "Launch", "status": "active", "bidding_type": "clicks", "budget": {"lifetime_spend_limit_micros": 5000000000},
              "start_time": 1793491200, "targeting": {"locations": {"include": [{"id": "US"}]}, "platforms": {"included": ["web"]}}},
             {"id": "cmpn_2", "name": "Old", "status": "archived"}]
GROUPS = {"cmpn_1": [{"id": "adgrp_1", "name": "Data teams", "status": "active", "context_hints": ["customer data"], "bidding_config": {"billing_event_type": "click", "max_bid_micros": 2500000}},
                     {"id": "adgrp_2", "name": "Brand", "status": "paused", "bidding_config": {"billing_event_type": "impression", "max_bid_micros": 60000}}]}
ADS = {"adgrp_1": [{"id": "ad_1", "name": "Card", "created_at": 1793491200, "status": "active", "review_status": "approved",
                    "creative": {"type": "chat_card", "title": "Unify your data", "body": "One platform.", "image_url": "https://cdn.example/1.png", "target_url": "https://acme.example/a"}},
                   {"id": "ad_x", "name": "Gone", "status": "archived", "creative": {}}],
       "adgrp_2": [{"id": "ad_2", "name": "Multi", "status": "active", "review_status": "in_review", "creative": {"type": "chat_card", "title": "See it", "body": "", "file_ids": ["f1", "f2", "f3", "f4"]}}]}
FULL = {"ad_2": {"creative": {"type": "chat_card", "title": "See it", "body": "", "images": [{"item_id": "i%d" % i, "file_id": "f%d" % i, "image_url": "https://cdn.example/m%d.png" % i} for i in range(1, 5)]}}}


class FakeApi(gpt_api.Api):
    def __init__(self, client, refuse_preview=False):
        self.requests, self.calls, self.refuse = 0, [], refuse_preview
        import threading
        self.lock = threading.Lock()

    def call(self, path, params=None, method="GET"):
        self.calls.append((method, path, dict(params or {})))
        if method == "POST":
            assert path.endswith("/preview"), "only previews are POSTed"
            if self.refuse:
                raise RuntimeError("OpenAI answered 403: not allowed")
            return {"data": [{"body": '<iframe src="https://ads.openai.com/preview/abc?x=1&amp;y=2"></iframe>'}]}
        if path == "ad_account":
            return ACCOUNT
        if path == "campaigns":
            return {"data": CAMPAIGNS, "has_more": False}
        if path == "ad_groups":
            return {"data": GROUPS.get(params["campaign_id"], []), "has_more": False}
        if path == "ads":
            return {"data": ADS.get(params["ad_group_id"], []), "has_more": False}
        if path.startswith("ads/"):
            return FULL[path.split("/")[1]]
        raise AssertionError(path)


def run(refuse=False):
    real = gpt_api.Api
    gpt_api.Api = lambda client: FakeApi(client, refuse)
    try:
        return gpt_api.pull({"name": "Acme Client", "chatgpt_ads": True})
    finally:
        gpt_api.Api = real


def test_pull_reads_ads_settings_and_previews():
    d = run()
    assert d["account"]["brand"] == "Acme" and d["account"]["name"] == "Acme US" and d["account"]["icon"].endswith("icon.png")
    ids = [a["id"] for a in d["ads"]]
    assert ids == ["ad_1", "ad_2"], ids                          # archived campaign and archived ad left out
    a, m = d["ads"]
    assert a["format"] == "single" and a["title"] == "Unify your data" and a["images"] == ["https://cdn.example/1.png"] and a["link"].endswith("/a")
    assert a["status"] == "active" and a["review"] == "Approved" and a["preview"] == "https://ads.openai.com/preview/abc?x=1&y=2"
    s = a["settings"]
    assert s["bidding"] == "Clicks (CPC)" and s["bid"] == "$2.50 max CPC" and s["budget"] == "$5,000"
    assert s["platforms"] == ["Web"] and s["locations"] == 1 and s["hints"] == ["customer data"]
    assert m["format"] == "multi" and len(m["images"]) == 4 and m["status"] == "paused" and m["review"] == "In review"
    assert m["settings"]["bid"] == "$60.00 max CPM"                 # 60000 micros per impression is a $60 CPM
    assert m["preview"] == ""                                        # previews only for ads that are running


def test_a_refused_preview_drops_the_link_only():
    d = run(refuse=True)
    assert len(d["ads"]) == 2 and all(a["preview"] == "" for a in d["ads"])


def test_not_set_up_or_no_key():
    assert not gpt_api.enabled({"name": "x"}) and gpt_api.enabled({"chatgpt_ads": True})
    os.environ.pop("OPENAI_ADS_API_KEY_TEST", None)
    c = {"chatgpt_ads": True, "chatgpt_key_env": "OPENAI_ADS_API_KEY_TEST"}
    assert gpt_api.token(c) == ""
    try:
        gpt_api.Api(c)
        raise AssertionError("expected no key to be refused")
    except RuntimeError as e:
        assert "OPENAI_ADS_API_KEY_TEST" in str(e)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
