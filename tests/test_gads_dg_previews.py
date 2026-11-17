"""gads_api.dg_previews: Demand Gen preview links from Google when it accepts them, never fatal when it refuses.

    python tests/test_gads_dg_previews.py      (or pytest)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gads_api  # noqa: E402

ROW = {"campaign": {"name": "DG-1"}, "adGroup": {"id": "7", "name": "Theme-Retail"}, "adGroupAd": {"ad": {"id": "99"}}}


class FakeApi:
    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    def search(self, cid, q):
        return [ROW]

    def _post(self, cid, body, suffix, version):
        self.sent.append(body)
        if self.fail:
            raise gads_api.ApiError(400, '"message": "Unknown name adGroupAdIdentifier"')
        return {"responses": [{"shareablePreviewResult": {"shareablePreviewUrl": "https://ads.google.com/p/1", "expirationDateTime": "2026-12-01 00:00:00"}}]}


def test_accepted_links_are_returned_per_ad():
    log = []
    out = gads_api.dg_previews(FakeApi(), {"id": "1"}, "2026-11-17 10:00", log.append)
    assert out == [{"id": "99", "campaign": "DG-1", "group": "Theme-Retail", "url": "https://ads.google.com/p/1", "expires": "2026-12-01", "created": "2026-11-17"}]


def test_a_refusal_is_logged_and_returns_nothing():
    log = []
    assert gads_api.dg_previews(FakeApi(fail=True), {"id": "1"}, "2026-11-17 10:00", log.append) == []
    assert "refused by Google" in log[0] and "adGroupAdIdentifier" in log[0]


if __name__ == "__main__":
    test_accepted_links_are_returned_per_ad()
    test_a_refusal_is_logged_and_returns_nothing()
    print("ok")
