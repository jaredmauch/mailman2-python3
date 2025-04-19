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

"""Handler for archiving messages."""

from typing import Any, Dict, List, Optional, Tuple, Union
import os
import time
import email
from email.message import Message
import logging
import mailbox
import shutil

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import i18n
from Mailman.Logging.Syslog import syslog

_ = i18n._

class ToArchive:
    """Handler for archiving messages."""
    
    def __init__(self, mlist: Any) -> None:
        """Initialize the handler.
        
        Args:
            mlist: The mailing list object
        """
        self.mlist = mlist
        self.logger = logging.getLogger('mailman.archive')
        
    def process(self, msg: Message, msgdata: Dict[str, Any]) -> None:
        """Process a message for archiving.
        
        Args:
            msg: The email message
            msgdata: Additional message metadata
            
        Raises:
            OSError: If there are file system errors
            mailbox.Error: If there are mailbox errors
        """
        try:
            # Get the archive directory
            archive_dir = os.path.join(mm_cfg.ARCHIVE_DIR, self.mlist.internal_name())
            
            # Create the archive directory if it doesn't exist
            os.makedirs(archive_dir, exist_ok=True)
            
            # Get the archive file path
            archive_file = os.path.join(archive_dir, 'archive.mbox')
            
            # Open the archive file and add the message
            with mailbox.mbox(archive_file) as mbox:
                mbox.add(msg)
                
        except (OSError, mailbox.Error) as e:
            self.logger.error('Failed to process archive message: %s', e)
            syslog('error', 'Failed to process archive message: %s', e)
            raise
            
    def reject(self, msg: Message, msgdata: Dict[str, Any], reason: str) -> None:
        """Reject a message from being archived.
        
        Args:
            msg: The email message
            msgdata: Additional message metadata
            reason: Reason for rejection
        """
        self.logger.warning('Rejected archive message: %s', reason)
        syslog('warning', 'Rejected archive message: %s', reason)
