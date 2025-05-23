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
from __future__ import print_function

import sys
import os
import time
import signal
import urllib.parse
import json
import ipaddress

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import MailList
from Mailman import Errors
from Mailman import i18n
from Mailman.Message import Message
from Mailman.UserDesc import UserDesc
from Mailman.htmlformat import *
from Mailman.Logging.Syslog import mailman_log
from Mailman.Utils import validate_ip_address

SLASH = '/'
ERRORSEP = '\n\n<p>'
COMMASPACE = ', '

# Set up i18n
_ = i18n._
i18n.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)


def validate_listname(listname):
    """Validate and sanitize a listname to prevent path traversal.
    
    Args:
        listname: The listname to validate
        
    Returns:
        tuple: (is_valid, sanitized_name, error_message)
    """
    if not listname:
        return False, None, _('List name is required')
        
    # Convert to lowercase and strip whitespace
    listname = listname.lower().strip()
    
    # Basic validation
    if not Utils.ValidateListName(listname):
        return False, None, _('Invalid list name')
        
    # Check for path traversal attempts
    if '..' in listname or '/' in listname or '\\' in listname:
        return False, None, _('Invalid list name')
        
    return True, listname, None


def main():
    doc = Document()
    doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)

    parts = Utils.GetPathPieces()
    if not parts:
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script')))
        print('Status: 400 Bad Request')
        print(doc.Format())
        return

    # Validate listname
    is_valid, listname, error_msg = validate_listname(parts[0])
    if not is_valid:
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(error_msg))
        print('Status: 400 Bad Request')
        print(doc.Format())
        return

    try:
        mlist = MailList.MailList(listname, lock=0)
    except Errors.MMListError as e:
        # Avoid cross-site scripting attacks and information disclosure
        safelistname = Utils.websafe(listname)
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('No such list <em>{safelistname}</em>')))
        # Send this with a 404 status.
        print('Status: 404 Not Found')
        print(doc.Format())
        mailman_log('error', 'subscribe: No such list "%s"', listname)
        return
    except Exception as e:
        # Log the full error but don't expose it to the user
        mailman_log('error', 'subscribe: Unexpected error for list "%s": %s', listname, str(e))
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('An error occurred processing your request')))
        print('Status: 500 Internal Server Error')
        print(doc.Format())
        return

    # See if the form data has a preferred language set, in which case, use it
    # for the results.  If not, use the list's preferred language.
    try:
        if os.environ.get('REQUEST_METHOD') == 'POST':
            # Get the content length
            content_length = int(os.environ.get('CONTENT_LENGTH', 0))
            # Read the form data
            form_data = sys.stdin.read(content_length)
            cgidata = urllib.parse.parse_qs(form_data, keep_blank_values=True)
        else:
            query_string = os.environ.get('QUERY_STRING', '')
            cgidata = urllib.parse.parse_qs(query_string, keep_blank_values=True)
    except Exception as e:
        # Log the error but don't expose details
        mailman_log('error', 'subscribe: Error parsing form data: %s', str(e))
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('Invalid request')))
        print('Status: 400 Bad Request')
        print(doc.Format())
        return

    try:
        language = cgidata.get('language', [''])[0]
    except TypeError:
        # Someone crafted a POST with a bad Content-Type:.
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script.')))
        # Send this with a 400 status.
        print('Status: 400 Bad Request')
        print(doc.Format())
        return
    if not Utils.IsLanguage(language):
        language = mlist.preferred_language
    i18n.set_language(language)
    doc.set_language(language)

    # We need a signal handler to catch the SIGTERM that can come from Apache
    # when the user hits the browser's STOP button.  See the comment in
    # admin.py for details.
    def sigterm_handler(signum, frame, mlist=mlist):
        # Make sure the list gets unlocked...
        mlist.Unlock()
        # ...and ensure we exit, otherwise race conditions could cause us to
        # enter MailList.Save() while we're in the unlocked state, and that
        # could be bad!
        sys.exit(0)

    # Install the emergency shutdown signal handler
    signal.signal(signal.SIGTERM, sigterm_handler)

    process_form(mlist, doc, cgidata, language)


def process_form(mlist, doc, cgidata, lang):
    listowner = mlist.GetOwnerEmail()
    realname = mlist.real_name
    results = []

    # The email address being subscribed, required
    email = cgidata.get('email', [''])[0]
    if isinstance(email, bytes):
        email = email.decode('utf-8', 'replace')
    email = email.strip().lower()
    if not email:
        results.append(_('You must supply a valid email address.'))

    fullname = cgidata.get('fullname', [''])[0]
    if isinstance(fullname, bytes):
        fullname = fullname.decode('utf-8', 'replace')
    # Canonicalize the full name
    fullname = Utils.canonstr(fullname, lang)
    # Who was doing the subscribing?
    remote = os.environ.get('HTTP_FORWARDED_FOR',
             os.environ.get('HTTP_X_FORWARDED_FOR',
             os.environ.get('REMOTE_ADDR',
                            'unidentified origin')))

    # Check reCAPTCHA submission, if enabled
    if mm_cfg.RECAPTCHA_SECRET_KEY:
        recaptcha_response = cgidata.get('g-recaptcha-response', [''])[0]
        if isinstance(recaptcha_response, bytes):
            recaptcha_response = recaptcha_response.decode('utf-8', 'replace')
        request_data = urllib.parse.urlencode({
                'secret': mm_cfg.RECAPTCHA_SECRET_KEY,
                'response': recaptcha_response,
                'remoteip': remote})
        request_data = request_data.encode('utf-8')
        request = urllib.request.Request(
            url = 'https://www.google.com/recaptcha/api/siteverify',
            data = request_data)
        try:
            httpresp = urllib.request.urlopen(request)
            captcha_response = json.load(httpresp)
            httpresp.close()
            if not captcha_response['success']:
                e_codes = COMMASPACE.join(captcha_response['error-codes'])
                results.append(_('reCAPTCHA validation failed: {}').format(e_codes))
        except urllib.error.URLError as e:
            e_reason = e.reason
            results.append(_('reCAPTCHA could not be validated: {e_reason}'))

    # Get and validate IP address
    ip = os.environ.get('REMOTE_ADDR', '')
    is_valid, normalized_ip = validate_ip_address(ip)
    if not is_valid:
        ip = ''
    else:
        ip = normalized_ip

    # Are we checking the hidden data?
    if mm_cfg.SUBSCRIBE_FORM_SECRET:
        now = int(time.time())
        # Try to accept a range in case of load balancers, etc.  (LP: #1447445)
        if ip.find('.') >= 0:
            # ipv4 - drop last octet
            remote1 = ip.rsplit('.', 1)[0]
        else:
            # ipv6 - drop last 16 (could end with :: in which case we just
            #        drop one : resulting in an invalid format, but it's only
            #        for our hash so it doesn't matter.
            remote1 = ip.rsplit(':', 1)[0]
        try:
            sub_form_token = cgidata.get('sub_form_token', [''])[0]
            if isinstance(sub_form_token, bytes):
                sub_form_token = sub_form_token.decode('utf-8', 'replace')
            ftime, fcaptcha_idx, fhash = sub_form_token.split(':')
            then = int(ftime)
        except ValueError:
            ftime = fcaptcha_idx = fhash = ''
            then = 0
        needs_hashing = (mm_cfg.SUBSCRIBE_FORM_SECRET + ":" + ftime + ":" + fcaptcha_idx +
                        ":" + mlist.internal_name() + ":" + remote1).encode('utf-8')
        token = Utils.sha_new(needs_hashing).hexdigest()
        if ftime and now - then > mm_cfg.FORM_LIFETIME:
            results.append(_('The form is too old.  Please GET it again.'))
        if ftime and now - then < mm_cfg.SUBSCRIBE_FORM_MIN_TIME:
            results.append(_('The form was submitted too quickly.  Please wait a moment and try again.'))
        if ftime and token != fhash:
            results.append(_('The form was tampered with.  Please GET it again.'))

    # Was an attempt made to subscribe the list to itself?
    if email == mlist.GetListEmail():
        mailman_log('mischief', 'Attempt to self subscribe %s: %s', email, remote)
        results.append(_('You may not subscribe a list to itself!'))
    # If the user did not supply a password, generate one for him
    password = cgidata.get('pw', [''])[0]
    if isinstance(password, bytes):
        password = password.decode('utf-8', 'replace')
    password = password.strip()
    
    confirmed = cgidata.get('pw-conf', [''])[0]
    if isinstance(confirmed, bytes):
        confirmed = confirmed.decode('utf-8', 'replace')
    confirmed = confirmed.strip()

    if not password and not confirmed:
        password = Utils.MakeRandomPassword()
    elif not password or not confirmed:
        results.append(_('If you supply a password, you must confirm it.'))
    elif password != confirmed:
        results.append(_('Your passwords did not match.'))

    # Get the digest option for the subscription.
    digestflag = cgidata.get('digest', [''])[0]
    if isinstance(digestflag, bytes):
        digestflag = digestflag.decode('utf-8', 'replace')
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
        privacy_results = _(f"""\
Your subscription request has been received, and will soon be acted upon.
Depending on the configuration of this mailing list, your subscription request
may have to be first confirmed by you via email, or approved by the list
moderator.  If confirmation is required, you will soon get a confirmation
email which contains further instructions.""")

    # Acquire the lock before attempting to add the member
    mlist.Lock()
    try:
        userdesc = UserDesc(email, fullname, password, digest, lang)
        mlist.AddMember(userdesc, remote)
        results = ''
        mlist.Save()
    except Errors.MembershipIsBanned:
        results = _(f"""The email address you supplied is banned from this
        mailing list.  If you think this restriction is erroneous, please
        contact the list owners at {listowner}.""")
    except Errors.MMBadEmailError:
        results = _(f"""\
The email address you supplied is not valid.  (E.g. it must contain an
`@'.)""")
    except Errors.MMHostileAddress:
        results = _(f"""\
Your subscription is not allowed because the email address you gave is
insecure.""")
    except Errors.MMSubscribeNeedsConfirmation:
        # Results string depends on whether we have private rosters or not
        if privacy_results:
            results = privacy_results
        else:
            results = _(f"""\
Confirmation from your email address is required, to prevent anyone from
subscribing you without permission.  Instructions are being sent to you at
{email}.  Please note your subscription will not start until you confirm
your subscription.""")
    except Errors.MMNeedApproval as x:
        # Results string depends on whether we have private rosters or not
        if privacy_results:
            results = privacy_results
        else:
            # We need to interpolate into x.__str__()
            x = _(str(x))
            results = _(f"""\
Your subscription request was deferred because {x}.  Your request has been
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
                msg = Mailman.Message.UserNotification(
                    mlist.getMemberCPAddress(email),
                    mlist.GetBouncesEmail(),
                    _('Mailman privacy alert'),
                    _(f"""\
An attempt was made to subscribe your address to the mailing list
{listaddr}.  You are already subscribed to this mailing list.

Note that the list membership is not public, so it is possible that a bad
person was trying to probe the list for its membership.  This would be a
privacy violation if we let them do this, but we didn't.

If you submitted the subscription request and forgot that you were already
subscribed to the list, then you can ignore this message.  If you suspect that
an attempt is being made to covertly discover whether you are a member of this
list, and you are worried about your privacy, then feel free to send a message
to the list administrator at {listowner}.
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
            results = _(f"""\
You have been successfully subscribed to the {realname} mailing list.""")
    finally:
        mlist.Unlock()
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
    print(doc.Format())
