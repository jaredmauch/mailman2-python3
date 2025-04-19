# Copyright (C) 2003-2018 by the Free Software Foundation, Inc.
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

"""Retry queue runner.

This module handles the retry queue for messages that need to be retried
after a temporary failure. It moves messages from the retry queue to the
outgoing queue when it's time to retry delivery.
"""

import time
import logging
from typing import Any, Dict, Optional, Union

from email.message import Message

from Mailman import mm_cfg
from Mailman.Queue.Runner import Runner
from Mailman.Queue.Switchboard import Switchboard


class RetryRunner(Runner):
    """Runner for retry queue.
    
    This class handles the retry queue for messages that need to be retried
    after a temporary failure. It moves messages from the retry queue to the
    outgoing queue when it's time to retry delivery.
    """
    
    QDIR = mm_cfg.RETRYQUEUE_DIR
    SLEEPTIME = mm_cfg.minutes(15)

    def __init__(self, slice: Optional[int] = None, numslices: int = 1) -> None:
        """Initialize the retry runner.
        
        Args:
            slice: Optional slice number for parallel processing
            numslices: Total number of slices for parallel processing
        """
        Runner.__init__(self, slice, numslices)
        self.__outq = Switchboard(mm_cfg.OUTQUEUE_DIR)
        self.logger = logging.getLogger('mailman.retry')

    def _dispose(self, mlist: Any, msg: Message, msgdata: Dict[str, Any]) -> bool:
        """Dispose of a message by moving it to the outgoing queue.
        
        Args:
            mlist: The mailing list object
            msg: The email message
            msgdata: Additional message metadata
            
        Returns:
            bool: True if message should be retried, False if moved to outgoing queue
        """
        # Move it to the out queue for another retry if it's time.
        deliver_after = msgdata.get('deliver_after', 0)
        if time.time() < deliver_after:
            return True
        self.logger.info('Moving message to outgoing queue for retry')
        self.__outq.enqueue(msg, msgdata)
        return False

    def _snooze(self, filecnt: int) -> None:
        """Sleep for the configured time.
        
        Args:
            filecnt: Number of files processed
        """
        # We always want to snooze
        time.sleep(self.SLEEPTIME)
