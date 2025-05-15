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

"""Virgin message queue runner.

This qrunner handles messages that the Mailman system gives virgin birth to.
E.g. acknowledgement responses to user posts or Replybot messages.  They need
to go through some minimal processing before they can be sent out to the
recipient.
"""

from Mailman import mm_cfg
from Mailman.Queue.Runner import Runner
from Mailman.Queue.IncomingRunner import IncomingRunner
from Mailman.Logging.Syslog import mailman_log
import time
import traceback
from Mailman import Errors
import threading


class VirginRunner(IncomingRunner):
    QDIR = mm_cfg.VIRGINQUEUE_DIR
    # Override the minimum retry delay for virgin messages
    MIN_RETRY_DELAY = 60  # 1 minute minimum delay between retries
    # Maximum age for message tracking data
    _max_tracking_age = 86400  # 24 hours in seconds
    # Cleanup interval for message tracking data
    _cleanup_interval = 3600  # 1 hour in seconds

    # Message tracking configuration
    _processed_messages = set()
    _processed_lock = threading.Lock()
    _last_cleanup = time.time()
    _max_processed_messages = 10000
    _processed_times = {}  # Track processing times for messages

    def __init__(self, slice=None, numslices=1):
        mailman_log('debug', 'VirginRunner: Starting initialization')
        try:
            Runner.__init__(self, slice, numslices)
            
            # Initialize processed messages tracking
            self._processed_messages = set()
            self._processed_times = {}
            self._last_cleanup = time.time()
            
            mailman_log('debug', 'VirginRunner: Initialization complete')
        except Exception as e:
            mailman_log('error', 'VirginRunner: Initialization failed: %s\nTraceback:\n%s',
                       str(e), traceback.format_exc())
            raise

    def _check_message_processed(self, msgid, filebase, msg):
        """Check if a message has already been processed and if retry delay is met.
        Returns True if the message can be processed, False if it's a duplicate or retry delay not met."""
        try:
            with self._processed_lock:
                current_time = time.time()
                
                # Check if cleanup is needed
                if current_time - self._last_cleanup > self._cleanup_interval:
                    try:
                        mailman_log('debug', 'VirginRunner: Starting cleanup of old message tracking data')
                        # Only clean up entries older than cleanup_interval
                        cutoff_time = current_time - self._cleanup_interval
                        # Clean up old message IDs
                        old_msgids = [mid for mid, process_time in self._processed_times.items() 
                                    if process_time < cutoff_time]
                        for mid in old_msgids:
                            self._processed_times.pop(mid, None)
                            self._processed_messages.discard(mid)
                        self._last_cleanup = current_time
                        mailman_log('debug', 'VirginRunner: Cleaned up %d old message entries', len(old_msgids))
                    except Exception as e:
                        mailman_log('error', 'VirginRunner: Error during cleanup: %s', str(e))
                        # Continue processing even if cleanup fails
                
                # For welcome messages, check content and recipients
                subject = msg.get('subject', '').lower()
                if 'welcome to the' in subject:
                    # Create a unique key based on subject, to, and from
                    content_key = f"{subject}|{msg.get('to', '')}|{msg.get('from', '')}"
                    if content_key in self._processed_messages:
                        mailman_log('info', 'VirginRunner: Duplicate welcome message detected: %s (file: %s)',
                                   content_key, filebase)
                        return False
                    # Mark this content as processed
                    self._processed_messages.add(content_key)
                    self._processed_times[content_key] = current_time
                    return True
                
                # For other messages, check message ID
                if msgid in self._processed_messages:
                    # Check retry delay
                    last_retry = self._processed_times.get(msgid)
                    if last_retry is not None:
                        time_since_last_retry = current_time - last_retry
                        if time_since_last_retry < self.MIN_RETRY_DELAY:
                            mailman_log('info', 'VirginRunner: Message %s (file: %s) retry delay not met. Time since last retry: %d seconds, minimum required: %d seconds',
                                       msgid, filebase, time_since_last_retry, self.MIN_RETRY_DELAY)
                            return False
                        else:
                            mailman_log('debug', 'VirginRunner: Message %s (file: %s) retry delay met. Time since last retry: %d seconds',
                                       msgid, filebase, time_since_last_retry)
                    else:
                        mailman_log('info', 'VirginRunner: Duplicate message detected: %s (file: %s)',
                                   msgid, filebase)
                        return False
                
                # Mark message as processed
                try:
                    self._processed_messages.add(msgid)
                    self._processed_times[msgid] = current_time
                    mailman_log('debug', 'VirginRunner: Message %s (file: %s) marked for processing',
                               msgid, filebase)
                    return True
                except Exception as e:
                    # If we fail to update the tracking data, remove the message from processed set
                    self._processed_messages.discard(msgid)
                    self._processed_times.pop(msgid, None)
                    mailman_log('error', 'VirginRunner: Failed to update tracking data for message %s: %s',
                               msgid, str(e))
                    return False
                    
        except Exception as e:
            mailman_log('error', 'VirginRunner: Unexpected error in message check for %s: %s',
                       msgid, str(e))
            return False

    def _dispose(self, mlist, msg, msgdata):
        """Process a virgin message."""
        msgid = msg.get('message-id', 'n/a')
        filebase = msgdata.get('_filebase', 'unknown')
        
        # Check if message has already been processed
        if not self._check_message_processed(msgid, filebase, msg):
            self._shunt.enqueue(msg, msgdata)
            return False
        
        mailman_log('debug', 'VirginRunner._dispose: Starting to process virgin message %s (file: %s)',
                   msgid, filebase)
        
        # Ensure we have a MailList object
        if isinstance(mlist, str):
            try:
                # Lazy import to avoid circular dependency
                from Mailman.MailList import MailList
                mlist = MailList(mlist, lock=0)
                should_unlock = True
            except Errors.MMUnknownListError:
                mailman_log('error', 'VirginRunner: Unknown list %s', mlist)
                self._shunt.enqueue(msg, msgdata)
                return False
        else:
            should_unlock = False
        
        try:
            # Process the message using IncomingRunner's _dispose method
            result = super()._dispose(mlist, msg, msgdata)
            
            mailman_log('debug', 'VirginRunner._dispose: Finished processing virgin message %s (file: %s)',
                       msgid, filebase)
            
            return result
        except Exception as e:
            # Log the full traceback for debugging
            mailman_log('error', 'VirginRunner: Error processing message %s (file: %s):\n%s\nTraceback:\n%s',
                       msgid, filebase, str(e), traceback.format_exc())
            # Move the message to the shunt queue
            self._shunt.enqueue(msg, msgdata)
            return False
        finally:
            if should_unlock:
                mlist.Unlock()

    def _get_pipeline(self, mlist, msg, msgdata):
        # It's okay to hardcode this, since it'll be the same for all
        # internally crafted messages.
        return ['CookHeaders', 'ToOutgoing']

    def _cleanup_old_messages(self):
        """Clean up old message tracking data."""
        with self._processed_lock:
            if len(self._processed_messages) > self._max_processed_messages:
                mailman_log('debug', 'VirginRunner._cleanup_old_messages: Clearing processed messages set (size: %d)',
                           len(self._processed_messages))
                self._processed_messages.clear()
            if len(self._processed_times) > self._max_processed_messages:
                mailman_log('debug', 'VirginRunner._cleanup_old_messages: Clearing processed times dict (size: %d)',
                           len(self._processed_times))
                self._processed_times.clear()
            self._last_cleanup = time.time()

    def _onefile(self, msg, msgdata):
        """Process a single file from the queue."""
        # Ensure _dispose always gets a MailList object, not a string
        listname = msgdata.get('listname')
        if not listname:
            listname = mm_cfg.MAILMAN_SITE_LIST
        try:
            # Lazy import to avoid circular dependency
            from Mailman.MailList import MailList
            mlist = MailList(listname, lock=0)
        except Errors.MMUnknownListError:
            mailman_log('error', 'VirginRunner: Unknown list %s', listname)
            self._shunt.enqueue(msg, msgdata)
            return False
        try:
            keepqueued = self._dispose(mlist, msg, msgdata)
            if keepqueued:
                self._switchboard.enqueue(msg, msgdata)
            return keepqueued
        finally:
            mlist.Unlock()

    def _unmark_message_processed(self, msgid):
        """Remove a message from the processed messages set."""
        with self._processed_lock:
            if msgid in self._processed_messages:
                self._processed_messages.remove(msgid)
                if msgid in self._processed_times:
                    del self._processed_times[msgid]
                mailman_log('debug', 'VirginRunner: Unmarked message %s as processed', msgid)
