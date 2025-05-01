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

"""Extend mailbox.UnixMailbox.
"""

import sys
import mailbox
from io import StringIO, BytesIO
from types import MethodType

import email
from email.parser import Parser
from email.errors import MessageParseError

from Mailman import mm_cfg
from Mailman.Message import Message


def _safeparser(fp):
    try:
        return email.message_from_file(fp, Message)
    except MessageParseError:
        # Don't return None since that will stop a mailbox iterator
        return ''


class BinaryGenerator(email.generator.Generator):
    """A generator that writes to a binary file."""
    def __init__(self, outfp, mangle_from_=True, maxheaderlen=78, *args, **kwargs):
        # Create a text buffer that we'll write to first
        self._buffer = StringIO()
        super().__init__(self._buffer, mangle_from_, maxheaderlen, *args, **kwargs)
        # Store the binary file object
        self._binary_fp = outfp

    def _write_lines(self, lines):
        # Override to handle both string and bytes input
        for line in lines:
            if isinstance(line, bytes):
                try:
                    line = line.decode('utf-8', 'replace')
                except UnicodeError:
                    line = line.decode('latin-1', 'replace')
            self._buffer.write(line)

    def flatten(self, msg, unixfrom=False, linesep='\n'):
        # Override to write the buffer contents to binary file
        super().flatten(msg, unixfrom=unixfrom, linesep=linesep)
        # Get the text content and encode it
        content = self._buffer.getvalue()
        # Reset the buffer
        self._buffer.seek(0)
        self._buffer.truncate()
        # Write to binary file
        self._binary_fp.write(content.encode('utf-8', 'replace'))


class Mailbox(mailbox.mbox):
    def __init__(self, fp):
        # In Python 3, we need to handle both file objects and paths
        if hasattr(fp, 'read') and hasattr(fp, 'write'):
            # It's a file object, get its path
            if hasattr(fp, 'name'):
                path = fp.name
            else:
                # Create a temporary file if we don't have a path
                import tempfile
                path = tempfile.mktemp()
                with open(path, 'wb') as f:
                    f.write(fp.read())
                fp.seek(0)
        else:
            # It's a path string
            path = fp
            
        # Initialize the parent class with the path
        super().__init__(path, _safeparser)
        # Store the file object if we have one
        if hasattr(fp, 'read') and hasattr(fp, 'write'):
            self.fp = fp
        else:
            self.fp = open(path, 'ab+')

    # msg should be an rfc822 message or a subclass.
    def AppendMessage(self, msg):
        # Check the last character of the file and write a newline if it isn't
        # a newline (but not at the beginning of an empty file).
        try:
            self.fp.seek(-1, 2)
        except IOError as e:
            # Assume the file is empty.  We can't portably test the error code
            # returned, since it differs per platform.
            pass
        else:
            if self.fp.read(1) != b'\n':
                self.fp.write(b'\n')
        # Seek to the last char of the mailbox
        self.fp.seek(0, 2)
        # Create a BinaryGenerator instance to write the message to the file
        g = BinaryGenerator(self.fp, mangle_from_=False, maxheaderlen=0)
        g.flatten(msg, unixfrom=True)
        # Add one more trailing newline for separation with the next message
        self.fp.write(b'\n')


# This stuff is used by pipermail.py:processUnixMailbox().  It provides an
# opportunity for the built-in archiver to scrub archived messages of nasty
# things like attachments and such...
def _archfactory(mailbox):
    # The factory gets a file object, but it also needs to have a MailList
    # object, so the clearest <wink> way to do this is to build a factory
    # function that has a reference to the mailbox object, which in turn holds
    # a reference to the mailing list.  Nested scopes would help here, BTW,
    # but we can't rely on them being around (e.g. Python 2.0).
    def scrubber(fp, mailbox=mailbox):
        msg = _safeparser(fp)
        if msg == '':
            return msg
        return mailbox.scrub(msg)
    return scrubber


class ArchiverMailbox(Mailbox):
    # This is a derived class which is instantiated with a reference to the
    # MailList object.  It is build such that the factory calls back into its
    # scrub() method, giving the scrubber module a chance to do its thing
    # before the message is archived.
    def __init__(self, fp, mlist):
        if mm_cfg.ARCHIVE_SCRUBBER:
            __import__(mm_cfg.ARCHIVE_SCRUBBER)
            self._scrubber = sys.modules[mm_cfg.ARCHIVE_SCRUBBER].process
        else:
            self._scrubber = None
        self._mlist = mlist
        mailbox.PortableUnixMailbox.__init__(self, fp, _archfactory(self))

    def scrub(self, msg):
        if self._scrubber:
            return self._scrubber(self._mlist, msg)
        else:
            return msg

    def skipping(self, flag):
        """ This method allows the archiver to skip over messages without
        scrubbing attachments into the attachments directory."""
        if flag:
            self.factory = _safeparser
        else:
            self.factory = _archfactory(self)
