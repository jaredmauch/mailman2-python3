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
import urllib.parse
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


def true_path(path):
    """Ensure that the path is safe by removing .. and other dangerous components.
    
    Args:
        path: The path to sanitize
        
    Returns:
        str: The sanitized path or None if invalid
    """
    if not path:
        return None
        
    # Remove any leading/trailing slashes
    path = path.strip('/')
    
    # Split into components and filter out dangerous parts
    parts = [x for x in path.split('/') if x and x not in ('.', '..')]
    
    # Reconstruct the path
    return '/'.join(parts)


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

    # Validate listname
    is_valid, listname, error_msg = validate_listname(parts[0])
    if not is_valid:
        doc.SetTitle(_("Private Archive Error"))
        doc.AddItem(Header(3, error_msg))
        print('Status: 400 Bad Request')
        print(doc.Format())
        syslog('mischief', 'Private archive invalid path: %s', parts[0])
        return

    # Validate and sanitize the full path
    path = os.environ.get('PATH_INFO', '')
    tpath = true_path(path)
    if not tpath:
        msg = _('Private archive - Invalid path')
        doc.SetTitle(msg)
        doc.AddItem(Header(2, msg))
        print('Status: 400 Bad Request')
        print(doc.Format())
        syslog('mischief', 'Private archive invalid path: %s', path)
        return

    # BAW: This needs to be converted to the Site module abstraction
    true_filename = os.path.join(mm_cfg.PRIVATE_ARCHIVE_FILE_DIR, tpath)

    try:
        mlist = MailList.MailList(listname, lock=0)
    except Errors.MMListError as e:
        # Avoid cross-site scripting attacks and information disclosure
        safelistname = Utils.websafe(listname)
        msg = _('No such list <em>{safelistname}</em>')
        doc.SetTitle(_("Private Archive Error - {msg}"))
        doc.AddItem(Header(2, msg))
        # Send this with a 404 status.
        print('Status: 404 Not Found')
        print(doc.Format())
        syslog('error', 'private: No such list "%s"', listname)
        return
    except Exception as e:
        # Log the full error but don't expose it to the user
        syslog('error', 'private: Unexpected error for list "%s": %s', listname, str(e))
        doc.SetTitle(_("Private Archive Error"))
        doc.AddItem(Header(2, _('An error occurred processing your request')))
        print('Status: 500 Internal Server Error')
        print(doc.Format())
        return

    i18n.set_language(mlist.preferred_language)
    doc.set_language(mlist.preferred_language)

    # Parse form data
    try:
        if os.environ.get('REQUEST_METHOD') == 'POST':
            content_length = int(os.environ.get('CONTENT_LENGTH', 0))
            if content_length > 0:
                form_data = sys.stdin.buffer.read(content_length).decode('utf-8')
                cgidata = urllib.parse.parse_qs(form_data, keep_blank_values=True)
            else:
                cgidata = {}
        else:
            query_string = os.environ.get('QUERY_STRING', '')
            cgidata = urllib.parse.parse_qs(query_string, keep_blank_values=True)
    except Exception:
        # Someone crafted a POST with a bad Content-Type:.
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script.')))
        # Send this with a 400 status.
        print('Status: 400 Bad Request')
        print(doc.Format())
        return

    username = cgidata.get('username', [''])[0].strip()
    password = cgidata.get('password', [''])[0]

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
        print('<!DOCTYPE html>')
        # Put the original full path in the authorization form, but avoid
        # trailing slash if we're not adding parts.  We add it below.
        action = mlist.GetScriptURL('private', absolute=1)
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
        # Use ParseTags for proper template processing
        replacements = {
            'action': Utils.websafe(action),
            'realname': mlist.real_name,
            'message': message
        }
        # Use list's preferred language as fallback before authentication
        output = mlist.ParseTags('private.html', replacements, mlist.preferred_language)
        print(output)
        return

    lang = mlist.getMemberLanguage(username)
    i18n.set_language(lang)
    doc.set_language(lang)

    # Authorization confirmed... output the desired file
    try:
        ctype, enc = guess_type(path, strict=0)
        if ctype is None:
            ctype = 'text/html'
        if true_filename.endswith('.gz'):
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
