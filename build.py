"""Build the ad preview dashboard.

Downloads the client's ad-copy sheet (Supermetrics tabs + the Google Ads Script's
combination tabs), joins raw assets with Google's served combinations, downloads
image/video thumbnails (the published page can't load external images), and
writes dist/index.html + dist/assets/*.

    python build.py            # Tealium (default)
"""
import hashlib
import io
import json
import re
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
DIST = ROOT / "dist"
CLIENT = {
    "name": "Tealium",
    "sheet_id": "1lMWQI2RECyNgFqwHZ6r2gs0WhA3EsOvj2uha9Qwu-70",
    "domain": "tealium.com",
}
MATCH_MIN = 0.6  # headline-set overlap needed to tie a sheet RSA row to a served ad


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read(), r.headers.get("Content-Type", "")


def clean(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def cols(row, prefix, n):
    return [t for t in (clean(row.get(f"{prefix} {i}")) for i in range(1, n + 1)) if t]


def short_account(name):
    return name.replace(f"{CLIENT['name']} - ENG - ", "").strip()


# --------------------------------------------------------------------- images
class Assets:
    """Downloads remote images once, returns the local published path."""

    def __init__(self):
        self.dir = DIST / "assets"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cache = {}

    def get(self, url):
        if not url:
            return ""
        if url in self.cache:
            return self.cache[url]
        key = hashlib.sha1(url.encode()).hexdigest()[:16]
        cached = next(self.dir.glob(f"{key}.*"), None)  # assets are immutable per URL
        if cached:
            self.cache[url] = f"assets/{cached.name}"
            return self.cache[url]
        try:
            data, ctype = fetch(url)
            ext = {"image/png": "png", "image/gif": "gif", "image/webp": "webp"}.get(ctype.split(";")[0], "jpg")
            (self.dir / f"{key}.{ext}").write_bytes(data)
            path = f"assets/{key}.{ext}"
        except Exception as e:  # keep building; the preview shows a placeholder
            print(f"  ! image failed {url}: {e}")
            path = ""
        self.cache[url] = path
        return path

    def video_thumb(self, vid):
        return self.get(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg") if vid else ""


def part(a, assets):
    """One served asset -> compact dict for the page."""
    p = {"f": a["field"], "t": a["type"]}
    if a.get("text"):
        p["x"] = a["text"]
    if a.get("lines"):
        p["l"] = a["lines"]
    if a.get("url"):
        p["img"] = assets.get(a["url"])
    if a.get("video"):
        p["vid"] = a["video"]
        p["img"] = assets.video_thumb(a["video"])
    return p


# ----------------------------------------------------------------------- RSA
def build_rsa(sheet, combos, assets):
    ads = {}
    for _, r in combos.iterrows():
        ad_id = clean(r["Ad ID"])
        ad = ads.get(ad_id)
        if not ad:
            ad = ads[ad_id] = {
                "id": ad_id, "kind": "RSA", "account": short_account(clean(r["Account"])),
                "campaign": clean(r["Campaign"]), "adGroup": clean(r["Ad group"]),
                "finalUrl": clean(r["Final URL"]), "path1": clean(r["Path 1"]), "path2": clean(r["Path 2"]),
                "served": True, "impressions": 0, "combos": [], "strength": "",
            }
        imp = int(float(clean(r["Impressions"]) or 0))
        ad["impressions"] += imp
        ad["combos"].append({"rank": int(float(clean(r["Rank"]))), "imp": imp,
                             "parts": [part(a, assets) for a in json.loads(r["Assets JSON"])]})

    # tie sheet rows (full 15H/4D + ad strength) to served ads by headline overlap
    rows = sheet[sheet["Responsive search ad headline 1"].notna()]
    unmatched = []
    for _, r in rows.iterrows():
        heads = cols(r, "Responsive search ad headline", 15)
        descs = cols(r, "Responsive search ad description", 5)
        camp = clean(r["Campaign name"])
        best, score = None, 0
        for ad in ads.values():
            if ad["campaign"] != camp:
                continue
            served_h = {p.get("x") for c in ad["combos"] for p in c["parts"] if p["f"].startswith("HEADLINE_")}
            s = len(set(heads) & served_h) / max(1, len(served_h))
            if s > score:
                best, score = ad, s
        if best and score >= MATCH_MIN and "headlines" not in best:
            best["headlines"], best["descriptions"] = heads, descs
            best["strength"] = clean(r["Ad strength"])
        else:
            unmatched.append({
                "id": f"sheet-{len(unmatched) + 1}", "kind": "RSA",
                "account": short_account(clean(r["Account"])), "campaign": camp, "adGroup": "",
                "finalUrl": "", "path1": clean(r["Responsive search ad path 1"]),
                "path2": clean(r["Responsive search ad path 2"]), "served": False, "impressions": 0,
                "strength": clean(r["Ad strength"]), "headlines": heads, "descriptions": descs, "combos": [],
            })

    for ad in ads.values():
        ad["combos"].sort(key=lambda c: c["rank"])
        served = lambda pre: list(dict.fromkeys(p["x"] for c in ad["combos"] for p in c["parts"]
                                                if p["f"].startswith(pre) and p.get("x")))
        # served-only assets (sheet row missing or different) are still listed
        ad["headlines"] = list(dict.fromkeys(ad.get("headlines", []) + served("HEADLINE_")))
        ad["descriptions"] = list(dict.fromkeys(ad.get("descriptions", []) + served("DESCRIPTION_")))
    return list(ads.values()) + unmatched


# ---------------------------------------------------------------------- PMax
def build_pmax(sheet, combos, assets):
    groups = {}
    for _, r in combos.iterrows():
        key = clean(r["Asset group ID"])
        g = groups.get(key)
        if not g:
            g = groups[key] = {
                "id": key, "kind": "PMAX", "account": short_account(clean(r["Account"])),
                "campaign": clean(r["Campaign"]), "adGroup": clean(r["Asset group"]),
                "finalUrl": clean(r["Final URL"]), "path1": clean(r["Path 1"]), "path2": clean(r["Path 2"]),
                "served": True, "impressions": None, "combos": [], "strength": "",
            }
        g["combos"].append({"rank": int(float(clean(r["Rank"]))), "cat": clean(r["Category"]),
                            "parts": [part(a, assets) for a in json.loads(r["Assets JSON"])]})

    # asset lists, strength and search themes from the Supermetrics Pmax tab
    sheet = sheet.copy()
    sheet["Campaign"] = sheet["Campaign"].ffill()
    for camp, rows in sheet.groupby("Campaign", sort=False):
        head = rows[rows["Headline 1"].notna()]
        g = next((g for g in groups.values() if g["campaign"] == camp), None)
        if g is None or head.empty:
            continue
        h = head.iloc[0]
        g["headlines"] = cols(h, "Headline", 15)
        g["longHeadlines"] = cols(h, "Long headline", 5)
        g["descriptions"] = cols(h, "Description", 5)
        g["cta"] = clean(h.get("Call to action"))
        g["videos"] = cols(h, "Video ID", 15)
        g["strength"] = clean(h.get("Ad strength"))
        g["themes"] = [clean(t) for t in rows["Search theme"] if clean(t)]
    for g in groups.values():
        g["combos"].sort(key=lambda c: ({"TEXT": 0, "IMAGE": 1, "VIDEO": 2}.get(c["cat"], 3), c["rank"]))
        imgs = []
        for c in g["combos"]:
            for p in c["parts"]:
                if p.get("img") and not p.get("vid") and p["img"] not in [i["img"] for i in imgs]:
                    imgs.append({"img": p["img"], "f": p["f"]})
        g["images"] = imgs
        g["videoThumbs"] = [{"vid": v, "img": assets.video_thumb(v)} for v in g.get("videos", [])]
    return list(groups.values())


# ---------------------------------------------------------------- Demand Gen
def build_demand_gen(sheet):
    rows = sheet[sheet["Business name"].notna() & sheet["Headline 1 (Multi asset)"].notna()]
    out = []
    for i, (_, r) in enumerate(rows.iterrows(), 1):
        out.append({
            "id": f"dg-{i}", "kind": "DG", "account": short_account(clean(r["Account"])),
            "campaign": clean(r["Campaign name"]), "adGroup": "", "finalUrl": "", "path1": "", "path2": "",
            "served": False, "impressions": None, "strength": clean(r["Ad strength"]),
            "business": clean(r["Business name"]),
            "headlines": cols(r, "Headline", 15) or [clean(r.get(f"Headline {n} (Multi asset)")) for n in range(1, 16)
                                                      if clean(r.get(f"Headline {n} (Multi asset)"))],
            "descriptions": [clean(r.get(f"Description {n} (Multi asset)")) for n in range(1, 6)
                             if clean(r.get(f"Description {n} (Multi asset)"))],
            "combos": [],
        })
    return out


# ----------------------------------------------------- campaign performance
def build_performance(tab):
    """Spend/clicks/conversions per enabled campaign, from the Google Ads Script's tab."""
    if tab is None or tab.dropna(how="all").empty:
        return []
    num = lambda v: float(clean(v) or 0)
    out = []
    for _, r in tab.iterrows():
        out.append({
            "account": short_account(clean(r["Account"])), "currency": clean(r["Currency"]),
            "campaign": clean(r["Campaign"]), "channel": clean(r["Channel"]), "status": clean(r["Status"]),
            "liveAds": int(num(r["Live ads"])),
            "d30": {k: num(r[f"{c} 30d"]) for k, c in (("cost", "Cost"), ("clicks", "Clicks"), ("impr", "Impressions"),
                                                        ("conv", "Conversions"), ("value", "Conv. value"))},
            "mtd": {k: num(r[f"{c} MTD"]) for k, c in (("cost", "Cost"), ("clicks", "Clicks"), ("impr", "Impressions"),
                                                        ("conv", "Conversions"), ("value", "Conv. value"))},
        })
    return out


def main():
    url = f"https://docs.google.com/spreadsheets/d/{CLIENT['sheet_id']}/export?format=xlsx"
    print("Downloading sheet...")
    data, _ = fetch(url)
    x = pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=str)
    for tab in ("RSA Combinations", "PMax Combinations"):
        if tab not in x or x[tab].dropna(how="all").empty:
            raise SystemExit(f"'{tab}' tab missing or empty — has the Google Ads Script run?")

    rsa_sheet = x["RSA and Demand Gen"].dropna(how="all")
    assets = Assets()
    ads = (build_rsa(rsa_sheet, x["RSA Combinations"], assets)
           + build_pmax(x["Pmax"], x["PMax Combinations"], assets)
           + build_demand_gen(rsa_sheet))

    rc = x["RSA Combinations"]
    payload = {
        "client": CLIENT["name"], "domain": CLIENT["domain"],
        "dateRange": clean(rc["Date range"].iloc[0]).replace("_", " ").title(),
        "exportedAt": clean(rc["Exported at"].iloc[0]),
        "builtAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "ads": ads,
        "performance": build_performance(x.get("Campaign Performance")),
        "perfExportedAt": clean(x["Campaign Performance"]["Exported at"].iloc[0])
        if "Campaign Performance" in x and not x["Campaign Performance"].empty else "",
    }
    template = (ROOT / "dashboard" / "template.html").read_text(encoding="utf-8")
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = template.replace("/*__DATA__*/null", blob)
    tmp = DIST / "index.html.tmp"
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(DIST / "index.html")  # atomic swap: the web server never serves a half-written page
    (DIST / "data.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    kinds = defaultdict(int)
    for a in ads:
        kinds[(a["kind"], a["served"])] += 1
    print("Ads:", dict(kinds), "| images:", len([p for p in assets.cache.values() if p]),
          "| html KB:", len(html.encode()) // 1024)


if __name__ == "__main__":
    main()
