/**
 * Ad Preview Dashboard — live assets export (separate from the combinations and performance scripts).
 *
 * Writes one tab into SPREADSHEET_URL:
 *   "Live Assets" — every asset that is live right now, one row per asset:
 *     - Responsive search ads: headlines + descriptions (with pins), paths, final URL
 *     - Demand Gen ads: headlines, descriptions, images, logos, videos, business name, CTA
 *     - Performance Max asset groups: every enabled asset-group asset
 *     - Sitelinks, callouts, structured snippets, image extensions, prices, promotions, calls, apps,
 *       business logo and business name
 *       at account, campaign and ad-group level
 *
 * Install as its own script at the MCC (it finds every client account by itself), schedule Daily.
 * Every query is guarded: a failure in one section is logged and the rest still export.
 */

var SPREADSHEET_URL = 'https://docs.google.com/spreadsheets/d/1lMWQI2RECyNgFqwHZ6r2gs0WhA3EsOvj2uha9Qwu-70/edit';
var ACCOUNT_IDS = [];   // MCC only: leave empty for all client accounts, or limit e.g. ['123-456-7890']
var TAB = 'Live Assets';
var ASSET_BATCH = 400;
var TIME_RESERVE_SEC = 180;   // stop starting new sections with less than this left, so results still get written

var HEADER = ['Account', 'Customer ID', 'Level', 'Campaign', 'Channel', 'Ad group / asset group', 'Owner ID',
  'Field', 'Asset type', 'Text', 'Lines', 'Image URL', 'Video ID', 'Pinned', 'Final URL', 'Path 1', 'Path 2',
  'Exported at'];

function main() {
  if (typeof AdsManagerApp !== 'undefined') {
    var sel = AdsManagerApp.accounts();
    if (ACCOUNT_IDS.length) sel = sel.withIds(ACCOUNT_IDS);
    sel.executeInParallel('processAccount', 'writeAll');
  } else {
    writeAll([{ getStatus: function () { return 'OK'; }, getReturnValue: processAccount }]);
  }
}

function processAccount() {
  var acct = AdsApp.currentAccount();
  var ctx = { name: acct.getName(), cid: acct.getCustomerId(),
    now: Utilities.formatDate(new Date(), acct.getTimeZone(), 'yyyy-MM-dd HH:mm') };
  var rows = [], pendingImages = [];   // Demand Gen images/videos arrive as asset references; resolved below
  var LIVE = " AND campaign.status = 'ENABLED' AND ad_group.status = 'ENABLED' AND ad_group_ad.status = 'ENABLED'";

  // row(level, campaign, channel, group, ownerId, field, type, text, lines, img, video, pinned, url, p1, p2)
  var row = function (o) {
    rows.push([ctx.name, ctx.cid, o.level, o.campaign || '', o.channel || '', o.group || '', String(o.owner || ''),
      o.field, o.type || '', o.text || '', (o.lines || []).join(' | '), o.img || '', o.video || '', o.pinned || '',
      o.url || '', o.p1 || '', o.p2 || '', ctx.now]);
    return rows[rows.length - 1];
  };

  // --- Responsive search ads
  guarded(ctx, 'RSA assets', function () {
    var it = AdsApp.search('SELECT campaign.name, campaign.advertising_channel_type, ad_group.name, ad_group_ad.ad.id, ' +
      'ad_group_ad.ad.final_urls, ad_group_ad.ad.responsive_search_ad.headlines, ' +
      'ad_group_ad.ad.responsive_search_ad.descriptions, ad_group_ad.ad.responsive_search_ad.path1, ' +
      'ad_group_ad.ad.responsive_search_ad.path2 FROM ad_group_ad ' +
      "WHERE ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'" + LIVE);
    while (it.hasNext()) {
      var r = it.next(), ad = r.adGroupAd.ad, rsa = ad.responsiveSearchAd || {};
      var base = { level: 'AD', campaign: r.campaign.name, channel: r.campaign.advertisingChannelType,
        group: r.adGroup.name, owner: ad.id, url: (ad.finalUrls || [])[0], p1: rsa.path1, p2: rsa.path2 };
      (rsa.headlines || []).forEach(function (h) { row(ext(base, { field: 'HEADLINE', type: 'TEXT', text: h.text, pinned: h.pinnedField })); });
      (rsa.descriptions || []).forEach(function (d) { row(ext(base, { field: 'DESCRIPTION', type: 'TEXT', text: d.text, pinned: d.pinnedField })); });
    }
  });

  // --- Demand Gen: multi-asset (image) ads and video responsive ads
  guarded(ctx, 'Demand Gen image ads', function () {
    var it = AdsApp.search('SELECT campaign.name, campaign.advertising_channel_type, ad_group.name, ad_group_ad.ad.id, ' +
      'ad_group_ad.ad.final_urls, ad_group_ad.ad.demand_gen_multi_asset_ad.headlines, ' +
      'ad_group_ad.ad.demand_gen_multi_asset_ad.descriptions, ad_group_ad.ad.demand_gen_multi_asset_ad.marketing_images, ' +
      'ad_group_ad.ad.demand_gen_multi_asset_ad.square_marketing_images, ' +
      'ad_group_ad.ad.demand_gen_multi_asset_ad.portrait_marketing_images, ' +
      'ad_group_ad.ad.demand_gen_multi_asset_ad.logo_images, ad_group_ad.ad.demand_gen_multi_asset_ad.business_name, ' +
      'ad_group_ad.ad.demand_gen_multi_asset_ad.call_to_action_text FROM ad_group_ad ' +
      "WHERE ad_group_ad.ad.type = 'DEMAND_GEN_MULTI_ASSET_AD'" + LIVE);
    while (it.hasNext()) {
      var r = it.next(), ad = r.adGroupAd.ad, dg = ad.demandGenMultiAssetAd || {};
      var base = { level: 'AD', campaign: r.campaign.name, channel: r.campaign.advertisingChannelType,
        group: r.adGroup.name, owner: ad.id, url: (ad.finalUrls || [])[0] };
      texts(base, dg.headlines, 'HEADLINE');
      texts(base, dg.descriptions, 'DESCRIPTION');
      images(base, dg.marketingImages, 'MARKETING_IMAGE');
      images(base, dg.squareMarketingImages, 'SQUARE_MARKETING_IMAGE');
      images(base, dg.portraitMarketingImages, 'PORTRAIT_MARKETING_IMAGE');
      images(base, dg.logoImages, 'LOGO');
      if (dg.businessName) row(ext(base, { field: 'BUSINESS_NAME', type: 'TEXT', text: dg.businessName }));
      if (dg.callToActionText) row(ext(base, { field: 'CALL_TO_ACTION', type: 'TEXT', text: dg.callToActionText }));
    }
  });
  guarded(ctx, 'Demand Gen video ads', function () {
    var it = AdsApp.search('SELECT campaign.name, campaign.advertising_channel_type, ad_group.name, ad_group_ad.ad.id, ' +
      'ad_group_ad.ad.final_urls, ad_group_ad.ad.demand_gen_video_responsive_ad.headlines, ' +
      'ad_group_ad.ad.demand_gen_video_responsive_ad.long_headlines, ' +
      'ad_group_ad.ad.demand_gen_video_responsive_ad.descriptions, ad_group_ad.ad.demand_gen_video_responsive_ad.videos, ' +
      'ad_group_ad.ad.demand_gen_video_responsive_ad.logo_images, ' +
      'ad_group_ad.ad.demand_gen_video_responsive_ad.business_name FROM ad_group_ad ' +
      "WHERE ad_group_ad.ad.type = 'DEMAND_GEN_VIDEO_RESPONSIVE_AD'" + LIVE);
    while (it.hasNext()) {
      var r = it.next(), ad = r.adGroupAd.ad, dv = ad.demandGenVideoResponsiveAd || {};
      var base = { level: 'AD', campaign: r.campaign.name, channel: r.campaign.advertisingChannelType,
        group: r.adGroup.name, owner: ad.id, url: (ad.finalUrls || [])[0] };
      texts(base, dv.headlines, 'HEADLINE');
      texts(base, dv.longHeadlines, 'LONG_HEADLINE');
      texts(base, dv.descriptions, 'DESCRIPTION');
      images(base, dv.videos, 'YOUTUBE_VIDEO');
      images(base, dv.logoImages, 'LOGO');
      if (dv.businessName && dv.businessName.text) row(ext(base, { field: 'BUSINESS_NAME', type: 'TEXT', text: dv.businessName.text }));
    }
  });

  // --- Performance Max: every enabled asset-group asset
  guarded(ctx, 'PMax assets', function () {
    var it = AdsApp.search('SELECT campaign.name, campaign.advertising_channel_type, asset_group.id, asset_group.name, ' +
      'asset_group.final_urls, asset_group.path1, asset_group.path2, asset_group_asset.field_type, asset.type, ' +
      'asset.text_asset.text, asset.image_asset.full_size.url, asset.youtube_video_asset.youtube_video_id, ' +
      'asset.call_to_action_asset.call_to_action FROM asset_group_asset ' +
      "WHERE asset_group_asset.status = 'ENABLED' AND asset_group.status = 'ENABLED' AND campaign.status = 'ENABLED'");
    while (it.hasNext()) {
      var r = it.next(), ag = r.assetGroup, a = r.asset;
      row({ level: 'ASSET_GROUP', campaign: r.campaign.name, channel: r.campaign.advertisingChannelType, group: ag.name,
        owner: ag.id, field: r.assetGroupAsset.fieldType, type: a.type,
        text: (a.textAsset && a.textAsset.text) || (a.callToActionAsset && a.callToActionAsset.callToAction) || '',
        img: a.imageAsset && a.imageAsset.fullSize && a.imageAsset.fullSize.url,
        video: a.youtubeVideoAsset && a.youtubeVideoAsset.youtubeVideoId,
        url: (ag.finalUrls || [])[0], p1: ag.path1, p2: ag.path2 });
    }
  });

  // --- Extensions + business logo / name, at account, campaign and ad-group level (per asset type)
  var LEVELS = [
    ['ACCOUNT', 'customer_asset', '', ''],
    ['CAMPAIGN', 'campaign_asset', 'campaign.name, campaign.status, campaign.advertising_channel_type, ',
      " AND campaign.status = 'ENABLED'"],
    ['AD_GROUP', 'ad_group_asset', 'campaign.name, campaign.status, campaign.advertising_channel_type, ad_group.name, ' +
      'ad_group.status, ',
      " AND campaign.status = 'ENABLED' AND ad_group.status = 'ENABLED'"]
  ];
  var money = function (m) {
    return m && m.amountMicros ? (Number(m.amountMicros) / 1e6).toLocaleString('en-US') + ' ' + (m.currencyCode || '') : '';
  };
  var pretty = function (s) { return String(s || '').toLowerCase().replace(/_/g, ' '); };
  // [label, fields, filter, format(asset) -> {text, lines, img}]
  var TYPES = [
    ['logo, name & image extensions', "asset.type, asset.text_asset.text, asset.image_asset.full_size.url",
      " AND asset.type IN ('TEXT', 'IMAGE')",
      function (a) { return { text: a.textAsset && a.textAsset.text, img: a.imageAsset && a.imageAsset.fullSize && a.imageAsset.fullSize.url }; }],
    ['sitelinks', "asset.type, asset.sitelink_asset.link_text, asset.sitelink_asset.description1, asset.sitelink_asset.description2",
      " AND asset.type = 'SITELINK'",
      function (a) { var s = a.sitelinkAsset || {}; return { text: s.linkText, lines: [s.description1, s.description2] }; }],
    ['callouts', "asset.type, asset.callout_asset.callout_text", " AND asset.type = 'CALLOUT'",
      function (a) { return { text: (a.calloutAsset || {}).calloutText }; }],
    ['snippets', "asset.type, asset.structured_snippet_asset.header, asset.structured_snippet_asset.values",
      " AND asset.type = 'STRUCTURED_SNIPPET'",
      function (a) { var s = a.structuredSnippetAsset || {}; return { text: s.header ? s.header + ': ' + (s.values || []).join(', ') : '' }; }],
    ['prices', "asset.type, asset.price_asset.type, asset.price_asset.price_qualifier, asset.price_asset.price_offerings",
      " AND asset.type = 'PRICE'",
      function (a) {
        var p = a.priceAsset || {};
        return { text: pretty(p.type) || 'prices', lines: (p.priceOfferings || []).map(function (o) {
          var unit = o.unit && o.unit !== 'UNSPECIFIED' ? ' ' + pretty(o.unit).replace('per ', '/ ') : '';
          return o.header + ': ' + money(o.price) + unit + (o.description ? ' — ' + o.description : '');
        }) };
      }],
    ['promotions', "asset.type, asset.promotion_asset.promotion_target, asset.promotion_asset.percent_off, " +
      "asset.promotion_asset.promotion_code, asset.promotion_asset.occasion",
      " AND asset.type = 'PROMOTION'",
      function (a) {
        var p = a.promotionAsset || {};
        var off = p.percentOff ? (Number(p.percentOff) / 10000) + '% off' : '';
        return { text: [off, p.promotionTarget].filter(Boolean).join(' '),
          lines: [p.promotionCode ? 'Code ' + p.promotionCode : '', p.occasion && p.occasion !== 'UNSPECIFIED' ? pretty(p.occasion) : ''] };
      }],
    ['calls', "asset.type, asset.call_asset.phone_number, asset.call_asset.country_code", " AND asset.type = 'CALL'",
      function (a) { var c = a.callAsset || {}; return { text: c.phoneNumber, lines: [c.countryCode] }; }],
    ['apps', "asset.type, asset.mobile_app_asset.link_text, asset.mobile_app_asset.app_id", " AND asset.type = 'MOBILE_APP'",
      function (a) { var m = a.mobileAppAsset || {}; return { text: m.linkText, lines: [m.appId] }; }]
  ];
  LEVELS.forEach(function (lv) {
    var res = lv[1], camel = res.replace(/_([a-z])/g, function (m, c) { return c.toUpperCase(); });
    TYPES.forEach(function (tp) {
      guarded(ctx, lv[0].toLowerCase() + ' ' + tp[0], function () {
        var it = AdsApp.search('SELECT ' + lv[2] + res + '.field_type, ' + tp[1] + ' FROM ' + res +
          ' WHERE ' + res + ".status = 'ENABLED'" + lv[3] + tp[2]);
        while (it.hasNext()) {
          var r = it.next(), f = tp[3](r.asset) || {};
          row({ level: lv[0], campaign: r.campaign ? r.campaign.name : '',
            channel: r.campaign ? r.campaign.advertisingChannelType : '', group: r.adGroup ? r.adGroup.name : '',
            field: r[camel].fieldType, type: r.asset.type, text: f.text || '',
            lines: (f.lines || []).filter(Boolean), img: f.img || '' });
        }
      });
    });
  });

  // Resolve Demand Gen image/video references in one batched lookup.
  if (pendingImages.length) {
    var names = {}; pendingImages.forEach(function (p) { names[p.asset] = 1; });
    var map = {}, list = Object.keys(names);
    guarded(ctx, 'image lookup', function () {
      for (var i = 0; i < list.length; i += ASSET_BATCH) {
        var inList = list.slice(i, i + ASSET_BATCH).map(function (n) { return "'" + n + "'"; }).join(', ');
        var it = AdsApp.search('SELECT asset.resource_name, asset.image_asset.full_size.url, ' +
          'asset.youtube_video_asset.youtube_video_id FROM asset WHERE asset.resource_name IN (' + inList + ')');
        while (it.hasNext()) {
          var a = it.next().asset;
          map[a.resourceName] = { img: a.imageAsset && a.imageAsset.fullSize && a.imageAsset.fullSize.url,
            video: a.youtubeVideoAsset && a.youtubeVideoAsset.youtubeVideoId };
        }
      }
    });
    pendingImages.forEach(function (p) {
      var m = map[p.asset] || {};
      p.row[11] = m.img || ''; p.row[12] = m.video || '';
    });
  }

  Logger.log('[' + ctx.name + '] ' + rows.length + ' live assets');
  return JSON.stringify(rows);

  function texts(base, list, field) {
    (list || []).forEach(function (t) { if (t && t.text) row(ext(base, { field: field, type: 'TEXT', text: t.text })); });
  }
  function images(base, list, field) {
    (list || []).forEach(function (x) {
      if (!x || !x.asset) return;
      var r = row(ext(base, { field: field, type: field === 'YOUTUBE_VIDEO' ? 'YOUTUBE_VIDEO' : 'IMAGE' }));
      pendingImages.push({ asset: x.asset, row: r });
    });
  }
}

function ext(base, extra) {
  var o = {}, k;
  for (k in base) o[k] = base[k];
  for (k in extra) o[k] = extra[k];
  return o;
}

function writeAll(results) {
  var rows = [];
  results.forEach(function (r) {
    if (r.getStatus() !== 'OK') { Logger.log('Account failed: ' + (r.getError ? r.getError() : '')); return; }
    rows = rows.concat(JSON.parse(r.getReturnValue()));
  });
  if (!rows.length) { Logger.log('No live assets — left "' + TAB + '" untouched.'); return; }
  var ss = SpreadsheetApp.openByUrl(SPREADSHEET_URL);
  var sh = ss.getSheetByName(TAB) || ss.insertSheet(TAB);
  sh.clearContents();
  var data = [HEADER].concat(rows);
  sh.getRange(1, 1, data.length, HEADER.length).setValues(data);
  sh.setFrozenRows(1);
  Logger.log('Wrote ' + rows.length + ' live asset rows.');
}

/**
 * Runs one section. Logs failures and slow sections (>5s), and skips the section entirely when the
 * account is close to Google's 30-minute limit, so whatever was collected still gets written.
 */
function guarded(ctx, label, fn) {
  var left = remainingSeconds();
  if (left !== null && left < TIME_RESERVE_SEC) {
    Logger.log('[' + ctx.name + '] skipped "' + label + '" — only ' + Math.round(left) + 's left before the time limit');
    return;
  }
  var t0 = Date.now();
  try { fn(); } catch (e) { Logger.log('[' + ctx.name + '] ' + label + ' query failed (continuing): ' + e); }
  var secs = (Date.now() - t0) / 1000;
  if (secs > 5) Logger.log('[' + ctx.name + '] ' + label + ' took ' + Math.round(secs) + 's');
}

function remainingSeconds() {
  try { return AdsApp.getExecutionInfo().getRemainingTime(); } catch (e) { return null; }
}
