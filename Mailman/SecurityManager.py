# Copyright (C) 1998-2020 by the Free Software Foundation, Inc.
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


"""Handle passwords and sanitize approved messages."""

# There are current 5 roles defined in Mailman, as codified in Defaults.py:
# user, list-creator, list-moderator, list-admin, site-admin.
#
# Here's how we do cookie based authentication.
#
# Each role (see above) has an associated password, which is currently the
# only way to authenticate a role (in the future, we'll authenticate a
# user and assign users to roles).
#
# Each cookie has the following ingredients: the authorization context's
# secret (i.e. the password, and a timestamp.  We generate an SHA1 hex
# digest of these ingredients, which we call the `mac'.  We then marshal
# up a tuple of the timestamp and the mac, hexlify that and return that as
# a cookie keyed off the authcontext.  Note that authenticating the user
# also requires the user's email address to be included in the cookie.
#
# The verification process is done in CheckCookie() below.  It extracts
# the cookie, unhexlifies and unmarshals the tuple, extracting the
# timestamp.  Using this, and the shared secret, the mac is calculated,
# and it must match the mac passed in the cookie.  If so, they're golden,
# otherwise, access is denied.
#
# It is still possible for an adversary to attempt to brute force crack
# the password if they obtain the cookie, since they can extract the
# timestamp and create macs based on password guesses.  They never get a
# cleartext version of the password though, so security rests on the
# difficulty and expense of retrying the cgi dialog for each attempt.  It
# also relies on the security of SHA1.

import os
import re
import time
from http import cookies as Cookie
import pickle
import binascii
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional, Tuple, Union, List, Dict, Any, Type, Callable

try:
    import crypt
except ImportError:
    crypt = None

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
from Mailman.Logging.Syslog import syslog
from Mailman.Utils import hashlib_new as sha_new

# True and False are built-in constants in Python 3

class SecurityManager:
    """Security manager for Mailman mailing lists.
    
    Handles authentication, authorization, and security-related operations.
    """
    
    def InitVars(self) -> None:
        """Initialize security-related variables."""
        self.mod_password: Optional[str] = None
        self.post_password: Optional[str] = None
        self.passwords: Dict[str, str] = {}

    def AuthContextInfo(self, authcontext: int, user: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """Get authentication context information.
        
        Args:
            authcontext: One of AuthUser, AuthListModerator, AuthListAdmin, AuthSiteAdmin
            user: Email address (required for AuthUser context)
            
        Returns:
            Tuple of (cookie key, secret) or (None, None) if context doesn't exist
            
        Raises:
            TypeError: If AuthUser context but no user provided
            NotAMemberError: If user not a member of the list
            MMBadUserError: If user's secret is None
        """
        key = f'{self.internal_name()}+'
        if authcontext == mm_cfg.AuthUser:
            if user is None:
                raise TypeError('No user supplied for AuthUser context')
            user = Utils.UnobscureEmail(urllib.parse.unquote(user))
            secret = self.getMemberPassword(user)
            userdata = urllib.parse.quote(Utils.ObscureEmail(user), safe='')
            key += f'user+{userdata}'
        elif authcontext == mm_cfg.AuthListPoster:
            secret = self.post_password
            key += 'poster'
        elif authcontext == mm_cfg.AuthListModerator:
            secret = self.mod_password
            key += 'moderator'
        elif authcontext == mm_cfg.AuthListAdmin:
            secret = self.password
            key += 'admin'
        elif authcontext == mm_cfg.AuthSiteAdmin:
            sitepass = Utils.get_global_password()
            if mm_cfg.ALLOW_SITE_ADMIN_COOKIES and sitepass:
                secret = sitepass
                key = 'site'
            else:
                secret = self.password
                key += 'admin'
        else:
            return None, None
        return key, secret

    def Authenticate(self, authcontexts: List[int], response: str, user: Optional[str] = None) -> int:
        """Authenticate a user against one of the provided contexts.
        
        Args:
            authcontexts: List of authentication contexts to check
            response: Password or authentication response
            user: Email address (required for AuthUser context)
            
        Returns:
            The matching authcontext or UnAuthorized
        """
        if not response:
            return mm_cfg.UnAuthorized
            
        for ac in authcontexts:
            if ac == mm_cfg.AuthCreator:
                if Utils.check_global_password(response, siteadmin=False):
                    return mm_cfg.AuthCreator
            elif ac == mm_cfg.AuthSiteAdmin:
                if Utils.check_global_password(response):
                    return mm_cfg.AuthSiteAdmin
            elif ac == mm_cfg.AuthListAdmin:
                def cryptmatchp(response: str, secret: str) -> bool:
                    try:
                        salt = secret[:2]
                        if crypt and crypt.crypt(response, salt) == secret:
                            return True
                        return False
                    except TypeError:
                        return False
                        
                key, secret = self.AuthContextInfo(ac)
                if secret is None:
                    continue
                    
                sharesponse = sha_new(response).hexdigest()
                upgrade = ok = False
                
                if sharesponse == secret:
                    ok = True
                elif md5_new(response).digest() == secret:
                    ok = upgrade = True
                elif cryptmatchp(response, secret):
                    ok = upgrade = True
                    
                if upgrade:
                    save_and_unlock = False
                    if not self.Locked():
                        self.Lock()
                        save_and_unlock = True
                    try:
                        self.password = sharesponse
                        if save_and_unlock:
                            self.Save()
                    finally:
                        if save_and_unlock:
                            self.Unlock()
                            
                if ok:
                    return ac
                    
            elif ac == mm_cfg.AuthListModerator:
                key, secret = self.AuthContextInfo(ac)
                if secret is None:
                    continue
                if sha_new(response).hexdigest() == secret:
                    return ac
                    
            elif ac == mm_cfg.AuthListPoster:
                key, secret = self.AuthContextInfo(ac)
                if secret is None:
                    continue
                if response == secret:
                    return ac
                    
            elif ac == mm_cfg.AuthUser:
                if user is None:
                    continue
                try:
                    key, secret = self.AuthContextInfo(ac, user)
                except Errors.NotAMemberError:
                    continue
                if secret is None:
                    continue
                if response == secret:
                    return ac
                    
        return mm_cfg.UnAuthorized

    def WebAuthenticate(self, authcontexts: List[int], response: str, user: Optional[str] = None) -> bool:
        """Authenticate a user via web interface.
        
        Args:
            authcontexts: List of authentication contexts to check
            response: Password or authentication response
            user: Email address (required for AuthUser context)
            
        Returns:
            True if authentication succeeded, False otherwise
        """
        for ac in authcontexts:
            ok = self.CheckCookie(ac, user)
            if ok:
                return True
        # Check passwords
        ac = self.Authenticate(authcontexts, response, user)
        if ac:
            print(self.MakeCookie(ac, user))
            return True
        return False

    def MakeCookie(self, authcontext: int, user: Optional[str] = None) -> Cookie.SimpleCookie:
        """Create an authentication cookie.
        
        Args:
            authcontext: Authentication context
            user: Email address (required for AuthUser context)
            
        Returns:
            A SimpleCookie object containing the authentication data
            
        Raises:
            ValueError: If authentication context is invalid
        """
        key, secret = self.AuthContextInfo(authcontext, user)
        if key is None or secret is None or not isinstance(secret, str):
            raise ValueError
        # Timestamp
        issued = int(time.time())
        # Get a digest of the secret, plus other information.
        mac = sha_new(secret + repr(issued)).hexdigest()
        # Create the cookie object.
        c = Cookie.SimpleCookie()
        c[key] = binascii.hexlify(marshal.dumps((issued, mac)))
        # The path to all Mailman stuff, minus the scheme and host,
        # i.e. usually the string `/mailman'
        parsed = urlparse(self.web_page_url)
        path = parsed[2]
        c[key]['path'] = path
        # Make sure to set the 'secure' flag on the cookie if mailman is
        # accessed by an https url.
        if parsed[0] == 'https':
            c[key]['secure'] = True
        # We use session cookies, so don't set `expires' or `max-age' keys.
        # Set the RFC 2109 required header.
        c[key]['version'] = 1
        return c

    def ZapCookie(self, authcontext: int, user: Optional[str] = None) -> Cookie.SimpleCookie:
        """Invalidate an authentication cookie.
        
        Args:
            authcontext: Authentication context
            user: Email address (required for AuthUser context)
            
        Returns:
            A SimpleCookie object that invalidates the authentication
        """
        key, secret = self.AuthContextInfo(authcontext, user)
        # Logout of the session by zapping the cookie.  For safety both set
        # max-age=0 (as per RFC2109) and set the cookie data to the empty
        # string.
        c = Cookie.SimpleCookie()
        c[key] = ''
        # The path to all Mailman stuff, minus the scheme and host,
        # i.e. usually the string `/mailman'
        path = urlparse(self.web_page_url)[2]
        c[key]['path'] = path
        c[key]['max-age'] = 0
        # Don't set expires=0 here otherwise it'll force a persistent cookie
        c[key]['version'] = 1
        return c

    def CheckCookie(self, authcontext: int, user: Optional[str] = None) -> bool:
        """Check if a cookie is valid for the given authentication context.
        
        Args:
            authcontext: Authentication context
            user: Email address (required for AuthUser context)
            
        Returns:
            True if the cookie is valid, False otherwise
        """
        cookiedata = os.environ.get('HTTP_COOKIE')
        if not cookiedata:
            return False
        # We can't use the Cookie module here because it isn't liberal in what
        # it accepts.  Feed it a MM2.0 cookie along with a MM2.1 cookie and
        # you get a CookieError. :(.  All we care about is accessing the
        # cookie data via getitem, so we'll use our own parser, which returns
        # a dictionary.
        c = parsecookie(cookiedata)
        # If the user was not supplied, but the authcontext is AuthUser, we
        # can try to glean the user address from the cookie key.  There may be
        # more than one matching key (if the user has multiple accounts
        # subscribed to this list), but any are okay.
        if authcontext == mm_cfg.AuthUser:
            if user:
                usernames = [user]
            else:
                usernames = []
                prefix = self.internal_name() + '+user+'
                for k in c.keys():
                    if k.startswith(prefix):
                        usernames.append(k[len(prefix):])
            # If any check out, we're golden.  Note: `@'s are no longer legal
            # values in cookie keys.
            for user in [Utils.UnobscureEmail(urllib.parse.unquote(u))
                         for u in usernames]:
                ok = self.__checkone(c, authcontext, user)
                if ok:
                    return True
            return False
        else:
            return self.__checkone(c, authcontext, user)

    def __checkone(self, c: Dict[str, str], authcontext: int, user: Optional[str] = None) -> bool:
        """Check a single cookie for validity.
        
        Args:
            c: Cookie dictionary
            authcontext: Authentication context
            user: Email address (required for AuthUser context)
            
        Returns:
            True if the cookie is valid, False otherwise
        """
        try:
            key, secret = self.AuthContextInfo(authcontext, user)
        except (Errors.NotAMemberError, TypeError):
            return False
        if key not in c or not isinstance(secret, str):
            return False
        # Undo the encoding we performed in MakeCookie() above.  BAW: I
        # believe this is safe from exploit because marshal can't be forced to
        # load recursive data structures, and it can't be forced to execute
        # any unexpected code.  The worst that can happen is that either the
        # client will have provided us bogus data, in which case we'll get one
        # of the caught exceptions, or marshal format will have changed, in
        # which case, the cookie decoding will fail.  In either case, we'll
        # simply request reauthorization, resulting in a new cookie being
        # returned to the client.
        try:
            data = marshal.loads(binascii.unhexlify(c[key]))
            issued, received_mac = data
        except (EOFError, ValueError, TypeError, KeyError):
            return False
        # Make sure the issued timestamp makes sense
        now = time.time()
        if now < issued:
            return False
        if (mm_cfg.AUTHENTICATION_COOKIE_LIFETIME and
                issued + mm_cfg.AUTHENTICATION_COOKIE_LIFETIME < now):
            return False
        # Calculate a MAC using the issued timestamp and the shared secret
        mac = sha_new(secret + repr(issued)).hexdigest()
        if mac != received_mac:
            return False
        
        # Authenticated!
        # Refresh the cookie
        print(self.MakeCookie(authcontext, user))
        return True

    def CheckProgrammers(self) -> bool:
        """Check if this list should be advertised as a programmers list.
        
        Returns:
            True if this is a programmers list, False otherwise
        """
        return self.programmer_members

    def IsProgrammersMember(self, addr: str) -> bool:
        """Check if an address is a member of the programmers list.
        
        Args:
            addr: Email address to check
            
        Returns:
            True if the address is a programmers list member, False otherwise
        """
        return addr in self.programmer_members

    def GetConfigInfo(self) -> Dict[str, Any]:
        """Get configuration information for this list.
        
        Returns:
            Dictionary containing configuration information
        """
        return self._config_info

    def __repr__(self) -> str:
        """Return string representation of list object.
        
        Returns:
            String representation of the list
        """
        return repr(self.internal_name())

    def __str__(self) -> str:
        """Return string representation of list object.
        
        Returns:
            String representation of the list
        """
        return str(self.internal_name())

    def __eq__(self, other: Any) -> bool:
        """Compare two SecurityManager instances.
        
        Args:
            other: Object to compare with
            
        Returns:
            True if the objects represent the same list, False otherwise
        """
        if not isinstance(other, SecurityManager):
            return NotImplemented
        return self.internal_name().lower() == other.internal_name().lower()

    def __lt__(self, other: Any) -> bool:
        """Compare two SecurityManager instances for ordering.
        
        Args:
            other: Object to compare with
            
        Returns:
            True if this list name is less than the other, False otherwise
        """
        if not isinstance(other, SecurityManager):
            return NotImplemented
        return self.internal_name().lower() < other.internal_name().lower()

    def __gt__(self, other: Any) -> bool:
        """Compare two SecurityManager instances for ordering.
        
        Args:
            other: Object to compare with
            
        Returns:
            True if this list name is greater than the other, False otherwise
        """
        if not isinstance(other, SecurityManager):
            return NotImplemented
        return self.internal_name().lower() > other.internal_name().lower()

    def __le__(self, other: Any) -> bool:
        """Compare two SecurityManager instances for ordering.
        
        Args:
            other: Object to compare with
            
        Returns:
            True if this list name is less than or equal to the other, False otherwise
        """
        if not isinstance(other, SecurityManager):
            return NotImplemented
        return self.internal_name().lower() <= other.internal_name().lower()

    def __ge__(self, other: Any) -> bool:
        """Compare two SecurityManager instances for ordering.
        
        Args:
            other: Object to compare with
            
        Returns:
            True if this list name is greater than or equal to the other, False otherwise
        """
        if not isinstance(other, SecurityManager):
            return NotImplemented
        return self.internal_name().lower() >= other.internal_name().lower()

    def __bool__(self) -> bool:
        """Return True for boolean context.
        
        Returns:
            True
        """
        return True

    def __hash__(self) -> int:
        """Return hash value for this list.
        
        Returns:
            Hash value based on the list name
        """
        return hash(self.internal_name().lower())


splitter = re.compile(r';\s*')

def parsecookie(s: str) -> Dict[str, str]:
    """Parse a cookie string into a dictionary.
    
    Args:
        s: Cookie string to parse
        
    Returns:
        Dictionary containing cookie key-value pairs
    """
    c = {}
    for line in s.splitlines():
        for p in splitter.split(line):
            try:
                k, v = p.split('=', 1)
            except ValueError:
                pass
            else:
                c[k] = v
    return c
