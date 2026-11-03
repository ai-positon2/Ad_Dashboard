"""copy_check.py without a network: a fake LanguageTool, the checks made here, and the promotions read from ad copy.

    python tests/test_copy_check.py      (or pytest)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("GADS_CACHE_DIR", tempfile.mkdtemp())

import copy_check  # noqa: E402

CLIENT = {"name": "Soteri Skin", "domain": "soteriskin.com", "copy_allow": ["Skinwell"]}


def fake_tool(calls):
    """LanguageTool's answer for the joined text: a misspelling for each 'recieve', 'Soteri' and 'Skinwell', a comma rule for ' ,'."""
    def ask(url, text, lang):
        calls.append(text)
        out = []
        for word, cat, msg in (("recieve", "TYPOS", "Possible spelling mistake found."), ("Soteri", "TYPOS", "Possible spelling mistake found."),
                               ("Skinwell", "TYPOS", "Possible spelling mistake found."), (" ,", "TYPOGRAPHY", "Put a space after the comma, but not before the comma."),
                               ("very unique", "STYLE", "Wordy.")):
            i = text.find(word)
            while i >= 0:
                out.append({"offset": i, "length": len(word), "message": msg, "rule": {"id": "R", "category": {"id": cat}}, "replacements": [{"value": "receive"}]})
                i = text.find(word, i + 1)
        return out
    return ask


def test_spelling_grammar_and_punctuation_come_back_per_field_and_brand_words_are_allowed():
    calls = []
    copy_check._ask, os.environ["LANGUAGETOOL_URL"] = fake_tool(calls), "https://lt.example/v2"
    fields = {"m:1": [("Headline", "Soteri Skin: recieve 15% off"), ("Primary text", "A very unique cream , for babies.")],
              "m:2": [("Headline", "Read the Skinwell blog")]}
    got = copy_check.review(fields, CLIENT, [])
    one = got["m:1"]["issues"]
    assert [(i["field"], i["kind"], i["text"][i["o"]:i["o"] + i["n"]]) for i in one] == [("Headline", "Spelling", "recieve"), ("Primary text", "Punctuation", " ,")]
    assert got["m:2"]["issues"] == [] and got["m:1"]["checked"] and len(calls) == 1          # all texts in one request; style left out; brand words allowed
    copy_check.review(fields, CLIENT, [])
    assert len(calls) == 1                                                                     # the second build reads the cache
    copy_check.review(fields, CLIENT, [], fresh=True)
    assert len(calls) == 2                                                                     # a Refresh asks again
    fixed = {"m:1": [("Headline", "Soteri Skin: receive 15% off")]}
    assert copy_check.review(fixed, CLIENT, [])["m:1"]["issues"] == [] and len(calls) == 3
    assert list(copy_check._load_cache()) == [copy_check._key("en-US", "Soteri Skin: receive 15% off")]  # the old copy's answer is dropped
    del os.environ["LANGUAGETOOL_URL"]


def test_without_languagetool_only_the_checks_made_here_run():
    os.environ.pop("LANGUAGETOOL_URL", None)
    got = copy_check.review({"m:1": [("Primary text", "Patented pH/LOCK® technology!! Try pH/LOCK™ today.."), ("Headline", "recieve")]}, CLIENT, [])["m:1"]
    assert not got["checked"]
    assert [(i["kind"], i["text"][i["o"]:i["o"] + i["n"]], i["fix"]) for i in got["issues"]] == [("Punctuation", "!!", ["!"]), ("Punctuation", "..", ["."]), ("Consistency", "™", ["®"])]


def test_promo_codes_and_offers_are_read_from_the_copy():
    p = copy_check.promos([("Headline", "Subscribe and Save 15%"), ("Primary text", "Use code GLOW20 at checkout for 20% off, plus free shipping."),
                           ("Description", "Enter the code at checkout"), ("Card headline", "Code red skin? We can help")])
    assert p["codes"] == [{"code": "GLOW20", "field": "Primary text"}]
    assert [(o["kind"], o["text"]) for o in p["offers"]] == [("Percent off", "Save 15%"), ("Subscribe and save", "Subscribe and Save"), ("Percent off", "20% off"), ("Free shipping", "free shipping")]
    assert copy_check.promos([("Headline", "Eczema relief that works"), ("Description", "Rebalances skin to pH 4.9 for 6 hours.")]) == {"codes": [], "offers": []}
    assert [o["kind"] for o in copy_check.promos([("Headline", "Buy 2, get 1 free")])["offers"]] == ["Multi-buy"]
    assert [o["text"] for o in copy_check.promos([("Primary text", "Get 20% OFF this month"), ("Promotion", "15% off the kit")])["offers"]] == ["Get 20% OFF"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
