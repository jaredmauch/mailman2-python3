# Copyright (C) 2001-2018 by the Free Software Foundation, Inc.
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

"""Cleanse a message for archiving."""

from __future__ import absolute_import, print_function, unicode_literals

import os
import re
import time
import errno
import binascii
import tempfile
from io import StringIO, BytesIO

from email.utils import parsedate
from email.parser import HeaderParser
from email.generator import Generator
from email.charset import Charset

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import LockFile
from Mailman.Message import Message
from Mailman.Errors import DiscardMessage
from Mailman.i18n import _
from Mailman.Logging.Syslog import syslog
from Mailman.Utils import sha_new

# Path characters for common platforms
pre = re.compile(r'[/\\:]')
# All other characters to strip out of Content-Disposition: filenames
# (essentially anything that isn't an alphanum, dot, dash, or underscore).
sre = re.compile(r'[^-\w.]')
# Regexp to strip out leading dots
dre = re.compile(r'^\.*')

BR = '<br>\n'
SPACE = ' '

try:
    from mimetypes import guess_all_extensions
except ImportError:
    import mimetypes
    def guess_all_extensions(ctype, strict=True):
        # BAW: sigh, guess_all_extensions() is new in Python 2.3
        all = []
        def check(map):
            for e, t in list(map.items()):
                if t == ctype:
                    all.append(e)
        check(mimetypes.types_map)
        # Python 2.1 doesn't have common_types.  Sigh, sigh.
        if not strict and hasattr(mimetypes, 'common_types'):
            check(mimetypes.common_types)
        return all


def guess_extension(ctype, ext):
    """Guess the file extension for a content type.
    
    This function handles both strict and non-strict MIME type matching.
    """
    all = guess_all_extensions(ctype, strict=False)
    if ext in all:
        return ext
    if ctype.lower() == 'application/octet-stream':
        # For this type, all[0] is '.obj'. '.bin' is better.
        return '.bin'
    if ctype.lower() == 'text/plain':
        # For this type, all[0] is '.ksh'. '.txt' is better.
        return '.txt'
    return all[0] if all else '.bin'


def safe_strftime(fmt, t):
    """Format time safely, handling invalid timestamps."""
    try:
        return time.strftime(fmt, t)
    except (TypeError, ValueError, OverflowError):
        return None


def calculate_attachments_dir(mlist, msg, msgdata):
    """Calculate the directory for storing message attachments.
    
    Uses a combination of date and message ID to create unique paths.
    """
    fmt = '%Y%m%d'
    datestr = msg.get('Date')
    if datestr:
        now = parsedate(datestr)
    else:
        now = time.gmtime(msgdata.get('received_time', time.time()))
    datedir = safe_strftime(fmt, now)
    if not datedir:
        datestr = msgdata.get('X-List-Received-Date')
        if datestr:
            datedir = safe_strftime(fmt, datestr)
    if not datedir:
        # What next?  Unixfrom, I guess.
        parts = msg.get_unixfrom().split()
        try:
            month = {'Jan':1, 'Feb':2, 'Mar':3, 'Apr':4, 'May':5, 'Jun':6,
                     'Jul':7, 'Aug':8, 'Sep':9, 'Oct':10, 'Nov':11, 'Dec':12,
                     }.get(parts[3], 0)
            day = int(parts[4])
            year = int(parts[6])
        except (IndexError, ValueError):
            # Best we can do I think
            month = day = year = 0
        datedir = '%04d%02d%02d' % (year, month, day)
    if not datedir:
        raise ValueError('Missing datedir parameter')
    # As for the msgid hash, we'll base this part on the Message-ID: so that
    # all attachments for the same message end up in the same directory (we'll
    # uniquify the filenames in that directory as needed).  We use the first 2
    # and last 2 bytes of the SHA1 hash of the message id as the basis of the
    # directory name.  Clashes here don't really matter too much, and that
    # still gives us a 32-bit space to work with.
    msgid = msg['message-id']
    if msgid is None:
        msgid = msg['Message-ID'] = Utils.unique_message_id(mlist)
    # We assume that the message id actually /is/ unique!
    digest = sha_new(msgid).hexdigest()
    return os.path.join('attachments', datedir, digest[:4] + digest[-4:])


def replace_payload_by_text(msg, text, charset):
    """Replace message payload with text using proper charset handling."""
    del msg['content-type']
    del msg['content-transfer-encoding']
    
    # Ensure we have str for text and bytes for charset
    if isinstance(text, bytes):
        text = text.decode('utf-8', 'replace')
    if isinstance(charset, str):
        charset = charset.encode('ascii')
        
    msg.set_payload(text, charset)


def process(mlist, msg, msgdata=None):
    """Process a message for archiving, handling attachments appropriately."""
    sanitize = mm_cfg.ARCHIVE_HTML_SANITIZER
    outer = True
    if msgdata is None:
        msgdata = {}
    if msgdata:
        # msgdata is available if it is in GLOBAL_PIPELINE
        # ie. not in digest or archiver
        # check if the list owner want to scrub regular delivery
        if not mlist.scrub_nondigest:
            return
    dir = calculate_attachments_dir(mlist, msg, msgdata)
    charset = None
    lcset = Utils.GetCharSet(mlist.preferred_language)
    lcset_out = Charset(lcset).output_charset or lcset
    # Now walk over all subparts of this message and scrub out various types
    format = delsp = None
    for part in msg.walk():
        ctype = part.get_content_type()
        # If the part is text/plain, we leave it alone
        if ctype == 'text/plain':
            # We need to choose a charset for the scrubbed message, so we'll
            # arbitrarily pick the charset of the first text/plain part in the
            # message.
            if charset is None:
                charset = part.get_content_charset(lcset)
                format = part.get_param('format')
                delsp = part.get_param('delsp')
            # TK: if part is attached then check charset and scrub if none
            if (part.get('content-disposition', '').lower() == 'attachment'
                    and not part.get_content_charset()):
                omask = os.umask(0o002)
                try:
                    url = save_attachment(mlist, part, dir)
                finally:
                    os.umask(omask)
                filename = part.get_filename(_('not available'))
                filename = Utils.oneline(filename, lcset)
                replace_payload_by_text(part, _("""\
An embedded and charset-unspecified text was scrubbed...
Name: %(filename)s
URL: %(url)s
"""), lcset)
        elif ctype == 'text/html' and isinstance(sanitize, int):
            if sanitize == 0:
                if outer:
                    raise DiscardMessage
                replace_payload_by_text(part,
                                 _('HTML attachment scrubbed and removed'),
                                 lcset)
            elif sanitize == 2:
                # By leaving it alone, Pipermail will automatically escape it
                pass
            elif sanitize == 3:
                # Pull it out as an attachment but leave it unescaped
                omask = os.umask(0o002)
                try:
                    url = save_attachment(mlist, part, dir, filter_html=False)
                finally:
                    os.umask(omask)
                replace_payload_by_text(part, _("""\
An HTML attachment was scrubbed...
URL: %(url)s
"""), lcset)
            else:
                # HTML-escape it and store it as an attachment
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    payload = payload.decode('utf-8', 'replace')
                payload = Utils.websafe(payload)
                # For whitespace in the margin, change spaces into
                # non-breaking spaces, and tabs into 8 of those
                def doreplace(s):
                    return s.expandtabs(8).replace(' ', '&nbsp;')
                lines = [doreplace(s) for s in payload.split('\n')]
                payload = '<tt>\n' + BR.join(lines) + '\n</tt>\n'
                part.set_payload(payload)
                # We're replacing the payload with the decoded payload so this
                # will just get in the way.
                del part['content-transfer-encoding']
                omask = os.umask(0o002)
                try:
                    url = save_attachment(mlist, part, dir, filter_html=False)
                finally:
                    os.umask(omask)
                replace_payload_by_text(part, _("""\
An HTML attachment was scrubbed...
URL: %(url)s
"""), lcset)
        elif ctype == 'message/rfc822':
            # This part contains a submessage, so it too needs scrubbing
            submsg = part.get_payload(0)
            omask = os.umask(0o002)
            try:
                url = save_attachment(mlist, part, dir)
            finally:
                os.umask(omask)
            subject = submsg.get('subject', _('no subject'))
            subject = Utils.oneline(subject, lcset)
            date = submsg.get('date', _('no date'))
            who = submsg.get('from', _('unknown sender'))
            size = len(str(submsg))
            replace_payload_by_text(part, _("""\
An embedded message was scrubbed...
From: %(who)s
Subject: %(subject)s
Date: %(date)s
Size: %(size)s
URL: %(url)s
"""), lcset)
        # If the message isn't a multipart, then we'll strip it out as an
        # attachment that would have to be separately downloaded.  Pipermail
        # will transform the url into a hyperlink.
        elif part.get_payload() and not part.is_multipart():
            payload = part.get_payload(decode=True)
            ctype = part.get_content_type()
            # XXX Under email 2.5, it is possible that payload will be None.
            # This can happen when you have a Content-Type: multipart/* with
            # only one part and that part has two blank lines between the
            # first boundary and the end boundary.  In email 3.0 you end up
            # with a string in the payload.  I think in this case it's safe to
            # ignore the part.
            if payload is None:
                continue
            size = len(payload)
            omask = os.umask(0o002)
            try:
                url = save_attachment(mlist, part, dir)
            finally:
                os.umask(omask)
            desc = part.get('content-description', _('not available'))
            desc = Utils.oneline(desc, lcset)
            filename = part.get_filename(_('not available'))
            filename = Utils.oneline(filename, lcset)
            replace_payload_by_text(part, _("""\
A non-text attachment was scrubbed...
Name: %(filename)s
Type: %(ctype)s
Size: %(size)d bytes
Desc: %(desc)s
URL: %(url)s
"""), lcset)
        outer = False
    # We still have to sanitize multipart messages to flat text because
    # Pipermail can't handle messages with list payloads.  This is a kludge;
    # def (n) clever hack ;).
    if msg.is_multipart():
        # By default we take the charset of the first text/plain part in the
        # message, but if there was none, we'll use the list's preferred
        # language's charset.
        if not charset or charset == 'us-ascii':
            charset = lcset_out
        else:
            # normalize to the output charset if input/output are different
            charset = Charset(charset).output_charset or charset
        # We now want to concatenate all the parts which have been scrubbed to
        # text/plain, into a single text/plain payload.  We need to make sure
        # all the characters in the concatenated string are in the same
        # encoding, so we'll use the 'replace' key in the coercion call.
        # BAW: Martin's original patch suggested we might want to try
        # generalizing to utf-8, and that's probably a good idea (eventually).
        text = []
        for part in msg.walk():
            # TK: bug-id 1099138 and multipart
            # MAS test payload - if part may fail if there are no headers.
            if not part.get_payload() or part.is_multipart():
                continue
            # All parts should be scrubbed to text/plain by now, except
            # if sanitize == 2, there could be text/html parts so keep them
            # but skip any other parts.
            partctype = part.get_content_type()
            if partctype != 'text/plain' and (partctype != 'text/html' or
                                              sanitize != 2):
                text.append(_('Skipped content of type %(partctype)s\n'))
                continue
            try:
                t = part.get_payload(decode=True) or ''
            # MAS: TypeError exception can occur if payload is None. This
            # was observed with a message that contained an attached
            # message/delivery-status part. Because of the special parsing
            # of this type, this resulted in a text/plain sub-part with a
            # null body. See bug 1430236.
            except (binascii.Error, TypeError):
                t = part.get_payload() or ''
            # TK: get_content_charset() returns 'iso-2022-jp' for internally
            # crafted (scrubbed) 'euc-jp' text part. So, first try
            # get_charset(), then get_content_charset() for the parts
            # which are already embeded in the incoming message.
            partcharset = part.get_charset()
            if partcharset:
                partcharset = str(partcharset)
            else:
                partcharset = part.get_content_charset()
            if partcharset and partcharset != charset:
                try:
                    t = str(t, partcharset, 'replace')
                except (UnicodeError, LookupError, ValueError,
                        AssertionError):
                    # We can get here if partcharset is bogus in come way.
                    # Replace funny characters.  We use errors='replace'
                    t = str(t, 'ascii', 'replace')
                try:
                    # Should use HTML-Escape, or try generalizing to UTF-8
                    t = t.encode(charset, 'replace')
                except (UnicodeError, LookupError, ValueError,
                        AssertionError):
                    # if the message charset is bogus, use the list's.
                    t = t.encode(lcset, 'replace')
            # Separation is useful
            if isinstance(t, str):
                if not t.endswith('\n'):
                    t += '\n'
                text.append(t)
        # Now join the text and set the payload
        sep = _('-------------- next part --------------\n')
        # The i18n separator is in the list's charset. Coerce it to the
        # message charset.
        try:
            s = str(sep, lcset, 'replace')
            sep = s.encode(charset, 'replace')
        except (UnicodeError, LookupError, ValueError,
                AssertionError):
            pass
        replace_payload_by_text(msg, sep.join(text), charset)
        if format:
            msg.set_param('Format', format)
        if delsp:
            msg.set_param('DelSp', delsp)
    return msg


def makedirs(dir):
    """Create directory hierarchy safely."""
    try:
        os.makedirs(dir, 0o02775)
        # Unfortunately, FreeBSD seems to be broken in that it doesn't honor
        # the mode arg of mkdir().
        def twiddle(arg, dirname, names):
            os.chmod(dirname, 0o02775)
        os.path.walk(dir, twiddle, None)
    except OSError as e:
        if e.errno != errno.EEXIST: raise


def save_attachment(mlist, msg, dir, filter_html=True):
    """Save a message attachment safely.
    
    Returns the URL where the attachment was saved.
    """
    # Get the attachment filename
    fname = msg.get_filename()
    if not fname:
        fname = msg.get_param('name')
    if not fname:
        # Use content-type if no filename is given
        ctype = msg.get_content_type()
        # Sanitize the content-type so it can be used as a filename
        fname = re.sub(r'[^-\w.]', '_', ctype)
        # Add an extension if possible
        ext = guess_extension(ctype, '')
        if ext:
            fname += ext
    
    # Sanitize the filename
    fname = re.sub(r'[/\\:]', '_', fname)
    fname = re.sub(r'[^-\w.]', '_', fname)
    fname = re.sub(r'^\.*', '_', fname)
    
    # Get the attachment content
    payload = msg.get_payload(decode=True)
    if not payload:
        return None
    
    # Create attachment directory
    dir = os.path.join(mlist.archive_dir(), dir)
    makedirs(dir)
    
    # Save the attachment
    path = None
    counter = 0
    while True:
        if counter:
            fname_parts = os.path.splitext(fname)
            fname = '%s-%d%s' % (fname_parts[0], counter, fname_parts[1])
        path = os.path.join(dir, fname)
        try:
            # Open in binary mode and write bytes directly
            with open(path, 'wb') as fp:
                fp.write(payload)
            break
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
            counter += 1
    
    # Make the file group writable
    os.chmod(path, 0o0664)
    
    # Return the URL
    baseurl = mlist.GetBaseArchiveURL()
    url = '%s/%s/%s' % (baseurl, dir, fname)
    return url
