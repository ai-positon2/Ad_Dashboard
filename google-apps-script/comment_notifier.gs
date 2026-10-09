/**
 * Ad Previews: comment notifier.
 *
 * Emails the team alias whenever someone comments on an ad in an Ad Previews dashboard.
 * Runs in your own Google account, so the dashboard never needs an email password.
 *
 * Setup (once):
 *  1. script.google.com > New project. Paste this file, replacing everything.
 *  2. Set NOTIFY_TO to the alias and SECRET to a long random word (letters and digits).
 *  3. Deploy > New deployment > type "Web app". Execute as: Me. Who has access: Anyone. Deploy, then authorise.
 *  4. Copy the Web app URL (ends in /exec).
 *  5. On each Railway service that has comments: NOTIFY_WEBHOOK = that URL, NOTIFY_SECRET = the same SECRET.
 *
 * One script serves every client dashboard; the subject line names the client.
 */
var NOTIFY_TO = 'your-alias@example.com';   // comma-separate for more than one address
var SECRET = 'change-me-to-a-long-random-word';

function doPost(e) {
  var data;
  try {
    data = JSON.parse(e.postData.contents);
  } catch (err) {
    return reply_('bad request');
  }
  if (!data || data.secret !== SECRET) return reply_('forbidden');
  var subject = String(data.subject || 'New comment on Ad Previews').slice(0, 200);
  var body = String(data.body || '').slice(0, 5000);
  MailApp.sendEmail({to: NOTIFY_TO, subject: subject, body: body, name: 'Ad Previews'});
  return reply_('sent');
}

function reply_(text) {
  return ContentService.createTextOutput(text).setMimeType(ContentService.MimeType.TEXT);
}

/** Run this once from the editor to authorise sending and check the alias gets mail. */
function testEmail() {
  MailApp.sendEmail({to: NOTIFY_TO, subject: '[Ad Previews] Test notification',
                     body: 'Comment notifications are set up.', name: 'Ad Previews'});
}
