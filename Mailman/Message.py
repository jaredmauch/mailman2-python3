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

"""Standard Mailman message object.

This is a subclass of email.message but provides a slightly extended interface
which is more convenient for use inside Mailman.
"""

import re
from io import StringIO

import email
import email.generator
import email.utils
from email.charset import Charset
from email.header import Header

from Mailman import mm_cfg
from Mailman import Utils

COMMASPACE = ', '

if hasattr(email, '__version__'):
    mo = re.match(r'([\d.]+)', email.__version__)
else:
    mo = re.match(r'([\d.]+)', '2.1.39') # XXX should use @@MM_VERSION@@ perhaps?
VERSION = tuple([int(s) for s in mo.group().split('.')])



class Generator(email.generator.Generator):
    """Generates output from a Message object tree, keeping signatures.

       Headers will by default _not_ be folded in attachments.
    """
    def __init__(self, outfp, mangle_from_=True,
                 maxheaderlen=78, children_maxheaderlen=0):
        email.generator.Generator.__init__(self, outfp,
                mangle_from_=mangle_from_, maxheaderlen=maxheaderlen)
        self.__children_maxheaderlen = children_maxheaderlen

    def clone(self, fp):
        """Clone this generator with maxheaderlen set for children"""
        return self.__class__(fp, self._mangle_from_,
                self.__children_maxheaderlen, self.__children_maxheaderlen)



class Message(email.message.Message):
    def __init__(self):
        # We need a version number so that we can optimize __setstate__()
        self.__version__ = VERSION
        email.message.Message.__init__(self)

    # BAW: For debugging w/ bin/dumpdb.  Apparently pprint uses repr.
    def __repr__(self):
        return self.__str__()

    def __setstate__(self, d):
        # The base class attributes have changed over time.  Which could
        # affect Mailman if messages are sitting in the queue at the time of
        # upgrading the email package.  We shouldn't burden email with this,
        # so we handle schema updates here.
        self.__dict__ = d
        # We know that email 2.4.3 is up-to-date
        version = d.get('__version__', (0, 0, 0))
        d['__version__'] = VERSION
        if version >= VERSION:
            return
        # Messages grew a _charset attribute between email version 0.97 and 1.1
        if '_charset' not in d:
            self._charset = None
        # Messages grew a _default_type attribute between v2.1 and v2.2
        if '_default_type' not in d:
            # We really have no idea whether this message object is contained
            # inside a multipart/digest or not, so I think this is the best we
            # can do.
            self._default_type = 'text/plain'
        # Header instances used to allow both strings and Charsets in their
        # _chunks, but by email 2.4.3 now it's just Charsets.
        headers = []
        hchanged = 0
        for k, v in self._headers:
            if isinstance(v, Header):
                chunks = []
                cchanged = 0
                for s, charset in v._chunks:
                    if type(charset) == str:
                        charset = Charset(charset)
                        cchanged = 1
                    chunks.append((s, charset))
                if cchanged:
                    v._chunks = chunks
                    hchanged = 1
            headers.append((k, v))
        if hchanged:
            self._headers = headers

    # I think this method ought to eventually be deprecated
    def get_sender(self, use_envelope=None, preserve_case=0):
        """Return the address considered to be the author of the email.

        This can return either the From: header, the Sender: header or the
        envelope header (a.k.a. the unixfrom header).  The first non-empty
        header value found is returned.  However the search order is
        determined by the following:

        - If mm_cfg.USE_ENVELOPE_SENDER is true, then the search order is
          Sender:, From:, unixfrom

        - Otherwise, the search order is From:, Sender:, unixfrom

        The optional argument use_envelope, if given overrides the
        mm_cfg.USE_ENVELOPE_SENDER setting.  It should be set to either 0 or 1
        (don't use None since that indicates no-override).

        unixfrom should never be empty.  The return address is always
        lowercased, unless preserve_case is true.

        This method differs from get_senders() in that it returns one and only
        one address, and uses a different search order.
        """
        senderfirst = mm_cfg.USE_ENVELOPE_SENDER
        if use_envelope is not None:
            senderfirst = use_envelope
        if senderfirst:
            headers = ('sender', 'from')
        else:
            headers = ('from', 'sender')
        for h in headers:
            # Use only the first occurrance of Sender: or From:, although it's
            # not likely there will be more than one.
            fieldval = self[h]
            if not fieldval:
                continue
            # Work around bug in email 2.5.8 (and ?) involving getaddresses()
            # from multi-line header values.
            # Don't use Utils.oneline() here because the header must not be
            # decoded before parsing since the decoded header may contain
            # an unquoted comma or other delimiter in a real name.
            fieldval = ''.join(fieldval.splitlines())
            addrs = email.utils.getaddresses([fieldval])
            try:
                realname, address = addrs[0]
            except IndexError:
                continue
            if address:
                break
        else:
            # We didn't find a non-empty header, so let's fall back to the
            # unixfrom address.  This should never be empty, but if it ever
            # is, it's probably a Really Bad Thing.  Further, we just assume
            # that if the unixfrom exists, the second field is the address.
            unixfrom = self.get_unixfrom()
            if unixfrom:
                address = unixfrom.split()[1]
            else:
                # TBD: now what?!
                address = ''
        if not preserve_case:
            return address.lower()
        return address

    def get_senders(self, preserve_case=0, headers=None):
        """Return a list of addresses representing the author of the email.

        The list will contain the following addresses (in order)
        depending on availability:

        1. From:
        2. unixfrom
        3. Reply-To:
        4. Sender:

        The return addresses are always lower cased, unless `preserve_case' is
        true.  Optional `headers' gives an alternative search order, with None
        meaning, search the unixfrom header.  Items in `headers' are field
        names without the trailing colon.
        """
        if headers is None:
            headers = mm_cfg.SENDER_HEADERS
        pairs = []
        for h in headers:
            if h is None:
                # get_unixfrom() returns None if there's no envelope
                fieldval = self.get_unixfrom() or ''
                try:
                    pairs.append(('', fieldval.split()[1]))
                except IndexError:
                    # Ignore badly formatted unixfroms
                    pass
            else:
                fieldvals = self.get_all(h)
                if fieldvals:
                    # See comment above in get_sender() regarding
                    # getaddresses() and multi-line headers
                    fieldvals = [''.join(fv.splitlines())
                                 for fv in fieldvals]
                    pairs.extend(email.utils.getaddresses(fieldvals))
        authors = []
        for pair in pairs:
            address = pair[1]
            if address is not None and not preserve_case:
                address = address.lower()
            authors.append(address)
        return authors

    def get_filename(self, failobj=None):
        """Some MUA have bugs in RFC2231 filename encoding and cause
        Mailman to stop delivery in Scrubber.py (called from ToDigest.py).
        """
        try:
            filename = email.message.Message.get_filename(self, failobj)
            return filename
        except (UnicodeError, LookupError, ValueError):
            return failobj


    def as_string(self, unixfrom=False, mangle_from_=True):
        """Return entire formatted message as a string using
        Mailman.Message.Generator.

        Operates like email.message.Message.as_string, only
        using Mailman's Message.Generator class. Only the top headers will
        get folded.
        """
        fp = StringIO()
        g = Generator(fp, mangle_from_=mangle_from_)
        g.flatten(self, unixfrom=unixfrom)
        return fp.getvalue()



class UserNotification(Message):
    """Class for internally crafted messages."""

    def __init__(self, recip, sender, subject=None, text=None, lang=None):
        Message.__init__(self)
        charset = None
        if lang is not None:
            charset = Charset(Utils.GetCharSet(lang))
        if text is not None:
            self.set_payload(text, charset)
        if subject is None:
            subject = '(no subject)'
        self['Subject'] = Header(subject, charset, header_name='Subject',
                                 errors='replace')
        self['From'] = sender
        if isinstance(recip, list):
            self['To'] = COMMASPACE.join(recip)
            self.recips = recip
        else:
            self['To'] = recip
            self.recips = [recip]

    def send(self, mlist, noprecedence=False, **_kws):
        """Sends the message by enqueuing it to the `virgin' queue.

        This is used for all internally crafted messages.
        """
        # Since we're crafting the message from whole cloth, let's make sure
        # this message has a Message-ID.  Yes, the MTA would give us one, but
        # this is useful for logging to logs/smtp.
        if 'message-id' not in self:
            self['Message-ID'] = Utils.unique_message_id(mlist)
        # Ditto for Date: which is required by RFC 2822
        if 'date' not in self:
            self['Date'] = email.utils.formatdate(localtime=1)
        # UserNotifications are typically for admin messages, and for messages
        # other than list explosions.  Send these out as Precedence: bulk, but
        # don't override an existing Precedence: header.
        # Also, if the message is To: the list-owner address, set Precedence:
        # list.  See note below in OwnerNotification.
        if not ('precedence' in self or noprecedence):
            if self.get('to') == mlist.GetOwnerEmail():
                self['Precedence'] = 'list'
            else:
                self['Precedence'] = 'bulk'
        self._enqueue(mlist, **_kws)

    def _enqueue(self, mlist, **_kws):
        # Not imported at module scope to avoid import loop
        from Mailman.Queue.sbcache import get_switchboard
        virginq = get_switchboard(mm_cfg.VIRGINQUEUE_DIR)
        # The message metadata better have a `recip' attribute
        virginq.enqueue(self,
                        listname = mlist.internal_name(),
                        recips = self.recips,
                        nodecorate = 1,
                        reduced_list_headers = 1,
                        **_kws)



class OwnerNotification(UserNotification):
    """Like user notifications, but this message goes to the list owners."""

    def __init__(self, mlist, subject=None, text=None, tomoderators=1):
        recips = mlist.owner[:]
        if tomoderators:
            recips.extend(mlist.moderator)
        # We have to set the owner to the site's -bounces address, otherwise
        # we'll get a mail loop if an owner's address bounces.
        sender = Utils.get_site_email(mlist.host_name, 'bounces')
        lang = mlist.preferred_language
        UserNotification.__init__(self, recips, sender, subject, text, lang)
        # Hack the To header to look like it's going to the -owner address
        del self['to']
        self['To'] = mlist.GetOwnerEmail()
        self._sender = sender
        # User notifications are normally sent with Precedence: bulk.  This
        # is appropriate as they can be backscatter of rejected spam.
        # Owner notifications are not backscatter and are perhaps more
        # important than 'bulk' so give them Precedence: list by default.
        # (LP: #1313146)
        self['Precedence'] = 'list'

    def _enqueue(self, mlist, **_kws):
        # Not imported at module scope to avoid import loop
        from Mailman.Queue.sbcache import get_switchboard
        virginq = get_switchboard(mm_cfg.VIRGINQUEUE_DIR)
        # The message metadata better have a `recip' attribute
        virginq.enqueue(self,
                        listname = mlist.internal_name(),
                        recips = self.recips,
                        nodecorate = 1,
                        reduced_list_headers = 1,
                        envsender = self._sender,
                        **_kws)
