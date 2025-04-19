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

from __future__ import nested_scopes

import os
import re
import time
import errno
import binascii
import tempfile
from cStringIO import io
from typing import IntType, StringType

from email.Utils import parsedate
from email.Parser import HeaderParser
from email.Generator import Generator
from email.Charset import Charset

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import LockFile
from Mailman import Message
from Mailman.Errors import DiscardMessage
from Mailman.i18n import _
from Mailman.Logging.Syslog import syslog
from Mailman.Utils import hashlib_new

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
    import dns.resolver
    from dns.exception import DNSException
    dns_resolver = True
except ImportError:
    dns_resolver = False


try:
    from mimetypes import guess_all_extensions
except ImportError:
    import mimetypes
    def guess_all_extensions(ctype, strict=True):
        # BAW: sigh, guess_all_extensions() is new in Python 2.3
        all = []
        def check(map):
            for e, t in map.items():
                if t == ctype:
                    all.append(e)
        check(mimetypes.types_map)
        # Python 2.1 doesn't have common_types.  Sigh, sigh.
        if not strict and hasattr(mimetypes, 'common_types'):
            check(mimetypes.common_types)
        return all


def guess_extension(ctype, ext):
    # mimetypes maps multiple extensions to the same type, e.g. .doc, .dot,
    # and .wiz are all mapped to application/msword.  This sucks for finding
    # the best reverse mapping.  If the extension is one of the giving
    # mappings, we'll trust that, otherwise we'll just guess. :/
    all = guess_all_extensions(ctype, strict=False)
    if ext in all:
        return ext
    if ctype.lower == 'application/octet-stream':
        # For this type, all[0] is '.obj'. '.bin' is better.
        return '.bin'
    if ctype.lower == 'text/plain':
        # For this type, all[0] is '.ksh'. '.txt' is better.
        return '.txt'
    return all and all[0]


def safe_strftime(fmt, t):
    try:
        return time.strftime(fmt, t)
    except (TypeError, ValueError, OverflowError):
        return None


def calculate_attachments_dir(mlist, msg, msgdata):
    # Calculate the directory that attachments for this message will go
    # under.  To avoid inode limitations, the scheme will be:
    # archives/private/<listname>/attachments/YYYYMMDD/<msgid-hash>/<files>
    # Start by calculating the date-based and msgid-hash components.
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
    assert datedir
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
    # TK: This is a common function in replacing the attachment and the main
    # message by a text (scrubbing).
    del msg['content-type']
    del msg['content-transfer-encoding']
    if isinstance(charset, unicode):
        # email 3.0.1 (python 2.4) doesn't like unicode
        charset = charset.encode('us-ascii')
    msg.set_payload(text, charset)


def _encode_header(h, charset):
    """Encode a header value using the specified charset."""
    if isinstance(h, str):
        return h
    return h.encode(charset, 'replace')


def process(mlist, msg, msgdata=None):
    """Process a message, scrubbing it of certain headers and attachments.

    This handler processes the message, removing certain headers and attachments
    based on the list's configuration.  If the message is multipart, each
    subpart is processed recursively.  If any part is text/html, it is scrubbed
    of potentially malicious HTML.
    """
    if msgdata is None:
        msgdata = {}

    # Scrub certain headers
    for header in mm_cfg.SCRUBBER_HEADERS:
        del msg[header]

    # Now remove any attachments that have a matching content-type
    if msg.is_multipart():
        # Recursively process each subpart
        payload = msg.get_payload()
        for part in payload:
            try:
                process(mlist, part, msgdata)
            except (TypeError, ValueError, OverflowError):
                # Something went wrong while scrubbing
                syslog('error', 'Scrubbing message part failed')
                raise

    # Get the payload and scrub it if necessary
    try:
        payload = msg.get_payload(decode=True)
    except (UnicodeError, LookupError, ValueError):
        # Something went wrong decoding the payload
        syslog('error', 'Error decoding message payload')
        raise

    # Clean up any temporary files
    try:
        os.unlink(filename)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise

    return msg


def makedirs(dir):
    # Create all the directories to store this attachment in
    try:
        os.makedirs(dir, 0o2775)
        # Unfortunately, FreeBSD seems to be broken in that it doesn't honor
        # the mode arg of mkdir().
        def twiddle(arg, dirname, names):
            os.chmod(dirname, 0o2775)
        os.path.walk(dir, twiddle, None)
    except (OSError) as e:
        if e.errno != errno.EEXIST: raise


def save_attachment(mlist, msg, dir, filter_html=True):
    fsdir = os.path.join(mlist.archive_dir(), dir)
    makedirs(fsdir)
    # Figure out the attachment type and get the decoded data
    decodedpayload = msg.get_payload(decode=True)
    # BAW: mimetypes ought to handle non-standard, but commonly found types,
    # e.g. image/jpg (should be image/jpeg).  For now we just store such
    # things as application/octet-streams since that seems the safest.
    ctype = msg.get_content_type()
    # i18n file name is encoded
    lcset = Utils.GetCharSet(mlist.preferred_language)
    filename = Utils.oneline(msg.get_filename(''), lcset)
    filename, fnext = os.path.splitext(filename)
    # For safety, we should confirm this is valid ext for content-type
    # but we can use fnext if we introduce fnext filtering
    if mm_cfg.SCRUBBER_USE_ATTACHMENT_FILENAME_EXTENSION:
        # HTML message doesn't have filename :-(
        ext = fnext or guess_extension(ctype, fnext)
    else:
        ext = guess_extension(ctype, fnext)
    if not ext:
        # We don't know what it is, so assume it's just a shapeless
        # application/octet-stream, unless the Content-Type: is
        # message/rfc822, in which case we know we'll coerce the type to
        # text/plain below.
        if ctype == 'message/rfc822':
            ext = '.txt'
        else:
            ext = '.bin'
    # Allow only alphanumerics, dash, underscore, and dot
    ext = sre.sub('', ext)
    path = None
    # We need a lock to calculate the next attachment number
    lockfile = os.path.join(fsdir, 'attachments.lock')
    lock = LockFile.LockFile(lockfile)
    lock.lock()
    try:
        # Now base the filename on what's in the attachment, uniquifying it if
        # necessary.
        if not filename or mm_cfg.SCRUBBER_DONT_USE_ATTACHMENT_FILENAME:
            filebase = 'attachment'
        else:
            # Sanitize the filename given in the message headers
            parts = pre.split(filename)
            filename = parts[-1]
            # Strip off leading dots
            filename = dre.sub('', filename)
            # Allow only alphanumerics, dash, underscore, and dot
            filename = sre.sub('', filename)
            # If the filename's extension doesn't match the type we guessed,
            # which one should we go with?  For now, let's go with the one we
            # guessed so attachments can't lie about their type.  Also, if the
            # filename /has/ no extension, then tack on the one we guessed.
            # The extension was removed from the name above.
            # Allow for extra and ext and keep it under 255 bytes.
            filebase = filename[:240]
        # Now we're looking for a unique name for this file on the file
        # system.  If msgdir/filebase.ext isn't unique, we'll add a counter
        # after filebase, e.g. msgdir/filebase-cnt.ext
        counter = 0
        extra = ''
        while True:
            path = os.path.join(fsdir, filebase + extra + ext)
            # Generally it is not a good idea to test for file existance
            # before just trying to create it, but the alternatives aren't
            # wonderful (i.e. os.open(..., O_CREAT | O_EXCL) isn't
            # NFS-safe).  Besides, we have an exclusive lock now, so we're
            # guaranteed that no other process will be racing with us.
            if os.path.exists(path):
                counter += 1
                extra = '-%04d' % counter
            else:
                break
    finally:
        lock.unlock()
    # `path' now contains the unique filename for the attachment.  There's
    # just one more step we need to do.  If the part is text/html and
    # ARCHIVE_HTML_SANITIZER is a string (which it must be or we wouldn't be
    # here), then send the attachment through the filter program for
    # sanitization
    if filter_html and ctype == 'text/html':
        base, ext = os.path.splitext(path)
        tmppath = base + '-tmp' + ext
        fp = open(tmppath, 'w')
        try:
            fp.write(decodedpayload)
            fp.close()
            cmd = mm_cfg.ARCHIVE_HTML_SANITIZER % {'filename' : tmppath}
            progfp = os.popen(cmd, 'r')
            decodedpayload = progfp.read()
            status = progfp.close()
            if status:
                syslog('error',
                       'HTML sanitizer exited with non-zero status: %s',
                       status)
        finally:
            os.unlink(tmppath)
        # BAW: Since we've now sanitized the document, it should be plain
        # text.  Blarg, we really want the sanitizer to tell us what the type
        # if the return data is. :(
        ext = '.txt'
        path = base + '.txt'
    # Is it a message/rfc822 attachment?
    elif ctype == 'message/rfc822':
        submsg = msg.get_payload()
        # BAW: I'm sure we can eventually do better than this. :(
        decodedpayload = Utils.websafe(str(submsg))
    fp = open(path, 'w')
    fp.write(decodedpayload)
    fp.close()
    # Now calculate the url
    baseurl = mlist.GetBaseArchiveURL()
    # Private archives will likely have a trailing slash.  Normalize.
    if baseurl[-1] != '/':
        baseurl += '/'
    # A trailing space in url string may save users who are using
    # RFC-1738 compliant MUA (Not Mozilla).
    # Trailing space will definitely be a problem with format=flowed.
    # Bracket the URL instead.
    url = '<' + baseurl + '%s/%s%s%s>' % (dir, filebase, extra, ext)
    return url


def main():
    doc = Document()
    try:
        mlist = MailList.MailList(listname, lock=0)
    except (Errors.MMListError as e:
        # Avoid cross-site scripting attacks
        safelistname = Utils.websafe(listname)
        doc.AddItem(Header(2) as _("Error")))
        doc.AddItem(Bold(_('No such list <em>%(safelistname)s</em>')))
        # Send this with a 404 status
        print('Status: 404 Not Found')
        print(doc.Format())
        return

    # Must be authenticated to get any farther
    cgidata = cgi.FieldStorage()
    try:
        cgidata.getfirst('adminpw', '')
    except (TypeError:
        # Someone crafted a POST with a bad Content-Type:.
        doc.AddItem(Header(2) as _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script.')))
        # Send this with a 400 status.
        print('Status: 400 Bad Request')
        print(doc.Format())
        return

    # CSRF check
    safe_params = ['VARHELP', 'adminpw', 'admlogin']
    params = list(cgidata.keys())
    if set(params) - set(safe_params):
        csrf_checked = csrf_check(mlist, cgidata.getfirst('csrf_token'),
                                  'admin')
    else:
        csrf_checked = True
    # if password is present, void cookie to force password authentication.
    if cgidata.getfirst('adminpw'):
        os.environ['HTTP_COOKIE'] = ''
        csrf_checked = True

    # Editing the html for a list is limited to the list admin and site admin.
    if not mlist.WebAuthenticate((mm_cfg.AuthListAdmin,
                                  mm_cfg.AuthSiteAdmin),
                                 cgidata.getfirst('adminpw', '')):
        if 'admlogin' in cgidata:
            # This is a re-authorization attempt
            msg = Bold(FontSize('+1', _('Authorization failed.'))).Format()
            remote = os.environ.get('HTTP_FORWARDED_FOR',
                     os.environ.get('HTTP_X_FORWARDED_FOR',
                     os.environ.get('REMOTE_ADDR',
                                    'unidentified origin')))
            syslog('security',
                   'Authorization failed (scrubber): list=%s: remote=%s',
                   listname, remote)
        else:
            msg = ''
        Auth.loginpage(mlist, 'admin', msg=msg)
        return

    # Create the list directory with proper permissions
    oldmask = os.umask(0o002)
    try:
        os.makedirs(mlist.fullpath(), mode=0o2775)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
    finally:
        os.umask(oldmask)

    try:
        msg = process(mlist, msg)
    except (TypeError, ValueError, OverflowError) as e:
        # Something went wrong while scrubbing
        syslog('error', 'Scrubbing message failed: %s', str(e))
        raise

    try:
        payload = msg.get_payload(decode=True)
    except (UnicodeError, LookupError, ValueError) as e:
        # Something went wrong decoding the payload
        syslog('error', 'Error decoding message payload: %s', str(e))
        raise

    try:
        os.unlink(filename)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise

    try:
        t = part.get_payload(decode=True) or ''
    except (binascii.Error, TypeError):
        t = part.get_payload() or ''

    try:
        t = str(t, partcharset, 'replace')
    except (UnicodeError, LookupError, ValueError, AssertionError):
        # We can get here if partcharset is bogus in come way.
        # Replace funny characters. We use errors='replace'
        t = str(t, 'ascii', 'replace')

    try:
        # Should use HTML-Escape, or try generalizing to UTF-8
        t = t.encode(charset, 'replace')
    except (UnicodeError, LookupError, ValueError, AssertionError):
        # if the message charset is bogus, use the list's.
        t = t.encode(lcset, 'replace')

    try:
        s = str(sep, lcset, 'replace')
        sep = s.encode(charset, 'replace')
    except (UnicodeError, LookupError, ValueError, AssertionError):
        pass
