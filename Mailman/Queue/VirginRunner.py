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
from Mailman import MailList
import time
import traceback
from Mailman import Errors


class VirginRunner(Runner):
    QDIR = mm_cfg.VIRGINQUEUE_DIR
    # Override the minimum retry delay for virgin messages
    MIN_RETRY_DELAY = 60  # 1 minute minimum delay between retries

    def _check_retry_delay(self, msgid, filebase):
        """Check if enough time has passed since the last retry attempt.
        Returns True if the message can be processed, False if it should be delayed."""
        try:
            with self._processed_lock:
                current_time = time.time()
                
                # Check if cleanup is needed
                if current_time - self._last_cleanup > self._cleanup_interval:
                    try:
                        mailman_log('debug', 'VirginRunner: Starting cleanup of old message tracking data')
                        # Only clean up entries older than cleanup_interval
                        cutoff_time = current_time - self._cleanup_interval
                        # Clean up retry times first
                        old_msgids = [mid for mid, retry_time in self._retry_times.items() 
                                    if retry_time < cutoff_time]
                        for mid in old_msgids:
                            self._retry_times.pop(mid, None)
                            self._processed_messages.discard(mid)
                        self._last_cleanup = current_time
                        mailman_log('debug', 'VirginRunner: Cleaned up %d old message entries', len(old_msgids))
                    except Exception as e:
                        mailman_log('error', 'VirginRunner: Error during cleanup: %s', str(e))
                        # Continue processing even if cleanup fails
                
                # Check retry delay
                last_retry = self._retry_times.get(msgid)
                time_since_last_retry = 0 if last_retry is None else current_time - last_retry
                
                # Log detailed retry information at debug level
                mailman_log('debug', 'VirginRunner: Retry check for message %s (file: %s):', msgid, filebase)
                mailman_log('debug', '  Last retry time: %s', time.ctime(last_retry) if last_retry else 'Never')
                mailman_log('debug', '  Current time: %s', time.ctime(current_time))
                mailman_log('debug', '  Time since last retry: %d seconds', time_since_last_retry)
                mailman_log('debug', '  Minimum retry delay: %d seconds', self.MIN_RETRY_DELAY)
                
                # If message has never been retried (last_retry is None), it can be processed
                if last_retry is None:
                    # Update both data structures atomically
                    try:
                        self._processed_messages.add(msgid)
                        self._retry_times[msgid] = current_time
                        mailman_log('debug', 'VirginRunner: Message %s (file: %s) has never been retried, proceeding with processing',
                                   msgid, filebase)
                        return True
                    except Exception as e:
                        # If we fail to update the tracking data, remove the message from processed set
                        self._processed_messages.discard(msgid)
                        self._retry_times.pop(msgid, None)
                        mailman_log('error', 'VirginRunner: Failed to update tracking data for message %s: %s',
                                   msgid, str(e))
                        return False
                
                # For messages that have been retried before, check the delay
                if time_since_last_retry < self.MIN_RETRY_DELAY:
                    # Log at info level when retry check fails
                    mailman_log('info', 'VirginRunner: Message %s (file: %s) retried too soon. Time since last retry: %d seconds, minimum required: %d seconds',
                               msgid, filebase, time_since_last_retry, self.MIN_RETRY_DELAY)
                    return False
                
                # Update both data structures atomically
                try:
                    self._processed_messages.add(msgid)
                    self._retry_times[msgid] = current_time
                    mailman_log('debug', 'VirginRunner: Message %s (file: %s) passed retry check, proceeding with processing',
                               msgid, filebase)
                    return True
                except Exception as e:
                    # If we fail to update the tracking data, remove the message from processed set
                    self._processed_messages.discard(msgid)
                    self._retry_times.pop(msgid, None)
                    mailman_log('error', 'VirginRunner: Failed to update tracking data for message %s: %s',
                               msgid, str(e))
                    return False
                    
        except Exception as e:
            mailman_log('error', 'VirginRunner: Unexpected error in retry check for message %s: %s',
                       msgid, str(e))
            return False

    def _dispose(self, mlist, msg, msgdata):
        """Process a virgin message."""
        msgid = msg.get('message-id', 'n/a')
        filebase = msgdata.get('_filebase', 'unknown')
        
        mailman_log('debug', 'VirginRunner._dispose: Starting to process virgin message %s (file: %s)',
                   msgid, filebase)
        
        # Ensure we have a MailList object
        if isinstance(mlist, str):
            try:
                mlist = MailList.MailList(mlist, lock=0)
                should_unlock = True
            except Errors.MMUnknownListError:
                mailman_log('error', 'VirginRunner: Unknown list %s', mlist)
                self._shunt.enqueue(msg, msgdata)
                return
        else:
            should_unlock = False
        
        try:
            # Get the IncomingRunner class
            from Mailman.Queue import get_incoming_runner
            IncomingRunner = get_incoming_runner()
            
            # Process the message using IncomingRunner's _dispose method
            result = IncomingRunner._dispose(self, mlist, msg, msgdata)
            
            mailman_log('debug', 'VirginRunner._dispose: Finished processing virgin message %s (file: %s)',
                       msgid, filebase)
            
            return result
        finally:
            if should_unlock:
                mlist.Unlock()

    def _get_pipeline(self, mlist, msg, msgdata):
        # It's okay to hardcode this, since it'll be the same for all
        # internally crafted messages.
        return ['CookHeaders', 'ToOutgoing']

    def _cleanup_old_messages(self):
        """Clean up old message tracking data."""
        try:
            mailman_log('debug', 'VirginRunner: Starting cleanup of old message tracking data')
            now = time.time()
            old_msgids = []
            for msgid, last_retry in list(self._retry_times.items()):
                if now - last_retry > self._max_retry_age:
                    old_msgids.append(msgid)
            for msgid in old_msgids:
                del self._retry_times[msgid]
            mailman_log('debug', 'VirginRunner: Cleaned up %d old message entries', len(old_msgids))
        except Exception as e:
            mailman_log('error', 'VirginRunner: Error during cleanup: %s', str(e))

    def _onefile(self, msg, msgdata):
        # Ensure _dispose always gets a MailList object, not a string
        listname = msgdata.get('listname')
        if not listname:
            listname = mm_cfg.MAILMAN_SITE_LIST
        try:
            mlist = MailList.MailList(listname, lock=0)
        except Errors.MMUnknownListError:
            mailman_log('error', 'VirginRunner: Unknown list %s', listname)
            self._shunt.enqueue(msg, msgdata)
            return
        try:
            keepqueued = self._dispose(mlist, msg, msgdata)
        finally:
            mlist.Unlock()
        if keepqueued:
            self._switchboard.enqueue(msg, msgdata)
