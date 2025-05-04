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
    # Shared processed messages tracking
    _processed_messages = set()
    _processed_lock = threading.Lock()
    _last_cleanup = time.time()
    _cleanup_interval = 3600  # Clean up every hour
    
    # Retry delay configuration
    MIN_RETRY_DELAY = 300  # 5 minutes minimum delay between retries
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

    def _dispose(self, mlist, msg, msgdata):
        """Process an outgoing message."""
        msgid = msg.get('message-id', 'n/a')
        filebase = msgdata.get('_filebase', 'unknown')
        
        # Check for duplicate messages with proper locking
        with self._processed_lock:
            if msgid in self._processed_messages:
                mailman_log('error', 'OutgoingRunner: Duplicate message detected: %s (file: %s)', msgid, filebase)
                return False
                
            # Clean up old message IDs periodically
            current_time = time.time()
            if current_time - self._last_cleanup > self._cleanup_interval:
                self._processed_messages.clear()
                self._retry_times.clear()  # Also clean up retry times
                self._last_cleanup = current_time
                
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
            self._func(mlist, msg, msgdata)
            
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
                # Requeue with delay
                self.__retryq.enqueue(msg, msgdata)
            return False

    def _queue_bounces(self, mlist, msg, msgdata, failures):
        """Queue bounce messages for failed deliveries."""
        msgid = msg.get('message-id', 'n/a')
        try:
            for recip, code, errmsg in failures:
                mailman_log('error', 'OutgoingRunner: Delivery failure for msgid: %s - Recipient: %s, Code: %s, Error: %s',
                           msgid, recip, code, errmsg)
                BounceMixin._queue_bounce(self, mlist, msg, recip, code, errmsg)
        except Exception as e:
            mailman_log('error', 'OutgoingRunner: Error queueing bounce for msgid: %s - %s', msgid, str(e))
            mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())

    def _cleanup(self):
        """Clean up resources."""
        mailman_log('debug', 'OutgoingRunner: Starting cleanup')
        BounceMixin._cleanup(self)
        Runner._cleanup(self)
        mailman_log('debug', 'OutgoingRunner: Cleanup complete')

    _doperiodic = BounceMixin._doperiodic

    def _validate_message(self, msg, msgdata):
        """Validate and convert message if needed.
        
        Returns a tuple of (msg, success) where success is a boolean indicating
        if validation was successful.
        """
        msgid = msg.get('message-id', 'n/a')
        try:
            # Convert email.message.Message to Mailman.Message if needed
            if isinstance(msg, email.message.Message) and not isinstance(msg, Message):
                mailman_log('debug', 'OutgoingRunner: Converting email.message.Message to Mailman.Message for %s', msgid)
                mailman_msg = Message()
                # Copy all attributes from the original message
                for key, value in msg.items():
                    mailman_msg[key] = value
                # Copy the payload
                if msg.is_multipart():
                    for part in msg.get_payload():
                        mailman_msg.attach(part)
                else:
                    mailman_msg.set_payload(msg.get_payload())
                msg = mailman_msg
                mailman_log('debug', 'OutgoingRunner: Successfully converted message %s', msgid)
            
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
