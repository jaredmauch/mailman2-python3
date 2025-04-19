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

"""Standard Mailman message object.

This is a subclass of email.Message but provides a slightly extended interface
which is more convenient for use inside Mailman.
"""

import re
import io
from typing import List, Optional, Union, Dict, Any, Tuple, Sequence

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
        """Initialize the generator.
        
        Args:
            outfp: Output file-like object
            mangle_from_: Whether to mangle From_ lines
            maxheaderlen: Maximum length for headers
            children_maxheaderlen: Maximum header length for child parts
        """
        super().__init__(outfp, mangle_from_=mangle_from_,
                        maxheaderlen=maxheaderlen)
        self.__children_maxheaderlen = children_maxheaderlen

    def clone(self, fp: io.TextIOBase) -> 'Generator':
        """Clone this generator with maxheaderlen set for children.
        
        Args:
            fp: Output file-like object
            
        Returns:
            A new Generator instance
        """
        return self.__class__(fp, self._mangle_from_,
                            self.__children_maxheaderlen,
                            self.__children_maxheaderlen)


class Message(email.message.Message):
    """Enhanced message class with additional Mailman-specific functionality."""

    def __init__(self) -> None:
        """Initialize a new Message instance."""
        # We need a version number so that we can optimize __setstate__()
        self.__version__ = VERSION
        super().__init__()

    def __repr__(self) -> str:
        """Return string representation of the message."""
        return str(self)

    def __setstate__(self, d: Dict[str, Any]) -> None:
        """Restore state from a pickle.
        
        Args:
            d: Dictionary containing pickled state
            
        This method handles schema updates for compatibility with older versions.
        """
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

    def get_sender(self, use_envelope: Optional[int] = None, preserve_case: bool = False) -> str:
        """Return the address considered to be the author of the email.

        Args:
            use_envelope: Controls whether to use envelope header:
                0 - never use envelope header
                1 - use envelope header if no From: header
                2 - use envelope header if no From: or Sender: header
            preserve_case: Whether to preserve case of email address
            
        Returns:
            The sender's email address
            
        This can return either the From: header, the Sender: header or the
        envelope header (a.k.a. the unixfrom header). The first non-empty
        header value found is returned.
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
        # the right thing if there aren't any commas in the value.
        fieldval = ''.join(fieldval.splitlines())
        addrs = email.utils.getaddresses([fieldval])
        try:
            realname, address = addrs[0]
        except (IndexError, ValueError):
            return ''
        if not preserve_case:
            address = address.lower()
        return address

    def get_senders(self, preserve_case: bool = False, 
                    headers: Optional[List[str]] = None) -> List[str]:
        """Return a list of addresses representing the author of the email.

        Args:
            preserve_case: Whether to preserve case of email addresses
            headers: Optional list of headers to search for addresses
            
        Returns:
            List of email addresses from headers
            
        The list will contain addresses from the following headers (in order):
            1. From: header address
            2. Sender: header address
            3. Reply-To: header addresses
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
        for realname, address in pairs:
            if not address:
                continue
            if not preserve_case:
                address = address.lower()
            # Uniqify the list
            if address not in authors:
                authors.append(address)
        return authors

    def get_filename(self, failobj: Any = None) -> Optional[str]:
        """Get the filename associated with the payload if present.
        
        Args:
            failobj: Default value to return if no filename found
            
        Returns:
            The filename if found, otherwise failobj
        """
        missing = []
        filename = self.get_param('filename', missing, 'content-disposition')
        if filename is missing:
            filename = self.get_param('name', missing)
        if filename is missing:
            return failobj
        return Utils.oneline(filename)

    def as_string(self, unixfrom: bool = False, mangle_from_: bool = True) -> str:
        """Return the entire formatted message as a string.
        
        Args:
            unixfrom: Whether to include Unix From_ line
            mangle_from_: Whether to mangle From_ lines
            
        Returns:
            The formatted message as a string
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
        """Create a new user notification message.
        
        Args:
            recip: Recipient address or list of addresses
            sender: Sender address
            subject: Optional subject line
            text: Optional message text
            lang: Optional language for the message
        """
        super().__init__()
        self['From'] = sender
        if isinstance(recip, str):
            self['To'] = recip
        else:
            self['To'] = COMMASPACE.join(recip)
        if subject:
            self['Subject'] = subject
        if text:
            self.set_payload(text, charset=Utils.GetCharSet(lang))

    def send(self, mlist, **_kws) -> None:
        """Send the message by enqueuing it to the incoming queue.
        
        Args:
            mlist: The mailing list to send through
            **_kws: Additional keyword arguments
        """
        # Since we're crafting the message from whole cloth, let's make sure our
        # headers are RFC 2822 compliant.  This is an opportunity for Mailman to
        # be a model email citizen.
        if 'message-id' not in self:
            self['Message-ID'] = email.utils.make_msgid()
        if 'date' not in self:
            self['Date'] = email.utils.formatdate(localtime=True)
        # Gather statistics and send the message
        self._enqueue(mlist, **_kws)

    def _enqueue(self, mlist, **_kws) -> None:
        """Enqueue message for sending.
        
        Args:
            mlist: The mailing list to send through
            **_kws: Additional keyword arguments
        """
        # Not imported at module scope to avoid import loop
        from Mailman.Queue.sbcache import get_switchboard
        switchboard = get_switchboard(mm_cfg.INQUEUE_DIR)
        # The message metadata better have a `recips' attribute
        switchboard.enqueue(self, _kws)


class OwnerNotification(UserNotification):
    """Like user notifications, but this message goes to the list owners."""

    def __init__(self, mlist, subject: Optional[str] = None,
                 text: Optional[str] = None,
                 roster: Optional[Union[str, List[str]]] = None) -> None:
        """Create a new owner notification message.
        
        Args:
            mlist: The mailing list
            subject: Optional subject line
            text: Optional message text
            roster: Optional list of recipients (defaults to list owners)
        """
        if roster is None:
            roster = mlist.owner[:]
        # Extend the subject with the list name
        if subject is None:
            subject = ''
        subject = f'[{mlist.real_name}] {subject}'
        super().__init__(roster, mlist.GetBouncesEmail(),
                        subject=subject, text=text,
                        lang=mlist.preferred_language)

    def _enqueue(self, mlist, **_kws) -> None:
        """Enqueue message for sending.
        
        Args:
            mlist: The mailing list to send through
            **_kws: Additional keyword arguments
        """
        # Not imported at module scope to avoid import loop
        from Mailman.Queue.sbcache import get_switchboard
        switchboard = get_switchboard(mm_cfg.VIRGINQUEUE_DIR)
        # The message metadata better have a `recips' attribute
        switchboard.enqueue(self, _kws)
