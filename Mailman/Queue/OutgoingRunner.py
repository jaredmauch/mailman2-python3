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
from Mailman.Message import Message
from Mailman.Logging.Syslog import mailman_log
from Mailman.Queue.Runner import Runner
from Mailman.Queue.Switchboard import Switchboard
from Mailman.Queue.BounceRunner import BounceMixin

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

    def _cleanup_old_messages(self):
        """Clean up old message tracking data."""
        with self._processed_lock:
            if len(self._processed_messages) > self._max_processed_messages:
                self._processed_messages.clear()
            if len(self._retry_times) > self._max_retry_times:
                self._retry_times.clear()
            self._last_cleanup = time.time()

    def _cleanup_resources(self, msg, msgdata):
        """Clean up any temporary resources."""
        try:
            if msgdata and '_tempfile' in msgdata:
                os.unlink(msgdata['_tempfile'])
        except Exception as e:
            mailman_log('error', 'Error cleaning up resources: %s', str(e))

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

    def _validate_message(self, msg, msgdata):
        """Validate and convert message if needed.
        
        Returns a tuple of (msg, success) where success is a boolean indicating
        if validation was successful.
        """
        msgid = msg.get('message-id', 'n/a')
        try:
            # Check message size
            if len(str(msg)) > mm_cfg.MAX_MESSAGE_SIZE:
                mailman_log('error', 'Message too large: %d bytes', len(str(msg)))
                return msg, False

            # Convert message if needed
            msg = self._convert_message(msg)
            
            # Validate required Mailman.Message methods
            required_methods = ['get_sender', 'get', 'items', 'is_multipart', 'get_payload']
            missing_methods = []
            for method in required_methods:
                if not hasattr(msg, method):
                    missing_methods.append(method)
            
            if missing_methods:
                mailman_log('error', 'OutgoingRunner: Message %s missing required methods: %s', 
                           msgid, ', '.join(missing_methods))
                return msg, False
                
            # Validate message headers
            if not msg.get('message-id'):
                mailman_log('error', 'OutgoingRunner: Message %s missing Message-ID header', msgid)
                return msg, False
                
            if not msg.get('from'):
                mailman_log('error', 'OutgoingRunner: Message %s missing From header', msgid)
                return msg, False
                
            if not msg.get('to') and not msg.get('recipients'):
                mailman_log('error', 'OutgoingRunner: Message %s missing To/Recipients', msgid)
                return msg, False
                
            mailman_log('debug', 'OutgoingRunner: Message %s validation successful', msgid)
            return msg, True
            
        except Exception as e:
            mailman_log('error', 'OutgoingRunner: Error validating message %s: %s', msgid, str(e))
            mailman_log('error', 'OutgoingRunner: Traceback:\n%s', traceback.format_exc())
            return msg, False

    def _dispose(self, mlist, msg, msgdata):
        """Process an outgoing message."""
        msgid = msg.get('message-id', 'n/a')
        filebase = msgdata.get('_filebase', 'unknown')
        
        # Check retry count
        retry_count = msgdata.get('_retry_count', 0)
        if retry_count >= self.MAX_RETRIES:
            mailman_log('error', 'Message %s exceeded maximum retries', msgid)
            return False
        
        with self._processed_lock:
            if msgid in self._processed_messages:
                mailman_log('error', 'OutgoingRunner: Duplicate message detected: %s (file: %s)', msgid, filebase)
                return False
                
            # Clean up old message IDs periodically
            current_time = time.time()
            if current_time - self._last_cleanup > self._cleanup_interval:
                self._cleanup_old_messages()
                
            # Check retry delay
            last_retry = self._retry_times.get(msgid, 0)
            time_since_last_retry = current_time - last_retry
            if time_since_last_retry < self.MIN_RETRY_DELAY:
                mailman_log('info', 'OutgoingRunner: Message %s (file: %s) retried too soon, delaying. Time since last retry: %d seconds',
                           msgid, filebase, time_since_last_retry)
                # Requeue with delay
                self.__retryq.enqueue(msg, msgdata)
                return False
                
            # Mark message as being processed and update retry time
            self._processed_messages.add(msgid)
            self._retry_times[msgid] = current_time
            
        try:
            # Log start of processing
            mailman_log('info', 'OutgoingRunner: Starting to process message %s (file: %s) for list %s',
                       msgid, filebase, mlist.internal_name())
            
            # Validate message type first
            msg, success = self._validate_message(msg, msgdata)
            if not success:
                mailman_log('error', 'Message validation failed for outgoing message %s', msgid)
                with self._processed_lock:
                    self._processed_messages.remove(msgid)
                return False

            # Process the message through the delivery module
            try:
                self._func(mlist, msg, msgdata)
            except smtplib.SMTPException as e:
                return self._handle_smtp_error(e, mlist, msg, msgdata)
            
            # Log successful completion
            mailman_log('info', 'OutgoingRunner: Successfully processed message %s (file: %s) for list %s',
                       msgid, filebase, mlist.internal_name())
            return True
        except Exception as e:
            # Enhanced error logging with more context
            mailman_log('error', 'Error processing outgoing message %s for list %s: %s',
                   msgid, mlist.internal_name(), str(e))
            mailman_log('error', 'Message details:')
            mailman_log('error', '  Message ID: %s', msgid)
            mailman_log('error', '  From: %s', msg.get('from', 'unknown'))
            mailman_log('error', '  To: %s', msg.get('to', 'unknown'))
            mailman_log('error', '  Subject: %s', msg.get('subject', '(no subject)'))
            mailman_log('error', '  Message type: %s', type(msg).__name__)
            mailman_log('error', '  Message data: %s', str(msgdata))
            mailman_log('error', 'Traceback:\n%s', traceback.format_exc())
            
            # Remove from processed messages on error and requeue
            with self._processed_lock:
                self._processed_messages.remove(msgid)
                # Update retry count and requeue
                msgdata['_retry_count'] = retry_count + 1
                self.__retryq.enqueue(msg, msgdata)
            return False
        finally:
            self._cleanup_resources(msg, msgdata)

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
