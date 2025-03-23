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

import os
import sys
import time
import errno
import email
from email import message
from email import parser
from email import policy
from typing import Optional, List, Dict, Any

import mailbox

from Mailman import mm_cfg
from Mailman import Utils
from Mailman.Logging.Syslog import syslog
from Mailman.Message import Generator
from Mailman.Message import Message


def _safeparser(fp):
    try:
        return email.message_from_file(fp, Message)
    except email.errors.MessageParseError:
        # Don't return None since that will stop a mailbox iterator
        return ''


class Mailbox(mailbox.PortableUnixMailbox):
    def __init__(self, path: str):
        """Initialize a mailbox.

        Args:
            path: The path to the mailbox file.
        """
        self.path = path
        self._parser = parser.Parser(policy=policy.default)
        self._messages: List[message.Message] = []
        self._loaded = False
        mailbox.PortableUnixMailbox.__init__(self, path, _safeparser)

    def __iter__(self):
        """Iterate over messages in the mailbox."""
        if not self._loaded:
            self.load()
        return iter(self._messages)

    def __len__(self):
        """Return the number of messages in the mailbox."""
        if not self._loaded:
            self.load()
        return len(self._messages)

    def __getitem__(self, index: int) -> message.Message:
        """Get a message by index.

        Args:
            index: The index of the message to get.

        Returns:
            The message at the given index.
        """
        if not self._loaded:
            self.load()
        return self._messages[index]

    def load(self):
        """Load messages from the mailbox file."""
        if not os.path.exists(self.path):
            self._messages = []
            self._loaded = True
            return

        try:
            with open(self.path, 'r', encoding='utf-8') as fp:
                while True:
                    try:
                        msg = self._parser.parse(fp)
                        self._messages.append(msg)
                    except email.errors.MessageParseError:
                        # Skip malformed messages
                        continue
                    except EOFError:
                        break
        except IOError as e:
            if e.errno != errno.ENOENT:
                raise
            self._messages = []
        self._loaded = True

    def save(self):
        """Save messages to the mailbox file."""
        if not self._loaded:
            return

        # Create parent directory if it doesn't exist
        parent = os.path.dirname(self.path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent)

        # Write messages to a temporary file
        tmpfile = self.path + '.tmp'
        try:
            with open(tmpfile, 'w', encoding='utf-8') as fp:
                for msg in self._messages:
                    fp.write(msg.as_string())
                    fp.write('\n')
                fp.flush()
                os.fsync(fp.fileno())
            # Rename temporary file to actual file
            os.rename(tmpfile, self.path)
        except:
            # Clean up temporary file on error
            try:
                os.unlink(tmpfile)
            except OSError:
                pass
            raise

    def add(self, msg: message.Message):
        """Add a message to the mailbox.

        Args:
            msg: The message to add.
        """
        if not self._loaded:
            self.load()
        self._messages.append(msg)

    def remove(self, msg: message.Message):
        """Remove a message from the mailbox.

        Args:
            msg: The message to remove.
        """
        if not self._loaded:
            self.load()
        self._messages.remove(msg)

    def clear(self):
        """Remove all messages from the mailbox."""
        self._messages = []
        self._loaded = True

    def lock(self):
        """Lock the mailbox for exclusive access."""
        # Not implemented yet
        pass

    def unlock(self):
        """Unlock the mailbox."""
        # Not implemented yet
        pass

    def close(self):
        """Close the mailbox and save changes."""
        if self._loaded:
            self.save()
        self._messages = []
        self._loaded = False

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
