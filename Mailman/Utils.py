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


"""Miscellaneous essential routines.

This includes actual message transmission routines, address checking and
message and address munging, a handy-dandy routine to map a function on all
the mailing lists, and whatever else doesn't belong elsewhere.

"""

import os
import sys
import re
import time
import errno
import base64
import random
import urllib.request, urllib.parse, urllib.error
import html.entities
import html
import email.header
import email.iterators
from email.errors import HeaderParseError
from string import whitespace, digits
from urllib.parse import urlparse
try:
    # Python 2.2
    from string import ascii_letters
except ImportError:
    # Older Pythons
    _lower = 'abcdefghijklmnopqrstuvwxyz'
    ascii_letters = _lower + _lower.upper()

from Mailman import mm_cfg
from Mailman import Errors
from Mailman import Site
from Mailman.SafeDict import SafeDict
from Mailman.Logging.Syslog import mailman_log

try:
    import hashlib
    md5_new = hashlib.md5
    sha_new = hashlib.sha1
except ImportError:
    import md5
    import sha
    md5_new = md5.new
    sha_new = sha.new

try:
    import dns.resolver
    from dns.exception import DNSException
    dns_resolver = True
except ImportError:
    dns_resolver = False

try:
    import ipaddress
    have_ipaddress = True
except ImportError:
    have_ipaddress = False

EMPTYSTRING = ''
UEMPTYSTRING = u''
CR = '\r'
NL = '\n'
DOT = '.'
IDENTCHARS = ascii_letters + digits + '_'

# Search for $(identifier)s strings, except that the trailing s is optional,
# since that's a common mistake
cre = re.compile(r'%\(([_a-z]\w*?)\)s?', re.IGNORECASE)
# Search for $$, $identifier, or ${identifier}
dre = re.compile(r'(\${2})|\$([_a-z]\w*)|\${([_a-z]\w*)}', re.IGNORECASE)


def list_exists(listname):
    """Return true iff list `listname' exists."""
    # The existance of any of the following file proves the list exists
    # <wink>: config.pck, config.pck.last, config.db, config.db.last
    #
    # The former two are for 2.1alpha3 and beyond, while the latter two are
    # for all earlier versions.
    #
    # But first ensure the list name doesn't contain a path traversal
    # attack.
    if len(re.sub(mm_cfg.ACCEPTABLE_LISTNAME_CHARACTERS, '', listname, flags=re.IGNORECASE)) > 0:
        remote = os.environ.get('HTTP_FORWARDED_FOR',
                 os.environ.get('HTTP_X_FORWARDED_FOR',
                 os.environ.get('REMOTE_ADDR',
                                'unidentified origin')))
        mailman_log('mischief',
               'Hostile listname: listname=%s: remote=%s', listname, remote)
        return False
    basepath = Site.get_listpath(listname)
    for ext in ('.pck', '.pck.last', '.db', '.db.last'):
        dbfile = os.path.join(basepath, 'config' + ext)
        if os.path.exists(dbfile):
            return True
    return False


def list_names():
    """Return the names of all lists in default list directory."""
    # We don't currently support separate listings of virtual domains
    # Ensure LIST_DATA_DIR is a string
    list_dir = mm_cfg.LIST_DATA_DIR
    if isinstance(list_dir, bytes):
        list_dir = list_dir.decode('utf-8', 'replace')
    names = []
    for name in os.listdir(list_dir):
        if list_exists(name):
            # Ensure we return strings, not bytes
            if isinstance(name, bytes):
                name = name.decode('utf-8', 'replace')
            names.append(name)
    return names


def wrap(text, column=70, honor_leading_ws=True):
    """Wrap and fill the text to the specified column.

    Wrapping is always in effect, although if it is not possible to wrap a
    line (because some word is longer than `column' characters) the line is
    broken at the next available whitespace boundary.  Paragraphs are also
    always filled, unless honor_leading_ws is true and the line begins with
    whitespace.  This is the algorithm that the Python FAQ wizard uses, and
    seems like a good compromise.

    """
    wrapped = ''
    # first split the text into paragraphs, defined as a blank line
    paras = re.split(r'\n\n', text)
    for para in paras:
        # fill
        lines = []
        fillprev = False
        for line in para.split(NL):
            if not line:
                lines.append(line)
                continue
            if honor_leading_ws and line[0] in whitespace:
                fillthis = False
            else:
                fillthis = True
            if fillprev and fillthis:
                # if the previous line should be filled, then just append a
                # single space, and the rest of the current line
                lines[-1] = lines[-1].rstrip() + ' ' + line
            else:
                # no fill, i.e. retain newline
                lines.append(line)
            fillprev = fillthis
        # wrap each line
        for text in lines:
            while text:
                if len(text) <= column:
                    line = text
                    text = ''
                else:
                    bol = column
                    # find the last whitespace character
                    while bol > 0 and text[bol] not in whitespace:
                        bol -= 1
                    # now find the last non-whitespace character
                    eol = bol
                    while eol > 0 and text[eol] in whitespace:
                        eol -= 1
                    # watch out for text that's longer than the column width
                    if eol == 0:
                        # break on whitespace after column
                        eol = column
                        while eol < len(text) and text[eol] not in whitespace:
                            eol += 1
                        bol = eol
                        while bol < len(text) and text[bol] in whitespace:
                            bol += 1
                        bol -= 1
                    line = text[:eol+1] + '\n'
                    # find the next non-whitespace character
                    bol += 1
                    while bol < len(text) and text[bol] in whitespace:
                        bol += 1
                    text = text[bol:]
                wrapped += line
            wrapped += '\n'
            # end while text
        wrapped += '\n'
        # end for text in lines
    # the last two newlines are bogus
    return wrapped[:-2]


def QuotePeriods(text):
    JOINER = '\n .\n'
    SEP = '\n.\n'
    return JOINER.join(text.split(SEP))


# This takes an email address, and returns a tuple containing (user,host)
def ParseEmail(email):
    user = None
    domain = None
    email = email.lower()
    at_sign = email.find('@')
    if at_sign < 1:
        return email, None
    user = email[:at_sign]
    rest = email[at_sign+1:]
    domain = rest.split('.')
    return user, domain


def LCDomain(addr):
    "returns the address with the domain part lowercased"
    atind = addr.find('@')
    if atind == -1: # no domain part
        return addr
    return addr[:atind] + '@' + addr[atind+1:].lower()


# TBD: what other characters should be disallowed?
_badchars = re.compile(r'[][()<>|:;^,\\"\000-\037\177-\377]')
# Strictly speaking, some of the above are allowed in quoted local parts, but
# this can open the door to certain web exploits so we don't allow them.
# Only characters allowed in domain parts.
_valid_domain = re.compile('[-a-z0-9]', re.IGNORECASE)

def ValidateEmail(s):
    """Validate an email address.

    This is used to validate email addresses entered by users.  It is more
    strict than RFC 822, but less strict than RFC 2822.  In particular, it
    does not allow local, unqualified addresses, and requires at least one
    domain part.  It also disallows various characters that are known to
    cause problems in various contexts.

    Returns None if the address is valid, raises an exception otherwise.
    """
    if not s:
        raise Exception(Errors.MMBadEmailError, s)
    if _badchars.search(s):
        raise Exception(Errors.MMHostileAddress, s)
    user, domain_parts = ParseEmail(s)
    # This means local, unqualified addresses, are not allowed
    if not domain_parts:
        raise Exception(Errors.MMBadEmailError, s)
    # Allow single-part domains for internal use
    if len(domain_parts) < 1:
        raise Exception(Errors.MMBadEmailError, s)
    # domain parts may only contain ascii letters, digits and hyphen
    # and must not begin with hyphen.
    for p in domain_parts:
        if len(p) == 0 or p[0] == '-' or len(_valid_domain.sub('', p)) > 0:
            raise Exception(Errors.MMHostileAddress, s)


# Patterns which may be used to form malicious path to inject a new
# line in the mailman error log. (TK: advisory by Moritz Naumann)
CRNLpat = re.compile(r'[^\x21-\x7e]')

def GetPathPieces(envar='PATH_INFO'):
    path = os.environ.get(envar)
    if path:
        remote = os.environ.get('HTTP_FORWARDED_FOR',
                 os.environ.get('HTTP_X_FORWARDED_FOR',
                 os.environ.get('REMOTE_ADDR',
                                'unidentified origin')))
        if CRNLpat.search(path):
            path = CRNLpat.split(path)[0]
            mailman_log('error',
                'Warning: Possible malformed path attack domain=%s remote=%s',
                   get_domain(),
                   remote)
        # Check for listname injections that won't be websafed.
        pieces = [p for p in path.split('/') if p]
        # Get the longest listname or 20 if none or use MAX_LISTNAME_LENGTH if
        # provided > 0.
        if mm_cfg.MAX_LISTNAME_LENGTH > 0:
            longest = mm_cfg.MAX_LISTNAME_LENGTH
        else:
            lst_names = list_names()
            if lst_names:
                longest = max([len(x) for x in lst_names])
            else:
                longest = 20
        if pieces and len(pieces[0]) > longest:
            mailman_log('mischief',
               'Hostile listname: listname=%s: remote=%s', pieces[0], remote)
            pieces[0] = pieces[0][:longest] + '...'
        return pieces
    return None


def GetRequestMethod():
    return os.environ.get('REQUEST_METHOD')


def ScriptURL(target, web_page_url=None, absolute=False):
    """target - scriptname only, nothing extra
    web_page_url - the list's configvar of the same name
    absolute - a flag which if set, generates an absolute url
    """
    if web_page_url is None:
        web_page_url = mm_cfg.DEFAULT_URL_PATTERN % get_domain()
        if web_page_url[-1] != '/':
            web_page_url = web_page_url + '/'
    fullpath = os.environ.get('REQUEST_URI')
    if fullpath is None:
        fullpath = os.environ.get('SCRIPT_NAME', '') + \
                   os.environ.get('PATH_INFO', '')
    baseurl = urlparse(web_page_url)[2]
    if not absolute and fullpath.startswith(baseurl):
        # Use relative addressing
        fullpath = fullpath[len(baseurl):]
        i = fullpath.find('?')
        if i > 0:
            count = fullpath.count('/', 0, i)
        else:
            count = fullpath.count('/')
        path = ('../' * count) + target
    else:
        path = web_page_url + target
    return path + mm_cfg.CGIEXT


def GetPossibleMatchingAddrs(name):
    """returns a sorted list of addresses that could possibly match
    a given name.

    For Example, given scott@pobox.com, return ['scott@pobox.com'],
    given scott@blackbox.pobox.com return ['scott@blackbox.pobox.com',
                                           'scott@pobox.com']"""

    name = name.lower()
    user, domain = ParseEmail(name)
    res = [name]
    if domain:
        domain = domain[1:]
        while len(domain) >= 2:
            res.append("%s@%s" % (user, DOT.join(domain)))
            domain = domain[1:]
    return res


def List2Dict(L, foldcase=False):
    """Return a dict keyed by the entries in the list passed to it."""
    d = {}
    if foldcase:
        for i in L:
            d[i.lower()] = True
    else:
        for i in L:
            d[i] = True
    return d


_vowels = ('a', 'e', 'i', 'o', 'u')
_consonants = ('b', 'c', 'd', 'f', 'g', 'h', 'k', 'm', 'n',
               'p', 'r', 's', 't', 'v', 'w', 'x', 'z')
_syllables = []

for v in _vowels:
    for c in _consonants:
        _syllables.append(c+v)
        _syllables.append(v+c)
del c, v

def UserFriendly_MakeRandomPassword(length):
    syls = []
    while len(syls) * 2 < length:
        syls.append(random.choice(_syllables))
    return EMPTYSTRING.join(syls)[:length]


def Secure_MakeRandomPassword(length):
    bytesread = 0
    bytes = []
    fd = None
    try:
        while bytesread < length:
            try:
                # Python 2.4 has this on available systems.
                newbytes = os.urandom(length - bytesread)
            except (AttributeError, NotImplementedError):
                if fd is None:
                    try:
                        fd = os.open('/dev/urandom', os.O_RDONLY)
                    except OSError as e:
                        if e.errno != errno.ENOENT:
                            raise
                        # We have no available source of cryptographically
                        # secure random characters.  Log an error and fallback
                        # to the user friendly passwords.
                        mailman_log('error',
                               'urandom not available, passwords not secure')
                        return UserFriendly_MakeRandomPassword(length)
                newbytes = os.read(fd, length - bytesread)
            bytes.append(newbytes)
            bytesread += len(newbytes)
        s = base64.encodestring(EMPTYSTRING.join(bytes))
        # base64 will expand the string by 4/3rds
        return s.replace('\n', '')[:length]
    finally:
        if fd is not None:
            os.close(fd)


def MakeRandomPassword(length=mm_cfg.MEMBER_PASSWORD_LENGTH):
    if mm_cfg.USER_FRIENDLY_PASSWORDS:
        return UserFriendly_MakeRandomPassword(length)
    return Secure_MakeRandomPassword(length)


def GetRandomSeed():
    chr1 = int(random.random() * 52)
    chr2 = int(random.random() * 52)
    def mkletter(c):
        if 0 <= c < 26:
            c += 65
        if 26 <= c < 52:
            #c = c - 26 + 97
            c += 71
        return c
    return "%c%c" % tuple(map(mkletter, (chr1, chr2)))


def set_global_password(pw, siteadmin=True):
    if siteadmin:
        filename = mm_cfg.SITE_PW_FILE
    else:
        filename = mm_cfg.LISTCREATOR_PW_FILE
    # rw-r-----
    omask = os.umask(0o026)
    try:
        # Use atomic write to prevent race conditions
        temp_filename = filename + '.tmp'
        with open(temp_filename, 'w') as fp:
            fp.write(sha_new(pw).hexdigest() + '\n')
        os.rename(temp_filename, filename)
    except (IOError, OSError) as e:
        mailman_log('error', 'Failed to write password file %s: %s', filename, str(e))
        raise
    finally:
        os.umask(omask)


def get_global_password(siteadmin=True):
    if siteadmin:
        filename = mm_cfg.SITE_PW_FILE
    else:
        filename = mm_cfg.LISTCREATOR_PW_FILE
    try:
        with open(filename) as fp:
            challenge = fp.read()[:-1]  # strip off trailing nl
            if not challenge:
                mailman_log('error', 'Empty password file: %s', filename)
                return None
            return challenge
    except IOError as e:
        if e.errno != errno.ENOENT:
            mailman_log('error', 'Error reading password file %s: %s', filename, str(e))
        return None


def check_global_password(response, siteadmin=True):
    challenge = get_global_password(siteadmin)
    if challenge is None:
        return None
    # Log the hashes for debugging
    computed_hash = sha_new(response).hexdigest()
    mailman_log('debug', 'Password check - stored hash: %s, computed hash: %s', 
                challenge, computed_hash)
    return challenge == computed_hash


_ampre = re.compile('&amp;((?:#[0-9]+|[a-z]+);)', re.IGNORECASE)
def websafe(s, doubleescape=False):
    """Convert a string to be safe for HTML output.
    
    This function handles:
    - Lists/tuples (takes last element)
    - Browser workarounds
    - Double escaping
    - Bytes decoding (including Python 2 style bytes)
    - HTML escaping
    """
    if isinstance(s, (list, tuple)):
        s = s[-1] if s else ''
    
    if mm_cfg.BROKEN_BROWSER_WORKAROUND and isinstance(s, str):
        for k in mm_cfg.BROKEN_BROWSER_REPLACEMENTS:
            s = s.replace(k, mm_cfg.BROKEN_BROWSER_REPLACEMENTS[k])
    
    if isinstance(s, bytes):
        # First try to detect if this is a Python 2 style bytes file
        # by checking for common Python 2 encodings
        try:
            # Try ASCII first as it's the most common Python 2 default
            s = s.decode('ascii', errors='strict')
        except UnicodeDecodeError:
            try:
                # Try UTF-8 next as it's common in Python 2 files
                s = s.decode('utf-8', errors='strict')
            except UnicodeDecodeError:
                try:
                    # Try ISO-8859-1 (latin1) which was common in Python 2
                    s = s.decode('iso-8859-1', errors='strict')
                except UnicodeDecodeError:
                    # As a last resort, try to detect the encoding
                    try:
                        import chardet
                        result = chardet.detect(s)
                        if result['confidence'] > 0.8:
                            s = s.decode(result['encoding'], errors='strict')
                        else:
                            # If we can't detect with confidence, fall back to latin1
                            s = s.decode('latin1', errors='replace')
                    except (ImportError, UnicodeDecodeError):
                        # If all else fails, use replace to avoid errors
                        s = s.decode('latin1', errors='replace')
    
    # First escape & to &amp; to prevent double escaping issues
    s = s.replace('&', '&amp;')
    
    # Then use html.escape for the rest
    s = html.escape(s, quote=True)
    
    # If double escaping is requested, escape again
    if doubleescape:
        s = html.escape(s, quote=True)
    
    return s


def nntpsplit(s):
    parts = s.split(':', 1)
    if len(parts) == 2:
        try:
            return parts[0], int(parts[1])
        except ValueError:
            pass
    # Use the defaults
    return s, 119


# Just changing these two functions should be enough to control the way
# that email address obscuring is handled.
def ObscureEmail(addr, for_text=False):
    """Make email address unrecognizable to web spiders, but invertable.

    When for_text option is set (not default), make a sentence fragment
    instead of a token."""
    if for_text:
        return addr.replace('@', ' at ')
    else:
        return addr.replace('@', '--at--')

def UnobscureEmail(addr):
    """Invert ObscureEmail() conversion."""
    # Contrived to act as an identity operation on already-unobscured
    # emails, so routines expecting obscured ones will accept both.
    return addr.replace('--at--', '@')


class OuterExit(Exception):
    pass

def findtext(templatefile, dict=None, raw=0, lang=None, mlist=None):
    """Find the template file and return its contents and path.

    The template file is searched for in the following order:
    1. In the list's language-specific template directory
    2. In the site's language-specific template directory
    3. In the list's default template directory
    4. In the site's default template directory

    If the template is found, returns a 2-tuple of (text, path) where text is
    the contents of the file and path is the absolute path to the file.
    Otherwise returns (None, None).
    """
    if dict is None:
        dict = {}
    # If lang is None, use the default language from mm_cfg
    if lang is None:
        lang = mm_cfg.DEFAULT_SERVER_LANGUAGE

    def read_template_file(path):
        try:
            with open(path, 'rb') as fp:
                raw_bytes = fp.read()
                # First check if the file contains HTML entities
                if b'&nbsp;' in raw_bytes or b'&amp;' in raw_bytes or b'&lt;' in raw_bytes or b'&gt;' in raw_bytes:
                    # If it contains HTML entities, try UTF-8 first
                    try:
                        return raw_bytes.decode('utf-8'), path
                    except UnicodeDecodeError:
                        # If UTF-8 fails, try ISO-8859-1 which is safe for HTML entities
                        return raw_bytes.decode('iso-8859-1'), path
                else:
                    # If no HTML entities, try all encodings in sequence
                    try:
                        return raw_bytes.decode('utf-8'), path
                    except UnicodeDecodeError:
                        try:
                            return raw_bytes.decode('euc-jp'), path
                        except UnicodeDecodeError:
                            try:
                                return raw_bytes.decode('iso-8859-1'), path
                            except UnicodeDecodeError:
                                return raw_bytes.decode('latin1'), path
        except IOError:
            return None, None

    # First try the list's language-specific template directory
    if lang and mlist:
        path = os.path.join(mlist.fullpath(), 'templates', lang, templatefile)
        if os.path.exists(path):
            result = read_template_file(path)
            if result[0] is not None:
                return result

    # Then try the site's language-specific template directory
    if lang:
        path = os.path.join(mm_cfg.TEMPLATE_DIR, lang, templatefile)
        if os.path.exists(path):
            result = read_template_file(path)
            if result[0] is not None:
                return result

    # Then try the list's default template directory
    if mlist:
        path = os.path.join(mlist.fullpath(), 'templates', templatefile)
        if os.path.exists(path):
            result = read_template_file(path)
            if result[0] is not None:
                return result

    # Finally try the site's default template directory
    path = os.path.join(mm_cfg.TEMPLATE_DIR, templatefile)
    if os.path.exists(path):
        result = read_template_file(path)
        if result[0] is not None:
            return result

    return None, None


def maketext(templatefile, dict=None, raw=0, lang=None, mlist=None):
    """Make text from a template file.

    Use this function to create text from the template file.  If dict is
    provided, use it as the substitution mapping.  If mlist is provided use it
    as the source for the substitution.  If both dict and mlist are provided,
    dict values take precedence.  lang is the language code to find the
    template in.  If raw is true, no substitution will be done on the text.
    """
    template, path = findtext(templatefile, dict, raw, lang, mlist)
    if template is None:
        # Log all paths that were searched
        paths = []
        if lang and mlist:
            paths.append(os.path.join(mlist.fullpath(), 'templates', lang, templatefile))
        if lang:
            paths.append(os.path.join(mm_cfg.TEMPLATE_DIR, lang, templatefile))
        if mlist:
            paths.append(os.path.join(mlist.fullpath(), 'templates', templatefile))
        paths.append(os.path.join(mm_cfg.TEMPLATE_DIR, templatefile))
        mailman_log('error', 'Template file not found: %s (language: %s). Searched paths: %s',
               templatefile, lang or 'default', ', '.join(paths))
        return ''  # Return empty string instead of None
    if raw:
        return template
    # Make the text from the template
    if dict is None:
        dict = SafeDict()
    if mlist:
        dict.update(mlist.__dict__)
    # Remove leading whitespace
    if isinstance(template, str):
        template = '\n'.join([line.lstrip() for line in template.splitlines()])
    try:
        text = template % dict
    except (ValueError, TypeError) as e:
        mailman_log('error', 'Template interpolation error for %s: %s',
               templatefile, str(e))
        return ''  # Return empty string instead of None
    return text


ADMINDATA = {
    # admin keyword: (minimum #args, maximum #args)
    'confirm':     (1, 1),
    'help':        (0, 0),
    'info':        (0, 0),
    'lists':       (0, 0),
    'options':     (0, 0),
    'password':    (2, 2),
    'remove':      (0, 0),
    'set':         (3, 3),
    'subscribe':   (0, 3),
    'unsubscribe': (0, 1),
    'who':         (0, 1),
    }

# Given a Message object, test for administrivia (eg subscribe,
# unsubscribe, etc).  The test must be a good guess -- messages that return
# true get sent to the list admin instead of the entire list.
def is_administrivia(msg):
    """Return true if the message is administrative in nature."""
    # Not imported at module scope to avoid import loop
    from Mailman.Message import Message
    if not isinstance(msg, Message):
        return False
    linecnt = 0
    lines = []
    for line in email.iterators.body_line_iterator(msg):
        # Strip out any signatures
        if line == '-- ':
            break
        if line.strip():
            linecnt += 1
        if linecnt > mm_cfg.DEFAULT_MAIL_COMMANDS_MAX_LINES:
            return False
        lines.append(line)
    bodytext = NL.join(lines)
    # See if the body text has only one word, and that word is administrivia
    if bodytext.strip().lower() in ADMINDATA:
        return True
    # Look at the first N lines and see if there is any administrivia on the
    # line.  BAW: N is currently hardcoded to 5.  str-ify the Subject: header
    # because it may be an email.Header.Header instance rather than a string.
    bodylines = lines[:5]
    subject = str(msg.get('subject', ''))
    bodylines.append(subject)
    for line in bodylines:
        if not line.strip():
            continue
        words = [word.lower() for word in line.split()]
        minargs, maxargs = ADMINDATA.get(words[0], (None, None))
        if minargs is None and maxargs is None:
            continue
        if minargs <= len(words[1:]) <= maxargs:
            # Special case the `set' keyword.  BAW: I don't know why this is
            # here.
            if words[0] == 'set' and words[2] not in ('on', 'off'):
                continue
            return True
    return False


def GetRequestURI(fallback=None, escape=True):
    """Return the full virtual path this CGI script was invoked with.

    Newer web servers seems to supply this info in the REQUEST_URI
    environment variable -- which isn't part of the CGI/1.1 spec.
    Thus, if REQUEST_URI isn't available, we concatenate SCRIPT_NAME
    and PATH_INFO, both of which are part of CGI/1.1.

    Optional argument `fallback' (default `None') is returned if both of
    the above methods fail.

    The url will be cgi escaped to prevent cross-site scripting attacks,
    unless `escape' is set to 0.
    """
    url = fallback
    if 'REQUEST_URI' in os.environ:
        url = os.environ['REQUEST_URI']
    elif 'SCRIPT_NAME' in os.environ and 'PATH_INFO' in os.environ:
        url = os.environ['SCRIPT_NAME'] + os.environ['PATH_INFO']
    if escape:
        return websafe(url)
    return url


# Wait on a dictionary of child pids
def reap(kids, func=None, once=False):
    while kids:
        if func:
            func()
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except OSError as e:
            # If the child procs had a bug we might have no children
            if e.errno != errno.ECHILD:
                raise
            kids.clear()
            break
        if pid != 0:
            try:
                del kids[pid]
            except KeyError:
                # Huh?  How can this happen?
                pass
        if once:
            break


def GetLanguageDescr(lang):
    return mm_cfg.LC_DESCRIPTIONS[lang][0]


def GetCharSet(lang):
    return mm_cfg.LC_DESCRIPTIONS[lang][1]

def GetDirection(lang):
    return mm_cfg.LC_DESCRIPTIONS[lang][2]

def IsLanguage(lang):
    return lang in mm_cfg.LC_DESCRIPTIONS


def get_domain():
    host = os.environ.get('HTTP_HOST', os.environ.get('SERVER_NAME'))
    port = os.environ.get('SERVER_PORT')
    # Strip off the port if there is one
    if port and host.endswith(':' + port):
        host = host[:-len(port)-1]
    if mm_cfg.VIRTUAL_HOST_OVERVIEW and host:
        return websafe(host.lower())
    else:
        # See the note in Defaults.py concerning DEFAULT_URL
        # vs. DEFAULT_URL_HOST.
        hostname = ((mm_cfg.DEFAULT_URL
                     and urllib.parse(mm_cfg.DEFAULT_URL)[1])
                     or mm_cfg.DEFAULT_URL_HOST)
        return hostname.lower()


def get_site_email(hostname=None, extra=None):
    if hostname is None:
        hostname = mm_cfg.VIRTUAL_HOSTS.get(get_domain(), get_domain())
    if extra is None:
        return '%s@%s' % (mm_cfg.MAILMAN_SITE_LIST, hostname)
    return '%s-%s@%s' % (mm_cfg.MAILMAN_SITE_LIST, extra, hostname)


# This algorithm crafts a guaranteed unique message-id.  The theory here is
# that pid+listname+host will distinguish the message-id for every process on
# the system, except when process ids wrap around.  To further distinguish
# message-ids, we prepend the integral time in seconds since the epoch.  It's
# still possible that we'll vend out more than one such message-id per second,
# so we prepend a monotonically incrementing serial number.  It's highly
# unlikely that within a single second, there'll be a pid wraparound.
_serial = 0
def unique_message_id(mlist):
    global _serial
    msgid = '<mailman.%d.%d.%d.%s@%s>' % (
        _serial, time.time(), os.getpid(),
        mlist.internal_name(), mlist.host_name)
    _serial += 1
    return msgid


# Figure out epoch seconds of midnight at the start of today (or the given
# 3-tuple date of (year, month, day).
def midnight(date=None):
    if date is None:
        date = time.localtime()[:3]
    # -1 for dst flag tells the library to figure it out
    return time.mktime(date + (0,)*5 + (-1,))


# Utilities to convert from simplified $identifier substitutions to/from
# standard Python $(identifier)s substititions.  The "Guido rules" for the
# former are:
#    $$ -> $
#    $identifier -> $(identifier)s
#    ${identifier} -> $(identifier)s

def to_dollar(s):
    """Convert from %-strings to $-strings."""
    s = s.replace('$', '$$').replace('%%', '%')
    parts = cre.split(s)
    for i in range(1, len(parts), 2):
        if parts[i+1] and parts[i+1][0] in IDENTCHARS:
            parts[i] = '${' + parts[i] + '}'
        else:
            parts[i] = '$' + parts[i]
    return EMPTYSTRING.join(parts)


def to_percent(s):
    """Convert from $-strings to %-strings."""
    s = s.replace('%', '%%').replace('$$', '$')
    parts = dre.split(s)
    for i in range(1, len(parts), 4):
        if parts[i] is not None:
            parts[i] = '$'
        elif parts[i+1] is not None:
            parts[i+1] = '%(' + parts[i+1] + ')s'
        else:
            parts[i+2] = '%(' + parts[i+2] + ')s'
    return EMPTYSTRING.join([_f for _f in parts if _f])


def dollar_identifiers(s):
    """Return the set (dictionary) of identifiers found in a $-string."""
    d = {}
    for name in [_f for _f in [b or c or None for a, b, c in dre.findall(s)] if _f]:
        d[name] = True
    return d


def percent_identifiers(s):
    """Return the set (dictionary) of identifiers found in a %-string."""
    d = {}
    for name in cre.findall(s):
        d[name] = True
    return d


# Utilities to canonicalize a string, which means un-HTML-ifying the string to
# produce a Unicode string or an 8-bit string if all the characters are ASCII.
def canonstr(s, lang=None):
    newparts = []
    parts = re.split(r'&(?P<ref>[^;]+);', s)
    def appchr(i):
        # do everything in unicode
        newparts.append(chr(i))
    def tounicode(s):
        # We want the default fallback to be iso-8859-1 even if the language
        # is English (us-ascii).  This seems like a practical compromise so
        # that non-ASCII characters in names can be used in English lists w/o
        # having to change the global charset for English from us-ascii (which
        # I superstitiously think may have unintended consequences).
        if isinstance(s, str):
            return s
        if lang is None:
            charset = 'iso-8859-1'
        else:
            charset = GetCharSet(lang)
            if charset == 'us-ascii':
                charset = 'iso-8859-1'
        return str(s, charset, 'replace')
    while True:
        newparts.append(tounicode(parts.pop(0)))
        if not parts:
            break
        ref = parts.pop(0)
        if ref.startswith('#'):
            try:
                appchr(int(ref[1:]))
            except ValueError:
                # Non-convertable, stick with what we got
                newparts.append(tounicode('&'+ref+';'))
        else:
            c = htmlentitydefs.entitydefs.get(ref, '?')
            if c.startswith('#') and c.endswith(';'):
                appchr(int(ref[1:-1]))
            else:
                newparts.append(tounicode(c))
    newstr = EMPTYSTRING.join(newparts)
    # newstr is unicode
    return newstr


# The opposite of canonstr() -- sorta.  I.e. it attempts to encode s in the
# charset of the given language, which is the character set that the page will
# be rendered in, and failing that, replaces non-ASCII characters with their
# html references.  It always returns a byte string.
def uncanonstr(s, lang=None):
    if s is None:
        s = u''
    if lang is None:
        charset = 'us-ascii'
    else:
        charset = GetCharSet(lang)
    # See if the string contains characters only in the desired character
    # set.  If so, return it unchanged, except for coercing it to a byte
    # string.
    try:
        if type(s) is str:
            return s.encode(charset)
        else:
            u = str(s, charset)
            return s
    except UnicodeError:
        # Nope, it contains funny characters, so html-ref it
        return uquote(s)


def uquote(s):
    a = []
    for c in s:
        o = ord(c)
        if o > 127:
            a.append('&#%3d;' % o)
        else:
            a.append(c)
    # Join characters together and coerce to byte string
    return str(EMPTYSTRING.join(a))


def oneline(s, cset):
    # Decode header string in one line and convert into specified charset
    try:
        h = email.header.make_header(email.header.decode_header(s))
        ustr = str(h)
        line = UEMPTYSTRING.join(ustr.splitlines())
        return line.encode(cset, 'replace')
    except (LookupError, UnicodeError, ValueError, HeaderParseError):
        # possibly charset problem. return with undecoded string in one line.
        return EMPTYSTRING.join(s.splitlines())


def strip_verbose_pattern(pattern):
    # Remove white space and comments from a verbose pattern and return a
    # non-verbose, equivalent pattern.  Replace CR and NL in the result
    # with '\\r' and '\\n' respectively to avoid multi-line results.
    if not isinstance(pattern, str):
        return pattern
    newpattern = ''
    i = 0
    inclass = False
    skiptoeol = False
    copynext = False
    while i < len(pattern):
        c = pattern[i]
        if copynext:
            if c == NL:
                newpattern += '\\n'
            elif c == CR:
                newpattern += '\\r'
            else:
                newpattern += c
            copynext = False
        elif skiptoeol:
            if c == NL:
                skiptoeol = False
        elif c == '#' and not inclass:
            skiptoeol = True
        elif c == '[' and not inclass:
            inclass = True
            newpattern += c
            copynext = True
        elif c == ']' and inclass:
            inclass = False
            newpattern += c
        elif re.search(r'\s', c, re.IGNORECASE):
            if inclass:
                if c == NL:
                    newpattern += '\\n'
                elif c == CR:
                    newpattern += '\\r'
                else:
                    newpattern += c
        elif c == '\\' and not inclass:
            newpattern += c
            copynext = True
        else:
            if c == NL:
                newpattern += '\\n'
            elif c == CR:
                newpattern += '\\r'
            else:
                newpattern += c
        i += 1
    return newpattern


# Patterns and functions to flag possible XSS attacks in HTML.
# This list is compiled from information at http://ha.ckers.org/xss.html,
# http://www.quirksmode.org/js/events_compinfo.html,
# http://www.htmlref.com/reference/appa/events1.htm,
# http://lxr.mozilla.org/mozilla/source/content/events/src/nsDOMEvent.cpp#59,
# http://www.w3.org/TR/DOM-Level-2-Events/events.html and
# http://www.xulplanet.com/references/elemref/ref_EventHandlers.html
# Many thanks are due to Moritz Naumann for his assistance with this.
_badwords = [
    '<i?frame',
    # Kludge to allow the specific tag that's in the options.html template.
    '<link(?! rel="SHORTCUT ICON" href="<mm-favicon>">)',
    '<meta',
    '<object',
    '<script',
    '@keyframes',
    r'\bj(?:ava)?script\b',
    r'\bvbs(?:cript)?\b',
    r'\bdomactivate\b',
    r'\bdomattrmodified\b',
    r'\bdomcharacterdatamodified\b',
    r'\bdomfocus(?:in|out)\b',
    r'\bdommenuitem(?:in)?active\b',
    r'\bdommousescroll\b',
    r'\bdomnodeinserted(?:intodocument)?\b',
    r'\bdomnoderemoved(?:fromdocument)?\b',
    r'\bdomsubtreemodified\b',
    r'\bfscommand\b',
    r'\bonabort\b',
    r'\bon(?:de)?activate\b',
    r'\bon(?:after|before)print\b',
    r'\bon(?:after|before)update\b',
    r'\b(?:on)?animation(?:end|iteration|start)\b',
    r'\bonbefore(?:(?:de)?activate|copy|cut|editfocus|paste)\b',
    r'\bonbeforeunload\b',
    r'\bonbegin\b',
    r'\bonblur\b',
    r'\bonbounce\b',
    r'\bonbroadcast\b',
    r'\boncanplay(?:through)?\b',
    r'\bon(?:cell)?change\b',
    r'\boncheckboxstatechange\b',
    r'\bon(?:dbl)?click\b',
    r'\bonclose\b',
    r'\boncommand(?:update)?\b',
    r'\boncomposition(?:end|start)\b',
    r'\boncontextmenu\b',
    r'\boncontrolselect\b',
    r'\boncopy\b',
    r'\boncut\b',
    r'\bondataavailable\b',
    r'\bondataset(?:changed|complete)\b',
    r'\bondrag(?:drop|end|enter|exit|gesture|leave|over)?\b',
    r'\bondragstart\b',
    r'\bondrop\b',
    r'\bondurationchange\b',
    r'\bonemptied\b',
    r'\bonend(?:ed)?\b',
    r'\bonerror(?:update)?\b',
    r'\bonfilterchange\b',
    r'\bonfinish\b',
    r'\bonfocus(?:in|out)?\b',
    r'\bonhashchange\b',
    r'\bonhelp\b',
    r'\boninput\b',
    r'\bonkey(?:up|down|press)\b',
    r'\bonlayoutcomplete\b',
    r'\bon(?:un)?load\b',
    r'\bonloaded(?:meta)?data\b',
    r'\bonloadstart\b',
    r'\bonlosecapture\b',
    r'\bonmedia(?:complete|error)\b',
    r'\bonmessage\b',
    r'\bonmouse(?:down|enter|leave|move|out|over|up|wheel)\b',
    r'\bonmove(?:end|start)?\b',
    r'\bon(?:off|on)line\b',
    r'\bonopen\b',
    r'\bonoutofsync\b',
    r'\bonoverflow(?:changed)?\b',
    r'\bonpage(?:hide|show)\b',
    r'\bonpaint\b',
    r'\bonpaste\b',
    r'\bonpause\b',
    r'\bonplay(?:ing)?\b',
    r'\bonpopstate\b',
    r'\bonpopup(?:hidden|hiding|showing|shown)\b',
    r'\bonprogress\b',
    r'\bonpropertychange\b',
    r'\bonradiostatechange\b',
    r'\bonratechange\b',
    r'\bonreadystatechange\b',
    r'\bonrepeat\b',
    r'\bonreset\b',
    r'\bonresize(?:end|start)?\b',
    r'\bonresume\b',
    r'\bonreverse\b',
    r'\bonrow(?:delete|enter|exit|inserted)\b',
    r'\bonrows(?:delete|enter|inserted)\b',
    r'\bonscroll\b',
    r'\bonsearch\b',
    r'\bonseek(?:ed|ing)?\b',
    r'\bonselect(?:start)?\b',
    r'\bonselectionchange\b',
    r'\bonshow\b',
    r'\bonstart\b',
    r'\bonstalled\b',
    r'\bonstop\b',
    r'\bonstorage\b',
    r'\bonsubmit\b',
    r'\bonsuspend\b',
    r'\bonsync(?:from|to)preference\b',
    r'\bonsyncrestored\b',
    r'\bontext\b',
    r'\bontime(?:error|update)\b',
    r'\bontoggle\b',
    r'\bontouch(?:cancel|end|move|start)\b',
    r'\bontrackchange\b',
    r'\b(?:on)?transitionend\b',
    r'\bonunderflow\b',
    r'\bonurlflip\b',
    r'\bonvolumechange\b',
    r'\bonwaiting\b',
    r'\bonwheel\b',
    r'\bseeksegmenttime\b',
    r'\bsvgabort\b',
    r'\bsvgerror\b',
    r'\bsvgload\b',
    r'\bsvgresize\b',
    r'\bsvgscroll\b',
    r'\bsvgunload\b',
    r'\bsvgzoom\b',
    ]


# This is the actual re to look for the above patterns
_badhtml = re.compile('|'.join(_badwords), re.IGNORECASE)
# This is used to filter non-printable us-ascii characters, some of which
# can be used to break words to avoid recognition.
_filterchars = re.compile('[\000-\011\013\014\016-\037\177-\237]')
# This is used to recognize '&#' and '%xx' strings for _translate which
# translates them to characters
_encodedchars = re.compile('(&#[0-9]+;?)|(&#x[0-9a-f]+;?)|(%[0-9a-f]{2})',
                           re.IGNORECASE)


def _translate(mo):
    """Translate &#... and %xx encodings into the encoded character."""
    match = mo.group().lower().strip('&#;')
    try:
        if match.startswith('x') or match.startswith('%'):
            val = int(match[1:], 16)
        else:
            val = int(match, 10)
    except ValueError:
        return ''
    if val < 256:
        return chr(val)
    else:
        return ''


def suspiciousHTML(html):
    """Check HTML string for various tags, script language names and
    'onxxx' actions that can be used in XSS attacks.
    Currently, this a very simple minded test.  It just looks for
    patterns without analyzing context.  Thus, it potentially flags lots
    of benign stuff.
    Returns True if anything suspicious found, False otherwise.
    """

    if _badhtml.search(_filterchars.sub(
                       '', _encodedchars.sub(_translate, html))):
        return True
    else:
        return False


# The next functions read data from
# https://publicsuffix.org/list/public_suffix_list.dat and implement the
# algorithm at https://publicsuffix.org/list/ to find the "Organizational
# Domain corresponding to a From: domain.

s_dict = {}

def get_suffixes(url):
    """Get the list of public suffixes from the given URL."""
    try:
        d = urllib.request.urlopen(url)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        mailman_log('error', 'Failed to fetch DMARC organizational domain data from %s: %s',
               url, e)
        return
    for line in d.readlines():
        # Convert bytes to string if necessary
        if isinstance(line, bytes):
            line = line.decode('utf-8')
        if not line.strip() or line.startswith(' ') or line.startswith('//'):
            continue
        line = re.sub(' .*', '', line.strip())
        if not line:
            continue
        parts = line.lower().split('.')
        if parts[0].startswith('!'):
            exc = True
            parts = [parts[0][1:]] + parts[1:]
        else:
            exc = False
        parts.reverse()
        k = '.'.join(parts)
        s_dict[k] = exc

def _get_dom(d, l):
    """A helper to get a domain name consisting of the first l+1 labels
    in d."""
    dom = d[:min(l+1, len(d))]
    dom.reverse()
    return '.'.join(dom)

def get_org_dom(domain):
    """Given a domain name, this returns the corresponding Organizational
    Domain which may be the same as the input."""
    global s_dict
    if not s_dict:
        get_suffixes(mm_cfg.DMARC_ORGANIZATIONAL_DOMAIN_DATA_URL)
    hits = []
    d = domain.lower().split('.')
    d.reverse()
    for k in list(s_dict.keys()):
        ks = k.split('.')
        if len(d) >= len(ks):
            for i in range(len(ks)-1):
                if d[i] != ks[i] and ks[i] != '*':
                    break
            else:
                if d[len(ks)-1] == ks[-1] or ks[-1] == '*':
                    hits.append(k)
    if not hits:
        return _get_dom(d, 1)
    l = 0
    for k in hits:
        if s_dict[k]:
            # It's an exception
            return _get_dom(d, len(k.split('.'))-1)
        if len(k.split('.')) > l:
            l = len(k.split('.'))
    return _get_dom(d, l)


# This takes an email address, and returns True if DMARC policy is p=reject
# or possibly quarantine.
def IsDMARCProhibited(mlist, email):
    if not dns_resolver:
        # This is a problem; log it.
        mailman_log('error',
            'DNS lookup for dmarc_moderation_action for list %s not available',
            mlist.real_name)
        return False

    email = email.lower()
    # Scan from the right in case quoted local part has an '@'.
    at_sign = email.rfind('@')
    if at_sign < 1:
        return False
    f_dom = email[at_sign+1:]
    x = _DMARCProhibited(mlist, email, '_dmarc.' + f_dom)
    if x != 'continue':
        return x
    o_dom = get_org_dom(f_dom)
    if o_dom != f_dom:
        x = _DMARCProhibited(mlist, email, '_dmarc.' + o_dom, org=True)
        if x != 'continue':
            return x
    return False

def _DMARCProhibited(mlist, email, domain):
    """Check if the domain has a DMARC policy that prohibits sending.
    """
    try:
        import dns.resolver
        import dns.exception
    except ImportError:
        return False
    try:
        txt_rec = dns.resolver.resolve(domain, 'TXT')
        # Newer versions of dnspython use strings property instead of strings attribute
        txt_strings = txt_rec.strings if hasattr(txt_rec, 'strings') else [str(r) for r in txt_rec]
        for txt in txt_strings:
            if txt.startswith('v=DMARC1'):
                # Parse the DMARC record
                parts = txt.split(';')
                for part in parts:
                    part = part.strip()
                    if part.startswith('p='):
                        policy = part[2:].lower()
                        if policy in ('reject', 'quarantine'):
                            return True
    except (dns.exception.DNSException, AttributeError):
        pass
    return False


# Check a known list in order to auto-moderate verbose members
# dictionary to remember recent posts.
recentMemberPostings = {}
# counter of times through
clean_count = 0
def IsVerboseMember(mlist, email):
    """For lists that request it, we keep track of recent posts by address.
    A message from an address to a list, if the list requests it, is remembered
    for a specified time whether or not the address is a list member, and if the
    address is a member and the member is over the threshold for the list, that
    fact is returned."""
    global clean_count, recentMemberPostings

    if mlist.member_verbosity_threshold == 0:
        return False

    email = email.lower()
    now = time.time()

    # Clean up old entries periodically
    clean_count += 1
    if clean_count >= mm_cfg.VERBOSE_CLEAN_LIMIT:
        clean_count = 0
        # Remove entries older than the maximum verbosity interval
        max_age = max(mlist.member_verbosity_interval for mlist in mm_cfg.LISTS.values())
        cutoff = now - max_age
        recentMemberPostings = {
            addr: [t for t in times if t > cutoff]
            for addr, times in recentMemberPostings.items()
            if any(t > cutoff for t in times)
        }

    # Add new posting time
    recentMemberPostings.setdefault(email, []).append(now + float(mlist.member_verbosity_interval))

    # Remove old times for this email
    recentMemberPostings[email] = [t for t in recentMemberPostings[email] if t > now]

    if not mlist.isMember(email):
        return False

    return len(recentMemberPostings.get(email, [])) > mlist.member_verbosity_threshold


def check_eq_domains(email, domains_list):
    """The arguments are an email address and a string representing a
    list of lists in a form like 'a,b,c;1,2' representing [['a', 'b',
    'c'],['1', '2']].  The inner lists are domains which are
    equivalent in some sense.  The return is an empty list or a list
    of email addresses equivalent to the first argument.
    For example, given

    email = 'user@me.com'
    domains_list = '''domain1, domain2; mac.com, me.com, icloud.com;
                   domaina, domainb
                   '''

    check_eq_domains(email, domains_list) will return
    ['user@mac.com', 'user@icloud.com']
    """
    if not domains_list:
        return []
    try:
        local, domain = email.rsplit('@', 1)
    except ValueError:
        return []
    domain = domain.lower()
    domains_list = re.sub(r'\s', '', domains_list, flags=re.IGNORECASE).lower()
    domains = domains_list.split(';')
    domains_list = []
    for d in domains:
        domains_list.append(d.split(','))
    for domains in domains_list:
        if domain in domains:
            return [local + '@' + x for x in domains if x != domain]
    return []


def _invert_xml(mo):
    # This is used with re.sub below to convert XML char refs and textual \u
    # escapes to unicodes.
    try:
        if mo.group(1)[:1] == '#':
            return chr(int(mo.group(1)[1:]))
        elif mo.group(1)[:1].lower() == 'u':
            return chr(int(mo.group(1)[1:], 16))
        else:
            return(u'\ufffd')
    except ValueError:
        # Value is out of range.  Return the unicode replace character.
        return(u'\ufffd')


def xml_to_unicode(s, cset):
    """This converts a string s, encoded in cset to a unicode with translation
    of XML character references and textual \\u xxxx escapes.  It is more or less
    the inverse of unicode.decode(cset, errors='xmlcharrefreplace').  It is
    similar to canonstr above except for replacing invalid refs with the
    unicode replace character and recognizing \\u escapes.
    """
    if isinstance(s, bytes):
        us = s.decode(cset, 'replace')
        us = re.sub(r'&(#[0-9]+);', _invert_xml, us, flags=re.IGNORECASE)
        us = re.sub(r'(?i)\\\\(u[a-f0-9]{4})', _invert_xml, us, flags=re.IGNORECASE)
        return us
    else:
        return s

def banned_ip(ip):
    """Check if an IP address is in the Spamhaus blocklist.
    
    Supports both IPv4 and IPv6 addresses.
    Returns True if the IP is in the blocklist, False otherwise.
    """
    if not dns_resolver:
        return False
    
    try:
        if isinstance(ip, bytes):
            ip = ip.decode('us-ascii', errors='replace')
        
        if have_ipaddress:
            try:
                ip_obj = ipaddress.ip_address(ip)
                if isinstance(ip_obj, ipaddress.IPv4Address):
                    # IPv4 format: 1.2.3.4 -> 4.3.2.1.zen.spamhaus.org
                    parts = str(ip_obj).split('.')
                    lookup = '{0}.{1}.{2}.{3}.zen.spamhaus.org'.format(
                        parts[3], parts[2], parts[1], parts[0])
                else:
                    # IPv6 format: 2001:db8::1 -> 1.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.8.b.d.0.1.0.0.2.zen.spamhaus.org
                    # Convert to reverse nibble format
                    expanded = ip_obj.exploded.replace(':', '')
                    lookup = '.'.join(reversed(expanded)) + '.zen.spamhaus.org'
            except ValueError:
                return False
        else:
            # Fallback for systems without ipaddress module
            if ':' in ip:
                # IPv6 address
                try:
                    # Basic IPv6 validation and conversion
                    parts = ip.split(':')
                    if len(parts) > 8:
                        return False
                    # Pad with zeros
                    expanded = ''.join(part.zfill(4) for part in parts)
                    lookup = '.'.join(reversed(expanded)) + '.zen.spamhaus.org'
                except (ValueError, IndexError):
                    return False
            else:
                # IPv4 address
                parts = ip.split('.')
                if len(parts) != 4:
                    return False
                try:
                    if not all(0 <= int(part) <= 255 for part in parts):
                        return False
                    lookup = '{0}.{1}.{2}.{3}.zen.spamhaus.org'.format(
                        parts[3], parts[2], parts[1], parts[0])
                except ValueError:
                    return False

        # Set DNS resolver timeouts to prevent DoS
        resolver = dns.resolver.Resolver()
        resolver.timeout = 2.0  # 2 second timeout
        resolver.lifetime = 4.0  # 4 second total lifetime
        
        try:
            # Check for blocklist response
            answers = resolver.resolve(lookup, 'A')
            for rdata in answers:
                if str(rdata).startswith('127.0.0.'):
                    return True
        except dns.resolver.NXDOMAIN:
            # IP not found in blocklist
            return False
        except dns.resolver.Timeout:
            mailman_log('error', 'DNS timeout checking IP %s in Spamhaus', ip)
            return False
        except dns.resolver.NoAnswer:
            mailman_log('error', 'No DNS answer for IP %s in Spamhaus', ip)
            return False
        except dns.exception.DNSException as e:
            mailman_log('error', 'DNS error checking IP %s in Spamhaus: %s', ip, str(e))
            return False
            
    except Exception as e:
        mailman_log('error', 'Error checking IP %s in Spamhaus: %s', ip, str(e))
        return False
        
    return False

def banned_domain(email):
    """Check if a domain is in the Spamhaus Domain Block List (DBL).
    
    Returns True if the domain is in the blocklist, False otherwise.
    """
    if not dns_resolver:
        return False

    email = email.lower()
    user, domain = ParseEmail(email)

    lookup = '%s.dbl.spamhaus.org' % (domain)

    # Set DNS resolver timeouts to prevent DoS
    resolver = dns.resolver.Resolver()
    resolver.timeout = 2.0  # 2 second timeout
    resolver.lifetime = 4.0  # 4 second total lifetime

    try:
        # Use resolve() instead of query()
        ans = resolver.resolve(lookup, 'A')
        if not ans:
            return False
        # Newer versions of dnspython use strings property instead of strings attribute
        text = ans.rrset.to_text() if hasattr(ans, 'rrset') else str(ans)
        if re.search(r'127\.0\.1\.\d{1,3}$', text, re.MULTILINE | re.IGNORECASE):
            if not re.search(r'127\.0\.1\.255$', text, re.MULTILINE | re.IGNORECASE):
                return True
    except dns.resolver.NXDOMAIN:
        # Domain not found in blocklist
        return False
    except dns.resolver.Timeout:
        mailman_log('error', 'DNS timeout checking domain %s in Spamhaus DBL', domain)
        return False
    except dns.resolver.NoAnswer:
        mailman_log('error', 'No DNS answer for domain %s in Spamhaus DBL', domain)
        return False
    except dns.exception.DNSException as e:
        mailman_log('error', 'DNS error checking domain %s in Spamhaus DBL: %s', domain, str(e))
        return False
    except Exception as e:
        mailman_log('error', 'Unexpected error checking domain %s in Spamhaus DBL: %s', domain, str(e))
        return False

    return False


def captcha_display(mlist, lang, captchas):
    """Returns a CAPTCHA question, the HTML for the answer box, and
    the data to be put into the CSRF token"""
    if not lang in captchas:
        lang = 'en'
    captchas = captchas[lang]
    idx = random.randrange(len(captchas))
    question = captchas[idx][0]
    box_html = mlist.FormatBox('captcha_answer', size=30)
    # Remember to encode the language in the index so that we can get it out
    # again!
    return (websafe(question), box_html, '{}-{}'.format(lang, idx))

def captcha_verify(idx, given_answer, captchas):
    try:
        (lang, idx) = idx.split("-")
        idx = int(idx)
    except ValueError:
        return False
    if not lang in captchas:
        return False
    captchas = captchas[lang]
    if not idx in range(len(captchas)):
        return False
    # Check the given answer.
    # We append a `$` to emulate `re.fullmatch`.
    correct_answer_pattern = captchas[idx][1] + "$"
    return re.match(correct_answer_pattern, given_answer)

def validate_ip_address(ip):
    """Validate and normalize an IP address.
    
    Args:
        ip: The IP address to validate.
        
    Returns:
        A tuple of (is_valid, normalized_ip). If the IP is invalid,
        normalized_ip will be None.
    """
    if not ip:
        return False, None
        
    try:
        if have_ipaddress:
            ip_obj = ipaddress.ip_address(ip)
            if isinstance(ip_obj, ipaddress.IPv4Address):
                # For IPv4, drop last octet
                parts = str(ip_obj).split('.')
                return True, '.'.join(parts[:-1])
            else:
                # For IPv6, drop last 16 bits
                expanded = ip_obj.exploded.replace(':', '')
                return True, expanded[:-4]
        else:
            # Fallback for systems without ipaddress module
            if ':' in ip:
                # IPv6 address
                parts = ip.split(':')
                if len(parts) <= 8:
                    # Pad with zeros and drop last 16 bits
                    expanded = ''.join(part.zfill(4) for part in parts)
                    return True, expanded[:-4]
            else:
                # IPv4 address
                parts = ip.split('.')
                if len(parts) == 4:
                    return True, '.'.join(parts[:-1])
    except (ValueError, IndexError):
        pass
        
    return False, None

def ValidateListName(listname):
    """Validate a list name against the acceptable character pattern.
    
    Args:
        listname: The list name to validate
        
    Returns:
        bool: True if the list name is valid, False otherwise
    """
    if not listname:
        return False
    # Check if the list name contains any characters not in the acceptable pattern
    return len(re.sub(mm_cfg.ACCEPTABLE_LISTNAME_CHARACTERS, '', listname, flags=re.IGNORECASE)) == 0

def formataddr(pair):
    """The inverse of parseaddr(), this takes a 2-tuple of (name, address)
    and returns the string value suitable for an RFC 2822 From, To or Cc
    header.

    If the first element of pair is false, then the second element is
    returned unmodified.
    """
    name, address = pair
    if name:
        # If name is bytes, decode it to str
        if isinstance(name, bytes):
            name = name.decode('utf-8', 'replace')
        # If name contains non-ASCII characters and is not already encoded,
        # encode it
        if isinstance(name, str) and any(ord(c) > 127 for c in name):
            name = email.header.Header(name, 'utf-8').encode()
        return '%s <%s>' % (name, address)
    return address
