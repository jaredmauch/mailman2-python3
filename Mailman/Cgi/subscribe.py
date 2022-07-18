# Copyright (C) 1998-2018 by the Free Software Foundation, Inc.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301,
# USA.

"""Process subscription or roster requests from listinfo form."""

import sys
import os
import cgi
import time
import signal
import urllib
import urllib2
import json

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import MailList
from Mailman import Errors
from Mailman import i18n
from Mailman import Message
from Mailman.UserDesc import UserDesc
from Mailman.htmlformat import *
from Mailman.Logging.Syslog import syslog

SLASH = '/'
ERRORSEP = '\n\n<p>'
COMMASPACE = ', '

# Set up i18n
_ = i18n._
i18n.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)



def main():
    doc = Document()
    doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)

    parts = Utils.GetPathPieces()
    if not parts:
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script')))
        print doc.Format()
        return

    listname = parts[0].lower()
    try:
        mlist = MailList.MailList(listname, lock=0)
    except Errors.MMListError, e:
        # Avoid cross-site scripting attacks
        safelistname = Utils.websafe(listname)
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('No such list <em>%(safelistname)s</em>')))
        # Send this with a 404 status.
        print 'Status: 404 Not Found'
        print doc.Format()
        syslog('error', 'subscribe: No such list "%s": %s\n', listname, e)
        return

    # See if the form data has a preferred language set, in which case, use it
    # for the results.  If not, use the list's preferred language.
    cgidata = cgi.FieldStorage()
    try:
        language = cgidata.getfirst('language', '')
    except TypeError:
        # Someone crafted a POST with a bad Content-Type:.
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script.')))
        # Send this with a 400 status.
        print 'Status: 400 Bad Request'
        print doc.Format()
        return
    if not Utils.IsLanguage(language):
        language = mlist.preferred_language
    i18n.set_language(language)
    doc.set_language(language)

    # We need a signal handler to catch the SIGTERM that can come from Apache
    # when the user hits the browser's STOP button.  See the comment in
    # admin.py for details.
    #
    # BAW: Strictly speaking, the list should not need to be locked just to
    # read the request database.  However the request database asserts that
    # the list is locked in order to load it and it's not worth complicating
    # that logic.
    def sigterm_handler(signum, frame, mlist=mlist):
        # Make sure the list gets unlocked...
        mlist.Unlock()
        # ...and ensure we exit, otherwise race conditions could cause us to
        # enter MailList.Save() while we're in the unlocked state, and that
        # could be bad!
        sys.exit(0)

    mlist.Lock()
    try:
        # Install the emergency shutdown signal handler
        signal.signal(signal.SIGTERM, sigterm_handler)

        process_form(mlist, doc, cgidata, language)
        mlist.Save()
    finally:
        mlist.Unlock()



def process_form(mlist, doc, cgidata, lang):
    listowner = mlist.GetOwnerEmail()
    realname = mlist.real_name
    results = []

    # The email address being subscribed, required
    email = cgidata.getfirst('email', '').strip()
    if not email:
        results.append(_('You must supply a valid email address.'))

    fullname = cgidata.getfirst('fullname', '')
    # Canonicalize the full name
    fullname = Utils.canonstr(fullname, lang)
    # Who was doing the subscribing?
    remote = os.environ.get('HTTP_FORWARDED_FOR',
             os.environ.get('HTTP_X_FORWARDED_FOR',
             os.environ.get('REMOTE_ADDR',
                            'unidentified origin')))

    # Check reCAPTCHA submission, if enabled
    if mm_cfg.RECAPTCHA_SECRET_KEY:
        request = urllib2.Request(
            url = 'https://www.google.com/recaptcha/api/siteverify',
            data = urllib.urlencode({
                'secret': mm_cfg.RECAPTCHA_SECRET_KEY,
                'response': cgidata.getvalue('g-recaptcha-response', ''),
                'remoteip': remote}))
        try:
            httpresp = urllib2.urlopen(request)
            captcha_response = json.load(httpresp)
            httpresp.close()
            if not captcha_response['success']:
                e_codes = COMMASPACE.join(captcha_response['error-codes'])
                results.append(_('reCAPTCHA validation failed: %(e_codes)s'))
        except urllib2.URLError, e:
            e_reason = e.reason
            results.append(_('reCAPTCHA could not be validated: %(e_reason)s'))

    # Are we checking the hidden data?
    if mm_cfg.SUBSCRIBE_FORM_SECRET:
        now = int(time.time())
        # Try to accept a range in case of load balancers, etc.  (LP: #1447445)
        if remote.find('.') >= 0:
            # ipv4 - drop last octet
            remote1 = remote.rsplit('.', 1)[0]
        else:
            # ipv6 - drop last 16 (could end with :: in which case we just
            #        drop one : resulting in an invalid format, but it's only
            #        for our hash so it doesn't matter.
            remote1 = remote.rsplit(':', 1)[0]
        try:
            ftime, fcaptcha_idx, fhash = cgidata.getfirst(
                    'sub_form_token', '').split(':')
            then = int(ftime)
        except ValueError:
            ftime = fcaptcha_idx = fhash = ''
            then = 0
        token = Utils.sha_new(mm_cfg.SUBSCRIBE_FORM_SECRET + ":" +
                              ftime + ":" +
                              fcaptcha_idx + ":" +
                              mlist.internal_name() + ":" +
                              remote1).hexdigest()
        if ftime and now - then > mm_cfg.FORM_LIFETIME:
            results.append(_('The form is too old.  Please GET it again.'))
        if ftime and now - then < mm_cfg.SUBSCRIBE_FORM_MIN_TIME:
            results.append(
    _('Please take a few seconds to fill out the form before submitting it.'))
        if ftime and token != fhash:
            results.append(
                _("The hidden token didn't match.  Did your IP change?"))
        if not ftime:
            results.append(
    _('There was no hidden token in your submission or it was corrupted.'))
            results.append(_('You must GET the form before submitting it.'))
        # Check captcha
        if isinstance(mm_cfg.CAPTCHAS, dict):
            captcha_answer = cgidata.getvalue('captcha_answer', '')
            if not Utils.captcha_verify(
                    fcaptcha_idx, captcha_answer, mm_cfg.CAPTCHAS):
                results.append(_(
                    'This was not the right answer to the CAPTCHA question.'))
    # Was an attempt made to subscribe the list to itself?
    if email == mlist.GetListEmail():
        syslog('mischief', 'Attempt to self subscribe %s: %s', email, remote)
        results.append(_('You may not subscribe a list to itself!'))
    # If the user did not supply a password, generate one for him
    password = cgidata.getfirst('pw', '').strip()
    confirmed = cgidata.getfirst('pw-conf', '').strip()

    if not password and not confirmed:
        password = Utils.MakeRandomPassword()
    elif not password or not confirmed:
        results.append(_('If you supply a password, you must confirm it.'))
    elif password <> confirmed:
        results.append(_('Your passwords did not match.'))

    # Get the digest option for the subscription.
    digestflag = cgidata.getfirst('digest')
    if digestflag:
        try:
            digest = int(digestflag)
        except (TypeError, ValueError):
            digest = 0
    else:
        digest = mlist.digest_is_default

    # Sanity check based on list configuration.  BAW: It's actually bogus that
    # the page allows you to set the digest flag if you don't really get the
    # choice. :/
    if not mlist.digestable:
        digest = 0
    elif not mlist.nondigestable:
        digest = 1

    if results:
        print_results(mlist, ERRORSEP.join(results), doc, lang)
        return

    # If this list has private rosters, we have to be careful about the
    # message that gets printed, otherwise the subscription process can be
    # used to mine for list members.  It may be inefficient, but it's still
    # possible, and that kind of defeats the purpose of private rosters.
    # We'll use this string for all successful or unsuccessful subscription
    # results.
    if mlist.private_roster == 0:
        # Public rosters
        privacy_results = ''
    else:
        privacy_results = _("""\
Your subscription request has been received, and will soon be acted upon.
Depending on the configuration of this mailing list, your subscription request
may have to be first confirmed by you via email, or approved by the list
moderator.  If confirmation is required, you will soon get a confirmation
email which contains further instructions.""")

    try:
        userdesc = UserDesc(email, fullname, password, digest, lang)
        mlist.AddMember(userdesc, remote)
        results = ''
    # Check for all the errors that mlist.AddMember can throw options on the
    # web page for this cgi
    except Errors.MembershipIsBanned:
        results = _("""The email address you supplied is banned from this
        mailing list.  If you think this restriction is erroneous, please
        contact the list owners at %(listowner)s.""")
    except Errors.MMBadEmailError:
        results = _("""\
The email address you supplied is not valid.  (E.g. it must contain an
`@'.)""")
    except Errors.MMHostileAddress:
        results = _("""\
Your subscription is not allowed because the email address you gave is
insecure.""")
    except Errors.MMSubscribeNeedsConfirmation:
        # Results string depends on whether we have private rosters or not
        if privacy_results:
            results = privacy_results
        else:
            results = _("""\
Confirmation from your email address is required, to prevent anyone from
subscribing you without permission.  Instructions are being sent to you at
%(email)s.  Please note your subscription will not start until you confirm
your subscription.""")
    except Errors.MMNeedApproval, x:
        # Results string depends on whether we have private rosters or not
        if privacy_results:
            results = privacy_results
        else:
            # We need to interpolate into x.__str__()
            x = _(str(x))
            results = _("""\
Your subscription request was deferred because %(x)s.  Your request has been
forwarded to the list moderator.  You will receive email informing you of the
moderator's decision when they get to your request.""")
    except Errors.MMAlreadyPending:
        # User already has a subscription pending
        results = _('You already have a subscription pending confirmation')
    except Errors.MMAlreadyAMember:
        # Results string depends on whether we have private rosters or not
        if not privacy_results:
            results = _('You are already subscribed.')
        else:
            results = privacy_results
        if privacy_results and mm_cfg.WARN_MEMBER_OF_SUBSCRIBE:
            # This could be a membership probe.  For safety, let the user know
            # a probe occurred.  BAW: should we inform the list moderator?
            listaddr = mlist.GetListEmail()
            # Set the language for this email message to the member's language.
            mlang = mlist.getMemberLanguage(email)
            otrans = i18n.get_translation()
            i18n.set_language(mlang)
            try:
                msg = Message.UserNotification(
                    mlist.getMemberCPAddress(email),
                    mlist.GetBouncesEmail(),
                    _('Mailman privacy alert'),
                    _("""\
An attempt was made to subscribe your address to the mailing list
%(listaddr)s.  You are already subscribed to this mailing list.

Note that the list membership is not public, so it is possible that a bad
person was trying to probe the list for its membership.  This would be a
privacy violation if we let them do this, but we didn't.

If you submitted the subscription request and forgot that you were already
subscribed to the list, then you can ignore this message.  If you suspect that
an attempt is being made to covertly discover whether you are a member of this
list, and you are worried about your privacy, then feel free to send a message
to the list administrator at %(listowner)s.
"""), lang=mlang)
            finally:
                i18n.set_translation(otrans)
            msg.send(mlist)
    # These shouldn't happen unless someone's tampering with the form
    except Errors.MMCantDigestError:
        results = _('This list does not support digest delivery.')
    except Errors.MMMustDigestError:
        results = _('This list only supports digest delivery.')
    else:
        # Everything's cool.  Our return string actually depends on whether
        # this list has private rosters or not
        if privacy_results:
            results = privacy_results
        else:
            results = _("""\
You have been successfully subscribed to the %(realname)s mailing list.""")
    # Show the results
    print_results(mlist, results, doc, lang)



def print_results(mlist, results, doc, lang):
    # The bulk of the document will come from the options.html template, which
    # includes its own html armor (head tags, etc.).  Suppress the head that
    # Document() derived pages get automatically.
    doc.suppress_head = 1

    replacements = mlist.GetStandardReplacements(lang)
    replacements['<mm-results>'] = results
    output = mlist.ParseTags('subscribe.html', replacements, lang)
    doc.AddItem(output)
    print doc.Format()
