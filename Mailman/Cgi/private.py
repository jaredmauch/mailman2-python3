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
from __future__ import print_function

import os
import sys
from Mailman.Utils import FieldStorage
import mimetypes

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import MailList
from Mailman import Errors
from Mailman import i18n
from Mailman.htmlformat import *
from Mailman.Logging.Syslog import syslog

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
    if tpath != path[1:]:
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
        msg = _(f'No such list <em>{safelistname}</em>')
        doc.SetTitle(_(f"Private Archive Error - {msg}"))
        doc.AddItem(Header(2, msg))
        # Send this with a 404 status.
        print('Status: 404 Not Found')
        print(doc.Format())
        syslog('error', 'private: No such list "%s": %s\n', listname, e)
        return

    i18n.set_language(mlist.preferred_language)
    doc.set_language(mlist.preferred_language)

    cgidata = FieldStorage()
    try:
        username = cgidata.getfirst('username', '').strip()
    except TypeError:
        # Someone crafted a POST with a bad Content-Type:.
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script.')))
        # Send this with a 400 status.
        print('Status: 400 Bad Request')
        print(doc.Format())
        return
    password = cgidata.getfirst('password', '')

    is_auth = 0
    realname = mlist.real_name
    message = ''

    if not mlist.WebAuthenticate((mm_cfg.AuthUser,
                                  mm_cfg.AuthListModerator,
                                  mm_cfg.AuthListAdmin,
                                  mm_cfg.AuthSiteAdmin),
                                 password, username):
        if 'submit' in cgidata:
            # This is a re-authorization attempt
            message = Bold(FontSize('+1', _('Authorization failed.'))).Format()
            remote = os.environ.get('HTTP_FORWARDED_FOR',
                     os.environ.get('HTTP_X_FORWARDED_FOR',
                     os.environ.get('REMOTE_ADDR',
                                    'unidentified origin')))
            syslog('security',
                 'Authorization failed (private): user=%s: list=%s: remote=%s',
                   username, listname, remote)
            # give an HTTP 401 for authentication failure
            print('Status: 401 Unauthorized')
        # Are we processing a password reminder from the login screen?
        if 'login-remind' in cgidata:
            if username:
                message = Bold(FontSize('+1', _(f"""If you are a list member,
                          your password has been emailed to you."""))).Format()
            else:
                message = Bold(FontSize('+1',
                                _('Please enter your email address'))).Format()
            if mlist.isMember(username):
                mlist.MailUserPassword(username)
            elif username:
                # Not a member. Don't report address in any case. It leads to
                # Content injection. Just log if roster is not public.
                if mlist.private_roster != 0:
                    syslog('mischief',
                       'Reminder attempt of non-member w/ private rosters: %s',
                       username)
        # Output the password form
        charset = Utils.GetCharSet(mlist.preferred_language)
        print('Content-type: text/html; charset=' + charset + '\n\n')
        # Put the original full path in the authorization form, but avoid
        # trailing slash if we're not adding parts.  We add it below.
        action = mlist.GetScriptURL('private', absolute=1)
        if mboxfile:
            action += '.mbox'
        if parts[1:]:
            action = os.path.join(action, SLASH.join(parts[1:]))
        # If we added '/index.html' to true_filename, add a slash to the URL.
        # We need this because we no longer add the trailing slash in the
        # private.html template.  It's always OK to test parts[-1] since we've
        # already verified parts[0] is listname.  The basic rule is if the
        # post URL (action) is a directory, it must be slash terminated, but
        # not if it's a file.  Otherwise, relative links in the target archive
        # page don't work.
        if true_filename.endswith('/index.html') and parts[-1] != 'index.html':
            action += SLASH
        # Escape web input parameter to avoid cross-site scripting.
        print(Utils.maketext(
            'private.html',
            {'action'  : Utils.websafe(action),
             'realname': mlist.real_name,
             'message' : message,
             }, mlist=mlist))
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
            f = open(true_filename, 'rb')
    except IOError:
        msg = _('Private archive file not found')
        doc.SetTitle(msg)
        doc.AddItem(Header(2, msg))
        print('Status: 404 Not Found')
        print(doc.Format())
        syslog('error', 'Private archive file not found: %s', true_filename)
    else:
        content = f.read()
        f.close()
        buffered = sys.stdout.getvalue()
        sys.stdout.truncate(0)
        sys.stdout.seek(0)
        orig_stdout = sys.stdout
        sys.stdout = sys.__stdout__
        sys.stdout.write(buffered)
        print('Content-type: %s\n' % ctype)
        sys.stdout.flush()
        sys.stdout.buffer.write(content)
        sys.stdout.flush()
        sys.stdout = orig_stdout
