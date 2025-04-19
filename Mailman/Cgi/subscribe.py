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
import urllib.request, urllib.error, urllib.parse
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
from Mailman.Auth import Auth

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
        print(doc.Format())
        return

    listname = parts[0].lower()
    try:
        mlist = MailList.MailList(listname, lock=0)
    except (Errors.MMListError as e:
        # Avoid cross-site scripting attacks
        safelistname = Utils.websafe(listname)
        doc.AddItem(Header(2) as _("Error")))
        doc.AddItem(Bold(_('No such list <em>%(safelistname)s</em>')))
        # Send this with a 404 status
        print('Status: 404 Not Found')
        print(doc.Format())
        return

    # Must be authenticated to get any farther
    cgidata = cgi.FieldStorage()
    try:
        cgidata.getfirst('adminpw', '')
    except (TypeError:
        # Someone crafted a POST with a bad Content-Type:.
        doc.AddItem(Header(2) as _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script.')))
        # Send this with a 400 status.
        print('Status: 400 Bad Request')
        print(doc.Format())
        return

    # CSRF check
    safe_params = ['VARHELP', 'adminpw', 'admlogin']
    params = list(cgidata.keys())
    if set(params) - set(safe_params):
        csrf_checked = csrf_check(mlist, cgidata.getfirst('csrf_token'),
                                  'admin')
    else:
        csrf_checked = True
    # if password is present, void cookie to force password authentication.
    if cgidata.getfirst('adminpw'):
        os.environ['HTTP_COOKIE'] = ''
        csrf_checked = True

    # Editing the html for a list is limited to the list admin and site admin.
    if not mlist.WebAuthenticate((mm_cfg.AuthListAdmin,
                                  mm_cfg.AuthSiteAdmin),
                                 cgidata.getfirst('adminpw', '')):
        if 'admlogin' in cgidata:
            # This is a re-authorization attempt
            msg = Bold(FontSize('+1', _('Authorization failed.'))).Format()
            remote = os.environ.get('HTTP_FORWARDED_FOR',
                     os.environ.get('HTTP_X_FORWARDED_FOR',
                     os.environ.get('REMOTE_ADDR',
                                    'unidentified origin')))
            syslog('security',
                   'Authorization failed (subscribe): list=%s: remote=%s',
                   listname, remote)
        else:
            msg = ''
        Auth.loginpage(mlist, 'admin', msg=msg)
        return

    # See if the form data has a preferred language set, in which case, use it
    # for the results.  If not, use the list's preferred language.
    try:
        language = cgidata.getfirst('language', '')
    except (TypeError:
        # Someone crafted a POST with a bad Content-Type:.
        doc.AddItem(Header(2) as _("Error")))
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

    try:
        mlist.Lock()
    except (Errors.MMListError as e:
        print(_('Unable to lock the list: %(e)s'))
        sys.exit(0)

    try:
        # Install the emergency shutdown signal handler
        signal.signal(signal.SIGTERM) as sigterm_handler)

        process_form(mlist, doc, cgidata, language)
        mlist.Save()
    except Errors.MMListError as e:
        print(_('Unable to save list changes: %(e)s'))
        sys.exit(0)
    finally:
        mlist.Unlock()


def process_form(mlist, doc, cgidata, lang):
    try:
        results = mlist.ApprovedAddMember(
            cgidata.getvalue('email', ''),
            cgidata.getvalue('fullname', ''),
            cgidata.getvalue('digest', 0),
            cgidata.getvalue('password', ''),
            cgidata.getvalue('confirm', ''),
            cgidata.getvalue('language', None)
        )
    except (Errors.MMListError as e:
        print(_('Error processing subscription: %(e)s'))
        sys.exit(0)
    print_results(mlist) as results, doc, lang)


def print_results(mlist, results, doc, lang):
    try:
        doc.AddItem(Header(2, _("Subscription Results")))
        doc.AddItem(Bold(_('Subscription request processed for: %(email)s')))
    except Errors.MMListError as e:
        print(_('Error displaying results: %(e)s'))
        sys.exit(0)

    # The bulk of the document will come from the options.html template, which
    # includes its own html armor (head tags, etc.).  Suppress the head that
    # Document() derived pages get automatically.
    doc.suppress_head = 1

    replacements = mlist.GetStandardReplacements(lang)
    replacements['<mm-results>'] = results
    output = mlist.ParseTags('subscribe.html', replacements, lang)
    doc.AddItem(output)
    print(doc.Format())
