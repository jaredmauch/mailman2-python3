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

"""HyperArch: Pipermail archiving for Mailman

     - The Dragon De Monsyne <dragondm@integral.org>

   TODO:
     - Should be able to force all HTML to be regenerated next time the
       archive is run, in case a template is changed.
     - Run a command to generate tarball of html archives for downloading
       (probably in the 'update_dirty_archives' method).
"""

import sys
import re
import errno
import urllib.request, urllib.parse, urllib.error
import time
import os
import types
from . import HyperDatabase
from . import pipermail
import weakref
import binascii

from email.header import decode_header, make_header
from email.errors import HeaderParseError
from email.charset import Charset

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
from Mailman import LockFile
from Mailman import MailList
from Mailman import i18n
from Mailman.SafeDict import SafeDict
from Mailman.Logging.Syslog import syslog
from Mailman.Mailbox import ArchiverMailbox

# Set up i18n.  Assume the current language has already been set in the caller.
_ = i18n._
C_ = i18n.C_

gzip = None
if mm_cfg.GZIP_ARCHIVE_TXT_FILES:
    try:
        import gzip
    except ImportError:
        pass

EMPTYSTRING = ''
NL = '\n'

# MacOSX has a default stack size that is too small for deeply recursive
# regular expressions.  We see this as crashes in the Python test suite when
# running test_re.py and test_sre.py.  The fix is to set the stack limit to
# 2048; the general recommendation is to do in the shell before running the
# test suite.  But that's inconvenient for a daemon like the qrunner.
#
# AFAIK, this problem only affects the archiver, so we're adding this work
# around to this file (it'll get imported by the bundled pipermail or by the
# bin/arch script.  We also only do this on darwin, a.k.a. MacOSX.
if sys.platform == 'darwin':
    try:
        import resource
    except ImportError:
        pass
    else:
        soft, hard = resource.getrlimit(resource.RLIMIT_STACK)
        newsoft = min(hard, max(soft, 1024*2048))
        resource.setrlimit(resource.RLIMIT_STACK, (newsoft, hard))


def html_quote(s, lang=None):
    repls = ( ('&', '&amp;'),
              ("<", '&lt;'),
              (">", '&gt;'),
              ('"', '&quot;'))
    for thing, repl in repls:
        s = s.replace(thing, repl)
    return Utils.uncanonstr(s, lang)


def url_quote(s):
    return urllib.parse.quote(s)


def null_to_space(s):
    return s.replace('\000', ' ')


def sizeof(filename, lang):
    try:
        size = os.path.getsize(filename)
    except OSError as e:
        # ENOENT can happen if the .mbox file was moved away or deleted, and
        # an explicit mbox file name was given to bin/arch.
        if e.errno != errno.ENOENT: raise
        return _('size not available')
    if size < 1000:
        # Avoid i18n side-effects
        otrans = i18n.get_translation()
        try:
            i18n.set_language(lang)
            out = _(' %(size)i bytes ')
        finally:
            i18n.set_translation(otrans)
        return out
    elif size < 1000000:
        return ' %d KiB ' % (size / 1024)
    # GB?? :-)
    return ' %d MiB ' % (size/  (1024*1024))


html_charset = '<META http-equiv="Content-Type" ' \
               'content="text/html; charset=%s">'

def CGIescape(arg, lang=None):
    if isinstance(arg, str):
        s = Utils.websafe(arg)
    else:
        s = Utils.websafe(str(arg))
    return Utils.uncanonstr(s.replace('"', '&quot;'), lang)

# Parenthesized human name
paren_name_pat = re.compile(r'([(].*[)])')

# Subject lines preceded with 'Re:'
REpat = re.compile( r"\s*RE\s*(\[\d+\]\s*)?:\s*", re.IGNORECASE)

# E-mail addresses and URLs in text
emailpat = re.compile(r'([-+,.\w]+@[-+.\w]+)')

#  Argh!  This pattern is buggy, and will choke on URLs with GET parameters.
# MAS: Given that people are not constrained in how they write URIs in plain
# text, it is not possible to have a single regexp to reliably match them.
# The regexp below is intended to match straightforward cases.  Even humans
# can't reliably tell whether various punctuation at the end of a URI is part
# of the URI or not.
urlpat = re.compile(r'([a-z]+://.*?)(?:_\s|_$|$|[]})>\'"\s])', re.IGNORECASE)

# Blank lines
blankpat = re.compile(r'^\s*$')

# Starting <html> directive
htmlpat = re.compile(r'^\s*<HTML>\s*$', re.IGNORECASE)
# Ending </html> directive
nohtmlpat = re.compile(r'^\s*</HTML>\s*$', re.IGNORECASE)
# Match quoted text
quotedpat = re.compile(r'^([>|:]|&gt;)+')


# Like Utils.maketext() but with caching to improve performance.
#
# _templatefilepathcache is used to associate a (templatefile, lang, listname)
# key with the file system path to a template file.  This path is the one that
# the Utils.findtext() function has computed is the one to match the values in
# the key tuple.
#
# _templatecache associate a file system path as key with the text
# returned after processing the contents of that file by Utils.findtext()
#
# We keep two caches to reduce the amount of template text kept in memory,
# since the _templatefilepathcache is a many->one mapping and _templatecache
# is a one->one mapping.  Imagine 1000 lists all using the same default
# English template.

_templatefilepathcache = {}
_templatecache = {}

def quick_maketext(templatefile, dict=None, lang=None, mlist=None):
    if mlist is None:
        listname = ''
    else:
        listname = mlist._internal_name
    if lang is None:
        if mlist is None:
            lang = mm_cfg.DEFAULT_SERVER_LANGUAGE
        else:
            lang = mlist.preferred_language
    cachekey = (templatefile, lang, listname)
    filepath =  _templatefilepathcache.get(cachekey)
    if filepath:
        template = _templatecache.get(filepath)
    if filepath is None or template is None:
        # Use the basic maketext, with defaults to get the raw template
        template, filepath = Utils.findtext(templatefile, lang=lang,
                                            raw=True, mlist=mlist)
        _templatefilepathcache[cachekey] = filepath
        _templatecache[filepath] = template
    # Copied from Utils.maketext()
    text = template
    if dict is not None:
        try:
            sdict = SafeDict(dict)
            try:
                text = sdict.interpolate(template)
            except UnicodeError:
                # Try again after coercing the template to unicode
                utemplate = str(template,
                                    Utils.GetCharSet(lang),
                                    'replace')
                text = sdict.interpolate(utemplate)
        except (TypeError, ValueError) as e:
            # The template is really screwed up
            syslog('error', 'broken template: %s\n%s', filepath, e)
    # Make sure the text is in the given character set, or html-ify any bogus
    # characters.
    return Utils.uncanonstr(text, lang)


# Note: I'm overriding most, if not all of the pipermail Article class
#       here -ddm
# The Article class encapsulates a single posting.  The attributes are:
#
#  sequence : Sequence number, unique for each article in a set of archives
#  subject  : Subject
#  datestr  : The posting date, in human-readable format
#  date     : The posting date, in purely numeric format
#  fromdate : The posting date, in `unixfrom' format
#  headers  : Any other headers of interest
#  author   : The author's name (and possibly organization)
#  email    : The author's e-mail address
#  msgid    : A unique message ID
#  in_reply_to : If !="", this is the msgid of the article being replied to
#  references: A (possibly empty) list of msgid's of earlier articles in
#              the thread
#  body     : A list of strings making up the message body

class Article(pipermail.Article):
    __super_init = pipermail.Article.__init__
    __super_set_date = pipermail.Article._set_date

    _last_article_time = time.time()

    def __init__(self, message, sequence, keepHeaders=0,
                 lang=mm_cfg.DEFAULT_SERVER_LANGUAGE, mlist=None):
        self.__super_init(message, sequence, keepHeaders)
        self.prev = None
        self.next = None
        # Trim Re: from the subject line
        i = 0
        while i != -1:
            result = REpat.match(self.subject)
            if result:
                i = result.end(0)
                self.subject = self.subject[i:]
                if self.subject == '':
                    self.subject = _('No subject')
            else:
                i = -1
        # Useful to keep around
        self._lang = lang
        self._mlist = mlist

        if mm_cfg.ARCHIVER_OBSCURES_EMAILADDRS:
            # Avoid i18n side-effects.  Note that the language for this
            # article (for this list) could be different from the site-wide
            # preferred language, so we need to ensure no side-effects will
            # occur.  Think what happens when executing bin/arch.
            otrans = i18n.get_translation()
            try:
                i18n.set_language(lang)
                if self.author == self.email:
                    self.author = self.email = re.sub('@', _(' at '),
                                                      self.email, flags=re.IGNORECASE)
                else:
                    self.email = re.sub('@', _(' at '), self.email, flags=re.IGNORECASE)
            finally:
                i18n.set_translation(otrans)

        # Get content type and encoding
        ctype = message.get_content_type()
        cenc = message.get('Content-Transfer-Encoding', '')
        self.ctype = ctype.lower()
        self.cenc = cenc.lower()
        self.decoded = {}
        cset = Utils.GetCharSet(mlist.preferred_language)
        cset_out = Charset(cset).output_charset or cset
        if isinstance(cset_out, str):
            # email 3.0.1 (python 2.4) doesn't like unicode
            cset_out = cset_out.encode('us-ascii')
        charset = message.get_content_charset()
        if charset:
            charset = charset.lower().strip()
            if charset[0]=='"' and charset[-1]=='"':
                charset = charset[1:-1]
            if charset[0]=="'" and charset[-1]=="'":
                charset = charset[1:-1]
            try:
                body = message.get_body().get_content()
            except (binascii.Error, AttributeError):
                body = None
            if body and charset != Utils.GetCharSet(self._lang):
                # decode body
                try:
                    body = str(body, charset)
                except (UnicodeError, LookupError):
                    body = None
            if body:
                self.body = [l + "\n" for l in body.splitlines()]

        self.decode_headers()

    # Mapping of listnames to MailList instances as a weak value dictionary.
    # This code is copied from Runner.py but there's one important operational
    # difference.  In Runner.py, we always .Load() the MailList object for
    # each _dispose() run, otherwise the object retrieved from the cache won't
    # be up-to-date.  Since we're creating a new HyperArchive instance for
    # each message being archived, we don't need to worry about that -- but it
    # does mean there are additional opportunities for optimization.
    _listcache = weakref.WeakValueDictionary()

    def _open_list(self, listname):
        # Cache the open list so that any use of the list within this process
        # uses the same object.  We use a WeakValueDictionary so that when the
        # list is no longer necessary, its memory is freed.
        mlist = self._listcache.get(listname)
        if not mlist:
            try:
                mlist = MailList.MailList(listname, lock=0)
            except Errors.MMListError as e:
                syslog('error', 'error opening list: %s\n%s', listname, e)
                return None
            else:
                self._listcache[listname] = mlist
        return mlist

    def __getstate__(self):
        d = self.__dict__.copy()
        # We definitely don't want to pickle the MailList instance, so just
        # pickle a reference to it.
        if '_mlist' in d:
            mlist = d['_mlist']
            del d['_mlist']
        else:
            mlist = None
        if mlist:
            d['__listname'] = self._mlist.internal_name()
        else:
            d['__listname'] = None
        # Delete a few other things we don't want in the pickle
        for attr in ('prev', 'next', 'body'):
            if attr in d:
                del d[attr]
        d['body'] = []
        return d

    def __setstate__(self, d):
        # For loading older Articles via pickle.  All this stuff was added
        # when Simone Piunni and Tokio Kikuchi i18n'ified Pipermail.  See SF
        # patch #594771.
        self.__dict__ = d
        listname = d.get('__listname')
        if listname:
            del d['__listname']
            d['_mlist'] = self._open_list(listname)
        if '_lang' not in d:
            if hasattr(self, '_mlist'):
                self._lang = self._mlist.preferred_language
            else:
                self._lang = mm_cfg.DEFAULT_SERVER_LANGUAGE
        if 'cenc' not in d:
            self.cenc = None
        if 'decoded' not in d:
            self.decoded = {}

    def setListIfUnset(self, mlist):
        if getattr(self, '_mlist', None) is None:
            self._mlist = mlist

    def quote(self, buf):
        return html_quote(buf, self._lang)

    def decode_headers(self):
        """MIME-decode headers.

        If the email, subject, or author attributes contain non-ASCII
        characters using the encoded-word syntax of RFC 2047, decoded versions
        of those attributes are placed in the self.decoded (a dictionary).

        If the list's charset differs from the header charset, an attempt is
        made to decode the headers as Unicode.  If that fails, they are left
        undecoded.
        """
        author = self.decode_charset(self.author)
        subject = self.decode_charset(self.subject)
        if author:
            self.decoded['author'] = author
            email = self.decode_charset(self.email)
            if email:
                self.decoded['email'] = email
        if subject:
            if mm_cfg.ARCHIVER_OBSCURES_EMAILADDRS:
                otrans = i18n.get_translation()
                try:
                    i18n.set_language(self._lang)
                    atmark = str(_(' at '), Utils.GetCharSet(self._lang))
                    subject = re.sub(r'([-+,.\w]+)@([-+.\w]+)',
                              r'\g<1>' + atmark + r'\g<2>', subject, flags=re.IGNORECASE)
                finally:
                    i18n.set_translation(otrans)
            self.decoded['subject'] = subject
        self.decoded['stripped'] = self.strip_subject(subject or self.subject)

    def strip_subject(self, subject):
        # Strip subject_prefix and Re: for subject sorting
        # This part was taken from CookHeaders.py (TK)
        prefix = self._mlist.subject_prefix.strip()
        if prefix:
            prefix_pat = re.escape(prefix)
            prefix_pat = '%'.join(prefix_pat.split(r'\%'))
            prefix_pat = re.sub(r'%\d*d', r'\s*\d+\s*', prefix_pat, flags=re.IGNORECASE)
            subject = re.sub(prefix_pat, '', subject, flags=re.IGNORECASE)
        subject = subject.lstrip()
        # MAS Should we strip FW and FWD too?
        strip_pat = re.compile(r'^((RE|AW|SV|VS)(\[\d+\])?:\s*)+', re.I)
        stripped = strip_pat.sub('', subject)
        # Also remove whitespace to avoid folding/unfolding differences
        stripped = re.sub(r'\s', '', stripped, flags=re.IGNORECASE)
        return stripped

    def decode_charset(self, field):
        # TK: This function was rewritten for unifying to Unicode.
        # Convert 'field' into Unicode one line string.
        try:
            if isinstance(field, str):
                return field
            pairs = decode_header(field)
            ustr = str(make_header(pairs))
        except (LookupError, UnicodeError, ValueError, HeaderParseError):
            # assume list's language
            cset = Utils.GetCharSet(self._mlist.preferred_language)
            if cset == 'us-ascii':
                cset = 'iso-8859-1' # assume this for English list
            ustr = str(field, cset, 'replace')
        return u''.join(ustr.splitlines())

    def as_html(self):
        d = self.__dict__.copy()
        # avoid i18n side-effects
        otrans = i18n.get_translation()
        i18n.set_language(self._lang)
        try:
            d["prev"], d["prev_wsubj"] = self._get_prev()
            d["next"], d["next_wsubj"] = self._get_next()

            d["email_html"] = self.quote(self.email)
            d["title"] = self.quote(self.subject)
            d["subject_html"] = self.quote(self.subject)
            d["message_id"] = self.quote(self._message_id)
            # TK: These two _url variables are used to compose a response
            # from the archive web page.  So, ...
            d["subject_url"] = url_quote('Re: ' + self.subject)
            d["in_reply_to_url"] = url_quote(self._message_id)
            if mm_cfg.ARCHIVER_OBSCURES_EMAILADDRS:
                # Point the mailto url back to the list
                author = re.sub('@', _(' at '), self.author, flags=re.IGNORECASE)
                emailurl = self._mlist.GetListEmail()
            else:
                author = self.author
                emailurl = self.email
            d["author_html"] = self.quote(author)
            d["email_url"] = url_quote(emailurl)
            d["datestr_html"] = self.quote(i18n.ctime(int(self.date)))
            d["body"] = self._get_body()
            d['listurl'] = self._mlist.GetScriptURL('listinfo', absolute=1)
            d['listname'] = self._mlist.real_name
            d['encoding'] = ''
        finally:
            i18n.set_translation(otrans)

        charset = Utils.GetCharSet(self._lang)
        d["encoding"] = html_charset % charset

        self._add_decoded(d)
        return quick_maketext(
             'article.html', d,
             lang=self._lang, mlist=self._mlist)

    def _get_prev(self):
        """Return the href and subject for the previous message"""
        if self.prev:
            subject = self._get_subject_enc(self.prev)
            prev = ('<LINK REL="Previous"  HREF="%s">'
                    % (url_quote(self.prev.filename)))
            prev_wsubj = ('<LI>' + _('Previous message (by thread):') +
                          ' <A HREF="%s">%s\n</A></li>'
                          % (url_quote(self.prev.filename),
                             self.quote(subject)))
        else:
            prev = prev_wsubj = ""
        return prev, prev_wsubj

    def _get_subject_enc(self, art):
        """Return the subject of art, decoded if possible.

        If the charset of the current message and art match and the
        article's subject is encoded, decode it.
        """
        return art.decoded.get('subject', art.subject)

    def _get_next(self):
        """Return the href and subject for the previous message"""
        if self.__next__:
            subject = self._get_subject_enc(self.__next__)
            next = ('<LINK REL="Next"  HREF="%s">'
                    % (url_quote(self.next.filename)))
            next_wsubj = ('<LI>' + _('Next message (by thread):') +
                          ' <A HREF="%s">%s\n</A></li>'
                          % (url_quote(self.next.filename),
                             self.quote(subject)))
        else:
            next = next_wsubj = ""
        return next, next_wsubj

    _rx_quote = re.compile('=([A-F0-9][A-F0-9])')
    _rx_softline = re.compile('=[ \t]*$')

    def _get_body(self):
        """Return the message body as HTML."""
        if not self.body:
            return ''
        # Convert the body to HTML
        body = []
        for line in self.body:
            # Handle HTML content
            if self.ctype == 'text/html':
                body.append(line)
            else:
                # Convert plain text to HTML
                line = self.quote(line)
                if self.SHOWBR:
                    body.append(line + '<br>\n')
                else:
                    body.append(line + '\n')
        return ''.join(body)

    def _add_decoded(self, d):
        """Add encoded-word keys to HTML output"""
        for src, dst in (('author', 'author_html'),
                         ('email', 'email_html'),
                         ('subject', 'subject_html'),
                         ('subject', 'title')):
            if src in self.decoded:
                d[dst] = self.quote(self.decoded[src])

    def as_text(self):
        """Return the message as plain text."""
        if not self.body:
            return ''
        # Convert the body to plain text
        body = []
        for line in self.body:
            # Handle HTML content
            if self.ctype == 'text/html':
                # Strip HTML tags
                line = re.sub(r'<[^>]*>', '', line)
            body.append(line)
        return ''.join(body)

    def _set_date(self, message):
        """Set the date from the message."""
        try:
            date = message.get('Date')
            if date:
                self.date = time.mktime(email.utils.parsedate_tz(date)[:9])
            else:
                self.date = time.time()
        except (TypeError, ValueError):
            self.date = time.time()
        self.datestr = time.ctime(self.date)

    def loadbody_fromHTML(self,fileobj):
        self.body = []
        begin = 0
        while 1:
            line = fileobj.readline()
            if not line:
                break
            if not begin:
                if line.strip() == '<!--beginarticle-->':
                    begin = 1
                continue
            if line.strip() == '<!--endarticle-->':
                break
            self.body.append(line)

    def finished_update_article(self):
        self.body = []
        try:
            del self.html_body
        except AttributeError:
            pass


class HyperArchive(pipermail.T):
    __super_init = pipermail.T.__init__
    __super_update_archive = pipermail.T.update_archive
    __super_update_dirty_archives = pipermail.T.update_dirty_archives
    __super_add_article = pipermail.T.add_article

    # some defaults
    DIRMODE = 0o02775
    FILEMODE = 0o0660

    VERBOSE = 0
    DEFAULTINDEX = 'thread'
    ARCHIVE_PERIOD = 'month'

    THREADLAZY = 0
    THREADLEVELS = 3

    ALLOWHTML = 1             # "Lines between <html></html>" handled as is.
    SHOWHTML = 0              # Eg, nuke leading whitespace in html manner.
    IQUOTES = 1               # Italicize quoted text.
    SHOWBR = 0                # Add <br> onto every line

    def __init__(self, maillist):
        # can't init the database while other processes are writing to it!
        # XXX TODO- implement native locking
        # with mailman's LockFile module for HyperDatabase.HyperDatabase
        #
        dir = maillist.archive_dir()
        self.database = HyperDatabase.HyperDatabase(dir, maillist)
        self.__super_init(dir, reload=1, database=self.database)

        self.maillist = maillist
        self._lock_file = None
        self.lang = maillist.preferred_language
        self.charset = Utils.GetCharSet(maillist.preferred_language)

        if hasattr(self.maillist,'archive_volume_frequency'):
            if self.maillist.archive_volume_frequency == 0:
                self.ARCHIVE_PERIOD='year'
            elif self.maillist.archive_volume_frequency == 2:
                self.ARCHIVE_PERIOD='quarter'
            elif self.maillist.archive_volume_frequency == 3:
                self.ARCHIVE_PERIOD='week'
            elif self.maillist.archive_volume_frequency == 4:
                self.ARCHIVE_PERIOD='day'
            else:
                self.ARCHIVE_PERIOD='month'

        yre = r'(?P<year>[0-9]{4,4})'
        mre = r'(?P<month>[01][0-9])'
        dre = r'(?P<day>[0123][0-9])'
        self._volre = {
            'year':    '^' + yre + '$',
            'quarter': '^' + yre + r'q(?P<quarter>[1234])$',
            'month':   '^' + yre + r'-(?P<month>[a-zA-Z]+)$',
            'week':    r'^Week-of-Mon-' + yre + mre + dre,
            'day':     '^' + yre + mre + dre + '$'
            }

    def _makeArticle(self, msg, sequence):
        return Article(msg, sequence,
                       lang=self.maillist.preferred_language,
                       mlist=self.maillist)

    def html_foot(self):
        # avoid i18n side-effects
        mlist = self.maillist
        otrans = i18n.get_translation()
        i18n.set_language(mlist.preferred_language)
        # Convenience
        def quotetime(s):
            return html_quote(i18n.ctime(s), self.lang)
        try:
            d = {"lastdate": quotetime(self.lastdate),
                 "archivedate": quotetime(self.archivedate),
                 "listinfo": mlist.GetScriptURL('listinfo', absolute=1),
                 "version": self.version,
                 "listname": html_quote(mlist.real_name, self.lang),
                 }
            i = {"thread": _("thread"),
                 "subject": _("subject"),
                 "author": _("author"),
                 "date": _("date")
                 }
        finally:
            i18n.set_translation(otrans)

        for t in list(i.keys()):
            cap = t[0].upper() + t[1:]
            if self.type == cap:
                d["%s_ref" % (t)] = ""
            else:
                d["%s_ref" % (t)] = ('<a href="%s.html#start">[ %s ]</a>'
                                     % (t, i[t]))
        return quick_maketext(
            'archidxfoot.html', d,
            mlist=mlist)

    def html_head(self):
        # avoid i18n side-effects
        mlist = self.maillist
        otrans = i18n.get_translation()
        i18n.set_language(mlist.preferred_language)
        # Convenience
        def quotetime(s):
            return html_quote(i18n.ctime(s), self.lang)
        try:
            d = {"listname": html_quote(mlist.real_name, self.lang),
                 "archtype": self.type,
                 "archive":  self.volNameToDesc(self.archive),
                 "listinfo": mlist.GetScriptURL('listinfo', absolute=1),
                 "firstdate": quotetime(self.firstdate),
                 "lastdate": quotetime(self.lastdate),
                 "size": self.size,
                 }
            i = {"thread": _("thread"),
                 "subject": _("subject"),
                 "author": _("author"),
                 "date": _("date"),
                 }
        finally:
            i18n.set_translation(otrans)

        for t in list(i.keys()):
            cap = t[0].upper() + t[1:]
            if self.type == cap:
                d["%s_ref" % (t)] = ""
                d["archtype"] = i[t]
            else:
                d["%s_ref" % (t)] = ('<a href="%s.html#start">[ %s ]</a>'
                                     % (t, i[t]))
        if self.charset:
            d["encoding"] = html_charset % self.charset
        else:
            d["encoding"] = ""
        return quick_maketext(
            'archidxhead.html', d,
            mlist=mlist)

    def html_TOC(self):
        mlist = self.maillist
        listname = mlist.internal_name()
        mbox = os.path.join(mlist.archive_dir()+'.mbox', listname+'.mbox')
        d = {"listname": mlist.real_name,
             "listinfo": mlist.GetScriptURL('listinfo', absolute=1),
             "fullarch": '../%s.mbox/%s.mbox' % (listname, listname),
             "size": sizeof(mbox, mlist.preferred_language),
             'meta': '',
             }
        # Avoid i18n side-effects
        otrans = i18n.get_translation()
        i18n.set_language(mlist.preferred_language)
        try:
            if not self.archives:
                d["noarchive_msg"] = _(
                    '<P>Currently, there are no archives. </P>')
                d["archive_listing_start"] = ""
                d["archive_listing_end"] = ""
                d["archive_listing"] = ""
            else:
                d["noarchive_msg"] = ""
                d["archive_listing_start"] = quick_maketext(
                    'archliststart.html',
                    lang=mlist.preferred_language,
                    mlist=mlist)
                d["archive_listing_end"] = quick_maketext(
                    'archlistend.html',
                    mlist=mlist)

                accum = []
                for a in self.archives:
                    accum.append(self.html_TOC_entry(a))
                d["archive_listing"] = EMPTYSTRING.join(accum)
        finally:
            i18n.set_translation(otrans)
        # The TOC is always in the charset of the list's preferred language
        d['meta'] += html_charset % Utils.GetCharSet(mlist.preferred_language)
        # The site can disable public access to the mbox file.
        if mm_cfg.PUBLIC_MBOX:
            template = 'archtoc.html'
        else:
            template = 'archtocnombox.html'
        return quick_maketext(template, d, mlist=mlist)

    def html_TOC_entry(self, arch):
        # Check to see if the archive is gzip'd or not
        txtfile = os.path.join(self.maillist.archive_dir(), arch + '.txt')
        gzfile = txtfile + '.gz'
        # which exists?  .txt.gz first, then .txt
        if os.path.exists(gzfile):
            file = gzfile
            url = arch + '.txt.gz'
            templ = '<td><A href="%(url)s">[ ' + _('Gzip\'d Text%(sz)s') \
                    + ']</a></td>'
        elif os.path.exists(txtfile):
            file = txtfile
            url = arch + '.txt'
            templ = '<td><A href="%(url)s">[ ' + _('Text%(sz)s') + ']</a></td>'
        else:
            # neither found?
            file = None
        # in Python 1.5.2 we have an easy way to get the size
        if file:
            textlink = templ % {
                'url': url,
                'sz' : sizeof(file, self.maillist.preferred_language)
                }
        else:
            # there's no archive file at all... hmmm.
            textlink = ''
        return quick_maketext(
            'archtocentry.html',
            {'archive': arch,
             'archivelabel': self.volNameToDesc(arch),
             'textlink': textlink
             },
            mlist=self.maillist)

    def GetArchLock(self):
        if self._lock_file:
            return 1
        self._lock_file = LockFile.LockFile(
            os.path.join(mm_cfg.LOCK_DIR,
                         self.maillist.internal_name() + '-arch.lock'))
        try:
            self._lock_file.lock(timeout=0.5)
        except LockFile.TimeOutError:
            return 0
        return 1

    def DropArchLock(self):
        if self._lock_file:
            self._lock_file.unlock(unconditionally=1)
            self._lock_file = None

    def processListArch(self):
        name = self.maillist.ArchiveFileName()
        wname= name+'.working'
        ename= name+'.err_unarchived'
        try:
            os.stat(name)
        except (IOError,os.error):
            #no archive file, nothin to do -ddm
            return

        #see if arch is locked here -ddm
        if not self.GetArchLock():
            #another archiver is running, nothing to do. -ddm
            return

        #if the working file is still here, the archiver may have
        # crashed during archiving. Save it, log an error, and move on.
        try:
            wf = open(wname)
            syslog('error',
                   'Archive working file %s present.  '
                   'Check %s for possibly unarchived msgs',
                   wname, ename)
            omask = os.umask(0o007)
            try:
                ef = open(ename, 'a+')
            finally:
                os.umask(omask)
            ef.seek(1,2)
            if ef.read(1) != '\n':
                ef.write('\n')
            ef.write(wf.read())
            ef.close()
            wf.close()
            os.unlink(wname)
        except IOError:
            pass
        os.rename(name,wname)
        archfile = open(wname)
        self.processUnixMailbox(archfile)
        archfile.close()
        os.unlink(wname)
        self.DropArchLock()

    def processUnixMailbox(self, archfile):
        """Process a Unix mailbox file."""
        from email import message_from_file
        from mailbox import mbox
        
        # If archfile is a file object, we need to read it directly
        if hasattr(archfile, 'read'):
            # Read the entire file content
            content = archfile.read()
            # Create a temporary file to store the content
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w+', delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            
            try:
                # Process the temporary file
                mbox = mbox(tmp_path)
                for key in mbox.keys():
                    msg = message_from_file(mbox.get_file(key))
                    self.add_article(msg)
            finally:
                # Clean up the temporary file
                os.unlink(tmp_path)
        else:
            # If it's a path, use it directly
            mbox = mbox(archfile)
            for key in mbox.keys():
                msg = message_from_file(mbox.get_file(key))
                self.add_article(msg)

    def format_article(self, article):
        """Format an article for HTML display."""
        # Get the message body
        body = article.get_body()
        if body is None:
            return article

        # Convert body to lines
        if isinstance(body, str):
            lines = body.splitlines()
        else:
            lines = [line.decode('utf-8', 'replace') for line in body.splitlines()]

        # Handle HTML content
        if article.ctype == 'text/html':
            article.html_body = lines
        else:
            # Process plain text
            processed_lines = []
            for line in lines:
                # Handle quoted text
                if self.IQUOTES and quotedpat.match(line):
                    line = '<i>' + CGIescape(line, self.lang) + '</i>'
                else:
                    line = CGIescape(line, self.lang)
                if self.SHOWBR:
                    line += '<br>'
                processed_lines.append(line)

            # Add HTML structure
            if not self.SHOWHTML:
                processed_lines.insert(0, '<pre>')
                processed_lines.append('</pre>')
            article.html_body = processed_lines

        return article
