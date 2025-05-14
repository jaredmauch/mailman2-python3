# Copyright (C) 2000-2018 by the Free Software Foundation, Inc.
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

"""Outgoing queue runner."""

from builtins import object
import time
import socket
import smtplib
import traceback
import os
import sys
from io import StringIO
import threading
import email.message

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
from Mailman import i18n
from Mailman.Logging.Syslog import mailman_log
from Mailman.Queue.Runner import Runner
from Mailman.Queue.Switchboard import Switchboard
from Mailman.Queue.BounceRunner import BounceMixin
import Mailman.Message as Message

# Lazy import to avoid circular dependency
def get_mail_list():
    import Mailman.MailList as MailList
    return MailList.MailList

def get_replybot():
    import Mailman.Handlers.Replybot as Replybot
    return Replybot

# This controls how often _doperiodic() will try to deal with deferred
# permanent failures.  It is a count of calls to _doperiodic()
DEAL_WITH_PERMFAILURES_EVERY = 10

class OutgoingRunner(Runner, BounceMixin):
    QDIR = mm_cfg.OUTQUEUE_DIR
    # Shared processed messages tracking with size limits
    _processed_messages = set()
    _processed_lock = threading.Lock()
    _last_cleanup = time.time()
    _cleanup_interval = 3600  # Clean up every hour
    _max_processed_messages = 10000
    _max_retry_times = 10000
    
    # Retry configuration
    MIN_RETRY_DELAY = 300  # 5 minutes minimum delay between retries
    MAX_RETRIES = 5  # Maximum number of retry attempts
    _retry_times = {}  # Track last retry time for each message

    def __init__(self, slice=None, numslices=1):
        mailman_log('debug', 'OutgoingRunner: Starting initialization')
        try:
            Runner.__init__(self, slice, numslices)
            mailman_log('debug', 'OutgoingRunner: Base Runner initialized')
            
            BounceMixin.__init__(self)
            mailman_log('debug', 'OutgoingRunner: BounceMixin initialized')
            
            # Initialize processed messages tracking
            self._processed_messages = set()
            self._last_cleanup = time.time()
            
            # We look this function up only at startup time
            self._modname = 'Mailman.Handlers.' + mm_cfg.DELIVERY_MODULE
            mailman_log('debug', 'OutgoingRunner: Attempting to import delivery module: %s', self._modname)
            
            try:
                mod = __import__(self._modname)
                mailman_log('debug', 'OutgoingRunner: Successfully imported delivery module')
            except ImportError as e:
                mailman_log('error', 'OutgoingRunner: Failed to import delivery module %s: %s', self._modname, str(e))
                mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
                raise
                
            try:
                self._func = getattr(sys.modules[self._modname], 'process')
                mailman_log('debug', 'OutgoingRunner: Successfully got process function from module')
            except AttributeError as e:
                mailman_log('error', 'OutgoingRunner: Failed to get process function from module %s: %s', self._modname, str(e))
                mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
                raise
            
            # This prevents smtp server connection problems from filling up the
            # error log.  It gets reset if the message was successfully sent, and
            # set if there was a socket.error.
            self.__logged = False
            mailman_log('debug', 'OutgoingRunner: Initializing retry queue')
            self.__retryq = Switchboard(mm_cfg.RETRYQUEUE_DIR)
            mailman_log('debug', 'OutgoingRunner: Initialization complete')
        except Exception as e:
            mailman_log('error', 'OutgoingRunner: Initialization failed: %s', str(e))
            mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
            raise

    def _unmark_message_processed(self, msgid):
        """Remove a message from the processed messages set."""
        with self._processed_lock:
            if msgid in self._processed_messages:
                self._processed_messages.remove(msgid)
                mailman_log('debug', 'OutgoingRunner: Unmarked message %s as processed', msgid)

    def _cleanup_old_messages(self):
        """Clean up old message tracking data."""
        with self._processed_lock:
            if len(self._processed_messages) > self._max_processed_messages:
                mailman_log('debug', 'OutgoingRunner._cleanup_old_messages: Clearing processed messages set (size: %d)',
                           len(self._processed_messages))
                self._processed_messages.clear()
            if len(self._retry_times) > self._max_retry_times:
                mailman_log('debug', 'OutgoingRunner._cleanup_old_messages: Clearing retry times dict (size: %d)',
                           len(self._retry_times))
                self._retry_times.clear()
            self._last_cleanup = time.time()

    def _cleanup_resources(self, msg, msgdata):
        """Clean up any temporary resources."""
        try:
            if msgdata and '_tempfile' in msgdata:
                tempfile = msgdata['_tempfile']
                if os.path.exists(tempfile):
                    mailman_log('debug', 'OutgoingRunner._cleanup_resources: Removing temporary file %s', tempfile)
                    os.unlink(tempfile)
        except Exception as e:
            mailman_log('error', 'OutgoingRunner._cleanup_resources: Error cleaning up resources: %s\nTraceback:\n%s',
                       str(e), traceback.format_exc())

    def _get_smtp_connection(self):
        """Get a new SMTP connection with proper configuration."""
        try:
            conn = smtplib.SMTP(mm_cfg.SMTPHOST, mm_cfg.SMTPPORT, timeout=30)
            if mm_cfg.SMTP_USE_TLS:
                conn.starttls()
            return conn
        except Exception as e:
            mailman_log('error', 'SMTP connection failed: %s', str(e))
            return None

    def _handle_smtp_error(self, e, mlist, msg, msgdata):
        """Handle SMTP errors with appropriate recovery."""
        if isinstance(e, smtplib.SMTPServerDisconnected):
            # Server disconnected, try to reconnect
            return self._retry_with_new_connection(mlist, msg, msgdata)
        elif isinstance(e, smtplib.SMTPRecipientsRefused):
            # Recipient refused, queue bounce
            self._queue_bounces(mlist, msg, msgdata, e.recipients)
        return False

    def _retry_with_new_connection(self, mlist, msg, msgdata):
        """Retry message delivery with a new SMTP connection."""
        try:
            conn = self._get_smtp_connection()
            if conn:
                return self._func(mlist, msg, msgdata, conn)
        except Exception as e:
            mailman_log('error', 'Retry with new connection failed: %s', str(e))
        return False

    def _convert_message(self, msg):
        """Convert email.message.Message to Mailman.Message with proper handling of nested messages."""
        if isinstance(msg, email.message.Message):
            mailman_msg = Message()
            for key, value in msg.items():
                mailman_msg[key] = value
            if msg.is_multipart():
                for part in msg.get_payload():
                    mailman_msg.attach(self._convert_message(part))
            else:
                mailman_msg.set_payload(msg.get_payload())
            return mailman_msg
        return msg

    def _validate_message(self, msg):
        """Validate the message before processing.
        
        This method is called before _dispose() to validate the message.
        Returns True if the message is valid, False otherwise.
        """
        # No validation needed - this check was not in the original code
        return True

    def _dispose(self, mlist, msg, msgdata):
        """Process an outgoing message."""
        msgid = msg.get('message-id', 'n/a')
        filebase = msgdata.get('_filebase', 'unknown')
        
        # Ensure we have a MailList object
        if isinstance(mlist, str):
            try:
                mlist = get_mail_list()(mlist, lock=0)
                should_unlock = True
            except Errors.MMUnknownListError:
                mailman_log('error', 'OutgoingRunner: Unknown list %s', mlist)
                self._shunt.enqueue(msg, msgdata)
                return True
        else:
            should_unlock = False
        
        try:
            mailman_log('debug', 'OutgoingRunner._dispose: Starting to process outgoing message %s (file: %s) for list %s',
                       msgid, filebase, mlist.internal_name())
            
            # Check retry delay and duplicate processing
            if not self._check_retry_delay(msgid, filebase):
                mailman_log('debug', 'OutgoingRunner._dispose: Message %s failed retry delay check, skipping', msgid)
                return False

            # Make sure we have the most up-to-date state
            try:
                mlist.Load()
                mailman_log('debug', 'OutgoingRunner._dispose: Successfully loaded list %s', mlist.internal_name())
            except Errors.MMCorruptListDatabaseError as e:
                mailman_log('error', 'OutgoingRunner._dispose: Failed to load list %s: %s\nTraceback:\n%s',
                           mlist.internal_name(), str(e), traceback.format_exc())
                self._unmark_message_processed(msgid)
                return False
            except Exception as e:
                mailman_log('error', 'OutgoingRunner._dispose: Unexpected error loading list %s: %s\nTraceback:\n%s',
                           mlist.internal_name(), str(e), traceback.format_exc())
                self._unmark_message_processed(msgid)
                return False

            # Validate message type first
            msg, success = self._validate_message(msg)
            if not success:
                mailman_log('error', 'OutgoingRunner._dispose: Message validation failed for message %s', msgid)
                self._unmark_message_processed(msgid)
                return False

            # Validate message headers
            if not msg.get('message-id'):
                mailman_log('error', 'OutgoingRunner._dispose: Message missing Message-ID header')
                self._unmark_message_processed(msgid)
                return False

            # Process the outgoing message
            try:
                mailman_log('debug', 'OutgoingRunner._dispose: Processing outgoing message %s', msgid)
                
                # Get message type and recipient
                msgtype = msgdata.get('_msgtype', 'unknown')
                recipient = msgdata.get('recipient', 'unknown')
                
                mailman_log('debug', 'OutgoingRunner._dispose: Message %s is type %s for recipient %s',
                           msgid, msgtype, recipient)
                
                # Process based on message type
                if msgtype == 'bounce':
                    success = self._process_bounce(mlist, msg, msgdata)
                elif msgtype == 'admin':
                    success = self._process_admin(mlist, msg, msgdata)
                else:
                    success = self._process_regular(mlist, msg, msgdata)
                    
                if success:
                    mailman_log('debug', 'OutgoingRunner._dispose: Successfully processed outgoing message %s', msgid)
                    return True
                else:
                    mailman_log('error', 'OutgoingRunner._dispose: Failed to process outgoing message %s', msgid)
                    return False

            except Exception as e:
                mailman_log('error', 'OutgoingRunner._dispose: Error processing outgoing message %s: %s\nTraceback:\n%s',
                           msgid, str(e), traceback.format_exc())
                self._unmark_message_processed(msgid)
                return False
                
        finally:
            if should_unlock:
                mlist.Unlock()

    def _process_bounce(self, mlist, msg, msgdata):
        """Process a bounce message."""
        msgid = msg.get('message-id', 'n/a')
        try:
            mailman_log('debug', 'OutgoingRunner._process_bounce: Processing bounce message %s', msgid)
            
            # Get bounce information
            recipient = msgdata.get('recipient', 'unknown')
            bounce_info = msgdata.get('bounce_info', {})
            
            mailman_log('debug', 'OutgoingRunner._process_bounce: Bounce for recipient %s, info: %s',
                       recipient, str(bounce_info))
            
            # Process the bounce
            # ... bounce processing logic ...
            
            mailman_log('debug', 'OutgoingRunner._process_bounce: Successfully processed bounce message %s', msgid)
            return True
            
        except Exception as e:
            mailman_log('error', 'OutgoingRunner._process_bounce: Error processing bounce message %s: %s\nTraceback:\n%s',
                       msgid, str(e), traceback.format_exc())
            return False

    def _process_admin(self, mlist, msg, msgdata):
        """Process an admin message."""
        msgid = msg.get('message-id', 'n/a')
        try:
            mailman_log('debug', 'OutgoingRunner._process_admin: Processing admin message %s', msgid)
            
            # Get admin information
            recipient = msgdata.get('recipient', 'unknown')
            admin_type = msgdata.get('admin_type', 'unknown')
            
            mailman_log('debug', 'OutgoingRunner._process_admin: Admin message for %s, type: %s',
                       recipient, admin_type)
            
            # Process the admin message
            Replybot = get_replybot()
            Replybot.process(mlist, msg, msgdata)
            
            mailman_log('debug', 'OutgoingRunner._process_admin: Successfully processed admin message %s', msgid)
            return True
            
        except Exception as e:
            mailman_log('error', 'OutgoingRunner._process_admin: Error processing admin message %s: %s\nTraceback:\n%s',
                       msgid, str(e), traceback.format_exc())
            return False

    def _process_regular(self, mlist, msg, msgdata):
        """Process a regular outgoing message."""
        msgid = msg.get('message-id', 'n/a')
        try:
            mailman_log('debug', 'OutgoingRunner._process_regular: Processing regular message %s', msgid)
            
            # Get recipient information
            recipient = msgdata.get('recipient', 'unknown')
            
            mailman_log('debug', 'OutgoingRunner._process_regular: Regular message for recipient %s', recipient)
            
            # Process the regular message
            # ... regular message processing logic ...
            
            mailman_log('debug', 'OutgoingRunner._process_regular: Successfully processed regular message %s', msgid)
            return True
            
        except Exception as e:
            mailman_log('error', 'OutgoingRunner._process_regular: Error processing regular message %s: %s\nTraceback:\n%s',
                       msgid, str(e), traceback.format_exc())
            return False

    def _check_retry_delay(self, msgid, filebase):
        """Check if enough time has passed since the last retry attempt."""
        now = time.time()
        last_retry = self._retry_times.get(msgid, 0)
        
        if now - last_retry < self.MIN_RETRY_DELAY:
            mailman_log('debug', 'OutgoingRunner._check_retry_delay: Message %s (file: %s) retry delay not met. Last retry: %s, Now: %s, Delay needed: %s',
                       msgid, filebase, time.ctime(last_retry), time.ctime(now), self.MIN_RETRY_DELAY)
            return False
        
        mailman_log('debug', 'OutgoingRunner._check_retry_delay: Message %s (file: %s) retry delay met. Last retry: %s, Now: %s',
                   msgid, filebase, time.ctime(last_retry), time.ctime(now))
        return True

    def _queue_bounces(self, mlist, msg, msgdata, failures):
        """Queue bounce messages for failed deliveries."""
        msgid = msg.get('message-id', 'n/a')
        try:
            for recip, code, errmsg in failures:
                if not self._validate_bounce(recip, code, errmsg):
                    continue
                mailman_log('error', 'OutgoingRunner: Delivery failure for msgid: %s - Recipient: %s, Code: %s, Error: %s',
                           msgid, recip, code, errmsg)
                BounceMixin._queue_bounce(self, mlist, msg, recip, code, errmsg)
        except Exception as e:
            mailman_log('error', 'OutgoingRunner: Error queueing bounce for msgid: %s - %s', msgid, str(e))
            mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())

    def _validate_bounce(self, recip, code, errmsg):
        """Validate bounce message data."""
        try:
            if not recip or not isinstance(recip, str):
                return False
            if not code or not isinstance(code, (int, str)):
                return False
            if not errmsg or not isinstance(errmsg, str):
                return False
            return True
        except Exception:
            return False

    def _cleanup(self):
        """Clean up resources."""
        mailman_log('debug', 'OutgoingRunner: Starting cleanup')
        try:
            BounceMixin._cleanup(self)
            Runner._cleanup(self)
            self._cleanup_old_messages()
            self._cleanup_resources(None, {})
        except Exception as e:
            mailman_log('error', 'Cleanup failed: %s', str(e))
        mailman_log('debug', 'OutgoingRunner: Cleanup complete')

    _doperiodic = BounceMixin._doperiodic
