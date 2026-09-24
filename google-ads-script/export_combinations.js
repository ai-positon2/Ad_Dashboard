/**
 * Ad Preview Dashboard — export Google's real served asset combinations.
 *
 * Writes two tabs into SPREADSHEET_URL:
 *   "RSA Combinations"  — ad_group_ad_asset_combination_view (RSA only; has impressions)
 *   "PMax Combinations" — asset_group_top_combination_view (ranked, no metrics)
 *
 * Run it either:
 *   - in a single Google Ads account (leave ACCOUNT_IDS empty), or
 *   - at MCC level, listing the client accounts in ACCOUNT_IDS (they run in parallel).
 * Schedule: Daily, after Supermetrics refreshes the ad-copy sheet.
 *
 * Speed notes: only assets that actually appear in a combination are looked up
 * (in batched IN (...) queries), instead of scanning every asset in the account.
 *
 * Demand Gen is not covered: Google's API exposes combination views only for
 * RSA and PMax today.
 */

var SPREADSHEET_URL = 'https://docs.google.com/spreadsheets/d/1lMWQI2RECyNgFqwHZ6r2gs0WhA3EsOvj2uha9Qwu-70/edit';
var ACCOUNT_IDS = [];          // e.g. ['123-456-7890', '234-567-8901'] when run from an MCC
var DATE_RANGE = 'LAST_30_DAYS';
var MAX_RSA_COMBOS = 20;       // top-N combinations kept per RSA (by impressions)
var MAX_PMAX_COMBOS = 20;      // top-N combinations kept per asset group
var ASSET_BATCH = 400;         // resource names per asset lookup query

var RSA_HEADER = ['Account', 'Customer ID', 'Campaign', 'Ad group', 'Ad ID', 'Rank', 'Impressions',
  'Enabled', 'Final URL', 'Path 1', 'Path 2', 'Headline 1', 'Headline 2', 'Headline 3',
  'Description 1', 'Description 2', 'Assets JSON', 'Date range', 'Exported at'];
var PMAX_HEADER = ['Account', 'Customer ID', 'Campaign', 'Asset group', 'Asset group ID', 'Category',
  'Rank', 'Final URL', 'Path 1', 'Path 2', 'Headlines', 'Long headline', 'Descriptions',
  'Business name', 'Call to action', 'Images', 'Logos', 'Videos', 'Assets JSON', 'Date range', 'Exported at'];

function main() {
  if (ACCOUNT_IDS.length && typeof AdsManagerApp !== 'undefined') {
    AdsManagerApp.accounts().withIds(ACCOUNT_IDS).executeInParallel('processAccount', 'writeAll');
  } else {
    writeAll([{ getStatus: function () { return 'OK'; }, getReturnValue: processAccount }]);
  }
}

/** Runs once per account (in parallel under an MCC). Returns JSON {rsa: [...], pmax: [...]}. */
function processAccount() {
  var acct = AdsApp.currentAccount();
  var ctx = {
    name: acct.getName(), cid: acct.getCustomerId(),
    now: Utilities.formatDate(new Date(), acct.getTimeZone(), 'yyyy-MM-dd HH:mm')
  };
  var rsa = fetchRsa(ctx), pmax = fetchPmax(ctx);

  // One batched lookup for every asset referenced by either feed.
  var needed = {};
  rsa.concat(pmax).forEach(function (c) { c.usages.forEach(function (u) { needed[u.asset] = 1; }); });
  var assets = loadAssets(Object.keys(needed));

  var out = { rsa: [], pmax: [] };
  rsa.forEach(function (c) {
    var served = resolve(c.usages, assets);
    var pick = function (f) { var a = served.filter(function (s) { return s.field === f; })[0]; return a ? a.text : ''; };
    out.rsa.push([ctx.name, ctx.cid, c.campaign, c.adGroup, c.adId, c.rank, c.impressions, c.enabled,
      c.finalUrl, c.path1, c.path2, pick('HEADLINE_1'), pick('HEADLINE_2'), pick('HEADLINE_3'),
      pick('DESCRIPTION_1'), pick('DESCRIPTION_2'), JSON.stringify(served), DATE_RANGE, ctx.now]);
  });
  pmax.forEach(function (c) {
    var served = resolve(c.usages, assets);
    var list = function (re) {
      return served.filter(function (s) { return re.test(s.field); })
        .map(function (s) { return s.text || s.url || s.video || ''; }).join(' | ');
    };
    out.pmax.push([ctx.name, ctx.cid, c.campaign, c.assetGroup, c.assetGroupId, c.category, c.rank,
      c.finalUrl, c.path1, c.path2, list(/^HEADLINE/), list(/^LONG_HEADLINE/), list(/^DESCRIPTION/),
      list(/^BUSINESS_NAME/), list(/^CALL_TO_ACTION/), list(/IMAGE$/), list(/LOGO/), list(/VIDEO/),
      JSON.stringify(served), DATE_RANGE, ctx.now]);
  });
  Logger.log('[' + ctx.name + '] ' + out.rsa.length + ' RSA / ' + out.pmax.length + ' PMax rows, ' +
    Object.keys(needed).length + ' assets looked up');
  return JSON.stringify(out);
}

/** Callback: merge every account's rows and write both tabs in one go. */
function writeAll(results) {
  var rsaRows = [], pmaxRows = [];
  results.forEach(function (r) {
    if (r.getStatus() !== 'OK') { Logger.log('Account failed: ' + (r.getError ? r.getError() : '')); return; }
    var d = JSON.parse(r.getReturnValue());
    rsaRows = rsaRows.concat(d.rsa);
    pmaxRows = pmaxRows.concat(d.pmax);
  });
  var ss = SpreadsheetApp.openByUrl(SPREADSHEET_URL);
  writeTab(ss, 'RSA Combinations', RSA_HEADER, rsaRows);
  writeTab(ss, 'PMax Combinations', PMAX_HEADER, pmaxRows);
  Logger.log('Wrote ' + rsaRows.length + ' RSA and ' + pmaxRows.length + ' PMax combination rows.');
}

function fetchRsa(ctx) {
  var q = 'SELECT campaign.name, ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.final_urls, ' +
    'ad_group_ad.ad.responsive_search_ad.path1, ad_group_ad.ad.responsive_search_ad.path2, ' +
    'ad_group_ad_asset_combination_view.served_assets, ad_group_ad_asset_combination_view.enabled, ' +
    'metrics.impressions ' +
    'FROM ad_group_ad_asset_combination_view ' +
    'WHERE segments.date DURING ' + DATE_RANGE + ' AND metrics.impressions > 0 ' +
    "AND campaign.advertising_channel_type = 'SEARCH' " +
    "AND campaign.status = 'ENABLED' AND ad_group.status = 'ENABLED' AND ad_group_ad.status = 'ENABLED'";
  var byAd = {};
  try {
    var rows = AdsApp.search(q);
    while (rows.hasNext()) {
      var r = rows.next();
      var ad = r.adGroupAd.ad, view = r.adGroupAdAssetCombinationView, rsa = ad.responsiveSearchAd || {};
      (byAd[ad.id] = byAd[ad.id] || []).push({
        campaign: r.campaign.name, adGroup: r.adGroup.name, adId: ad.id,
        impressions: Number(r.metrics.impressions || 0), enabled: view.enabled,
        finalUrl: (ad.finalUrls || [])[0] || '', path1: rsa.path1 || '', path2: rsa.path2 || '',
        usages: view.servedAssets || []
      });
    }
  } catch (e) {
    Logger.log('[' + ctx.name + '] RSA combination query failed: ' + e);
  }
  var out = [];
  Object.keys(byAd).forEach(function (id) {
    byAd[id].sort(function (a, b) { return b.impressions - a.impressions; })
      .slice(0, MAX_RSA_COMBOS)
      .forEach(function (c, i) { c.rank = i + 1; out.push(c); });
  });
  return out;
}

function fetchPmax(ctx) {
  var q = 'SELECT campaign.name, asset_group.id, asset_group.name, asset_group.final_urls, ' +
    'asset_group.path1, asset_group.path2, asset_group_top_combination_view.resource_name, ' +
    'asset_group_top_combination_view.asset_group_top_combinations ' +
    'FROM asset_group_top_combination_view ' +
    'WHERE segments.date DURING ' + DATE_RANGE +
    " AND campaign.status = 'ENABLED' AND asset_group.status = 'ENABLED'";
  var out = [];
  try {
    var rows = AdsApp.search(q);
    while (rows.hasNext()) {
      var p = rows.next();
      var ag = p.assetGroup, tv = p.assetGroupTopCombinationView;
      var category = String(tv.resourceName || '').split('~').pop();
      (tv.assetGroupTopCombinations || []).slice(0, MAX_PMAX_COMBOS).forEach(function (combo, i) {
        out.push({
          campaign: p.campaign.name, assetGroup: ag.name, assetGroupId: ag.id, category: category,
          rank: i + 1, finalUrl: (ag.finalUrls || [])[0] || '', path1: ag.path1 || '', path2: ag.path2 || '',
          usages: combo.assetCombinationServedAssets || []
        });
      });
    }
  } catch (e) {
    Logger.log('[' + ctx.name + '] PMax top-combination query failed: ' + e);
  }
  return out;
}

/** Looks up only the given asset resource names, in batches. */
function loadAssets(names) {
  var map = {};
  for (var i = 0; i < names.length; i += ASSET_BATCH) {
    var inList = names.slice(i, i + ASSET_BATCH).map(function (n) { return "'" + n + "'"; }).join(', ');
    var rows = AdsApp.search('SELECT asset.resource_name, asset.type, asset.text_asset.text, ' +
      'asset.image_asset.full_size.url, asset.youtube_video_asset.youtube_video_id, ' +
      'asset.call_to_action_asset.call_to_action, asset.sitelink_asset.link_text, ' +
      'asset.sitelink_asset.description1, asset.sitelink_asset.description2, ' +
      'asset.callout_asset.callout_text, asset.structured_snippet_asset.header, ' +
      'asset.structured_snippet_asset.values ' +
      'FROM asset WHERE asset.resource_name IN (' + inList + ')');
    while (rows.hasNext()) {
      var a = rows.next().asset;
      var sl = a.sitelinkAsset || {}, ss = a.structuredSnippetAsset || {};
      map[a.resourceName] = {
        type: a.type,
        text: (a.textAsset && a.textAsset.text) || sl.linkText ||
          (a.calloutAsset && a.calloutAsset.calloutText) ||
          (ss.header ? ss.header + ': ' + (ss.values || []).join(', ') : ''),
        lines: [sl.description1, sl.description2].filter(Boolean),
        url: (a.imageAsset && a.imageAsset.fullSize && a.imageAsset.fullSize.url) || '',
        video: (a.youtubeVideoAsset && a.youtubeVideoAsset.youtubeVideoId) || '',
        cta: (a.callToActionAsset && a.callToActionAsset.callToAction) || ''
      };
    }
  }
  return map;
}

function resolve(usages, assets) {
  return (usages || []).map(function (u) {
    var a = assets[u.asset] || {};
    var o = { field: u.servedAssetFieldType, type: a.type || '', text: a.text || a.cta || '',
      url: a.url || '', video: a.video || '' };
    if (a.lines && a.lines.length) o.lines = a.lines;
    return o;
  });
}

function writeTab(ss, title, header, rows) {
  var sh = ss.getSheetByName(title) || ss.insertSheet(title);
  sh.clearContents();
  var data = [header].concat(rows);
  sh.getRange(1, 1, data.length, header.length).setValues(data);
  sh.setFrozenRows(1);
}
