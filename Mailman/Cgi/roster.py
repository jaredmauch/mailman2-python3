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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

"""Produce subscriber roster, using listinfo form data, roster.html template.

Takes listname in PATH_INFO.
"""
from __future__ import print_function


# We don't need to lock in this script, because we're never going to change
# data.

import sys
import os
from urllib.parse import parse_qs

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import MailList
from Mailman import Errors
from Mailman import i18n
from Mailman.htmlformat import *
from Mailman.Logging.Syslog import syslog

# Set up i18n
_ = i18n._
i18n.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)


def main():
    doc = Document()
    doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)

    # Get form data using parse_qs
    try:
        if os.environ.get('REQUEST_METHOD', '').lower() == 'post':
            content_type = os.environ.get('CONTENT_TYPE', '')
            if content_type.startswith('application/x-www-form-urlencoded'):
                content_length = int(os.environ.get('CONTENT_LENGTH', 0))
                form_data = sys.stdin.read(content_length)
                cgidata = parse_qs(form_data, keep_blank_values=1)
            else:
                raise ValueError('Invalid content type')
        else:
            cgidata = parse_qs(os.environ.get('QUERY_STRING', ''), keep_blank_values=1)
    except Exception:
        # Someone crafted a POST with a bad Content-Type:.
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script.')))
        # Send this with a 400 status.
        print('Status: 400 Bad Request')
        print(doc.Format())
        return

    parts = Utils.GetPathPieces()
    if not parts:
        error_page(_('Invalid options to CGI script'))
        return

    listname = parts[0].lower()
    try:
        mlist = MailList.MailList(listname, lock=0)
    except Errors.MMListError as e:
        # Avoid cross-site scripting attacks
        safelistname = Utils.websafe(listname)
        # Send this with a 404 status.
        print('Status: 404 Not Found')
        error_page(_('No such list <em>{safelistname}</em>'))
        syslog('error', 'roster: No such list "%s": %s', listname, e)
        return

    try:
        lang = cgidata.get('language', [None])[0]
    except Exception:
        # Someone crafted a POST with a bad Content-Type:.
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script.')))
        # Send this with a 400 status.
        print('Status: 400 Bad Request')
        print(doc.Format())
        return

    if not Utils.IsLanguage(lang):
        lang = mlist.preferred_language
    i18n.set_language(lang)

    # Perform authentication for protected rosters.  If the roster isn't
    # protected, then anybody can see the pages.  If members-only or
    # "admin"-only, then we try to cookie authenticate the user, and failing
    # that, we check roster-email and roster-pw fields for a valid password.
    # (also allowed: the list moderator, the list admin, and the site admin).
    password = cgidata.get('roster-pw', [None])[0]
    addr = cgidata.get('roster-email', [None])[0]
    list_hidden = (not mlist.WebAuthenticate((mm_cfg.AuthUser,),
                                             password, addr)
                   and mlist.WebAuthenticate((mm_cfg.AuthListModerator,
                                              mm_cfg.AuthListAdmin,
                                              mm_cfg.AuthSiteAdmin),
                                             password))
    if mlist.private_roster == 0:
        # No privacy
        ok = 1
    elif mlist.private_roster == 1:
        # Members only
        ok = mlist.WebAuthenticate((mm_cfg.AuthUser,
                                    mm_cfg.AuthListModerator,
                                    mm_cfg.AuthListAdmin,
                                    mm_cfg.AuthSiteAdmin),
                                   password, addr)
    else:
        # Admin only, so we can ignore the address field
        ok = mlist.WebAuthenticate((mm_cfg.AuthListModerator,
                                    mm_cfg.AuthListAdmin,
                                    mm_cfg.AuthSiteAdmin),
                                   password)
    if not ok:
        realname = mlist.real_name
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('{realname} roster authentication failed.')))
        # Send this with a 401 status.
        print('Status: 401 Unauthorized')
        doc.AddItem(mlist.GetMailmanFooter())
        print(doc.Format())
        remote = os.environ.get('HTTP_FORWARDED_FOR',
                 os.environ.get('HTTP_X_FORWARDED_FOR',
                 os.environ.get('REMOTE_ADDR',
                                'unidentified origin')))
        syslog('security',
               'Authorization failed (roster): user=%s: list=%s: remote=%s',
               addr, listname, remote)
        return

    # The document and its language
    doc = HeadlessDocument()
    doc.set_language(lang)

    replacements = mlist.GetAllReplacements(lang, list_hidden)
    replacements['<mm-displang-box>'] = mlist.FormatButton(
        'displang-button',
        text = _('View this page in'))
    replacements['<mm-lang-form-start>'] = mlist.FormatFormStart('roster')
    doc.AddItem(mlist.ParseTags('roster.html', replacements, lang))
    print(doc.Format())


def error_page(errmsg):
    doc = Document()
    doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)
    error_page_doc(doc, errmsg)
    print(doc.Format())


def error_page_doc(doc, errmsg):
    # Produce a simple error-message page on stdout and exit.
    doc.SetTitle(_("Error"))
    doc.AddItem(Header(2, _("Error")))
    doc.AddItem(Bold(errmsg))
