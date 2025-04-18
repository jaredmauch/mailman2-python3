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

"""Provide a password-interface wrapper around private archives."""

import os
import sys
import cgi
import mimetypes

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import MailList
from Mailman import Errors
from Mailman import i18n
from Mailman.htmlformat import *
from Mailman.Logging.Syslog import syslog
from Mailman.Web import Auth

# Set up i18n.  Until we know which list is being requested, we use the
# server's default.
_ = i18n._
i18n.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)

SLASH = '/'


def true_path(path):
    "Ensure that the path is safe by removing .."
    # Workaround for path traverse vulnerability.  Unsuccessful attempts will
    # be logged in logs/error.
    parts = [x for x in path.split(SLASH) if x not in ('.', '..')]
    return SLASH.join(parts)[1:]


def guess_type(url, strict):
    if hasattr(mimetypes, 'common_types'):
        return mimetypes.guess_type(url, strict)
    return mimetypes.guess_type(url)


def main():
    doc = Document()
    doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)

    parts = Utils.GetPathPieces()
    if not parts:
        doc.SetTitle(_("Private Archive Error"))
        doc.AddItem(Header(3, _("You must specify a list.")))
        print(doc.Format())
        return

    path = os.environ.get('PATH_INFO')
    tpath = true_path(path)
    if tpath <> path[1:]:
        msg = _('Private archive - "./" and "../" not allowed in URL.')
        doc.SetTitle(msg)
        doc.AddItem(Header(2, msg))
        print(doc.Format())
        syslog('mischief', 'Private archive hostile path: %s', path)
        return
    # BAW: This needs to be converted to the Site module abstraction
    true_filename = os.path.join(
        mm_cfg.PRIVATE_ARCHIVE_FILE_DIR, tpath)

    listname = parts[0].lower()
    mboxfile = ''
    if len(parts) > 1:
        mboxfile = parts[1]

    # See if it's the list's mbox file is being requested
    if listname.endswith('.mbox') and mboxfile.endswith('.mbox') and \
           listname[:-5] == mboxfile[:-5]:
        listname = listname[:-5]
    else:
        mboxfile = ''

    # If it's a directory, we have to append index.html in this script.  We
    # must also check for a gzipped file, because the text archives are
    # usually stored in compressed form.
    if os.path.isdir(true_filename):
        true_filename = true_filename + '/index.html'
    if not os.path.exists(true_filename) and \
           os.path.exists(true_filename + '.gz'):
        true_filename = true_filename + '.gz'

    try:
        mlist = MailList.MailList(listname, lock=0)
    except Errors.MMListError as e:
        # Avoid cross-site scripting attacks
        safelistname = Utils.websafe(listname)
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('No such list <em>%(safelistname)s</em>')))
        # Send this with a 404 status
        print('Status: 404 Not Found')
        print(doc.Format())
        syslog('error', 'private: No such list "%s": %s\n', listname, e)
        return

    i18n.set_language(mlist.preferred_language)
    doc.set_language(mlist.preferred_language)

    # Must be authenticated to get any farther
    cgidata = cgi.FieldStorage()
    try:
        cgidata.getfirst('adminpw', '')
    except TypeError:
        # Someone crafted a POST with a bad Content-Type:.
        doc.AddItem(Header(2, _("Error")))
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
                   'Authorization failed (private): list=%s: remote=%s',
                   listname, remote)
        else:
            msg = ''
        Auth.loginpage(mlist, 'admin', msg=msg)
        return

    lang = mlist.getMemberLanguage(username)
    i18n.set_language(lang)
    doc.set_language(lang)

    # Authorization confirmed... output the desired file
    try:
        ctype, enc = guess_type(path, strict=0)
        if ctype is None:
            ctype = 'text/html'
        if mboxfile:
            f = open(os.path.join(mlist.archive_dir() + '.mbox',
                                  mlist.internal_name() + '.mbox'))
            ctype = 'text/plain'
        elif true_filename.endswith('.gz'):
            import gzip
            f = gzip.open(true_filename, 'r')
        else:
            f = open(true_filename, 'r')
    except IOError:
        msg = _('Private archive file not found')
        doc.SetTitle(msg)
        doc.AddItem(Header(2, msg))
        print('Status: 404 Not Found')
        print(doc.Format())
        syslog('error', 'Private archive file not found: %s', true_filename)
    else:
        print('Content-type: %s\n' % ctype)
        sys.stdout.write(f.read())
        f.close()
