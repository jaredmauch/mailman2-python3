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

import time
import traceback

from Mailman import mm_cfg
from Mailman.Queue.Runner import Runner
from Mailman.Queue.Switchboard import Switchboard


class RetryRunner(Runner):
    QDIR = mm_cfg.RETRYQUEUE_DIR
    SLEEPTIME = mm_cfg.minutes(15)

    def __init__(self, slice=None, numslices=1):
        Runner.__init__(self, slice, numslices)
        self.__outq = Switchboard(mm_cfg.OUTQUEUE_DIR)

    def _dispose(self, mlist, msg, msgdata):
        msgid = msg.get('message-id', 'n/a')
        filebase = msgdata.get('_filebase', 'unknown')
        
        # Check retry delay and duplicate processing
        if not self._check_retry_delay(msgid, filebase):
            return True  # Keep in retry queue

        # Validate message type first
        msg, success = self._validate_message(msg, msgdata)
        if not success:
            mailman_log('error', 'Message validation failed for retry message')
            self._unmark_message_processed(msgid)
            return True  # Keep in retry queue

        try:
            # Log start of processing
            mailman_log('info', 'RetryRunner: Starting to process retry message %s (file: %s) for list %s',
                       msgid, filebase, mlist.internal_name())
            
            # Move it to the out queue for another retry if it's time.
            deliver_after = msgdata.get('deliver_after', 0)
            if time.time() < deliver_after:
                self._unmark_message_processed(msgid)
                return True  # Keep in retry queue
                
            # Move to outgoing queue
            self.__outq.enqueue(msg, msgdata)
            
            # Log successful completion
            mailman_log('info', 'RetryRunner: Successfully moved retry message %s (file: %s) to outgoing queue for list %s',
                       msgid, filebase, mlist.internal_name())
            return False  # Remove from retry queue
        except Exception as e:
            # Enhanced error logging with more context
            mailman_log('error', 'Error processing retry message %s for list %s: %s',
                   msgid, mlist.internal_name(), str(e))
            mailman_log('error', 'Message details:')
            mailman_log('error', '  Message ID: %s', msgid)
            mailman_log('error', '  From: %s', msg.get('from', 'unknown'))
            mailman_log('error', '  To: %s', msg.get('to', 'unknown'))
            mailman_log('error', '  Subject: %s', msg.get('subject', '(no subject)'))
            mailman_log('error', '  Message type: %s', type(msg).__name__)
            mailman_log('error', '  Message data: %s', str(msgdata))
            mailman_log('error', 'Traceback:\n%s', traceback.format_exc())
            
            # Remove from processed messages on error
            self._unmark_message_processed(msgid)
            return True  # Keep in retry queue

    def _snooze(self, filecnt):
        # We always want to snooze, but check for stop flag periodically
        for _ in range(self.SLEEPTIME):
            if self._stop:
                return
            time.sleep(1)
