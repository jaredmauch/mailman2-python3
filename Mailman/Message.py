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
which is more convenient for use inside Mailman. The module provides enhanced
message handling capabilities including:

- Improved sender address handling
- Better message formatting and generation
- Support for user and owner notifications
- Unicode and charset handling
- XML character reference conversion
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import re
from io import StringIO
from typing import List, Optional, Tuple, Union, Any, Dict, cast

import email
from email import generator, message, utils
from email.charset import Charset
from email.header import Header
from email.message import Message as EmailMessage

from Mailman import mm_cfg
from Mailman import Utils

COMMASPACE: str = ', '

mo = re.match(r'([\d.]+)', email.__version__)
if mo:
    VERSION = tuple(int(s) for s in mo.group().split('.'))
else:
    VERSION = (0, 0, 0)  # Fallback version if parsing fails


class Generator(generator.Generator):
    """Generates output from a Message object tree, keeping signatures.

    This class extends the standard email.generator.Generator to provide
    additional functionality for Mailman, particularly around header handling
    and signature preservation.

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
    including better sender handling, message formatting, and state management.
    It extends the standard email.message.Message class with Mailman-specific
    functionality.
    """
    
    def __init__(self) -> None:
        """Initialize the message.
        
        We need a version number so that we can optimize __setstate__().
        This helps with backwards compatibility when upgrading email package
        versions.
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
        
        The base class attributes have changed over time, which could
        affect Mailman if messages are sitting in the queue at the time of
        upgrading the email package. We handle schema updates here rather
        than burdening the email package.
        
        Args:
            d: Dictionary containing the message state.
        """
        self.__dict__ = d
        # We know that email 2.4.3 is up-to-date
        version = d.get('__version__', (0, 0, 0))
        d['__version__'] = VERSION
        if version >= VERSION:
            return
            
        # Handle backwards compatibility updates
        if '_charset' not in d:
            self._charset = None
        if '_default_type' not in d:
            self._default_type = 'text/plain'
            
        # Update Header instances to use Charset objects instead of strings
        headers = []
        header_changed = False
        for key, value in self._headers:
            if isinstance(value, Header):
                chunks = []
                chunks_changed = False
                for text, charset in value._chunks:
                    if isinstance(charset, str):
                        charset = Charset(charset)
                        chunks_changed = True
                    chunks.append((text, charset))
                if chunks_changed:
                    value._chunks = chunks
                    header_changed = True
            headers.append((key, value))
        if header_changed:
            self._headers = headers

    def get_sender(self, use_envelope: Optional[bool] = None, 
                  preserve_case: bool = False) -> str:
        """Return the address considered to be the author of the email.

        This can return either the From: header, the Sender: header or the
        envelope header (a.k.a. the unixfrom header). The first non-empty
        header value found is returned. The search order is determined by:

        - If mm_cfg.USE_ENVELOPE_SENDER is true or use_envelope is True:
          Sender:, From:, unixfrom
        - Otherwise: From:, Sender:, unixfrom

        Args:
            use_envelope: Override mm_cfg.USE_ENVELOPE_SENDER setting.
                         Should be set to either True or False.
            preserve_case: Whether to preserve the case of the address.

        Returns:
            The sender's email address, lowercased unless preserve_case is True.
        """
        sender_first = mm_cfg.USE_ENVELOPE_SENDER if use_envelope is None else use_envelope
        headers = ('sender', 'from') if sender_first else ('from', 'sender')
        
        # Try to get address from headers first
        for header in headers:
            field_val = self[header]
            if not field_val:
                continue
                
            # Handle multi-line headers properly
            field_val = ''.join(field_val.splitlines())
            addrs = utils.getaddresses([field_val])
            
            try:
                realname, address = addrs[0]
                if address:
                    return address if preserve_case else address.lower()
            except IndexError:
                continue
                
        # Fall back to unixfrom if no valid header found
        unixfrom = self.get_unixfrom()
        if unixfrom:
            try:
                address = unixfrom.split()[1]
                return address if preserve_case else address.lower()
            except IndexError:
                pass
                
        return ''

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
            headers: Alternative search order for headers. None means use
                    mm_cfg.SENDER_HEADERS. Items are field names without
                    trailing colon.

        Returns:
            List of sender addresses, lowercased unless preserve_case is True.
        """
        if headers is None:
            headers = mm_cfg.SENDER_HEADERS
            
        pairs = []
        for header in headers:
            if header is None:
                # Handle unixfrom header
                unixfrom = self.get_unixfrom()
                if unixfrom:
                    try:
                        pairs.append(('', unixfrom.split()[1]))
                    except IndexError:
                        pass
            else:
                # Handle regular headers
                field_vals = self.get_all(header)
                if field_vals:
                    # Handle multi-line headers properly
                    field_vals = [''.join(val.splitlines())
                                for val in field_vals]
                    pairs.extend(utils.getaddresses(field_vals))
                    
        # Process and normalize addresses
        authors = []
        for realname, address in pairs:
            if address:
                if not preserve_case:
                    address = address.lower()
                authors.append(address)
                
        return authors

    def get_filename(self, failobj: Any = None) -> Any:
        """Get the filename from the Content-Disposition header.

        Some MUAs have bugs in RFC2231 filename encoding that can cause
        Mailman to stop delivery in Scrubber.py (called from ToDigest.py).
        This method handles such cases gracefully.

        Args:
            failobj: Object to return if filename cannot be determined.

        Returns:
            The filename from Content-Disposition header or failobj.
        """
        try:
            return super().get_filename(failobj)
        except (UnicodeError, LookupError, ValueError):
            return failobj

    def as_string(self, unixfrom: bool = False, 
                 mangle_from_: bool = True) -> str:
        """Return entire formatted message as a string.

        Uses Mailman.Message.Generator to format the message. Only the
        top-level headers will be folded. This provides better handling
        of signatures and headers compared to the standard email package.

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
    """Class for internally crafted notification messages.
    
    This class provides functionality for creating and sending notification
    messages to users, with support for internationalization and proper
    message formatting.
    """

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
            lang: Optional language code for internationalization.
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
            **kws: Additional keyword arguments for message processing.
        """
        if not noprecedence:
            self['Precedence'] = 'bulk'
        self._enqueue(mlist, **kws)

    def _enqueue(self, mlist: Any, **kws: Any) -> None:
        """Enqueue the message for delivery.
        
        Args:
            mlist: The mailing list object.
            **kws: Additional keyword arguments for queue processing.
        """
        # Not imported at module scope to avoid import loop
        from Mailman.Queue import Switchboard
        switchboard = Switchboard()
        switchboard.enqueue(self, mlist=mlist, **kws)


class OwnerNotification(UserNotification):
    """Class for notifications to list owners.
    
    This class extends UserNotification to provide specialized handling
    for messages sent to list owners and moderators.
    """

    def __init__(self, mlist: Any, subject: Optional[str] = None, 
                 text: Optional[str] = None, tomoderators: bool = True) -> None:
        """Initialize an owner notification message.
        
        Args:
            mlist: The mailing list object.
            subject: Optional subject line.
            text: Optional message body.
            tomoderators: Whether to send to moderators (True) or 
                         bounce address (False).
        """
        recip = mlist.GetOwnerEmail() if tomoderators else mlist.GetBouncesEmail()
        super().__init__(recip, mlist.GetRequestEmail(), subject, text)
        self['X-List-Administrivia'] = 'yes'

    def _enqueue(self, mlist: Any, **kws: Any) -> None:
        """Enqueue the message for delivery.
        
        Args:
            mlist: The mailing list object.
            **kws: Additional keyword arguments for queue processing.
        """
        # Not imported at module scope to avoid import loop
        from Mailman.Queue import Switchboard
        switchboard = Switchboard()
        switchboard.enqueue(self, mlist=mlist, **kws)


def _invert_xml(mo: re.Match[str]) -> str:
    """Convert XML character references and textual \u escapes to unicodes.
    
    Args:
        mo: Match object from regex containing character reference.
        
    Returns:
        The converted unicode character or replacement character if invalid.
    """
    try:
        if mo.group(1)[:1] == '#':
            return chr(int(mo.group(1)[1:]))
        elif mo.group(1)[:1].lower() == 'u':
            return chr(int(mo.group(1)[1:], 16))
        else:
            return '\ufffd'  # Unicode replacement character
    except ValueError:
        # Value is out of range. Return the unicode replacement character.
        return '\ufffd'
