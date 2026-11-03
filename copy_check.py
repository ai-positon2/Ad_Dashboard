"""Ad copy checks: spelling, grammar and punctuation, and the promotions an ad runs (promo codes, percent off and the like).

Spelling, grammar and punctuation come from LanguageTool (LANGUAGETOOL_URL: our own server, for example
http://languagetool.railway.internal:8010/v2, or https://api.languagetool.org/v2). Without it only the checks made here are run: the same
trademark written with ® in one place and ™ in another, and doubled punctuation. A Refresh checks all the copy again; the hourly rebuilds
check only copy not seen before. Only the answers for the copy now in the ads are kept, so a fixed mistake is gone after the next refresh.

Promotions are read from the copy itself: a promo code ("use code SAVE15"), percent or money off, free shipping, subscribe and save.
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import gads_api

CACHE_VERSION = 1
PUBLIC_MAX_REQUESTS = 15     # LanguageTool's public server allows 20 a minute; what is left is checked on the next refresh. Our own has no limit
CHUNK = 15000                # characters per request (the public server takes up to 20 KB)
# the kinds of LanguageTool match shown; style and wording suggestions are left out
KINDS = {"TYPOS": "Spelling", "GRAMMAR": "Grammar", "CONFUSED_WORDS": "Grammar", "COLLOCATIONS": "Grammar", "NONSTANDARD_PHRASES": "Grammar",
         "PUNCTUATION": "Punctuation", "TYPOGRAPHY": "Punctuation", "CASING": "Capitalisation"}
# rules that misfire on ad copy: headlines carry no full stop, curly and straight quotes are both fine, dashes are a house choice
DISABLED = ("PUNCTUATION_PARAGRAPH_END", "EN_QUOTES", "DASH_RULE", "ENGLISH_WORD_REPEAT_BEGINNING_RULE", "EN_UNPAIRED_QUOTES", "PLUS_MINUS", "MULTIPLICATION_SIGN")


def endpoint():
    return os.environ.get("LANGUAGETOOL_URL", "").strip().rstrip("/")


def _key(lang, text):
    return hashlib.sha1((lang + "\n" + text).encode("utf-8")).hexdigest()[:20]


def _cache_path():
    return gads_api.CACHE_DIR / "copycheck.json"


def _load_cache():
    try:
        got = json.loads(_cache_path().read_text(encoding="utf-8"))
        return got.get("texts") or {} if got.get("version") == CACHE_VERSION else {}
    except (OSError, ValueError):
        return {}


def _save_cache(texts):
    gads_api.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _cache_path().with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": CACHE_VERSION, "texts": texts}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_cache_path())


def _ask(url, text, lang):
    body = urllib.parse.urlencode({"text": text, "language": lang, "disabledRules": ",".join(DISABLED)}).encode()
    req = urllib.request.Request(url + "/check", data=body, headers={"User-Agent": "ad-preview-dashboard", "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("matches") or []


def _public(url):
    return "api.languagetool.org" in url


def language_tool(texts, lang, log, fresh=False):
    """{text: [match]} for each text, from LanguageTool or (not `fresh`) the last answer for the same text. Texts are sent many to a request,
    a blank line apart, and the answers split back. The cache keeps only these texts' answers."""
    url = endpoint()
    if not url:
        return {}
    cache, out, todo = ({} if fresh else _load_cache()), {}, []
    for t in texts:
        k = _key(lang, t)
        if k in cache:
            out[t] = cache[k]
        elif t not in todo:
            todo.append(t)
    chunks, cur = [], []
    for t in todo:
        if cur and sum(len(x) + 2 for x in cur) + len(t) > CHUNK:
            chunks.append(cur)
            cur = []
        cur.append(t)
    if cur:
        chunks.append(cur)
    limit = PUBLIC_MAX_REQUESTS if _public(url) else len(chunks)
    sent, failed = 0, False
    for chunk in chunks[:limit]:
        joined, starts, pos = "", [], 0
        for t in chunk:
            starts.append(pos)
            joined += t + "\n\n"
            pos += len(t) + 2
        try:
            if sent and _public(url):
                time.sleep(3.5)  # stay under 20 requests a minute
            matches = _ask(url, joined, lang)
            sent += 1
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.append("The spelling and grammar check could not reach LanguageTool (%s); it runs again on the next refresh" % str(e)[:120])
            failed = True
            break
        per = {t: [] for t in chunk}
        for m in matches:
            o, n = m.get("offset", 0), m.get("length", 0)
            for t, s in zip(chunk, starts):
                if s <= o and o + n <= s + len(t):
                    per[t].append({"o": o - s, "n": n, "msg": m.get("message") or "", "rule": (m.get("rule") or {}).get("id") or "",
                                   "cat": ((m.get("rule") or {}).get("category") or {}).get("id") or "", "fix": [r.get("value") for r in (m.get("replacements") or [])[:3]]})
                    break
        for t in chunk:
            cache[_key(lang, t)] = out[t] = per[t]
    if len(chunks) > limit and not failed:
        log.append("%d ad texts are not checked for spelling and grammar yet; they are checked on the next refresh" % sum(len(c) for c in chunks[limit:]))
    if sent or not fresh:
        _save_cache({k: v for k, v in cache.items() if k in {_key(lang, t) for t in out}})  # copy no longer in the ads is dropped
    return out


# ── the checks made here ────────────────────────────────────────────────────

MARK = re.compile(r"([A-Za-z][\w/.-]*?)\s?([®™])")


def local_issues(fields):
    """Issues found without LanguageTool, over one ad's (field, text) pairs."""
    out, marks = [], {}
    for field, text in fields:
        for m in MARK.finditer(text):
            marks.setdefault(m.group(1).lower(), []).append((field, text, m))
        for m in re.finditer(r"([!?,;:])\1+|(?<!\.)\.\.(?!\.)", text):
            out.append({"field": field, "text": text, "o": m.start(), "n": m.end() - m.start(), "kind": "Punctuation", "msg": "Doubled punctuation.", "fix": [m.group(0)[0]]})
    for word, seen in marks.items():
        symbols = {m.group(2) for _, _, m in seen}
        if len(symbols) > 1:
            first = seen[0][2].group(2)
            for field, text, m in seen:
                if m.group(2) != first:
                    out.append({"field": field, "text": text, "o": m.start(2), "n": 1, "kind": "Consistency",
                                "msg": "%s is written with %s elsewhere in this ad." % (seen[0][2].group(1), first), "fix": [first]})
    return out


def _allowed(word, allow):
    w = word.strip("'’®™")
    return (not w or w.lower() in allow or any(c.isdigit() for c in w) or (len(w) > 1 and w.isupper())
            or any(c.isupper() for c in w[1:]) or "/" in w)


def issues(fields, found, allow):
    """One ad's issues: LanguageTool's matches (spelling ones for brand words dropped) plus the checks made here, in field order."""
    out = []
    for field, text in fields:
        for m in found.get(text) or []:
            kind = KINDS.get(m["cat"])
            if not kind:
                continue
            word = text[m["o"]:m["o"] + m["n"]]
            if kind == "Spelling" and _allowed(word, allow):
                continue
            out.append({"field": field, "text": text, "o": m["o"], "n": m["n"], "kind": kind, "msg": m["msg"], "fix": m["fix"]})
    return out + local_issues(fields)


# ── promotions ──────────────────────────────────────────────────────────────

CODE = re.compile(r"\b(?:promo(?:tion)?\s*code|coupon(?:\s*code)?|discount\s*code|offer\s*code|use\s+code|code)\s*[:\-]?\s*[\"“'‘]?([A-Z0-9][A-Za-z0-9_-]{2,19})\b", re.I)
OFFERS = [
    (r"\b(?:save|get|take|enjoy)\s+(?:up\s+to\s+)?\d{1,2}\s?%(?:\s?off)?(?!\w)|\b\d{1,2}\s?%\s?(?:off|discount|savings?)\b", "Percent off"),
    (r"[$£€]\s?\d+(?:\.\d\d)?\s?off\b", "Money off"), (r"\b(?:save|get)\s+[$£€]\s?\d+", "Money off"),
    (r"\bfree\s+(?:shipping|delivery)\b", "Free shipping"), (r"\bfree\s+(?:gift|sample|trial)\b", "Free gift or trial"),
    (r"\bbogo\b|\bbuy\s+(?:one|\d),?\s+get\s+(?:one|\d)\b|\b\d\s+for\s+[$£€]\d+|\b\d\s+for\s+the\s+price\s+of\s+\d\b", "Multi-buy"), (r"\bsubscribe\s+(?:and|&)\s+save\b", "Subscribe and save"),
    (r"\b(?:sale|clearance)\b", "Sale"), (r"\b(?:limited[\s-]time|today\s+only|ends\s+(?:soon|today|tonight)|while\s+supplies\s+last)\b", "Limited time"),
]
NOT_CODES = {"and", "the", "for", "your", "you", "with", "now", "today", "this", "that", "here", "below", "above", "at", "checkout"}


def promos(fields):
    """{codes: [{code, field}], offers: [{kind, text, field}]}: the promo codes and offers written in one ad's copy. A Google promotion asset is
    shown as itself, so its words are not read again here; an offer inside another one ("Get 20% OFF") is listed once."""
    codes, offers, seen = [], [], set()
    for field, text in fields:
        if field == "Promotion":
            continue
        taken = []
        for m in CODE.finditer(text):
            c = m.group(1)
            if c.lower() in NOT_CODES or not (c.isupper() or any(ch.isdigit() for ch in c)):
                continue
            if ("code", c) not in seen:
                seen.add(("code", c))
                codes.append({"code": c, "field": field})
        for pat, kind in OFFERS:
            for m in re.finditer(pat, text, re.I):
                if any(k == kind and a < m.end() and m.start() < b for k, a, b in taken):
                    continue
                taken.append((kind,) + m.span())
                if (kind, m.group(0).lower()) not in seen:
                    seen.add((kind, m.group(0).lower()))
                    offers.append({"kind": kind, "text": m.group(0), "field": field})
    return {"codes": codes, "offers": offers}


# ── one build ───────────────────────────────────────────────────────────────

def review(ads_fields, client, log, fresh=False):
    """{ad id: {issues, promos}} for every ad, ads_fields being {id: [(field, text)]}. Spelling allows the client's name, domain and `copy_allow`
    words. `fresh` (a Refresh) checks all the copy again instead of reusing the last answers."""
    lang = client.get("copy_language") or "en-US"
    texts = sorted({t for fs in ads_fields.values() for _, t in fs if t})
    found = language_tool(texts, lang, log, fresh) if texts else {}
    allow = {w.lower() for w in re.findall(r"[\w']+", " ".join([client.get("name") or "", (client.get("domain") or "").split(".")[0]] + list(client.get("copy_allow") or [])))}
    return {k: {"issues": issues(fs, found, allow), "promos": promos(fs), "checked": bool(found) and all(t in found for _, t in fs if t)} for k, fs in ads_fields.items()}
