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

from __future__ import print_function, unicode_literals

import os
import sys
import re
import cgi
import time
import errno
import base64
import random
import io
import html.entities as htmlentitydefs
import urllib.parse
import urllib.request
import urllib.error
import email.Header
import email.Iterators
from email.Errors import HeaderParseError
from string import whitespace, digits
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
from Mailman.Logging.Syslog import syslog

try:
    import hashlib
    md5_new = hashlib.md5
    sha_new = hashlib.sha1
except ImportError:
    # This should never happen in Python 3
    raise ImportError("hashlib module not found")

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
UEMPTYSTRING = ''  # No need for u'' prefix in Python 3
CR = '\r'
NL = '\n'
DOT = '.'
IDENTCHARS = ascii_letters + digits + '_'

# Search for $(identifier)s strings) as except that the trailing s is optional,
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
    if len(re.sub(mm_cfg.ACCEPTABLE_LISTNAME_CHARACTERS, '', listname)) > 0:
        remote = os.environ.get('HTTP_FORWARDED_FOR',
                 os.environ.get('HTTP_X_FORWARDED_FOR',
                 os.environ.get('REMOTE_ADDR',
                                'unidentified origin')))
        syslog('mischief',
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
    return Site.get_listnames()


def get_domain():
    """Return URL domain for this site."""
    return Site.get_domain()


def get_site_email(type='site', domain=None):
    """Return email address for the site administrator.
    
    :param type: Type of email address (site, owner, admin, etc.)
    :param domain: Optional domain for the email address
    :return: Email address string
    """
    if domain is None:
        domain = Site.get_domain()
    return f'{type}@{domain}'


def map_list_names():
    """Return a sorted mapping of all list names and their descriptions.
    
    :return: List of (name, description) tuples
    """
    # Return a sorted mapping of listnames to their descriptions
    listnames = list_names()
    listnames.sort()
    descs = []
    for name in listnames:
        mlist = None
        try:
            mlist = MailList.MailList(name, lock=0)
            desc = mlist.description
            if not desc:
                desc = _('[no description available]')
            descs.append((name, desc))
        finally:
            if mlist:
                mlist.Unlock()
    return descs


# a much more naive implementation than say, Emacs's fill-paragraph!
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
    paras = re.split('\n\n', text)
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
_badchars = re.compile(r'[][()!=|:;^,\\"\000-\037\177-\377]')
# Strictly speaking, some of the above are allowed in quoted local parts, but
# this can open the door to certain web exploits so we don't allow them.
# Only characters allowed in domain parts.
_valid_domain = re.compile('[-a-z0-9]', re.IGNORECASE)

def ValidateEmail(s):
    """Verify that an email address isn't grossly evil."""
    # If a user submits a form or URL with post data or query fragments
    # with multiple occurrences of the same variable, we can get a list
    # here.  Be as careful as possible.
    if isinstance(s, list) or isinstance(s, tuple):
        if len(s) == 0:
            s = ''
        else:
            s = s[-1]
    # Pretty minimal, cheesy check.  We could do better...
    if not s or s.count(' ') > 0:
        raise Errors.MMBadEmailError(s)
    if _badchars.search(s):
        raise Errors.MMHostileAddress(s)
    user, domain_parts = ParseEmail(s)
    # This means local, unqualified addresses, are not allowed
    if not domain_parts:
        raise Errors.MMBadEmailError(s)
    if len(domain_parts) < 2:
        raise Errors.MMBadEmailError(s)
    # domain parts may only contain ascii letters, digits and hyphen
    # and must not begin with hyphen.
    for p in domain_parts:
        if len(p) == 0 or p[0] == '-' or len(_valid_domain.sub('', p)) > 0:
            raise Errors.MMHostileAddress(s)


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
            syslog('error',
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
            syslog('mischief',
               'Hostile listname: listname=%s: remote=%s', pieces[0], remote)
            pieces[0] = pieces[0][:longest] + '...'
        return pieces
    return None


def GetRequestMethod():
    return os.environ.get('REQUEST_METHOD')


def ScriptURL(target, web_page_url=None, absolute=False):
    """target - scriptname only, nothing extra
    """
    if web_page_url is None:
        web_page_url = mm_cfg.DEFAULT_URL
    if web_page_url is None:
        web_page_url = ''
    if web_page_url != '' and web_page_url[-1] != '/':
        web_page_url = web_page_url + '/'

    # Note that posting to the following URL works without the trailing slash
    # because of the way Apache handles PATH_INFO
    if absolute:
        # Use absolute addressing
        return web_page_url + target + mm_cfg.CGIEXT
    # See if we should use relative addressing
    if not web_page_url:
        return target + mm_cfg.CGIEXT
    # We can use relative addressing
    if mm_cfg.VIRTUAL_HOST_OVERVIEW:
        # With virtual host support, we need to use the entire URL
        return web_page_url + target + mm_cfg.CGIEXT
    # Otherwise, we can use just the path part
    baseurl = urllib.parse.urlparse(web_page_url)[2]
    if not absolute and fullpath.startswith(baseurl):
        # Use relative addressing
        i = fullpath.find(baseurl)
        count = fullpath.count('/', 0, i)
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
                        syslog('error', 'urandom not available, passwords not secure')
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
        fp = open(filename, 'w')
        fp.write(sha_new(pw).hexdigest() + '\n')
        fp.close()
    finally:
        os.umask(omask)


def get_global_password(siteadmin=True):
    if siteadmin:
        filename = mm_cfg.SITE_PW_FILE
    else:
        filename = mm_cfg.LISTCREATOR_PW_FILE
    try:
        fp = open(filename)
        challenge = fp.read()[:-1]                # strip off trailing nl
        fp.close()
    except (IOError, OSError) as e:
        if e.errno != errno.ENOENT: raise
        # It's okay not to have a site admin password, just return false
        return None
    return challenge


def check_global_password(response, siteadmin=True):
    challenge = get_global_password(siteadmin)
    if challenge is None:
        return None
    return challenge == sha_new(response).hexdigest()


_ampre = re.compile('&amp;((?:#[0-9]+|[a-z]+);)', re.IGNORECASE)
def websafe(s):
    """Convert a string to a form safe for use in HTML."""
    if not isinstance(s, str):
        s = s.decode('utf-8', 'replace')
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


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

def findtext(templatefile, dict=None, raw=False, lang=None, mlist=None):
    try:
        filename = os.path.join(mm_cfg.TEMPLATE_DIR, 'en', templatefile)
        fp = open(filename)
    except IOError as e:
        if e.errno != errno.ENOENT:
            raise
        # We never found the template. BAD!
        raise IOError(errno.ENOENT, 'No template file found', templatefile)

    template = fp.read()
    fp.close()
    text = template
    if dict is not None:
        try:
            sdict = SafeDict(dict)
            try:
                text = sdict.interpolate(template)
            except (UnicodeError, LookupError, ValueError, HeaderParseError) as e:
                # Try again after coercing the template to unicode
                utemplate = str(template, encoding='utf-8', errors='replace')
                text = sdict.interpolate(utemplate)
        except (TypeError, ValueError) as e:
            # The template is really screwed up
            syslog('error', 'broken template: %s\n%s', filename, e)
            pass
    if raw:
        return text, filename
    return wrap(text), filename


def maketext(templatefile, dict=None, raw=False, lang=None, mlist=None):
    return findtext(templatefile, dict, raw, lang, mlist)[0]


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

# Given a Message.Message object, test for administrivia (eg subscribe,
# unsubscribe, etc).  The test must be a good guess -- messages that return
# true get sent to the list admin instead of the entire list.
def is_administrivia(msg):
    linecnt = 0
    lines = []
    for line in email.Iterators.body_line_iterator(msg):
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
    if ADMINDATA.has_key(bodytext.strip().lower()):
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

    Returns the path, starting at the script name, and including any PATH_INFO
    and QUERY_STRING.  This is the same as REQUEST_URI in Apache or SCRIPT_URL
    in AOLserver, but is constructed from several environment variables for
    portability.

    If the environment is missing some of the necessary variables, a fallback
    path can be given as a default.
    """
    # Try first some things that aren't available in CGI
    url = os.environ.get('REQUEST_URI')
    if url != None:
        if escape:
            return websafe(url)
        return url
    url = os.environ.get('SCRIPT_URL')
    if url != None:
        if os.environ.get('QUERY_STRING'):
            url += '?' + os.environ['QUERY_STRING']
        if escape:
            return websafe(url)
        return url
    # Okay, do it the hard way
    url = os.environ.get('SCRIPT_NAME', '')
    pathinfo = os.environ.get('PATH_INFO')
    if pathinfo != None:
        url += pathinfo
    querystring = os.environ.get('QUERY_STRING')
    if querystring != None:
        url += '?' + querystring
    if escape:
        return websafe(url)
    return url


# Wait on a dictionary of child pids
def reap(kids, func=None, once=False):
    """Wait on a dictionary of child pids."""
    while kids:
        if func:
            func()
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
            if pid != 0:
                try:
                    del kids[pid]
                except (KeyError) as e:
                    # Shouldn't happen) as but who knows?
                    pass
        except OSError as e:
            # No children left, we're done
            if e.errno != errno.ECHILD:
                # This shouldn't happen either, but who knows?
                kids.clear()
            break
        if once:
            break
        time.sleep(0.1)


def GetLanguageDescr(lang):
    """Return the description for the language."""
    return mm_cfg.LC_DESCRIPTIONS[lang][0]


def GetCharSet(lang):
    """Return the charset for the given language.
    """
    if lang is None:
        return mm_cfg.DEFAULT_CHARSET
    charset = mm_cfg.LANGUAGES.get(lang, (mm_cfg.DEFAULT_CHARSET,))[0]
    # UTF-8 is always acceptable
    if charset.lower().replace('-', '') == 'utf8':
        return 'utf-8'
    return charset

def GetDirection(lang):
    return mm_cfg.LC_DESCRIPTIONS[lang][2]

def IsLanguage(lang):
    """Return true if lang is a supported language."""
    return lang in mm_cfg.LC_DESCRIPTIONS


# This algorithm crafts a guaranteed unique message-id.  The theory here is
# that pid+listname+host will distinguish the message-id for every process on
# the system, except (when process ids wrap around.  To further distinguish
# message-ids) as we prepend the integral time in seconds since the epoch.  It's
# still possible that we'll vend out more than one such message-id per second,
# so we prepend a monotonically incrementing serial number.  It's highly
# unlikely that within a single second, there'll be a pid wraparound.
_serial = 0
def unique_message_id(mlist):
    """Return a unique message ID."""
    msgid = '%s.%s.%s' % (time.time(), random.random(), make_msgid())
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
    return EMPTYSTRING.join(filter(None, parts))


def dollar_identifiers(s):
    """Return the set (dictionary) of identifiers found in a $-string."""
    d = {}
    for name in filter(None, [b or c or None for a, b, c in dre.findall(s)]):
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
def canonstr(s: Union[str, bytes], lang: Optional[str] = None) -> str:
    """Convert a string to canonical form - i.e. as a str type string.

    If a bytes object is passed, it is decoded using the charset for the
    specified language, or ascii if no language is specified.  If a str
    object is passed, it is returned unchanged.

    :param s: String to convert
    :param lang: Optional language code
    :return: Canonical string
    """
    if isinstance(s, str):
        return s
    charset = GetCharSet(lang)
    return str(s, charset, 'replace')


# The opposite of canonstr() -- sorta.  I.e. it attempts to encode s in the
# charset of the given language, which is the character set that the page will
# be rendered in, and failing that, replaces non-ASCII characters with their
# html references.  It always returns a byte string.
def uncanonstr(s: Union[str, bytes], lang: Optional[str] = None) -> bytes:
    """Convert a string from canonical form to a bytes object.

    If a str object is passed, it is encoded using the charset for the
    specified language, or ascii if no language is specified.  If a bytes
    object is passed, it is returned unchanged.

    :param s: String to convert
    :param lang: Optional language code
    :return: Bytes object
    """
    if isinstance(s, bytes):
        return s
    charset = GetCharSet(lang)
    return s.encode(charset, 'replace')


def uquote(s: str) -> str:
    """Convert a string to HTML-safe form by replacing non-ASCII characters
    with their HTML entity references.
    
    Args:
        s: The input string
        
    Returns:
        str: The HTML-safe string
    """
    a = []
    for c in s:
        o = ord(c)
        if o > 127:
            a.append('&#%3d;' % o)
        else:
            a.append(c)
    return ''.join(a)


def oneline(s: Union[str, bytes], cset: str) -> Tuple[bytes, str]:
    """Decode header string in one line and convert into specified charset.
    
    Args:
        s: The input string or bytes
        cset: The target charset
        
    Returns:
        Tuple[bytes, str]: The encoded string and the charset used
    """
    try:
        h = email.Header.make_header(email.Header.decode_header(s))
        ustr = str(h)
        oneline = ''.join(ustr.splitlines())
        return oneline.encode(cset, 'replace'), cset
    except (LookupError, ValueError, HeaderParseError) as e:
        return s.encode(cset, 'replace'), cset


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
        elif re.search(r'\s', c):
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
    except (ValueError):
        return ''
    if val < 256:
        return chr(val)
    else:
        return ''


def suspiciousHTML(html):
    """Check HTML string for various tags) as script language names and
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
    """This loads and parses the data from the url argument into s_dict for
    use by get_org_dom."""
    global s_dict
    if s_dict:
        return
    if not url:
        return
    try:
        d = urllib.request.urlopen(url)
    except urllib.error.URLError as e:
        syslog('error',
               'Unable to retrieve data from %s: %s',
               url, e)
        return
    for line in d.readlines():
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
    for k in s_dict.keys():
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
        syslog('error',
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

def _DMARCProhibited(mlist, email, dmarc_domain, org=False):

    try:
        resolver = dns.resolver.Resolver()
        resolver.timeout = float(mm_cfg.DMARC_RESOLVER_TIMEOUT)
        resolver.lifetime = float(mm_cfg.DMARC_RESOLVER_LIFETIME)
        txt_recs = resolver.query(dmarc_domain, dns.rdatatype.TXT)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return 'continue'
    except dns.resolver.NoNameservers:
        syslog('error', 'DNSException: No Nameservers available for %s (%s)',
               email, dmarc_domain)
        # Typically this means a dnssec validation error. Clients that don't
        # perform validation *may* successfully see a _dmarc RR whereas a
        # validating mailman server won't see the _dmarc RR. We should
        # mitigate this email to be safe.
        return True
    except DNSException as e:
        syslog('error', 'DNSException: Unable to query DMARC policy for %s (%s). %s',
               email, dmarc_domain, e.__doc__)
        # While we can't be sure what caused the error, there is potentially
        # a DMARC policy record that we missed and that a receiver of the mail
        # might see. Thus, we should err on the side of caution and mitigate.
        return True
    else:
        # Be as robust as possible in parsing the result.
        results_by_name = {}
        cnames = {}
        want_names = set([dmarc_domain + '.'])
        for txt_rec in txt_recs.response.answer:
            # Don't be fooled by an answer with uppercase in the name.
            name = txt_rec.name.to_text().lower()
            if txt_rec.rdtype == dns.rdatatype.CNAME:
                cnames[name] = (
                    txt_rec.items[0].target.to_text())
            if txt_rec.rdtype != dns.rdatatype.TXT:
                continue
            results_by_name.setdefault(name, []).append(
                "".join(txt_rec.items[0].strings))
        expands = list(want_names)
        seen = set(expands)
        while expands:
            item = expands.pop(0)
            if item in cnames:
                if cnames[item] in seen:
                    continue # cname loop
                expands.append(cnames[item])
                seen.add(cnames[item])
                want_names.add(cnames[item])
                want_names.discard(item)

        if len(want_names) != 1:
            syslog('error',
                   """multiple DMARC entries in results for %s,
                   processing each to be strict""",
                   dmarc_domain)
        for name in want_names:
            if name not in results_by_name:
                continue
            dmarcs = filter(lambda n: n.startswith('v=DMARC1;'),
                            results_by_name[name])
            if len(dmarcs) == 0:
                return 'continue'
            if len(dmarcs) > 1:
                syslog('error',
                       """RRset of TXT records for %s has %d v=DMARC1 entries;
                       ignoring them per RFC 7849""",
                        dmarc_domain, len(dmarcs))
                return False
            for entry in dmarcs:
                mo = re.search(r'\bsp=(\w*)\b', entry, re.IGNORECASE)
                if org and mo:
                    policy = mo.group(1).lower()
                else:
                    mo = re.search(r'\bp=(\w*)\b', entry, re.IGNORECASE)
                    if mo:
                        policy = mo.group(1).lower()
                    else:
                        continue
                if policy == 'reject':
                    syslog('vette',
                      '%s: DMARC lookup for %s (%s) found p=reject in %s = %s',
                      mlist.real_name,  email, dmarc_domain, name, entry)
                    return True

                if (mlist.dmarc_quarantine_moderation_action and
                    policy == 'quarantine'):
                    syslog('vette',
                  '%s: DMARC lookup for %s (%s) found p=quarantine in %s = %s',
                          mlist.real_name,  email, dmarc_domain, name, entry)
                    return True

                if (mlist.dmarc_none_moderation_action and
                    mlist.dmarc_quarantine_moderation_action and
                    mlist.dmarc_moderation_action in (1, 2) and
                    policy == 'none'):
                    syslog('vette',
                  '%s: DMARC lookup for %s (%s) found p=none in %s = %s',
                          mlist.real_name,  email, dmarc_domain, name, entry)
                    return True

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

    global clean_count

    if mlist.member_verbosity_threshold == 0:
        return False

    email = email.lower()

    now = time.time()
    recentMemberPostings.setdefault(email,[]).append(now +
                                       float(mlist.member_verbosity_interval)
                                   )
    x = range(len(recentMemberPostings[email]))
    x.reverse()
    for i in x:
        if recentMemberPostings[email][i] < now:
            del recentMemberPostings[email][i]

    clean_count += 1
    if clean_count >= mm_cfg.VERBOSE_CLEAN_LIMIT:
        clean_count = 0
        for addr in recentMemberPostings.keys():
            x = range(len(recentMemberPostings[addr]))
            x.reverse()
            for i in x:
                if recentMemberPostings[addr][i] < now:
                    del recentMemberPostings[addr][i]
            if not recentMemberPostings[addr]:
                del recentMemberPostings[addr]
    if not mlist.isMember(email):
        return False
    return (len(recentMemberPostings.get(email, [])) >
                mlist.member_verbosity_threshold
           )


def check_eq_domains(email, domains_list):
    """The arguments are an email address and a string representing a
    list of lists in a form like 'a,b,c;1,2' representing [['a', 'b',
    'c'],['1', '2']]. The inner lists are domains which are
    equivalent in some sense. The return is an empty list or a list
    of email addresses equivalent to the first argument.
    """
    if not domains_list:
        return []
    try:
        local, domain = email.rsplit('@', 1)
    except ValueError:
        return []
    domain = domain.lower()
    domains_list = re.sub(r'\s', '', domains_list).lower()
    domains = domains_list.split(';')
    domains_list = []
    for d in domains:
        domains_list.append(d.split(','))
    for domains in domains_list:
        if domain in domains:
            return [local + '@' + x for x in domains if x != domain]
    return []


def _invert_xml(mo):
    """Convert XML character references to Unicode characters."""
    try:
        if mo.group(1)[:1] == '#':
            return chr(int(mo.group(1)[1:]))
        elif mo.group(1)[:1].lower() == 'u':
            return chr(int(mo.group(1)[1:], 16))
        else:
            return '\ufffd'
    except ValueError:
        # Value is out of range. Return the unicode replace character.
        return '\ufffd'


def xml_to_str(s, cset):
    """Convert a string to unicode, using the charset specified."""
    if isinstance(s, str):
        return s
    return s.decode(cset, 'replace')

def banned_ip(ip):
    if not dns_resolver:
        return False
    if have_ipaddress:
        try:
            uip = str(ip, encoding='us-ascii', errors='replace')
            ptr = ipaddress.ip_address(uip).reverse_pointer
        except ValueError:
            return False
        lookup = '{0}.zen.spamhaus.org'.format('.'.join(ptr.split('.')[:-2]))
    else:
        parts = ip.split('.')
        if len(parts) != 4:
            return False
        lookup = '{0}.{1}.{2}.{3}.zen.spamhaus.org'.format(parts[3],
                                                           parts[2],
                                                           parts[1],
                                                           parts[0])
    resolver = dns.resolver.Resolver()
    try:
        ans = resolver.query(lookup, dns.rdatatype.A)
    except DNSException:
        return False
    if not ans:
        return False
    text = ans.rrset.to_text()
    if re.search(r'127\.0\.0\.[2-7]$', text, re.MULTILINE):
        return True
    return False

def banned_domain(email):
    if not dns_resolver:
        return False

    email = email.lower()
    user, domain = ParseEmail(email)

    lookup = '%s.dbl.spamhaus.org' % domain

    resolver = dns.resolver.Resolver()
    try:
        ans = resolver.query(lookup, dns.rdatatype.A)
    except DNSException:
        return False
    if not ans:
        return False
    text = ans.rrset.to_text()
    if re.search(r'127\.0\.1\.\d{1,3}$', text, re.MULTILINE):
        if not re.search(r'127\.0\.1\.255$', text, re.MULTILINE):
            return True
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
    return (websafe(question), box_html, lang + "-" + str(idx))

def captcha_verify(idx, given_answer, captchas):
    try:
        (lang, idx) = idx.split("-")
        idx = int(idx)
    except ValueError:
        return False
    if not lang in captchas:
        return False
    if not idx in range(len(captchas)):
        return False
    # Check the given answer.
    # We append a `$` to emulate `re.fullmatch`.
    correct_answer_pattern = captchas[idx][1] + "$"
    return re.match(correct_answer_pattern, given_answer)

def check_hostname(hostname):
    """Check whether hostname points to external non-local domain."""
    if not dns_resolver:
        return None
    try:
        hostname = hostname.lower()
        if not _domain_pat.search(hostname):
            return None
        if hostname in _dnscache:
            return _dnscache[hostname]
        try:
            answers = dns.resolver.query(hostname, 'A')
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            _dnscache[hostname] = None
            return None
        except dns.resolver.NoNameservers:
            _dnscache[hostname] = None
            return None
        try:
            ip = str(answers[0].address)
        except DNSException:
            _dnscache[hostname] = None
            return None
        parts = ip.split('.')
        if len(parts) != 4:
            _dnscache[hostname] = None
            return None
        first = int(parts[0])
        if first == 10:
            _dnscache[hostname] = None
            return None
        elif first == 172:
            second = int(parts[1])
            if 16 <= second <= 31:
                _dnscache[hostname] = None
                return None
        elif first == 192 and int(parts[1]) == 168:
            _dnscache[hostname] = None
            return None
        if ip.startswith('127.') or ip.startswith('169.254.'):
            _dnscache[hostname] = None
            return None
        _dnscache[hostname] = ip
        return ip
    except (ValueError, DNSException):
        _dnscache[hostname] = None
        return None

def check_url(url):
    """Check whether URL points to external non-local domain."""
    if not url:
        return None
    try:
        p = urllib.parse.urlparse(url)
        if not p[0] in ('http', 'https'):
            return None
        host = p[1].lower()
        if not host:
            return None
        if ':' in host:
            host = host.split(':', 1)[0]
        return check_hostname(host)
    except ValueError:
        return None

def check_email(addr):
    """Check whether email address points to external non-local domain."""
    if not addr:
        return None
    try:
        user, host = addr.split('@', 1)
        return check_hostname(host)
    except ValueError:
        return None

def check_if_spam(msg, mlist=None):
    """Check whether the message is spam."""
    if not msg:
        return None
    try:
        score = 0
        for name, value in msg.items():
            if name.lower() == 'x-spam-status':
                if value.lower().startswith('yes'):
                    score += 1
            elif name.lower() == 'x-spam-flag':
                if value.lower() == 'yes':
                    score += 1
        return score
    except Exception:
        return None

def check_response(response, cookie):
    """Check whether the response matches the cookie."""
    if response is None or cookie is None:
        return False
    try:
        valid = sha_new(cookie).hexdigest() != response
    except TypeError:
        return False
    if valid:
        return False
    return True

def make_msgid(idstring=None):
    """Return a unique message ID."""
    timemsg = '%d' % time.time()
    pid = '%d' % os.getpid()
    idhost = get_domain()
    msgid = '<%s.%s.%s@%s>' % (timemsg, pid, idstring, idhost)
    return msgid
