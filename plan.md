# Ad Preview Dashboard — Plan

Goal: a shareable dashboard showing every live Google ad (RSA, Demand Gen, PMax)
for a client — the raw assets **and** the combinations/previews Google renders
(Search, Display, YouTube, Discover/Gmail), like the "Ad preview" panel in Google Ads.

First client: **Tealium** — sheet `Tealium_AdCopies`
(`1lMWQI2RECyNgFqwHZ6r2gs0WhA3EsOvj2uha9Qwu-70`, Supermetrics-refreshed,
link-shared so it downloads directly as xlsx).

## What the sheet actually contains (checked 2026-09-24)

| Tab | Rows | Content |
|---|---|---|
| RSA and Demand Gen | 65 RSA + 22 Demand Gen + 4 empty | Account, campaign, 15 headlines, 4 descriptions, path 1/2, ad strength. Demand Gen rows add business name + 5 descriptions. |
| Pmax | 3 asset groups (APAC / EMEA / NA) + 150 search-theme rows | 15 headlines, 5 long headlines, 5 descriptions, CTA, 4 YouTube video IDs, final URL, paths, status, ad strength. |

**Gaps that affect previews**
- RSA rows have **no ad group, ad ID, final URL, or pinning info** → display URL
  has to fall back to `tealium.com/{path1}/{path2}`; pins can't be honored.
- **No images/logos** anywhere (Demand Gen & PMax image columns are empty) →
  Display/Discover previews can only use YouTube thumbnails or a placeholder.
- Sitelinks / callouts / snippets columns are empty → Search previews show no extensions.

## Phases

- [ ] **Phase 1 — Data layer.** `fetch.py` downloads the sheet, normalises into
  `ads.json`: account → campaign → ad (type, headlines, descriptions, long
  headlines, paths, CTA, videos, strength). Drop empty rows; attach search themes to PMax.
- [ ] **Phase 2 — Asset view.** Dashboard with filters (account, campaign, ad type,
  ad strength) and per-ad asset lists with char counts.
- [ ] **Phase 3 — Combination previews.** For each ad, render Google-style previews:
  - RSA: Search (desktop + mobile) — 3 headlines + 2 descriptions, shuffle / step through combos.
  - PMax: Search, Display, YouTube, Discover, Gmail formats.
  - Demand Gen: Discover, Gmail, YouTube formats.
  Source of combinations = see decision below.
- [ ] **Phase 4 — Publish + refresh.** Publish as a claude.ai artifact; daily
  refresh after Supermetrics runs.
- [ ] **Phase 5 — More clients** (same sheet template per client).

## Open decision — where combinations come from

A. **Simulated** — generate combinations ourselves using Google's assembly rules
   (character limits, 3H+2D, no duplicate headlines). Works today from the sheet.
   Not what Google *actually* served.
B. **Real** — Google's own top combinations (Assets → Combinations report;
   API: `ad_group_ad_asset_combination_view` for RSA,
   `asset_group_top_combination_view` for PMax). Needs a new data feed into the
   sheet (Google Ads Script or Supermetrics query) since the current tabs don't have it.

**Decided 2026-09-24: B first (real combinations).**

### Phase 0 — Real-combination feed (in progress)
- [x] `google-ads-script/export_combinations.js` — Google Ads Script, writes
  `RSA Combinations` + `PMax Combinations` tabs into the ad-copy sheet
  (RSA: every served combo w/ impressions, ranked; PMax: Google's top-20 per asset group).
- [x] Installed at MCC (3 Tealium accounts: ANZ/ASIA 669-051-0760, EMEA, NAM). Runs in ~32s.
  Runner needs **edit** access to the sheet (owned by supermetrics@position2.co.in).
- [x] First run 2026-09-24: 959 RSA combos (44 ads, 17 ad groups) + 54 PMax combos
  (3 asset groups × TEXT/IMAGE/VIDEO categories × top 6), with real image/logo URLs.
- [ ] v3 script adds sitelink/callout/snippet text (were blank in v2) — user to re-paste + schedule daily.
  Note: only 13/959 RSA combos show a 3rd headline; Google mostly serves 2H + sitelinks.
- Demand Gen: no combination API exists → shown as simulated previews, labelled as such.
