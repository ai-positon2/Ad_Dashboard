"""The comment emails name the ad on every platform, keyed as the page keys comments.

    python tests/test_comment_places.py      (or pytest)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("GADS_CACHE_DIR", tempfile.mkdtemp())

import main  # noqa: E402

DATA = {"ads": [{"id": "111", "account": "US", "campaign": "Brand", "adGroup": "Core", "kind": "RSA"}],
        "meta": {"account": {"name": "Example Marketing"}, "ads": [{"id": "222", "name": "Spring-Sale", "campaign": "P2-Meta", "adset": "Broad"}]},
        "reddit": {"account": {"name": "Example Reddit"}, "ads": [{"id": "t3_x", "headline": "Try it", "campaign": "R1", "adset": "Tech"}]},
        "gpt": None}


def test_every_platforms_ads_are_named_by_the_key_the_page_comments_with():
    p = main.comment_places(DATA)
    assert p["111"] == {"account": "US", "campaign": "Brand", "adGroup": "Core", "kind": "RSA"}
    assert p["meta:222"] == {"account": "Example Marketing", "campaign": "P2-Meta", "adGroup": "Broad", "kind": "Meta ad: Spring-Sale"}
    assert p["rd:t3_x"] == {"account": "Example Reddit", "campaign": "R1", "adGroup": "Tech", "kind": "Reddit ad: Try it"}
    assert main.comment_places({}) == {}


if __name__ == "__main__":
    test_every_platforms_ads_are_named_by_the_key_the_page_comments_with()
    print("ok")
