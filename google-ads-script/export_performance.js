/**
 * Ad Preview Dashboard — campaign performance export (separate from export_combinations.js).
 *
 * Writes one tab into SPREADSHEET_URL:
 *   "Campaign Performance" — per enabled campaign: spend, clicks, impressions, conversions and
 *                            conversion value for the last 30 days and month to date, plus how
 *                            many ads (or PMax asset groups) are live.
 *
 * Install as its own script at the MCC (it finds every client account by itself) and schedule it
 * Daily, like the combinations script. It never touches the combination tabs.
 */

var SPREADSHEET_URL = 'https://docs.google.com/spreadsheets/d/1lMWQI2RECyNgFqwHZ6r2gs0WhA3EsOvj2uha9Qwu-70/edit';
var ACCOUNT_IDS = [];   // MCC only: leave empty for all client accounts, or limit e.g. ['123-456-7890']
var TAB = 'Campaign Performance';

var HEADER = ['Account', 'Customer ID', 'Currency', 'Campaign', 'Channel', 'Status', 'Live ads',
  'Cost 30d', 'Clicks 30d', 'Impressions 30d', 'Conversions 30d', 'Conv. value 30d',
  'Cost MTD', 'Clicks MTD', 'Impressions MTD', 'Conversions MTD', 'Conv. value MTD', 'Exported at'];

function main() {
  if (typeof AdsManagerApp !== 'undefined') {
    var sel = AdsManagerApp.accounts();
    if (ACCOUNT_IDS.length) sel = sel.withIds(ACCOUNT_IDS);
    sel.executeInParallel('processAccount', 'writeAll');
  } else {
    writeAll([{ getStatus: function () { return 'OK'; }, getReturnValue: processAccount }]);
  }
}

/** Runs once per account (in parallel under an MCC). Returns a JSON array of rows. */
function processAccount() {
  var acct = AdsApp.currentAccount();
  var ctx = { name: acct.getName(), cid: acct.getCustomerId() };
  var now = Utilities.formatDate(new Date(), acct.getTimeZone(), 'yyyy-MM-dd HH:mm');
  var camps = {}, order = [];
  var get = function (name) {
    if (!camps[name]) {
      camps[name] = { channel: '', status: '', ads: 0, d30: [0, 0, 0, 0, 0], mtd: [0, 0, 0, 0, 0] };
      order.push(name);
    }
    return camps[name];
  };

  guarded(ctx, 'campaign list', function () {
    var rows = AdsApp.search("SELECT campaign.name, campaign.status, campaign.advertising_channel_type " +
      "FROM campaign WHERE campaign.status = 'ENABLED'");
    while (rows.hasNext()) {
      var r = rows.next(), c = get(r.campaign.name);
      c.status = r.campaign.status;
      c.channel = r.campaign.advertisingChannelType;
    }
  });
  [['d30', 'LAST_30_DAYS'], ['mtd', 'THIS_MONTH']].forEach(function (p) {
    guarded(ctx, 'metrics ' + p[1], function () {
      var rows = AdsApp.search('SELECT campaign.name, campaign.status, campaign.advertising_channel_type, ' +
        'metrics.cost_micros, metrics.clicks, metrics.impressions, metrics.conversions, metrics.conversions_value ' +
        'FROM campaign WHERE segments.date DURING ' + p[1] + ' AND metrics.impressions > 0');
      while (rows.hasNext()) {
        var r = rows.next(), c = get(r.campaign.name), m = r.metrics;
        c.status = c.status || r.campaign.status;
        c.channel = c.channel || r.campaign.advertisingChannelType;
        c[p[0]] = [Number(m.costMicros || 0) / 1e6, Number(m.clicks || 0), Number(m.impressions || 0),
          Number(m.conversions || 0), Number(m.conversionsValue || 0)];
      }
    });
  });
  guarded(ctx, 'live ads', function () {
    var rows = AdsApp.search("SELECT campaign.name, ad_group_ad.ad.id FROM ad_group_ad " +
      "WHERE ad_group_ad.status = 'ENABLED' AND ad_group.status = 'ENABLED' AND campaign.status = 'ENABLED'");
    while (rows.hasNext()) get(rows.next().campaign.name).ads++;
  });
  guarded(ctx, 'live asset groups', function () {
    var rows = AdsApp.search("SELECT campaign.name, asset_group.id FROM asset_group " +
      "WHERE asset_group.status = 'ENABLED' AND campaign.status = 'ENABLED'");
    while (rows.hasNext()) get(rows.next().campaign.name).ads++;
  });

  Logger.log('[' + ctx.name + '] ' + order.length + ' campaigns');
  return JSON.stringify(order.map(function (name) {
    var c = camps[name];
    return [ctx.name, ctx.cid, acct.getCurrencyCode(), name, c.channel, c.status, c.ads].concat(c.d30, c.mtd, [now]);
  }));
}

/** Callback: merge every account's rows and write the tab once. */
function writeAll(results) {
  var rows = [];
  results.forEach(function (r) {
    if (r.getStatus() !== 'OK') { Logger.log('Account failed: ' + (r.getError ? r.getError() : '')); return; }
    rows = rows.concat(JSON.parse(r.getReturnValue()));
  });
  if (!rows.length) { Logger.log('No campaign rows — left "' + TAB + '" untouched.'); return; }
  var ss = SpreadsheetApp.openByUrl(SPREADSHEET_URL);
  var sh = ss.getSheetByName(TAB) || ss.insertSheet(TAB);
  sh.clearContents();
  var data = [HEADER].concat(rows);
  sh.getRange(1, 1, data.length, HEADER.length).setValues(data);
  sh.setFrozenRows(1);
  Logger.log('Wrote ' + rows.length + ' campaign rows.');
}

function guarded(ctx, label, fn) {
  try { fn(); } catch (e) { Logger.log('[' + ctx.name + '] ' + label + ' query failed (continuing): ' + e); }
}
