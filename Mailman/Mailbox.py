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

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import sys
import mailbox

import email
from email.parser import Parser
from email.errors import MessageParseError

from Mailman import mm_cfg
from Mailman.Message import Generator
from Mailman.Message import Message


def _safeparser(fp):
    try:
        return email.message_from_file(fp, Message)
    except MessageParseError:
        # Don't return None since that will stop a mailbox iterator
        return ''


class Mailbox(mailbox.PortableUnixMailbox):
    def __init__(self, fp):
        mailbox.PortableUnixMailbox.__init__(self, fp, _safeparser)

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
        g = Generator(self.fp)
        g.flatten(msg, unixfrom=True)
        # Add one more trailing newline for separation with the next message
        # to be appended to the mbox.
        print('', file=self.fp)


# This stuff is used by pipermail.py:processUnixMailbox().  It provides an
# opportunity for the built-in archiver to scrub archived messages of nasty
# things like attachments and such...
def _archfactory(fp):
    """Create a scrubber for archiving messages."""
    return ArchiverMailbox(fp)


class ArchiverMailbox(Mailbox):
    """A mailbox class that scrubs messages for archiving."""

    def __init__(self, fp):
        Mailbox.__init__(self, fp)
        self._scrubber = None

    def _scrub(self, msg):
        """Scrub the message of attachments and other unnecessary parts."""
        if self._scrubber is None:
            from Mailman.Archiver import Scrubber
            self._scrubber = Scrubber(convert_html_to_plaintext=1)
        return self._scrubber.scrub(msg)

    def AppendMessage(self, msg):
        """Append a scrubbed message to the mailbox."""
        # First scrub the message
        msg = self._scrub(msg)
        # Then append it using the parent class's method
        Mailbox.AppendMessage(self, msg)

    def SkipAttachment(self, msg, part):
        """Return true if the attachment should be skipped."""
        # Skip attachments with content-disposition: attachment
        if part.get('content-disposition', '').lower().startswith('attachment'):
            return True
        # Skip base64 encoded parts larger than 40KB
        if part.get('content-transfer-encoding', '').lower() == 'base64':
            try:
                size = int(part.get('content-length', 0))
                if size > 40 * 1024:  # 40KB
                    return True
            except (ValueError, TypeError):
                pass
        return False
