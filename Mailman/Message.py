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

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import re
from io import StringIO
from typing import List, Optional, Tuple, Union, Any, Dict

import email
from email import generator, message, utils
from email.charset import Charset
from email.header import Header

from Mailman import mm_cfg
from Mailman import Utils

COMMASPACE = ', '

mo = re.match(r'([\d.]+)', email.__version__)
VERSION = tuple([int(s) for s in mo.group().split('.')])


class Generator(generator.Generator):
    """Generates output from a Message object tree, keeping signatures.

    Headers will by default _not_ be folded in attachments.
    
    Attributes:
        __children_maxheaderlen: Maximum header length for child messages.
    """
    def __init__(self, outfp: Any, mangle_from_: bool = True,
                 maxheaderlen: int = 78, children_maxheaderlen: int = 0) -> None:
        """Initialize the generator.
        
        Args:
            outfp: The output file object.
            mangle_from_: Whether to mangle From headers.
            maxheaderlen: Maximum header length for top-level message.
            children_maxheaderlen: Maximum header length for child messages.
        """
        super().__init__(outfp, mangle_from_=mangle_from_, 
                        maxheaderlen=maxheaderlen)
        self.__children_maxheaderlen = children_maxheaderlen

    def clone(self, fp: Any) -> 'Generator':
        """Clone this generator with maxheaderlen set for children.
        
        Args:
            fp: The new output file object.
            
        Returns:
            A new Generator instance with the same settings.
        """
        return self.__class__(fp, self._mangle_from_,
                            self.__children_maxheaderlen, 
                            self.__children_maxheaderlen)


class Message(message.Message):
    """Extended email.Message class with additional functionality for Mailman.
    
    This class provides a more convenient interface for use inside Mailman,
    including better sender handling and message formatting.
    """
    def __init__(self) -> None:
        """Initialize the message.
        
        We need a version number so that we can optimize __setstate__()
        """
        self.__version__ = VERSION
        super().__init__()

    def __repr__(self) -> str:
        """Return string representation of the message.
        
        Returns:
            The string representation of the message.
        """
        return str(self)

    def __setstate__(self, d: Dict[str, Any]) -> None:
        """Restore the message state from a dictionary.
        
        The base class attributes have changed over time. Which could
        affect Mailman if messages are sitting in the queue at the time of
        upgrading the email package. We shouldn't burden email with this,
        so we handle schema updates here.
        
        Args:
            d: Dictionary containing the message state.
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
                    if isinstance(charset, str):
                        charset = Charset(charset)
                        cchanged = 1
                    chunks.append((s, charset))
                if cchanged:
                    v._chunks = chunks
                    hchanged = 1
            headers.append((k, v))
        if hchanged:
            self._headers = headers

    def get_sender(self, use_envelope: Optional[bool] = None, 
                  preserve_case: bool = False) -> str:
        """Return the address considered to be the author of the email.

        This can return either the From: header, the Sender: header or the
        envelope header (a.k.a. the unixfrom header). The first non-empty
        header value found is returned. However the search order is
        determined by the following:

        - If mm_cfg.USE_ENVELOPE_SENDER is true, then the search order is
          Sender:, From:, unixfrom

        - Otherwise, the search order is From:, Sender:, unixfrom

        Args:
            use_envelope: Override mm_cfg.USE_ENVELOPE_SENDER setting.
                         Should be set to either 0 or 1.
            preserve_case: Whether to preserve the case of the address.

        Returns:
            The sender's email address, lowercased unless preserve_case is True.
        """
        senderfirst = mm_cfg.USE_ENVELOPE_SENDER
        if use_envelope is not None:
            senderfirst = use_envelope
        if senderfirst:
            headers = ('sender', 'from')
        else:
            headers = ('from', 'sender')
        for h in headers:
            # Use only the first occurrence of Sender: or From:, although it's
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
            addrs = utils.getaddresses([fieldval])
            try:
                realname, address = addrs[0]
            except IndexError:
                continue
            if address:
                break
        else:
            # We didn't find a non-empty header, so let's fall back to the
            # unixfrom address. This should never be empty, but if it ever
            # is, it's probably a Really Bad Thing. Further, we just assume
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

    def get_senders(self, preserve_case: bool = False, 
                   headers: Optional[List[str]] = None) -> List[str]:
        """Return a list of addresses representing the author of the email.

        The list will contain the following addresses (in order)
        depending on availability:

        1. From:
        2. unixfrom
        3. Reply-To:
        4. Sender:

        Args:
            preserve_case: Whether to preserve the case of addresses.
            headers: Alternative search order for headers. None means search
                    the unixfrom header. Items in headers are field names
                    without the trailing colon.

        Returns:
            List of sender addresses, lowercased unless preserve_case is True.
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
                    pairs.extend(utils.getaddresses(fieldvals))
        authors = []
        for pair in pairs:
            address = pair[1]
            if address is not None and not preserve_case:
                address = address.lower()
            authors.append(address)
        return authors

    def get_filename(self, failobj: Any = None) -> Any:
        """Get the filename from the Content-Disposition header.

        Some MUA have bugs in RFC2231 filename encoding and cause
        Mailman to stop delivery in Scrubber.py (called from ToDigest.py).

        Args:
            failobj: Object to return if filename cannot be determined.

        Returns:
            The filename from Content-Disposition header or failobj.
        """
        try:
            filename = super().get_filename(failobj)
            return filename
        except (UnicodeError, LookupError, ValueError):
            return failobj

    def as_string(self, unixfrom: bool = False, 
                 mangle_from_: bool = True) -> str:
        """Return entire formatted message as a string using
        Mailman.Message.Generator.

        Operates like email.Message.Message.as_string, only
        using Mailman's Message.Generator class. Only the top headers will
        get folded.

        Args:
            unixfrom: Whether to include the Unix From header.
            mangle_from_: Whether to mangle From headers.

        Returns:
            The formatted message as a string.
        """
        fp = StringIO()
        g = Generator(fp, mangle_from_=mangle_from_)
        g.flatten(self, unixfrom=unixfrom)
        return fp.getvalue()


class UserNotification(Message):
    """Class for internally crafted messages."""

    def __init__(self, recip: str, sender: str, 
                 subject: Optional[str] = None, 
                 text: Optional[str] = None, 
                 lang: Optional[str] = None) -> None:
        """Initialize a user notification message.
        
        Args:
            recip: The recipient's email address.
            sender: The sender's email address.
            subject: Optional subject line.
            text: Optional message body.
            lang: Optional language code.
        """
        super().__init__()
        self['To'] = recip
        self['From'] = sender
        if subject:
            self['Subject'] = subject
        if text:
            self.set_payload(text)
        if lang:
            self['Content-Language'] = lang

    def send(self, mlist: Any, noprecedence: bool = False, **kws: Any) -> None:
        """Send the notification message.
        
        Args:
            mlist: The mailing list object.
            noprecedence: Whether to skip adding Precedence header.
            **kws: Additional keyword arguments.
        """
        if not noprecedence:
            self['Precedence'] = 'bulk'
        self._enqueue(mlist, **kws)

    def _enqueue(self, mlist: Any, **kws: Any) -> None:
        """Enqueue the message for delivery.
        
        Args:
            mlist: The mailing list object.
            **kws: Additional keyword arguments.
        """
        # Not imported at module scope to avoid import loop
        from Mailman.Queue import Switchboard
        switchboard = Switchboard()
        switchboard.enqueue(self, mlist=mlist, **kws)


class OwnerNotification(UserNotification):
    """Class for notifications to list owners."""

    def __init__(self, mlist: Any, subject: Optional[str] = None, 
                 text: Optional[str] = None, tomoderators: bool = True) -> None:
        """Initialize an owner notification message.
        
        Args:
            mlist: The mailing list object.
            subject: Optional subject line.
            text: Optional message body.
            tomoderators: Whether to send to moderators.
        """
        if tomoderators:
            recip = mlist.GetOwnerEmail()
        else:
            recip = mlist.GetBouncesEmail()
        super().__init__(recip, mlist.GetRequestEmail(), subject, text)
        self['X-List-Administrivia'] = 'yes'

    def _enqueue(self, mlist: Any, **kws: Any) -> None:
        """Enqueue the message for delivery.
        
        Args:
            mlist: The mailing list object.
            **kws: Additional keyword arguments.
        """
        # Not imported at module scope to avoid import loop
        from Mailman.Queue import Switchboard
        switchboard = Switchboard()
        switchboard.enqueue(self, mlist=mlist, **kws)

def _invert_xml(mo: Any) -> str:
    """Convert XML character references and textual \u escapes to unicodes.
    
    Args:
        mo: Match object from regex.
        
    Returns:
        The converted character.
    """
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
