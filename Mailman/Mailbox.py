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
import email.message
from email.message import Message
from email.parser import Parser
from email.errors import MessageParseError
from email.generator import Generator

from Mailman import mm_cfg
from Mailman.Message import Message

def _safeparser(fp):
    try:
        return email.message_from_file(fp, Mailman.Message.Message)
    except MessageParseError:
        # Don't return None since that will stop a mailbox iterator
        return ''

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
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(fp.read().decode('utf-8', 'replace'))
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
            # Open in text mode for writing
            self.fp = open(path, 'a+', encoding='utf-8')

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
            if self.fp.read(1) != '\n':
                self.fp.write('\n')
        # Seek to the last char of the mailbox
        self.fp.seek(0, 2)
        
        # Create a Generator instance to write the message to the file
        g = Generator(self.fp, mangle_from_=False, maxheaderlen=0)
        g.flatten(msg, unixfrom=True)
        # Add one more trailing newline for separation with the next message
        self.fp.write('\n')


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
