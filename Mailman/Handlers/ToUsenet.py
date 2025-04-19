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

"""Handler for Usenet messages."""

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

class ToUsenet:
    """Handler for Usenet messages."""
    
    def __init__(self, mlist: Any) -> None:
        """Initialize the handler.
        
        Args:
            mlist: The mailing list object
        """
        self.mlist = mlist
        self.logger = logging.getLogger('mailman.usenet')
        
    def process(self, msg: Message, msgdata: Dict[str, Any]) -> None:
        """Process a message for Usenet.
        
        Args:
            msg: The email message
            msgdata: Additional message metadata
        """
        # Get the Usenet directory
        usenet_dir = os.path.join(mm_cfg.USENET_DIR, self.mlist.internal_name())
        
        # Create the Usenet directory if it doesn't exist
        if not os.path.exists(usenet_dir):
            try:
                os.makedirs(usenet_dir)
            except OSError as e:
                self.logger.error('Failed to create Usenet directory: %s', e)
                syslog('error', 'Failed to create Usenet directory: %s', e)
                return
                
        # Get the Usenet file path
        usenet_file = os.path.join(usenet_dir, 'usenet.mbox')
        
        try:
            # Open the Usenet file
            mbox = mailbox.mbox(usenet_file)
            
            # Add the message to the Usenet
            mbox.add(msg)
            
            # Close the Usenet file
            mbox.close()
            
        except (OSError, mailbox.Error) as e:
            self.logger.error('Failed to process Usenet message: %s', e)
            syslog('error', 'Failed to process Usenet message: %s', e)
            
    def reject(self, msg: Message, msgdata: Dict[str, Any], reason: str) -> None:
        """Reject a message from being added to Usenet.
        
        Args:
            msg: The email message
            msgdata: Additional message metadata
            reason: Reason for rejection
        """
        self.logger.warning('Rejected Usenet message: %s', reason)
        syslog('warning', 'Rejected Usenet message: %s', reason)
