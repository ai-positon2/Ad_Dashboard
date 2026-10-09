/**
 * Ad Previews: comment notifier.
 *
 * Every 5 minutes, asks each dashboard for comments posted since the last check and emails the team alias.
 * It only makes outgoing calls, so it works under a Google Workspace that blocks public web apps.
 *
 * Setup (once):
 *  1. Fill in NOTIFY_TO, SECRET and DASHBOARDS below, then save (Ctrl+S).
 *  2. On each Railway service in DASHBOARDS, set NOTIFY_SECRET to the same SECRET (and COMMENTS=on).
 *  3. Pick "setup" in the function dropdown and click Run. Approve the permissions prompt.
 *     It starts the 5-minute timer. Comments that already exist are skipped; only new ones are emailed.
 *
 * To add a client later: add its dashboard URL to DASHBOARDS, save, and run "setup" again.
 */
var NOTIFY_TO = 'your-alias@example.com';   // comma-separate for more than one address
var SECRET = 'change-me-to-a-long-random-word';
var DASHBOARDS = [
  'https://your-dashboard.up.railway.app',
];

function setup() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'checkComments') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('checkComments').timeBased().everyMinutes(5).create();
  checkComments();
  Logger.log('Checking ' + DASHBOARDS.length + ' dashboard(s) every 5 minutes.');
}

function checkComments() {
  var props = PropertiesService.getScriptProperties();
  DASHBOARDS.forEach(function (base) {
    base = base.replace(/\/+$/, '');
    var key = 'last:' + base;
    var seen = props.getProperty(key);
    var after = seen === null ? 0 : Number(seen);
    var res = UrlFetchApp.fetch(base + '/api/comments/new?after=' + after + '&key=' + encodeURIComponent(SECRET),
                                {muteHttpExceptions: true});
    if (res.getResponseCode() !== 200) {
      Logger.log(base + ': HTTP ' + res.getResponseCode() + ' ' + res.getContentText().slice(0, 200) +
                 ' (check NOTIFY_SECRET and COMMENTS=on on that Railway service)');
      return;
    }
    var data = JSON.parse(res.getContentText());
    if (seen !== null) {  // first run only records where we are, so old comments aren't emailed
      data.comments.forEach(function (c) { email_(data.client, base, c); });
    }
    props.setProperty(key, String(data.last));
    Logger.log(base + ': ' + (seen === null ? 'starting after comment ' + data.last : data.comments.length + ' new'));
  });
}

function email_(client, base, c) {
  var w = c.where || {};
  var place = [w.account, w.campaign, w.adGroup].filter(String).join(' › ');
  var body = c.name + ' left a comment on the ' + client + ' ad previews dashboard.\n\n' +
             'Ad: ' + (place || c.ad) + '\nType: ' + (w.kind || '') + '  ·  Ad ID: ' + c.ad + '\n\n' +
             c.text + '\n\nOpen the ad: ' + base + '/#ad-' + encodeURIComponent(c.ad) + '\n';
  MailApp.sendEmail({
    to: NOTIFY_TO,
    subject: '[' + client + ' Ad Previews] ' + c.name + ' commented on ' + (w.adGroup || w.campaign || 'an ad'),
    body: body,
    name: 'Ad Previews',
  });
}

/** Optional: run once from the editor to check the alias gets mail. */
function testEmail() {
  MailApp.sendEmail({to: NOTIFY_TO, subject: '[Ad Previews] Test notification',
                     body: 'Comment notifications are set up.', name: 'Ad Previews'});
}
