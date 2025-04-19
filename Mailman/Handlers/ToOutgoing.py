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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

"""Re-queue the message to the outgoing queue.

This module is only for use by the IncomingRunner for delivering messages
posted to the list membership.  Anything else that needs to go out to some
recipient should just be placed in the out queue directly.
"""

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
from Mailman.Queue.sbcache import get_switchboard

_ = i18n._

class ToOutgoing:
    """Handler for outgoing messages."""
    
    def __init__(self, mlist: Any) -> None:
        """Initialize the handler.
        
        Args:
            mlist: The mailing list object
        """
        self.mlist = mlist
        self.logger = logging.getLogger('mailman.outgoing')
        
    def process(self, msg: Message, msgdata: Dict[str, Any]) -> None:
        """Process an outgoing message.
        
        Args:
            msg: The email message
            msgdata: Additional message metadata
            
        Raises:
            OSError: If there are file system errors
            mailbox.Error: If there are mailbox errors
        """
        try:
            # Get the outgoing directory
            outgoing_dir = os.path.join(mm_cfg.OUTGOING_DIR, self.mlist.internal_name())
            
            # Create the outgoing directory if it doesn't exist
            os.makedirs(outgoing_dir, exist_ok=True)
            
            # Get the outgoing file path
            outgoing_file = os.path.join(outgoing_dir, 'outgoing.mbox')
            
            # Open the outgoing file and add the message
            with mailbox.mbox(outgoing_file) as mbox:
                mbox.add(msg)
                
        except (OSError, mailbox.Error) as e:
            self.logger.error('Failed to process outgoing message: %s', e)
            syslog('error', 'Failed to process outgoing message: %s', e)
            raise
            
    def reject(self, msg: Message, msgdata: Dict[str, Any], reason: str) -> None:
        """Reject an outgoing message.
        
        Args:
            msg: The email message
            msgdata: Additional message metadata
            reason: Reason for rejection
        """
        self.logger.warning('Rejected outgoing message: %s', reason)
        syslog('warning', 'Rejected outgoing message: %s', reason)
