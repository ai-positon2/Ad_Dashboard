"""Build the ad preview dashboard.

Downloads the client's ad-copy sheet (Supermetrics tabs + the Google Ads Script's
combination tabs), joins raw assets with Google's served combinations, downloads
image/video thumbnails (the published page can't load external images), and
writes dist/index.html + dist/assets/*.

    python build.py            # Tealium (default)
    python build.py oia        # any key in clients.json; on Railway set the CLIENT env var instead
"""
import hashlib
import io
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pandas as pd
from PIL import Image

ROOT = Path(__file__).parent
CLIENTS = json.loads((ROOT / "clients.json").read_text(encoding="utf-8"))
CLIENT_KEY = (sys.argv[1] if __name__ == "__main__" and len(sys.argv) > 1 else os.environ.get("CLIENT", "tealium")).lower()
if CLIENT_KEY not in CLIENTS:
    raise SystemExit(f"Unknown client '{CLIENT_KEY}'. Known: {', '.join(CLIENTS)}")
CLIENT = CLIENTS[CLIENT_KEY]
DIST = ROOT / "dist" / CLIENT_KEY
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
    prefix = CLIENT.get("account_prefix", "")
    return (name[len(prefix):] if prefix and name.startswith(prefix) else name).strip()


def tab(x, *names):
    """A sheet tab by any of its names, ignoring case and spaces ("URLs" == "URLS")."""
    norm = {k.strip().lower(): v for k, v in x.items()}
    return next((norm[n.lower()] for n in names if n.lower() in norm), None)


# Supermetrics tabs are optional; without them the scripts' tabs supply everything except
# ad strength and PMax search themes.
RSA_SHEET_COLS = ["Account", "Campaign name", "Ad strength", "Business name", "Headline 1 (Multi asset)",
                  "Responsive search ad headline 1", "Responsive search ad path 1", "Responsive search ad path 2"]
PMAX_SHEET_COLS = ["Campaign", "Headline 1", "Search theme", "Ad strength"]


def sheet_or_empty(df, columns):
    if df is None:
        return pd.DataFrame(columns=columns, dtype=str)
    df = df.dropna(how="all")
    for c in columns:
        if c not in df:
            df[c] = None
    return df


# --------------------------------------------------------------------- images
IMG_MAX_PX = 800  # longest side; ad previews never show images larger than this


def shrink(data):
    """Uploaded originals can be 5 MB print files: resize to preview size and re-encode as WebP."""
    im = Image.open(io.BytesIO(data))
    im.thumbnail((IMG_MAX_PX, IMG_MAX_PX))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA" if "transparency" in im.info or im.mode in ("LA", "P") else "RGB")
    out = io.BytesIO()
    im.save(out, "WEBP", quality=80, method=4)
    return out.getvalue()


class Assets:
    """Downloads remote images once (resized to preview size), returns the local published path."""

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
        target = self.dir / f"{key}.webp"
        old = next((f for f in self.dir.glob(f"{key}.*") if f != target), None)  # pre-resize cache file (left in place)
        try:
            if not target.exists():
                data = old.read_bytes() if old else fetch(url)[0]
                target.write_bytes(shrink(data))
            path = f"assets/{target.name}"
        except Exception as e:  # keep building; the preview shows a placeholder
            print(f"  ! image failed {url}: {e}")
            path = ""
        self.cache[url] = path
        return path

    def prefetch(self, urls):
        """Download many images in parallel up front; later get() calls hit the cache."""
        with ThreadPoolExecutor(16) as pool:
            list(pool.map(self.get, {u for u in urls if u}))

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


# --------------------------------------------------------------- live assets
EXT_TYPES = ("SITELINK", "CALLOUT", "STRUCTURED_SNIPPET", "AD_IMAGE", "PRICE", "PROMOTION", "CALL", "MOBILE_APP",
             "BUSINESS_LOGO", "BUSINESS_NAME")
LINE_TYPES = ("SITELINK", "PRICE", "PROMOTION", "CALL", "MOBILE_APP")  # text + detail lines
IMAGE_EXT = ("AD_IMAGE", "BUSINESS_LOGO")
IMAGE_FIELDS = ("MARKETING_IMAGE", "SQUARE_MARKETING_IMAGE", "PORTRAIT_MARKETING_IMAGE")
LOGO_FIELDS = ("LOGO", "LANDSCAPE_LOGO", "BUSINESS_LOGO")


def read_live(tab, assets):
    """Group the 'Live Assets' tab (export_assets.js) by owner: RSA ads, Demand Gen ads, asset groups, extensions."""
    live = {"rsa": {}, "dg": {}, "pmax": {}, "ext": defaultdict(lambda: defaultdict(list))}
    if tab is None or tab.dropna(how="all").empty:
        return None
    for _, r in tab.iterrows():
        level, field, owner = clean(r["Level"]), clean(r["Field"]), clean(r["Owner ID"])
        acct, camp, group = short_account(clean(r["Account"])), clean(r["Campaign"]), clean(r["Ad group / asset group"])
        text, img, vid = clean(r["Text"]), clean(r["Image URL"]), clean(r["Video ID"])
        lines = [l for l in clean(r["Lines"]).split(" | ") if l]
        if level in ("ACCOUNT", "CAMPAIGN", "AD_GROUP"):
            if field in ("LOGO", "LANDSCAPE_LOGO"):  # PMax brand assets live at campaign level as LOGO
                field = "BUSINESS_LOGO"
            if field in EXT_TYPES:
                key = (acct, level, camp if level != "ACCOUNT" else "", group if level == "AD_GROUP" else "")
                item = ({"x": text, "l": lines} if field in LINE_TYPES
                        else assets.get(img) if field in IMAGE_EXT else text)
                if item and item not in live["ext"][key][field]:
                    live["ext"][key][field].append(item)
            continue
        kind = "pmax" if level == "ASSET_GROUP" else ("rsa" if clean(r["Channel"]) == "SEARCH" else "dg")
        o = live[kind].setdefault(owner, {"account": acct, "campaign": camp, "group": group, "finalUrl": clean(r["Final URL"]),
                                          "path1": clean(r["Path 1"]), "path2": clean(r["Path 2"]), "headlines": [],
                                          "longHeadlines": [], "descriptions": [], "pins": {}, "images": [], "logos": [],
                                          "videos": [], "business": "", "cta": ""})
        if field == "HEADLINE" and text:
            o["headlines"].append(text)
        elif field == "LONG_HEADLINE" and text:
            o["longHeadlines"].append(text)
        elif field == "DESCRIPTION" and text:
            o["descriptions"].append(text)
        elif field in IMAGE_FIELDS and img:
            o["images"].append({"img": assets.get(img), "f": field})
        elif field in LOGO_FIELDS and img:
            o["logos"].append(assets.get(img))
        elif field == "YOUTUBE_VIDEO" and vid:
            o["videos"].append(vid)
        elif field == "BUSINESS_NAME" and text:
            o["business"] = text
        elif field.startswith("CALL_TO_ACTION") and text:
            o["cta"] = text
        pin = clean(r["Pinned"])
        if pin and pin != "UNSPECIFIED" and text:
            o["pins"][text] = pin.replace("HEADLINE_", "H").replace("DESCRIPTION_", "D")
    return live


def ext_for(live, account, campaign, group):
    """Extensions that apply to an ad: Google uses the most specific level that has that asset type."""
    out = {}
    for t in EXT_TYPES:
        for key in ((account, "AD_GROUP", campaign, group), (account, "CAMPAIGN", campaign, ""), (account, "ACCOUNT", "", "")):
            vals = live["ext"].get(key, {}).get(t)
            if vals:
                out[t] = vals
                break
    return {"sitelinks": out.get("SITELINK", []), "callouts": out.get("CALLOUT", []),
            "snippets": out.get("STRUCTURED_SNIPPET", []), "images": out.get("AD_IMAGE", []),
            "prices": out.get("PRICE", []), "promotions": out.get("PROMOTION", []), "calls": out.get("CALL", []),
            "apps": out.get("MOBILE_APP", []), "logo": (out.get("BUSINESS_LOGO") or [""])[0],
            "business": (out.get("BUSINESS_NAME") or [""])[0]}


def apply_live(ads, live, sheet, assets):
    """Swap sheet/served asset lists for what is live in Google Ads right now."""
    by_id = {a["id"]: a for a in ads}
    out = [a for a in ads if not a["id"].startswith(("sheet-", "dg-"))]  # sheet-only rows are replaced by live ones
    for ad_id, o in live["rsa"].items():
        ad = by_id.get(ad_id)
        if ad is None:  # live, but no impressions in the combination window
            ad = {"id": ad_id, "kind": "RSA", "account": o["account"], "campaign": o["campaign"], "adGroup": o["group"],
                  "served": False, "impressions": 0, "combos": [], "strength": ""}
            out.append(ad)
        ad.update({"headlines": o["headlines"], "descriptions": o["descriptions"], "pins": o["pins"], "live": True,
                   "finalUrl": o["finalUrl"] or ad.get("finalUrl", ""), "path1": o["path1"], "path2": o["path2"]})
        ad["ext"] = ext_for(live, o["account"], o["campaign"], o["group"])
    for gid, o in live["pmax"].items():
        g = by_id.get(gid)
        if g is None:
            g = {"id": gid, "kind": "PMAX", "account": o["account"], "campaign": o["campaign"], "adGroup": o["group"],
                 "served": False, "impressions": None, "combos": [], "strength": ""}
            out.append(g)
        g.update({"headlines": o["headlines"], "longHeadlines": o["longHeadlines"], "descriptions": o["descriptions"],
                  "images": o["images"], "logos": o["logos"], "business": o["business"], "cta": o["cta"] or g.get("cta", ""),
                  "videoThumbs": [{"vid": v, "img": assets.video_thumb(v)} for v in o["videos"]], "live": True,
                  "finalUrl": o["finalUrl"] or g.get("finalUrl", "")})
        g["ext"] = ext_for(live, o["account"], o["campaign"], "")
    strengths = {}
    for _, r in sheet[sheet["Business name"].notna()].iterrows():  # Demand Gen ad strength only lives in the sheet
        strengths.setdefault(clean(r["Campaign name"]), []).append(
            ({clean(r.get(f"Headline {n} (Multi asset)")) for n in range(1, 6)}, clean(r["Ad strength"])))
    for ad_id, o in live["dg"].items():
        best = max(strengths.get(o["campaign"], []), key=lambda s: len(s[0] & set(o["headlines"])), default=(set(), ""))
        out.append({"id": ad_id, "kind": "DG", "account": o["account"], "campaign": o["campaign"], "adGroup": o["group"],
                    "finalUrl": o["finalUrl"], "path1": "", "path2": "", "served": False, "impressions": None,
                    "strength": best[1], "business": o["business"], "cta": o["cta"], "headlines": o["headlines"],
                    "longHeadlines": o["longHeadlines"], "descriptions": o["descriptions"], "images": o["images"],
                    "logos": o["logos"], "videoThumbs": [{"vid": v, "img": assets.video_thumb(v)} for v in o["videos"]],
                    "combos": [], "live": True, "ext": ext_for(live, o["account"], o["campaign"], o["group"])})
    return out


def account_logos(ads, live):
    """Each account's real business logo: live account asset, else the logo Google served most, else a PMax logo."""
    seen = defaultdict(lambda: defaultdict(int))
    for a in ads:
        for c in a["combos"]:
            for p in c["parts"]:
                if p["f"] in ("BUSINESS_LOGO", "LOGO") and p.get("img"):
                    seen[a["account"]][(p["f"] != "BUSINESS_LOGO", p["img"])] += 1  # prefer BUSINESS_LOGO, then count
    logos = {acct: min(c, key=lambda k: (k[0], -c[k]))[1] for acct, c in seen.items()}
    if live:
        for (acct, level, _, _), types in live["ext"].items():
            if level == "ACCOUNT" and types.get("BUSINESS_LOGO"):
                logos[acct] = types["BUSINESS_LOGO"][0]
    return logos


# ------------------------------------------------------------ preview links
PREVIEW_DAYS = 30  # the team creates shared preview links with the 30-day expiry option


def build_preview_links(tab):
    """Google 'External Preview' links the team pastes into the sheet (tab "URLS" or "Preview Links").

    Columns: Campaign | Ad Group (or asset group) | Ad ID (optional) | Format | Desktop URL | Mobile URL | Created.
    A single URL column also works. Each row is one ad; rows sharing a campaign + ad group are numbered.
    """
    if tab is None or tab.dropna(how="all").empty:
        return []
    col = {c.lower().strip(): c for c in tab.columns}
    pick = lambda r, *names: next((clean(r[col[n]]) for n in names if n in col and clean(r[col[n]])), "")
    out, seen = [], defaultdict(int)
    for _, r in tab.iterrows():
        desktop = pick(r, "desktop url", "url", "preview link", "link")
        mobile = pick(r, "mobile url")
        if not (desktop.startswith("http") or mobile.startswith("http")):
            continue  # rows still waiting for a link
        created = pick(r, "created", "date", "added")
        try:
            # real date cells arrive as "2026-10-06 00:00:00" (year first); typed text is day-first, e.g. "28/9/2026"
            made = pd.to_datetime(created, dayfirst=not re.match(r"\d{4}-", created)).date()
            expires = (made + pd.Timedelta(days=PREVIEW_DAYS)).isoformat()
        except (ValueError, TypeError):
            made, expires = None, ""
        camp, group = pick(r, "campaign"), pick(r, "ad group", "asset group", "ad group / asset group")
        seen[(camp, group)] += 1
        out.append({"campaign": camp, "group": group, "adId": pick(r, "ad id"), "format": pick(r, "format", "type"),
                    "n": seen[(camp, group)], "desktop": desktop if desktop.startswith("http") else "",
                    "mobile": mobile if mobile.startswith("http") else "",
                    "created": made.isoformat() if made else "", "expires": expires})
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
    rc, pc = tab(x, "RSA Combinations"), tab(x, "PMax Combinations")
    for name, df in (("RSA Combinations", rc), ("PMax Combinations", pc)):
        if df is None:
            raise SystemExit(f"'{name}' tab missing — has the combinations Google Ads Script run for {CLIENT['name']}?")
    live_tab, perf_tab = tab(x, "Live Assets"), tab(x, "Campaign Performance")
    rsa_sheet = sheet_or_empty(tab(x, "RSA and Demand Gen", "Search and Demand"), RSA_SHEET_COLS)
    assets = Assets()
    urls = list(live_tab["Image URL"].dropna()) if live_tab is not None and "Image URL" in live_tab else []
    for df in (rc, pc):
        for j in df["Assets JSON"].dropna():
            urls += [a.get("url") for a in json.loads(j)]
    assets.prefetch(urls)
    ads = (build_rsa(rsa_sheet, rc.dropna(how="all"), assets)
           + build_pmax(sheet_or_empty(tab(x, "Pmax"), PMAX_SHEET_COLS), pc.dropna(how="all"), assets)
           + build_demand_gen(rsa_sheet))
    live = read_live(live_tab, assets)
    if live:
        ads = apply_live(ads, live, rsa_sheet, assets)
    logos = account_logos(ads, live)

    first = lambda df, col: clean(df[col].dropna().iloc[0]) if df is not None and col in df and df[col].notna().any() else ""
    payload = {
        "client": CLIENT["name"], "domain": CLIENT["domain"],
        "dateRange": first(rc, "Date range").replace("_", " ").title(),
        "exportedAt": first(rc, "Exported at") or first(pc, "Exported at"),
        "builtAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "ads": ads,
        "logos": logos,
        "previewLinks": build_preview_links(tab(x, "URLS", "Preview Links")),
        "liveAssets": bool(live),
        "liveExportedAt": first(live_tab, "Exported at") if live else "",
        "performance": build_performance(perf_tab),
        "perfExportedAt": first(perf_tab, "Exported at"),
    }
    template = (ROOT / "dashboard" / "template.html").read_text(encoding="utf-8").replace("__CLIENT__", CLIENT["name"])
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = template.replace("/*__DATA__*/null", blob)
    DIST.mkdir(parents=True, exist_ok=True)
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
