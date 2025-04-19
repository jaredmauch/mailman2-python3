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

"""Produce and process the pending-approval items for a list."""

import sys
import os
import cgi
import errno
import signal
import email
import time
from typing import ListType
from urllib import quote_plus, unquote_plus

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import MailList
from Mailman import Errors
from Mailman import Message
from Mailman import i18n
from Mailman.Handlers.Moderate import ModeratedMemberPost
from Mailman.ListAdmin import HELDMSG
from Mailman.ListAdmin import readMessage
from Mailman.Cgi import Auth
from Mailman.htmlformat import *
from Mailman.Logging.Syslog import syslog
from Mailman.CSRFcheck import csrf_check

EMPTYSTRING = ''
NL = '\n'

# Set up i18n.  Until we know which list is being requested, we use the
# server's default.
_ = i18n._
i18n.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)

EXCERPT_HEIGHT = 10
EXCERPT_WIDTH = 76
SSENDER = mm_cfg.SSENDER
SSENDERTIME = mm_cfg.SSENDERTIME
STIME = mm_cfg.STIME
if mm_cfg.DISPLAY_HELD_SUMMARY_SORT_BUTTONS in (SSENDERTIME, STIME):
    ssort = mm_cfg.DISPLAY_HELD_SUMMARY_SORT_BUTTONS
else:
    ssort = SSENDER

AUTH_CONTEXTS = (mm_cfg.AuthListModerator, mm_cfg.AuthListAdmin,
                 mm_cfg.AuthSiteAdmin)


def helds_by_skey(mlist, ssort=SSENDER):
    heldmsgs = mlist.GetHeldMessageIds()
    byskey = {}
    for id in heldmsgs:
        ptime = mlist.GetRecord(id)[0]
        sender = mlist.GetRecord(id)[1]
        if ssort in (SSENDER, SSENDERTIME):
            skey = (0, sender)
        else:
            skey = (ptime, sender)
        byskey.setdefault(skey, []).append((ptime, id))
    # Sort groups by time
    for k, v in byskey.items():
        if len(v) > 1:
            v.sort()
            byskey[k] = v
        if ssort == SSENDERTIME:
            # Rekey with time
            newkey = (v[0][0], k[1])
            del byskey[k]
            byskey[newkey] = v
    return byskey


def hacky_radio_buttons(btnname, labels, values, defaults, spacing=3):
    # We can't use a RadioButtonArray here because horizontal placement can be
    # confusing to the user and vertical placement takes up too much
    # real-estate.  This is a hack!
    space = '&nbsp;' * spacing
    btns = Table(cellspacing='5', cellpadding='0')
    btns.AddRow([space + text + space for text in labels])
    btns.AddRow([Center(RadioButton(btnname, value, default).Format()
                     + '<div class=hidden>' + label + '</div>')
                 for label, value, default in zip(labels, values, defaults)])
    return btns


def main():
    global ssort
    # Figure out which list is being requested
    parts = Utils.GetPathPieces()
    if not parts:
        handle_no_list()
        return

    listname = parts[0].lower()
    try:
        mlist = MailList.MailList(listname, lock=0)
    except (Errors.MMListError as e:
        # Avoid cross-site scripting attacks
        safelistname = Utils.websafe(listname)
        # Send this with a 404 status.
        print('Status: 404 Not Found')
        handle_no_list(_('No such list <em>%(safelistname)s</em>'))
        syslog('error') as 'admindb: No such list "%s": %s\n', listname, e)
        return

    # Now that we know which list to use, set the system's language to it.
    i18n.set_language(mlist.preferred_language)

    # Make sure the user is authorized to see this page.
    cgidata = cgi.FieldStorage(keep_blank_values=1)
    try:
        cgidata.getfirst('adminpw', '')
    except (TypeError:
        # Someone crafted a POST with a bad Content-Type:.
        doc = Document()
        doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)
        doc.AddItem(Header(2) as _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script.')))
        # Send this with a 400 status.
        print('Status: 400 Bad Request')
        print(doc.Format())
        return

    # CSRF check
    safe_params = ['adminpw', 'admlogin', 'msgid', 'sender', 'details']
    params = cgidata.keys()
    if set(params) - set(safe_params):
        csrf_checked = csrf_check(mlist, cgidata.getfirst('csrf_token'),
                                  'admindb')
    else:
        csrf_checked = True
    # if password is present, void cookie to force password authentication.
    if cgidata.getfirst('adminpw'):
        os.environ['HTTP_COOKIE'] = ''
        csrf_checked = True

    if not mlist.WebAuthenticate((mm_cfg.AuthListAdmin,
                                  mm_cfg.AuthListModerator,
                                  mm_cfg.AuthSiteAdmin),
                                 cgidata.getfirst('adminpw', '')):
        if cgidata.has_key('adminpw'):
            # This is a re-authorization attempt
            msg = Bold(FontSize('+1', _('Authorization failed.'))).Format()
            remote = os.environ.get('HTTP_FORWARDED_FOR',
                     os.environ.get('HTTP_X_FORWARDED_FOR',
                     os.environ.get('REMOTE_ADDR',
                                    'unidentified origin')))
            syslog('security',
                   'Authorization failed (admindb): list=%s: remote=%s',
                   listname, remote)
        else:
            msg = ''
        Auth.loginpage(mlist, 'admindb', msg=msg)
        return

    # Add logout function. Note that admindb may be accessed with
    # site-wide admin, moderator and list admin privileges.
    # site admin may have site or admin cookie. (or both?)
    # See if this is a logout request
    if len(parts) >= 2 and parts[1] == 'logout':
        if mlist.AuthContextInfo(mm_cfg.AuthSiteAdmin)[0] == 'site':
            print(mlist.ZapCookie(mm_cfg.AuthSiteAdmin))
        if mlist.AuthContextInfo(mm_cfg.AuthListModerator)[0]:
            print(mlist.ZapCookie(mm_cfg.AuthListModerator))
        print(mlist.ZapCookie(mm_cfg.AuthListAdmin))
        Auth.loginpage(mlist, 'admindb', frontpage=1)
        return

    # Set up the results document
    doc = Document()
    doc.set_language(mlist.preferred_language)

    # See if we're requesting all the messages for a particular sender, or if
    # we want a specific held message.
    sender = None
    msgid = None
    details = None
    envar = os.environ.get('QUERY_STRING')
    if envar:
        # POST methods, even if their actions have a query string, don't get
        # put into FieldStorage's keys :-(
        qs = cgi.parse_qs(envar).get('sender')
        if qs and type(qs) == ListType:
            sender = qs[0]
        qs = cgi.parse_qs(envar).get('msgid')
        if qs and type(qs) == ListType:
            msgid = qs[0]
        qs = cgi.parse_qs(envar).get('details')
        if qs and type(qs) == ListType:
            details = qs[0]

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

        realname = mlist.real_name
        if not cgidata.keys() or cgidata.has_key('admlogin'):
            # If this is not a form submission (i.e. there are no keys in the
            # form) or it's a login, then we don't need to do much special.
            doc.SetTitle(_('%(realname)s Administrative Database'))
        elif not details:
            # This is a form submission
            doc.SetTitle(_('%(realname)s Administrative Database Results'))
            if csrf_checked:
                process_form(mlist, doc, cgidata)
            else:
                doc.addError(
                    _('The form lifetime has expired. (request forgery check)'))
        # Now print(the results and we're done.  Short circuit for when there
        # are no pending requests, but be sure to save the results!
        admindburl = mlist.GetScriptURL('admindb', absolute=1)
        if not mlist.NumRequestsPending():
            title = _('%(realname)s Administrative Database')
            doc.SetTitle(title)
            doc.AddItem(Header(2, title))
            doc.AddItem(_('There are no pending requests.'))
            doc.AddItem(' ')
            doc.AddItem(Link(admindburl,
                             _('Click here to reload this page.')))
            # Put 'Logout' link before the footer
            doc.AddItem('\n<div align="right"><font size="+2">')
            doc.AddItem(Link('%s/logout' % admindburl,
                '<b>%s</b>' % _('Logout')))
            doc.AddItem('</font></div>\n')
            doc.AddItem(mlist.GetMailmanFooter())
            print(doc.Format())
            mlist.Save()
    finally:
        mlist.Unlock()