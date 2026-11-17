"""build.build_landing: the Landing pages tab is made from the live Google Ads ads, not copied from a sheet.

    python tests/test_landing_pages.py      (or pytest)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build  # noqa: E402


def ad(kind, campaign, url, live=True, **kw):
    return dict({"id": 1, "kind": kind, "campaign": campaign, "finalUrl": url, "live": live}, **kw)


def rows(ads):
    build.CLIENT = {"landing_pages": True}
    data, err = build.build_landing(ads)
    assert err == ""
    return data["rows"]


def test_one_row_per_page_and_campaign_with_ads_and_types():
    r = rows([ad("RSA", "Non-Brand-Competitor-APAC", "https://tealium.com/tealium-vs-treasure-data/?utm=x"),
              ad("RSA", "Non-Brand-Competitor-APAC", "https://Tealium.com/tealium-vs-treasure-data"),
              ad("DG", "P2-DemandGen-APAC", "https://tealium.com/industry-retail-2/", images=["a"], videoThumbs=[{"vid": "v"}]),
              ad("PMAX", "P2-Brand-Core", "https://tealium.com/platform/cdp-overview/")])
    assert ["APAC", "Competitor Comparison", "Tealium Vs Treasure Data", "https://tealium.com/tealium-vs-treasure-data/", "Non-Brand-Competitor-APAC", "2", "RSA"] in r
    assert ["APAC", "Industry", "Industry Retail 2", "https://tealium.com/industry-retail-2/", "P2-DemandGen-APAC", "1", "DG Image, DG Video"] in r
    assert ["NAM", "Platform", "Cdp Overview", "https://tealium.com/platform/cdp-overview/", "P2-Brand-Core", "1", "PMax"] in r


def test_paused_ads_and_ads_without_a_page_are_left_out():
    r = rows([ad("RSA", "A", "https://x.test/a/"), ad("RSA", "A", "https://x.test/b/", live=False), ad("RSA", "A", "")])
    assert [x[3] for x in r] == ["https://x.test/a/"]


def test_not_set_up_gives_nothing():
    build.CLIENT = {}
    assert build.build_landing([ad("RSA", "A", "https://x.test/a/")]) == (None, "")


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("ok")
