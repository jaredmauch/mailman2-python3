# Copyright (C) 2011-2018 by the Free Software Foundation, Inc.
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

""" Cross-Site Request Forgery checker """

import time
import urllib
import marshal
import binascii

from Mailman import mm_cfg
from Mailman.Logging.Syslog import syslog
from Mailman.Utils import UnobscureEmail, sha_new

keydict = {
    'user':      mm_cfg.AuthUser,
    'poster':    mm_cfg.AuthListPoster,
    'moderator': mm_cfg.AuthListModerator,
    'admin':     mm_cfg.AuthListAdmin,
    'site':      mm_cfg.AuthSiteAdmin,
}



def csrf_token(mlist, contexts, user=None):
    """ create token by mailman cookie generation algorithm """

    if user:
        # Unmunge a munged email address.
        user = UnobscureEmail(urllib.unquote(user))
        
    for context in contexts:
        key, secret = mlist.AuthContextInfo(context, user)
        if key and secret:
            break
    else:
        return None     # not authenticated
    issued = int(time.time())
    mac = sha_new(secret + `issued`).hexdigest()
    keymac = '%s:%s' % (key, mac)
    token = binascii.hexlify(marshal.dumps((issued, keymac)))
    return token

def csrf_check(mlist, token, cgi_user=None):
    """ check token by mailman cookie validation algorithm """
    try:
        issued, keymac = marshal.loads(binascii.unhexlify(token))
        key, received_mac = keymac.split(':', 1)
        if not key.startswith(mlist.internal_name() + '+'):
            return False
        key = key[len(mlist.internal_name()) + 1:]
        if '+' in key:
            key, user = key.split('+', 1)
        else:
            user = None
        # Don't allow unprivileged tokens for admin or admindb.
        if cgi_user == 'admin':
            if key not in ('admin', 'site'):
                syslog('mischief',
                       'admin form submitted with CSRF token issued for %s.',
                       key + '+' + user if user else key)
                return False
        elif cgi_user == 'admindb':
            if key not in ('moderator', 'admin', 'site'):
                syslog('mischief',
                       'admindb form submitted with CSRF token issued for %s.',
                       key + '+' + user if user else key)
                return False
        if user:
            # This is for CVE-2021-42097.  The token is a user token because
            # of the fix for CVE-2021-42096 but it must match the user for
            # whom the options page is requested.
            raw_user = UnobscureEmail(urllib.unquote(user))
            if cgi_user and cgi_user.lower() != raw_user.lower():
                syslog('mischief',
                       'Form for user %s submitted with CSRF token '
                       'issued for %s.',
                       cgi_user, raw_user)
                return False
        context = keydict.get(key)
        key, secret = mlist.AuthContextInfo(context, user)
        assert key
        mac = sha_new(secret + `issued`).hexdigest()
        if (mac == received_mac 
            and 0 < time.time() - issued < mm_cfg.FORM_LIFETIME):
            return True
        return False
    except (AssertionError, ValueError, TypeError):
        return False
