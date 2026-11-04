"""The dashboard's ads as an Excel workbook (clients with `"download": true`): a sheet per platform with a row per ad,
and a Copy check sheet with a row per issue. Built with each refresh, from the same data the page shows."""
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

FILE = "ads.xlsx"
HEAD_FILL = PatternFill("solid", fgColor="1F2937")
HEAD_FONT = Font(bold=True, color="FFFFFF")
WRAP = Alignment(wrap_text=True, vertical="top")
MAX_CELL = 32000  # Excel's limit is 32,767 characters a cell


def _lines(xs):
    return "\n".join(str(x) for x in (xs or []) if x not in (None, ""))


def _copy(ad):
    c = ad.get("copy") or {}
    p = c.get("promos") or {}
    issues = c.get("issues") or []
    return (", ".join(x["code"] for x in p.get("codes") or []),
            _lines(sorted({o["text"] for o in p.get("offers") or []})),
            len(issues))


def _issue_text(i):
    word = i["text"][i["o"]:i["o"] + i["n"]]
    fix = (" → " + " / ".join(i["fix"][:2])) if i.get("fix") else ""
    return f'{i["kind"]}: "{word}"{fix}'


def _google_rows(ads):
    for a in ads:
        e = a.get("ext") or {}
        codes, offers, n = _copy(a)
        promo_assets = [" ".join([p.get("x") or ""] + [x for x in p.get("l") or [] if x and x != "unknown"]).strip() for p in e.get("promotions") or []]
        yield [a.get("id"), a.get("kind"), a.get("account"), a.get("campaign"), a.get("adGroup"),
               "Yes" if a.get("served") else "No", a.get("impressions") or 0, a.get("strength") or "",
               _lines(a.get("headlines")), _lines(a.get("longHeadlines")), _lines(a.get("descriptions")),
               a.get("finalUrl") or "", "/".join(x for x in (a.get("path1"), a.get("path2")) if x),
               _lines(" | ".join([s.get("x") or ""] + list(s.get("l") or [])) for s in e.get("sitelinks") or []),
               _lines(e.get("callouts")), _lines(promo_assets), codes, offers, n]


GOOGLE_COLS = [("Ad ID", 16), ("Type", 10), ("Account", 18), ("Campaign", 28), ("Ad group", 24), ("Served", 8), ("Impressions", 11),
               ("Ad strength", 11), ("Headlines", 40), ("Long headlines", 40), ("Descriptions", 50), ("Final URL", 40), ("Path", 16),
               ("Sitelinks", 40), ("Callouts", 26), ("Promotion assets", 30), ("Promo codes", 14), ("Offers in the copy", 24), ("Copy issues", 10)]


def _meta_rows(ads):
    for a in ads:
        codes, offers, n = _copy(a)
        en = a.get("enhancements") or {}
        items = en.get("items") or []
        group = lambda g, on: ", ".join(i["label"] for i in items if i.get("group") == g and bool(i.get("on")) == on)  # noqa: E731
        o = a.get("optimization") or {}
        pl = a.get("placements") or {}
        yield [a.get("id"), a.get("name"), a.get("status"), a.get("created"), a.get("campaign"), a.get("adset"), a.get("objective"),
               a.get("format"), _lines(a.get("primary")), _lines(a.get("headline")), _lines(a.get("description")),
               a.get("ctaText") or a.get("cta") or "", a.get("link") or "",
               "Automatic" if pl.get("automatic") else ", ".join(pl.get("networks") or []),
               group("setup", True), group("setup", False), group("creative", True), group("creative", False),
               o.get("goal") or "", o.get("bid") or "", codes, offers, n]


META_COLS = [("Ad ID", 20), ("Ad name", 30), ("Status", 14), ("Created", 11), ("Campaign", 28), ("Ad set", 26), ("Objective", 16),
             ("Format", 9), ("Primary text", 60), ("Headline", 36), ("Description", 40), ("Call to action", 14), ("Link", 36),
             ("Placements", 18), ("Creative setup on", 18), ("Creative setup off", 18), ("Advantage+ enhancements on", 30),
             ("Advantage+ enhancements off", 30), ("Goal", 16), ("Bid strategy", 16), ("Promo codes", 14), ("Offers in the copy", 24), ("Copy issues", 10)]


def _other_rows(ads):
    for a in ads:
        codes, offers, n = _copy(a)
        yield [a.get("id"), a.get("name") or "", a.get("status") or "", a.get("campaign") or "",
               a.get("headline") or a.get("title") or "", a.get("body") or "", a.get("link") or a.get("url") or "", codes, offers, n]


OTHER_COLS = [("Ad ID", 20), ("Ad name", 30), ("Status", 14), ("Campaign", 28), ("Headline", 40), ("Text", 60), ("Link", 36),
              ("Promo codes", 14), ("Offers in the copy", 24), ("Copy issues", 10)]


def _sheet(wb, title, cols, rows):
    ws = wb.create_sheet(title)
    ws.append([c for c, _ in cols])
    for cell in ws[1]:
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
    for r in rows:
        ws.append([(v[:MAX_CELL] if isinstance(v, str) else v) for v in r])
    for i, (_, w) in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = WRAP
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    return ws


def workbook(payload):
    """The workbook for one dashboard payload."""
    wb = Workbook()
    wb.remove(wb.active)
    plats = [("Google", "g", payload.get("ads") or [], GOOGLE_COLS, _google_rows)]
    for key, title, cols, rows in (("meta", "Meta", META_COLS, _meta_rows), ("reddit", "Reddit", OTHER_COLS, _other_rows),
                                   ("gpt", "ChatGPT", OTHER_COLS, _other_rows)):
        ads = (payload.get(key) or {}).get("ads") or []
        if ads:
            plats.append((title, key, ads, cols, rows))
    issues = []
    for title, _, ads, cols, rows in plats:
        _sheet(wb, title, cols, rows(ads))
        for a in ads:
            for i in (a.get("copy") or {}).get("issues") or []:
                issues.append([title, str(a.get("id")), a.get("name") or a.get("campaign") or "", a.get("status") or ("Served" if a.get("served") else ""),
                               i["field"], _issue_text(i), i.get("msg") or "", i["text"]])
    _sheet(wb, "Copy check", [("Platform", 10), ("Ad ID", 20), ("Ad", 30), ("Status", 12), ("Field", 16), ("Issue", 34), ("Why", 50), ("Text", 70)], issues)
    return wb


def write(payload, path):
    workbook(payload).save(path)
