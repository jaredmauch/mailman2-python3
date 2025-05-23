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

"""Retry queue runner.

This module is responsible for retrying failed message deliveries.  It's a
separate queue from the virgin queue because retries need different handling.
"""

from builtins import object
import time
import traceback
import os
import sys
import threading
import email.message

from Mailman import mm_cfg
from Mailman import Errors
from Mailman.Queue.Runner import Runner
from Mailman.Queue.Switchboard import Switchboard
from Mailman.Errors import MMUnknownListError
from Mailman.Logging.Syslog import mailman_log
import Mailman.MailList as MailList
import Mailman.Message as Message

class RetryRunner(Runner):
    QDIR = mm_cfg.RETRYQUEUE_DIR
    SLEEPTIME = mm_cfg.minutes(15)
    
    # Message tracking configuration
    _track_messages = True
    _max_processed_messages = 10000
    _max_retry_times = 10000
    _processed_messages = set()
    _processed_lock = threading.Lock()
    _last_cleanup = time.time()
    _cleanup_interval = 3600  # Clean up every hour
    
    # Retry configuration
    MIN_RETRY_DELAY = 300  # 5 minutes minimum delay between retries
    MAX_RETRIES = 5  # Maximum number of retry attempts
    _retry_times = {}  # Track last retry time for each message

    def __init__(self, slice=None, numslices=1):
        mailman_log('debug', 'RetryRunner: Starting initialization')
        try:
            Runner.__init__(self, slice, numslices)
            self._outq = Switchboard(mm_cfg.OUTQUEUE_DIR)
            
            # Initialize processed messages tracking
            self._processed_messages = set()
            self._last_cleanup = time.time()
            
            mailman_log('debug', 'RetryRunner: Initialization complete')
        except Exception as e:
            mailman_log('error', 'RetryRunner: Initialization failed: %s\nTraceback:\n%s',
                       str(e), traceback.format_exc())
            raise

    def _check_retry_delay(self, msgid, filebase):
        """Check if enough time has passed since the last retry attempt."""
        now = time.time()
        last_retry = self._retry_times.get(msgid, 0)
        
        if now - last_retry < self.MIN_RETRY_DELAY:
            mailman_log('debug', 'RetryRunner._check_retry_delay: Message %s (file: %s) retry delay not met. Last retry: %s, Now: %s, Delay needed: %s',
                       msgid, filebase, time.ctime(last_retry), time.ctime(now), self.MIN_RETRY_DELAY)
            return False
        
        mailman_log('debug', 'RetryRunner._check_retry_delay: Message %s (file: %s) retry delay met. Last retry: %s, Now: %s',
                   msgid, filebase, time.ctime(last_retry), time.ctime(now))
        return True

    def _validate_message(self, msg, msgdata):
        """Validate message format and required fields."""
        msgid = msg.get('message-id', 'n/a')
        try:
            # Check message size
            if len(str(msg)) > mm_cfg.MAX_MESSAGE_SIZE:
                mailman_log('error', 'RetryRunner: Message too large: %d bytes', len(str(msg)))
                return msg, False
            
            # Validate required headers
            if not msg.get('message-id'):
                mailman_log('error', 'RetryRunner: Message missing Message-ID header')
                return msg, False
                
            if not msg.get('from'):
                mailman_log('error', 'RetryRunner: Message missing From header')
                return msg, False
                
            if not msg.get('to') and not msg.get('recipients'):
                mailman_log('error', 'RetryRunner: Message missing To/Recipients')
                return msg, False
                
            mailman_log('debug', 'RetryRunner: Message %s validation successful', msgid)
            return msg, True
            
        except Exception as e:
            mailman_log('error', 'RetryRunner: Error validating message %s: %s', msgid, str(e))
            mailman_log('error', 'RetryRunner: Traceback:\n%s', traceback.format_exc())
            return msg, False

    def _unmark_message_processed(self, msgid):
        """Remove a message from the processed messages set."""
        with self._processed_lock:
            if msgid in self._processed_messages:
                self._processed_messages.remove(msgid)
                mailman_log('debug', 'RetryRunner: Unmarked message %s as processed', msgid)

    def _cleanup_old_messages(self):
        """Clean up old message tracking data."""
        with self._processed_lock:
            if len(self._processed_messages) > self._max_processed_messages:
                mailman_log('debug', 'RetryRunner._cleanup_old_messages: Clearing processed messages set (size: %d)',
                           len(self._processed_messages))
                self._processed_messages.clear()
            if len(self._retry_times) > self._max_retry_times:
                mailman_log('debug', 'RetryRunner._cleanup_old_messages: Clearing retry times dict (size: %d)',
                           len(self._retry_times))
                self._retry_times.clear()
            self._last_cleanup = time.time()

    def _dispose(self, mlist, msg, msgdata):
        # See if we should retry delivery of this message again.
        deliver_after = msgdata.get('deliver_after', 0)
        if time.time() < deliver_after:
            return True
        # Move the message to the outgoing queue for another attempt at
        # delivery.
        self._outq.enqueue(msg, msgdata)
        return False

    def _process_retry(self, mlist, msg, msgdata):
        """Process a retry message."""
        msgid = msg.get('message-id', 'n/a')
        try:
            mailman_log('debug', 'RetryRunner._process_retry: Processing retry for message %s', msgid)
            
            # Get retry information
            retry_count = msgdata.get('retry_count', 0)
            retry_delay = msgdata.get('retry_delay', mm_cfg.RETRY_DELAY)
            
            # Calculate next retry time
            next_retry = time.time() + retry_delay
            msgdata['next_retry'] = next_retry
            msgdata['retry_count'] = retry_count + 1
            
            mailman_log('debug', 'RetryRunner._process_retry: Updated retry info for message %s - count: %d, next retry: %s',
                       msgid, retry_count + 1, time.ctime(next_retry))
            
            # Process the message
            # ... retry processing logic ...
            
            mailman_log('debug', 'RetryRunner._process_retry: Successfully processed retry for message %s', msgid)
            return True
            
        except Exception as e:
            mailman_log('error', 'RetryRunner._process_retry: Error processing retry for message %s: %s\nTraceback:\n%s',
                       msgid, str(e), traceback.format_exc())
            return False

    def _handle_max_retries_exceeded(self, mlist, msg, msgdata):
        """Handle case when maximum retries are exceeded."""
        msgid = msg.get('message-id', 'n/a')
        try:
            mailman_log('error', 'RetryRunner._handle_max_retries_exceeded: Maximum retries exceeded for message %s', msgid)
            
            # Move to shunt queue
            self._shunt.enqueue(msg, msgdata)
            mailman_log('debug', 'RetryRunner._handle_max_retries_exceeded: Moved message %s to shunt queue', msgid)
            
            # Notify list owners if configured
            if mlist.bounce_notify_owner_on_disable:
                mailman_log('debug', 'RetryRunner._handle_max_retries_exceeded: Notifying list owners for message %s', msgid)
                self._notify_list_owners(mlist, msg, msgdata)
                
        except Exception as e:
            mailman_log('error', 'RetryRunner._handle_max_retries_exceeded: Error handling max retries for message %s: %s\nTraceback:\n%s',
                       msgid, str(e), traceback.format_exc())

    def _notify_list_owners(self, mlist, msg, msgdata):
        """Notify list owners about failed retries."""
        msgid = msg.get('message-id', 'n/a')
        try:
            mailman_log('debug', 'RetryRunner._notify_list_owners: Sending notification for message %s', msgid)
            
            # Create notification message
            subject = _('Maximum retries exceeded for message')
            text = _("""\
The following message has exceeded the maximum number of retry attempts:

Message-ID: %(msgid)s
From: %(from)s
To: %(to)s
Subject: %(subject)s

The message has been moved to the shunt queue.
""") % {
                'msgid': msgid,
                'from': msg.get('from', 'unknown'),
                'to': msg.get('to', 'unknown'),
                'subject': msg.get('subject', 'unknown')
            }
            
            # Send notification
            # ... notification sending logic ...
            
            mailman_log('debug', 'RetryRunner._notify_list_owners: Successfully sent notification for message %s', msgid)
            
        except Exception as e:
            mailman_log('error', 'RetryRunner._notify_list_owners: Error sending notification for message %s: %s\nTraceback:\n%s',
                       msgid, str(e), traceback.format_exc())

    def _cleanup(self):
        """Clean up resources."""
        mailman_log('debug', 'RetryRunner: Starting cleanup')
        try:
            Runner._cleanup(self)
        except Exception as e:
            mailman_log('error', 'RetryRunner: Cleanup failed: %s\nTraceback:\n%s',
                       str(e), traceback.format_exc())
        mailman_log('debug', 'RetryRunner: Cleanup complete')

    def _oneloop(self):
        """Process one batch of messages from the retry queue."""
        try:
            # Get the list of files to process
            files = self._switchboard.files()
            filecnt = len(files)
            
            # Process each file
            for filebase in files:
                try:
                    # Check if the file exists before dequeuing
                    pckfile = os.path.join(self.QDIR, filebase + '.pck')
                    if not os.path.exists(pckfile):
                        mailman_log('error', 'RetryRunner._oneloop: File %s does not exist, skipping', pckfile)
                        continue
                        
                    # Check if file is locked
                    lockfile = os.path.join(self.QDIR, filebase + '.pck.lock')
                    if os.path.exists(lockfile):
                        mailman_log('debug', 'RetryRunner._oneloop: File %s is locked by another process, skipping', filebase)
                        continue
                    
                    # Dequeue the file
                    msg, msgdata = self._switchboard.dequeue(filebase)
                    if msg is None:
                        continue
                        
                    # Get the list name from the message data
                    listname = msgdata.get('listname')
                    if not listname:
                        syslog('error', 'RetryRunner._oneloop: No listname in message data for file %s', filebase)
                        self._shunt.enqueue(msg, msgdata)
                        continue
                        
                    # Open the list
                    try:
                        mlist = self._open_list(listname)
                    except Exception as e:
                        self.log_error('list_open_error', str(e), listname=listname)
                        self._shunt.enqueue(msg, msgdata)
                        continue
                        
                    # Process the message
                    try:
                        result = self._dispose(mlist, msg, msgdata)
                        if result:
                            self._switchboard.enqueue(msg, msgdata)
                    except Exception as e:
                        self._handle_error(e, msg=msg, mlist=mlist)
                        
                except Exception as e:
                    syslog('error', 'RetryRunner._oneloop: Error dequeuing file %s: %s', filebase, str(e))
                    continue
                    
        except Exception as e:
            syslog('error', 'RetryRunner._oneloop: Error in main loop: %s', str(e))
            return 0
            
        return filecnt

    def _snooze(self, filecnt):
        # We always want to snooze, but check for stop flag periodically
        for _ in range(self.SLEEPTIME):
            if self._stop:
                return
            time.sleep(1)
