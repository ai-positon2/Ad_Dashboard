"""A new comment is posted to Slack (when SLACK_COMMENT_WEBHOOK is set), and a Slack problem never raises.

    python tests/test_comment_slack.py      (or pytest)
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("GADS_CACHE_DIR", tempfile.mkdtemp())

import main  # noqa: E402

WHERE = {"account": "Tealium - ENG - US", "campaign": "Brand", "adGroup": "Core", "kind": "RSA"}


def test_message_names_who_where_and_links_to_the_ad_without_letting_the_comment_make_a_mention():
    t = main.slack_comment_text("Tealium", "https://x.up.railway.app/tealium/", "meta:12", "Marie <!channel>", "Fix <this> & that\nnow", WHERE)
    assert "*Marie &lt;!channel&gt;* commented on the Tealium ad previews dashboard" in t
    assert "Tealium - ENG - US › Brand › Core  ·  RSA" in t
    assert ">Fix &lt;this&gt; &amp; that\n>now" in t
    assert "<https://x.up.railway.app/tealium/#ad-meta:12|Open the ad>" in t
    assert "<!channel>" not in t


def test_it_posts_json_to_the_webhook_and_never_raises():
    seen = {}

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"ok"

    def fake(req, timeout=0):
        seen["url"], seen["body"] = req.full_url, json.loads(req.data)
        return R()
    with mock.patch.object(main.urllib.request, "urlopen", fake):
        assert main.post_comment_to_slack("Tealium", "https://x/", "1", "Marie", "hi", WHERE, webhook="https://hooks.slack.com/services/T/B/x") is None
    assert seen["url"].startswith("https://hooks.slack.com/") and "Marie" in seen["body"]["text"]
    with mock.patch.object(main.urllib.request, "urlopen", side_effect=OSError("down")):
        assert "down" in main.post_comment_to_slack("Tealium", "https://x/", "1", "Marie", "hi", WHERE, webhook="https://hooks.slack.com/services/T/B/x")
    assert main.post_comment_to_slack("Tealium", "https://x/", "1", "Marie", "hi", WHERE, webhook="") is None        # off without a webhook


if __name__ == "__main__":
    test_message_names_who_where_and_links_to_the_ad_without_letting_the_comment_make_a_mention()
    test_it_posts_json_to_the_webhook_and_never_raises()
    print("ok")
