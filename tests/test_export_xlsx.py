"""export_xlsx.py: a sheet per platform with a row per ad, and a Copy check sheet with a row per issue.

    python tests/test_export_xlsx.py      (or pytest)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import load_workbook  # noqa: E402

import export_xlsx  # noqa: E402

ISSUE = {"field": "Headline", "text": "Subsribe and Save", "o": 0, "n": 8, "kind": "Spelling", "msg": "Possible spelling mistake found.", "fix": ["Subscribe"]}
PAYLOAD = {
    "ads": [{"id": 1, "kind": "RSA", "account": "Acme", "campaign": "Brand", "adGroup": "acme", "served": True, "impressions": 20,
             "strength": "Good", "headlines": ["Acme Cream", "Get 20% Off"], "descriptions": ["Soothes skin."], "finalUrl": "https://acme.test",
             "path1": "shop", "ext": {"sitelinks": [{"x": "Shop", "l": ["All products"]}], "callouts": ["Free shipping"],
                                      "promotions": [{"x": "15% off the kit", "l": ["unknown"]}]},
             "copy": {"issues": [], "promos": {"codes": [], "offers": [{"kind": "Percent off", "text": "Get 20% Off"}]}}}],
    "meta": {"ads": [{"id": "m1", "name": "Static 1", "status": "PAUSED", "campaign": "Sales", "adset": "Feed", "format": "image",
                      "primary": ["Use code GLOW20"], "headline": ["Subsribe and Save"], "description": [], "ctaText": "Shop now",
                      "enhancements": {"items": [{"label": "Site links", "group": "setup", "on": False}, {"label": "Add music", "group": "creative", "on": True}]},
                      "optimization": {"goal": "Purchases", "bid": "Highest volume"}, "placements": {"automatic": True},
                      "copy": {"issues": [ISSUE], "promos": {"codes": [{"code": "GLOW20", "field": "Primary text"}], "offers": []}}}]},
    "reddit": {"ads": []},
}


def test_a_sheet_per_platform_and_a_row_per_issue():
    path = Path(tempfile.mkdtemp()) / "ads.xlsx"
    export_xlsx.write(PAYLOAD, path)
    wb = load_workbook(path)
    assert wb.sheetnames == ["Google", "Meta", "Copy check"]                     # an empty platform gets no sheet
    g = {h.value: c.value for h, c in zip(wb["Google"][1], wb["Google"][2])}
    assert (g["Headlines"], g["Sitelinks"], g["Promotion assets"], g["Offers in the copy"], g["Served"]) == (
        "Acme Cream\nGet 20% Off", "Shop | All products", "15% off the kit", "Get 20% Off", "Yes")
    m = {h.value: c.value for h, c in zip(wb["Meta"][1], wb["Meta"][2])}
    assert (m["Status"], m["Promo codes"], m["Creative setup off"], m["Advantage+ enhancements on"], m["Placements"], m["Copy issues"]) == (
        "PAUSED", "GLOW20", "Site links", "Add music", "Automatic", 1)
    issue = [c.value for c in wb["Copy check"][2]]
    assert issue[:6] == ["Meta", "m1", "Static 1", "PAUSED", "Headline", 'Spelling: "Subsribe" → Subscribe']


if __name__ == "__main__":
    test_a_sheet_per_platform_and_a_row_per_issue()
    print("ok")
