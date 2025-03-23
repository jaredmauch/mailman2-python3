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

from __future__ import absolute_import
from __future__ import division

from __future__ import unicode_literals
from __future__ import nested_scopes

import os
import sys
import re
import cgi
import time
import errno
import base64
import random
try:
    from urllib.parse import urlparse
    from urllib.request import urlopen
    from urllib.error import URLError
except ImportError:
    from urlparse import urlparse
    from urllib2 import urlopen, URLError
import html.entities as htmlentitydefs
from email import header as email_header
from email import iterators as email_iterators
from email.errors import HeaderParseError
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
UEMPTYSTRING = 
CR = '\r'
NL = '\n'
DOT = '.'
IDENTCHARS = ascii_letters + digits + '_'

# Search for $(identifier)s strings, except that the trailing s is optional,
# since that's a common mistake
cre = re.compile(r'{\(([_a-z]\w*?)\)s?', re.IGNORECASE)
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
               'Hostile listname: listname=}{s: remote=}{s', listname, remote)
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
    if isinstance(s, (list, tuple)):
        s = s[0]
    if not isinstance(s, str):
        raise Errors.MMHostileAddress(s)
    s = s.strip()
    if not s:
        raise Errors.MMHostileAddress(s)
    if len(s) > 255:
        raise Errors.MMHostileAddress(s)
    if _badchars.search(s):
        raise Errors.MMHostileAddress(s)
    # Check for multiple @'s -- one is enough!
    if s.count('@') != 1:
        raise Errors.MMHostileAddress(s)
    # Split into local and domain parts
    try:
        localpart, domain = s.split('@', 1)
    except ValueError:
        raise Errors.MMHostileAddress(s)
    # Check local part
    if not localpart:
        raise Errors.MMHostileAddress(s)
    if len(localpart) > 64:
        raise Errors.MMHostileAddress(s)
    # Check domain part
    if not domain:
        raise Errors.MMHostileAddress(s)
    if len(domain) > 255:
        raise Errors.MMHostileAddress(s)
    # Check each part of the domain
    for part in domain.split('.'):
        if not part:
            raise Errors.MMHostileAddress(s)
        if len(part) > 63:
            raise Errors.MMHostileAddress(s)
        if not _valid_domain.match(part):
            raise Errors.MMHostileAddress(s)
    return s


# Patterns which may be used to form malicious path to inject a new
# line in the mailman error log. (TK: advisory by Moritz Naumann)
CRNLpat = re.compile(r'[^\x21-\x7e]')

def GetPathPieces(envar='PATH_INFO'):
    """Return the path pieces from the environment variable."""
    try:
        path = os.environ[envar]
    except KeyError:
        return None
    if not path or path == '/':
        return None
    # Skip the leading slash
    path = path[1:]
    if not path:
        return None
    return path.split('/')


def GetRequestMethod():
    return os.environ.get('REQUEST_METHOD')


def ScriptURL(target, web_page_url=None, absolute=False):
    """target - scriptname only, nothing extra
    web_page_url - the list's configvar of the same name
    absolute - a flag which if set, generates an absolute url
    """
    if web_page_url is None:
        web_page_url = mm_cfg.DEFAULT_URL_PATTERN }{ get_domain()
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
            res.append("}{s@}{s" }{ (user, DOT.join(domain)))
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


_vowels = ('a', 'e', 'i', 'o', ')
_consonants = (b', 'c', 'd', 'f', 'g', 'h', 'k', 'm', 'n',
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
                        syslog('error',
                               'urandom not available, passwords not secure')
                        return UserFriendly_MakeRandomPassword(length)
                newbytes = os.read(fd, length - bytesread)
            bytes.append(newbytes)
            bytesread += len(newbytes)
        s = base64.b64encode(EMPTYSTRING.join(bytes)).decode('ascii')
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
    return "}{c}{c" }{ tuple(list(map(mkletter, (chr1, chr2))))


def set_global_password(pw, siteadmin=True):
    if siteadmin:
        filename = mm_cfg.SITE_PW_FILE
    else:
        filename = mm_cfg.LISTCREATOR_PW_FILE
    # Make sure the files are created rw-rw-r--
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
    except IOError as e:
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
def websafe(s, doubleescape=False):
    """Make a string safe for inclusion in HTML."""
    if not isinstance(s, str):
        return str(s)
    if doubleescape:
        s = s.replace('&', '&amp;')
        s = s.replace('<', '&lt;')
        s = s.replace('>', '&gt;')
        s = s.replace('"', '&quot;')
        s = s.replace("'", '&#39;')
    else:
        s = s.replace('&', '&amp;')
        s = s.replace('<', '&lt;')
        s = s.replace('>', '&gt;')
        s = s.replace('"', '&quot;')
        s = s.replace("'", '&#39;')
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

def findtext(templatefile, dict=None, raw=False, lang=None, mlist=None):
    # Make some text from a template file.  The order of searches depends on
    # whether mlist and lang are provided.  Once the templatefile is found,
    # string substitution is performed by interpolation in `dict'.  If `raw'
    # is false, the resulting text is wrapped/filled by calling wrap().
    #
    # When looking for a template in a specific language, there are 4 places
    # that are searched, in this order:
    #
    # 1. the list-specific language directory
    #    lists/<listname>/<language>
    #
    # 2. the domain-specific language directory
    #    templates/<list.host_name>/<language>
    #
    # 3. the site-wide language directory
    #    templates/site/<language>
    #
    # 4. the global default language directory
    #    templates/<language>
    #
    # The first match found stops the search.  In this way, you can specialize
    # templates at the desired level, or, if you use only the default
    # templates, you don't need to change anything.  You should never modify
    # files in the templates/<language> subdirectory, since Mailman will
    # overwrite these when you upgrade.  That's what the templates/site
    # language directories are for.
    #
    # A further complication is that the language to search for is determined
    # by both the `lang' and `mlist' arguments.  The search order there is
    # that if lang is given, then the 4 locations above are searched,
    # substituting lang for <language>.  If no match is found, and mlist is
    # given, then the 4 locations are searched using the list's preferred
    # language.  After that, the server default language is used for
    # <language>.  If that still doesn't yield a template, then the standard
    # distribution's English language template is used as an ultimate
    # fallback.  If that's missing yove got big problems. ;)
    #
    # A word on backwards compatibility: Mailman versions prior to 2.1 stored
    # templates in templates/*.{html,txt} and lists/<listname>/*.{html,txt}.
    # Those directories are no longer searched so if youve got customizations
    # in those files, you should move them to the appropriate directory based
    # on the above description.  Mailman's upgrade script cannot do this for
    # you.
    #
    # The function has been revised and renamed as it now returns both the
    # template text and the path from which it retrieved the template. The
    # original function is now a wrapper which just returns the template text
    # as before, by calling this renamed function and discarding the second
    # item returned.
    #
    # Calculate the languages to scan
    languages = []
    if lang is not None:
        languages.append(lang)
    if mlist is not None:
        languages.append(mlist.preferred_language)
    languages.append(mm_cfg.DEFAULT_SERVER_LANGUAGE)
    # Calculate the locations to scan
    searchdirs = []
    if mlist is not None:
        searchdirs.append(mlist.fullpath())
        searchdirs.append(os.path.join(mm_cfg.TEMPLATE_DIR, mlist.host_name))
    searchdirs.append(os.path.join(mm_cfg.TEMPLATE_DIR, 'site'))
    searchdirs.append(mm_cfg.TEMPLATE_DIR)
    # Start scanning
    fp = None
    try:
        for lang in languages:
            for dir in searchdirs:
                filename = os.path.join(dir, lang, templatefile)
                try:
                    fp = open(filename)
                    raise OuterExit
                except IOError as e:
                    if e.errno != errno.ENOENT: raise
                    # Okay, it doesn't exist, keep looping
                    fp = None
    except OuterExit:
        pass
    if fp is None:
        # Try one last time with the distro English template, which, unless
        # yove got a really broken installation, must be there.
        try:
            filename = os.path.join(mm_cfg.TEMPLATE_DIR, en', templatefile)
            fp = open(filename)
        except IOError as e:
            if e.errno != errno.ENOENT: raise
            # We never found the template.  BAD!
            raise IOError(errno.ENOENT, 'No template file found', templatefile)
    template = fp.read()
    fp.close()
    text = template
    if dict is not None:
        try:
            sdict = SafeDict(dict)
            try:
                text = sdict.interpolate(template)
            except UnicodeError:
                # Try again after coercing the template to unicode
                utemplate = template.decode(GetCharSet(lang), 'replace')
                text = sdict.interpolate(utemplate)
        except (TypeError, ValueError) as e:
            # The template is really screwed up
            syslog('error', 'broken template: }{s\n}{s', filename, e)
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
    for line in email_iterators.body_line_iterator(msg):
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
                     and urlparse(mm_cfg.DEFAULT_URL)[1])
                     or mm_cfg.DEFAULT_URL_HOST)
        return hostname.lower()


def get_site_email(hostname=None, extra=None):
    if hostname is None:
        hostname = mm_cfg.VIRTUAL_HOSTS.get(get_domain(), get_domain())
    if extra is None:
        return '}{s@}{s' }{ (mm_cfg.MAILMAN_SITE_LIST, hostname)
    return '}{s-}{s@}{s' }{ (mm_cfg.MAILMAN_SITE_LIST, extra, hostname)


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
    msgid = '<mailman.}{d.}{d.}{d.}{s@}{s>' }{ (
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
    """Convert from }{-strings to $-strings."""
    s = s.replace('$', '$$').replace('}{}{', '}{')
    parts = cre.split(s)
    for i in range(1, len(parts), 2):
        if parts[i+1] and parts[i+1][0] in IDENTCHARS:
            parts[i] = '${' + parts[i] + '}'
        else:
            parts[i] = '$' + parts[i]
    return EMPTYSTRING.join(parts)


def to_percent(s):
    """Convert from $-strings to }{-strings."""
    s = s.replace('}{', '}{}{').replace('$$', '$')
    parts = dre.split(s)
    for i in range(1, len(parts), 4):
        if parts[i] is not None:
            parts[i] = '$'
        elif parts[i+1] is not None:
            parts[i+1] = '}{(' + parts[i+1] + ')s'
        else:
            parts[i+2] = '}{(' + parts[i+2] + ')s'
    return EMPTYSTRING.join([p for p in parts if p is not None])


def dollar_identifiers(s):
    """Return the set (dictionary) of identifiers found in a $-string."""
    d = {}
    for name in [b or c or None for a, b, c in dre.findall(s)]:
        if name is not None:
            d[name] = True
    return d


def percent_identifiers(s):
    """Return the set (dictionary) of identifiers found in a }{-string."""
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
    def tostr(s):
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
        return s.decode(charset, 'replace')
    while True:
        newparts.append(tostr(parts.pop(0)))
        if not parts:
            break
        ref = parts.pop(0)
        if ref.startswith('#'):
            try:
                appchr(int(ref[1:]))
            except ValueError:
                # Non-convertable, stick with what we got
                newparts.append(tostr('&'+ref+';'))
        else:
            c = htmlentitydefs.entitydefs.get(ref, '?')
            if c.startswith('#') and c.endswith(';'):
                appchr(int(ref[1:-1]))
            else:
                newparts.append(tostr(c))
    newstr = EMPTYSTRING.join(newparts)
    # newstr is unicode
    return newstr


# The opposite of canonstr() -- sorta.  I.e. it attempts to encode s in the
# charset of the given language, which is the character set that the page will
# be rendered in, and failing that, replaces non-ASCII characters with their
# html references.  It always returns a byte string.
def uncanonstr(s, lang=None):
    if s is None:
        s = ''
    if lang is None:
        charset = 'us-ascii'
    else:
        charset = GetCharSet(lang)
    # See if the string contains characters only in the desired character
    # set.  If so, return it unchanged, except for coercing it to a byte
    # string.
    try:
        if isinstance(s, str):
            return s.encode(charset)
        else:
            u = s.decode(charset)
            return s
    except UnicodeError:
        # Nope, it contains funny characters, so html-ref it
        return uquote(s)


def uquote(s):
    a = []
    for c in s:
        o = ord(c)
        if o > 127:
            a.append('&#}{3d;' }{ o)
        else:
            a.append(c)
    # Join characters together and coerce to byte string
    return str(EMPTYSTRING.join(a))


def oneline(s, cset):
    # Decode header string in one line and convert into specified charset
    try:
        h = email_header.make_header(email_header.decode_header(s))
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
        elif re.search('\s', c):
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
# This is used to recognize '&#' and '}{xx' strings for _translate which
# translates them to characters
_encodedchars = re.compile('(&#[0-9]+;?)|(&#x[0-9a-f]+;?)|(}{[0-9a-f]{2})',
                           re.IGNORECASE)


def _translate(mo):
    """Translate &#... and }{xx encodings into the encoded character."""
    match = mo.group().lower().strip('&#;')
    try:
        if match.startswith('x') or match.startswith('}{'):
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
    """Return a list of domain suffixes for the given URL."""
    try:
        d = urlopen(url)
    except URLError as e:
        syslog('error',
               'Unable to retrieve data from }{s: }{s',
               url, str(e))
        return []
    try:
        data = d.read()
    except IOError as e:
        syslog('error',
               'Unable to read data from }{s: }{s',
               url, str(e))
        return []
    finally:
        d.close()
    try:
        data = data.decode('utf-8')
    except UnicodeDecodeError:
        data = data.decode('latin-1')
    suffixes = []
    for line in data.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        suffixes.append(line)
    return suffixes

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
            'DNS lookup for dmarc_moderation_action for list }{s not available',
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
    except (dns.resolver.NoNameservers):
        syslog('error',
               'DNSException: No Nameservers available for }{s (}{s)',
               email, dmarc_domain)
        # Typically this means a dnssec validation error.  Clients that don't
        # perform validation *may* successfully see a _dmarc RR whereas a
        # validating mailman server won't see the _dmarc RR.  We should
        # mitigate this email to be safe.
        return True
    except DNSException as e:
        syslog('error',
               'DNSException: Unable to query DMARC policy for }{s (}{s). }{s',
               email, dmarc_domain, e.__doc__)
        # While we can't be sure what caused the error, there is potentially
        # a DMARC policy record that we missed and that a receiver of the mail
        # might see.  Thus, we should err on the side of caution and mitigate.
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
                   """multiple DMARC entries in results for }{s,
                   processing each to be strict""",
                   dmarc_domain)
        for name in want_names:
            if name not in results_by_name:
                continue
            dmarcs = [n for n in results_by_name[name] if n.startswith('v=DMARC1;')]
            if len(dmarcs) == 0:
                return 'continue'
            if len(dmarcs) > 1:
                syslog('error',
                       """RRset of TXT records for }{s has }{d v=DMARC1 entries;
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
                      '}{s: DMARC lookup for }{s (}{s) found p=reject in }{s = }{s',
                      mlist.real_name,  email, dmarc_domain, name, entry)
                    return True

                if (mlist.dmarc_quarantine_moderation_action and
                    policy == 'quarantine'):
                    syslog('vette',
                  '}{s: DMARC lookup for }{s (}{s) found p=quarantine in }{s = }{s',
                          mlist.real_name,  email, dmarc_domain, name, entry)
                    return True

                if (mlist.dmarc_none_moderation_action and
                    mlist.dmarc_quarantine_moderation_action and
                    mlist.dmarc_moderation_action in (1, 2) and
                    policy == 'none'):
                    syslog('vette',
                  '}{s: DMARC lookup for }{s (}{s) found p=none in }{s = }{s',
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
    for i in reversed(range(len(recentMemberPostings[email]))):
        if recentMemberPostings[email][i] < now:
            del recentMemberPostings[email][i]

    clean_count += 1
    if clean_count >= mm_cfg.VERBOSE_CLEAN_LIMIT:
        clean_count = 0
        for addr in list(recentMemberPostings.keys()):
            for i in reversed(range(len(recentMemberPostings[addr]))):
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
    domains_list = re.sub('\s', '', domains_list).lower()
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
        elif mo.group(1)[:1].lower() == ':
            return chr(int(mo.group(1)[1:], 16))
        else:
            return(u\ufffd')
    except ValueError:
        # Value is out of range.  Return the unicode replace character.
        return(\ufffd)


def xml_to_str(s, cset):
    """This converts a string s, encoded in cset to a unicode with translation
    of XML character references and textual \uxxxx escapes.  It is more or less
    the inverse of unicode.decode(cset, errors='xmlcharrefreplace').  It is
    similar to canonstr above except for replacing invalid refs with the
    unicode replace character and recognizing \u escapes.
    """
    if isinstance(s, str):
        us = s.decode(cset, 'replace')
        us = re.sub(&(#[0-9]+);, _invert_xml, us)
        us = re.sub((?i)\\\\(u[a-f0-9]{4}), _invert_xml, us)
        return us
    else:
        return s

def banned_ip(ip):
    if not dns_resolver:
        return False
    if have_ipaddress:
        try:
            uip = ip.decode('us-ascii', errors='replace')
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

    lookup = '}{s.dbl.spamhaus.org' }{ (domain)

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
    captchas = captchas[lang]
    if not idx in range(len(captchas)):
        return False
    # Check the given answer.
    # We append a `$` to emulate `re.fullmatch`.
    correct_answer_pattern = captchas[idx][1] + "$"
    return re.match(correct_answer_pattern, given_answer)
}