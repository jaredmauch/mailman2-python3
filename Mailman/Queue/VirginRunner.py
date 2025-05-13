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


class VirginRunner(IncomingRunner):
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
                last_retry = self._retry_times.get(msgid, 0)
                time_since_last_retry = current_time - last_retry
                
                # Log detailed retry information at debug level
                mailman_log('debug', 'VirginRunner: Retry check for message %s (file: %s):', msgid, filebase)
                mailman_log('debug', '  Last retry time: %s', time.ctime(last_retry) if last_retry else 'Never')
                mailman_log('debug', '  Current time: %s', time.ctime(current_time))
                mailman_log('debug', '  Time since last retry: %d seconds', time_since_last_retry)
                mailman_log('debug', '  Minimum retry delay: %d seconds', self.MIN_RETRY_DELAY)
                
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

    def _dispose(self, listname, msg, msgdata):
        msgid = msg.get('message-id', 'n/a')
        filebase = msgdata.get('_filebase', 'unknown')
        
        # Check retry delay but don't shunt on timeout
        if not self._check_retry_delay(msgid, filebase):
            mailman_log('info', 'VirginRunner: Message %s (file: %s) not ready for processing yet, will retry',
                       msgid, filebase)
            return 1  # Return 1 to indicate we should keep trying

        try:
            # Get the MailList object
            try:
                mlist = MailList.MailList(listname, lock=0)
            except Exception as e:
                mailman_log('error', 'Failed to get MailList object for list %s: %s',
                           listname, str(e))
                return 1  # Return 1 to keep trying instead of shunting

            # Log start of processing
            mailman_log('info', 'VirginRunner: Starting to process virgin message %s (file: %s) for list %s',
                       msgid, filebase, mlist.internal_name())
            
            # We need to fasttrack this message through any handlers that touch
            # it.  E.g. especially CookHeaders.
            msgdata['_fasttrack'] = 1
            
            # Process through the pipeline
            result = IncomingRunner._dispose(self, mlist, msg, msgdata)
            
            # Log successful completion
            mailman_log('info', 'VirginRunner: Successfully processed virgin message %s (file: %s) for list %s',
                       msgid, filebase, mlist.internal_name())
            return result
        except Exception as e:
            # Enhanced error logging with more context
            mailman_log('error', 'Error processing virgin message %s for list %s: %s',
                   msgid, listname, str(e))
            mailman_log('error', 'Message details:')
            mailman_log('error', '  Message ID: %s', msgid)
            mailman_log('error', '  From: %s', msg.get('from', 'unknown'))
            mailman_log('error', '  To: %s', msg.get('to', 'unknown'))
            mailman_log('error', '  Subject: %s', msg.get('subject', '(no subject)'))
            mailman_log('error', '  Message type: %s', type(msg).__name__)
            mailman_log('error', '  Message data: %s', str(msgdata))
            mailman_log('error', 'Traceback:\n%s', traceback.format_exc())
            
            # Don't remove from processed messages on error, let it retry
            return 1  # Return 1 to keep trying instead of shunting

    def _get_pipeline(self, mlist, msg, msgdata):
        # It's okay to hardcode this, since it'll be the same for all
        # internally crafted messages.
        return ['CookHeaders', 'ToOutgoing']
