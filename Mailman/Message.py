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

This is a subclass of email.Message but provides a slightly extended interface
which is more convenient for use inside Mailman.
"""

import re
import io
from typing import List, Optional, Union

import email
import email.generator as generator
import email.message
import email.utils
from email.charset import Charset
from email.header import Header

from Mailman import mm_cfg
from Mailman import Utils

COMMASPACE = ', '

# We're in Python 3, so we can simplify this
VERSION = tuple(int(x) for x in email.__version__.split('.'))


class Generator(generator.Generator):
    """Generates output from a Message object tree, keeping signatures.

    Headers will by default _not_ be folded in attachments.
    """
    def __init__(self, outfp: io.TextIOBase,
                 mangle_from_: bool = True,
                 maxheaderlen: int = 78,
                 children_maxheaderlen: int = 0) -> None:
        super().__init__(outfp, mangle_from_=mangle_from_,
                        maxheaderlen=maxheaderlen)
        self.__children_maxheaderlen = children_maxheaderlen

    def clone(self, fp: io.TextIOBase) -> 'Generator':
        """Clone this generator with maxheaderlen set for children"""
        return self.__class__(fp, self._mangle_from_,
                            self.__children_maxheaderlen,
                            self.__children_maxheaderlen)


class Message(email.message.Message):
    def __init__(self) -> None:
        # We need a version number so that we can optimize __setstate__()
        self.__version__ = VERSION
        super().__init__()

    def __repr__(self) -> str:
        return str(self)

    def __setstate__(self, d: dict) -> None:
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
        hchanged = False
        for k, v in self._headers:
            if isinstance(v, Header):
                chunks = []
                cchanged = False
                for s, charset in v._chunks:
                    if isinstance(charset, str):
                        charset = Charset(charset)
                        cchanged = True
                    chunks.append((s, charset))
                if cchanged:
                    v._chunks = chunks
                    hchanged = True
            headers.append((k, v))
        if hchanged:
            self._headers = headers

    def get_sender(self, use_envelope=None, preserve_case=0):
        """Return the address considered to be the author of the email.

        This can return either the From: header, the Sender: header or the
        envelope header (a.k.a. the unixfrom header).  The first non-empty
        header value found is returned.  However the search order depends on the
        values of use_envelope and preserve_case.

        use_envelope is a flag that controls whether the envelope header is
        considered.  It can have three values:

            0 - never use the envelope header
            1 - use the envelope header, but only if there is no From: header
            2 - use the envelope header, but only if there is no From: or
                Sender: header

        preserve_case is a flag that controls whether the email address is
        returned in case preserved form or in lowercase form.  When it is false,
        the email address is lowercased.  When it is true, the email address is
        left in its original case.
        """
        senderfirst = mm_cfg.DEFAULT_SENDER_FIRST
        if use_envelope is None:
            use_envelope = mm_cfg.USE_ENVELOPE_SENDER
        # Find the first non-empty header to use
        value = None
        if senderfirst:
            if 'sender' in self:
                value = self['sender']
            if not value and 'from' in self:
                value = self['from']
        else:
            if 'from' in self:
                value = self['from']
            if not value and 'sender' in self:
                value = self['sender']
        if not value and use_envelope:
            if senderfirst:
                if use_envelope == 1:
                    value = self.get_unixfrom()
            else:
                if use_envelope == 2:
                    value = self.get_unixfrom()
        if not value:
            return ''
        # Now that we have a value, parse it and extract the first email address
        fieldval = str(value)
        # Split at ',' in case there are multiple addresses in the field value.
        # We'll use the first one, and note that the call to getaddresses() does
        # the right thing if there aren't any commas in the value.  Note that
        # this will be a problem if someone has an unquoted comma in their
        # display name.
        fieldval = ''.join(fieldval.splitlines())
        addrs = email.utils.getaddresses([fieldval])
        try:
            realname, address = addrs[0]
        except (IndexError, ValueError):
            return ''
        if not preserve_case:
            address = address.lower()
        return address

    def get_senders(self, preserve_case=0, headers=None):
        """Return a list of addresses representing the author of the email.

        The list will contain the following addresses (in order):
            1. From: header address
            2. Sender: header address
            3. Reply-To: header addresses

        The return addresses are always uniqified.  If preserve_case is false,
        then the email addresses are returned in lowercase form.  If it is true,
        they are returned in their original case.

        If the optional headers list is provided, it must be a list of headers
        to search for addresses in.  These strings must be lower case.
        """
        if headers is None:
            headers = ['from', 'sender', 'reply-to']
        pairs = []
        for h in headers:
            if h in self:
                fieldvals = self.get_all(h)
                if fieldvals:
                    fieldvals = [''.join(fv.splitlines())
                               for fv in fieldvals]
                    pairs.extend(email.utils.getaddresses(fieldvals))
        authors = []
        for pair in pairs:
            try:
                realname, address = pair
            except ValueError:
                continue
            if not address:
                continue
            if not preserve_case:
                address = address.lower()
            authors.append(address)
        # uniqify the list
        return list(set(authors))

    def get_filename(self, failobj=None):
        """Some MUA have bugs in RFC2231 filename encoding and cause
        email.Message.get_filename() to raise decoding exceptions. This
        is a workaround for those bugs.
        """
        try:
            filename = email.message.Message.get_filename(self, failobj)
            return filename
        except (UnicodeError, LookupError, ValueError):
            return failobj

    def as_string(self, unixfrom=False, mangle_from_=True):
        """Return entire formatted message as a string.

        Optional 'unixfrom', when true, means include the Unix From_ envelope
        header.  For backward compatibility reasons, if maxheaderlen is not
        specified, it will be set to 0, meaning don't fold headers.  If
        maxheaderlen is not specified, the header will be folded.
        """
        fp = io.StringIO()
        g = Generator(fp, mangle_from_=mangle_from_)
        g.flatten(self, unixfrom=unixfrom)
        return fp.getvalue()


class UserNotification(Message):
    """Class for internally crafted messages."""

    def __init__(self, recip: Union[str, List[str]], sender: str,
                 subject: Optional[str] = None,
                 text: Optional[str] = None,
                 lang: Optional[str] = None) -> None:
        super().__init__()
        charset = Charset(Utils.GetCharSet(lang))
        subject = str(Header(str(subject), charset))
        self['Subject'] = subject
        self['From'] = sender
        self['To'] = recip
        self.set_charset(charset)
        if text is not None:
            self.set_payload(text, charset=charset)

    def send(self, mlist, **_kws) -> None:
        """Send the message by enqueuing it to the 'virgin' queue.

        This is used for all internally crafted messages.
        """
        # Since we're crafting the message from whole cloth, let's make sure
        # this message has a Message-ID.  Yes, the MTA would give us one, but
        # this is useful for logging to logs/smtp.
        if 'message-id' not in self:
            self['Message-ID'] = Utils.unique_message_id(mlist)
        # Ditto for Date: which is required by RFC 2822
        if 'date' not in self:
            self['Date'] = email.utils.formatdate(localtime=True)
        # UserNotifications are typically for admin messages, and for messages
        # other than list explosions.  Send these out as Precedence: bulk, but
        # don't override an existing Precedence: header.
        if 'precedence' not in self:
            self['Precedence'] = 'bulk'
        self._enqueue(mlist, **_kws)

    def _enqueue(self, mlist, **_kws) -> None:
        # Not imported at module scope to avoid import loop
        from Mailman.Queue.sbcache import get_switchboard
        switchboard = get_switchboard(mm_cfg.VIRGINQUEUE_DIR)
        # The message metadata better have a `recip' attribute
        switchboard.enqueue(self,
                          listname=mlist.internal_name(),
                          recips=self.get_all('to'),
                          nodecorate=True,
                          reduced_list_headers=True,
                          **_kws)


class OwnerNotification(UserNotification):
    """Like user notifications, but this message goes to the list owners."""

    def __init__(self, mlist, subject: Optional[str] = None,
                 text: Optional[str] = None,
                 roster: Optional[Union[str, List[str]]] = None) -> None:
        recips = mlist.owner[:]
        if roster is None:
            roster = []
        if isinstance(roster, str):
            roster = [roster]
        recips.extend(roster)
        sender = mlist.GetBouncesEmail()
        lang = mlist.preferred_language
        super().__init__(COMMASPACE.join(recips),
                        sender, subject, text, lang)
        # Hack the To: header to look like it's going to the -owner address
        del self['to']
        self['To'] = mlist.GetOwnerEmail()
        self._sender = sender

    def _enqueue(self, mlist, **_kws) -> None:
        """See `Message._enqueue()`."""
        # Not imported at module scope to avoid import loop
        from Mailman.Queue.sbcache import get_switchboard
        switchboard = get_switchboard(mm_cfg.VIRGINQUEUE_DIR)
        switchboard.enqueue(self,
                          listname=mlist.internal_name(),
                          recips=self.get_all('to'),
                          nodecorate=True,
                          reduced_list_headers=True,
                          **_kws)
