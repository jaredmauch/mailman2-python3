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
import urllib.parse
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
        user = UnobscureEmail(urllib.parse.unquote(user))
        syslog('debug', 'CSRF token generation: mlist=%s, contexts=%s, user=%s',
               mlist.internal_name(), contexts, user)
    else:
        syslog('debug', 'CSRF token generation: mlist=%s, contexts=%s',
               mlist.internal_name(), contexts)
        
    selected_context = None
    for context in contexts:
        key, secret = mlist.AuthContextInfo(context, user)
        if key and secret:
            selected_context = context
            syslog('debug', 'CSRF token generation: Selected context=%s, key=%s',
                   context, key)
            break
    else:
        syslog('debug', 'CSRF token generation failed: No valid context found in %s',
               contexts)
        return None     # not authenticated
        
    issued = int(time.time())
    needs_hash = (secret + repr(issued)).encode('utf-8')
    mac = sha_new(needs_hash).hexdigest()
    keymac = '%s:%s' % (key, mac)
    token = binascii.hexlify(marshal.dumps((issued, keymac))).decode('utf-8')
    
    syslog('debug', 'CSRF token generated: context=%s, key=%s, issued=%s, mac=%s, token=%s',
           selected_context, key, time.ctime(issued), mac, token)
    return token

def csrf_check(mlist, token, cgi_user=None):
    """ check token by mailman cookie validation algorithm """
    try:
        syslog('debug', 'CSRF token validation: mlist=%s, cgi_user=%s, token=%s',
               mlist.internal_name(), cgi_user, token)
               
        issued, keymac = marshal.loads(binascii.unhexlify(token))
        key, received_mac = keymac.split(':', 1)
        
        syslog('debug', 'CSRF token details: issued=%s, key=%s, received_mac=%s',
               time.ctime(issued), key, received_mac)
               
        if not key.startswith(mlist.internal_name() + '+'):
            syslog('debug', 'CSRF token validation failed: Invalid mailing list name in key. Expected %s, got %s',
                   mlist.internal_name(), key)
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
            raw_user = UnobscureEmail(urllib.parse.unquote(user))
            if cgi_user and cgi_user.lower() != raw_user.lower():
                syslog('mischief',
                       'Form for user %s submitted with CSRF token '
                       'issued for %s.',
                       cgi_user, raw_user)
                return False
                
        context = keydict.get(key)
        key, secret = mlist.AuthContextInfo(context, user)
        assert key
        
        mac = sha_new(secret + repr(issued)).hexdigest()
        age = time.time() - issued
        
        syslog('debug', 'CSRF token validation: context=%s, generated_mac=%s, age=%s seconds',
               context, mac, age)
               
        if (mac == received_mac 
            and 0 < age < mm_cfg.FORM_LIFETIME):
            syslog('debug', 'CSRF token validation successful')
            return True
            
        if mac != received_mac:
            syslog('debug', 'CSRF token validation failed: MAC mismatch. Expected %s, got %s. Full token details: expected=(%s, %s:%s), received=(%s, %s:%s)',
                   mac, received_mac, time.ctime(issued), key, mac, time.ctime(issued), key, received_mac)
        elif age <= 0:
            syslog('debug', 'CSRF token validation failed: Token issued in the future. Token details: issued=%s, key=%s, mac=%s',
                   time.ctime(issued), key, received_mac)
        else:
            syslog('debug', 'CSRF token validation failed: Token expired. Age: %s seconds, FORM_LIFETIME=%s seconds, contexts=%s. Token details: issued=%s, key=%s, mac=%s',
                   age, mm_cfg.FORM_LIFETIME, keydict.keys(), time.ctime(issued), key, received_mac)
                   
        return False
    except (AssertionError, ValueError, TypeError) as e:
        syslog('error', 'CSRF token validation failed with error: %s', str(e))
        return False
