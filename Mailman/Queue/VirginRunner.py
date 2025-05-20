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
import email.header


class VirginRunner(IncomingRunner):
    QDIR = mm_cfg.VIRGINQUEUE_DIR
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
        IncomingRunner.__init__(self, slice, numslices)
        # VirginRunner is a subclass of IncomingRunner, but we want to use a
        # different pipeline for processing virgin messages.  The main
        # difference is that we don't need to do bounce detection, and we can
        # skip a few other checks.
        self._pipeline = self._get_pipeline()
        # VirginRunner is a subclass of IncomingRunner, but we want to use a
        # different pipeline for processing virgin messages.  The main
        # difference is that we don't need to do bounce detection, and we can
        # skip a few other checks.
        self._fasttrack = 1
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
        """Check if a message has already been processed.
        Returns True if the message can be processed, False if it's a duplicate."""
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
                subject = msg.get('subject', '')
                if isinstance(subject, email.header.Header):
                    subject = str(subject)
                subject = subject.lower()
                
                if 'welcome to the' in subject:
                    # Create a unique key based on subject, to, and from
                    to_addr = msg.get('to', '')
                    from_addr = msg.get('from', '')
                    if isinstance(to_addr, email.header.Header):
                        to_addr = str(to_addr)
                    if isinstance(from_addr, email.header.Header):
                        from_addr = str(from_addr)
                        
                    content_key = f"{subject}|{to_addr}|{from_addr}"
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
        # We need to fasttrack this message through any handlers that touch
        # it.  E.g. especially CookHeaders.
        msgdata['_fasttrack'] = 1
        return IncomingRunner._dispose(self, mlist, msg, msgdata)

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
