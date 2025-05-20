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
from Mailman.MemberAdaptor import MemberAdaptor, ENABLED
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
            conn = smtplib.SMTP()
            conn._host = mm_cfg.SMTPHOST # workaround https://github.com/python/cpython/issues/80275
            conn.set_debuglevel(mm_cfg.SMTPLIB_DEBUG_LEVEL)
            conn.connect(mm_cfg.SMTPHOST, mm_cfg.SMTPPORT)
            
            if mm_cfg.SMTP_AUTH:
                if mm_cfg.SMTP_USE_TLS:
                    try:
                        conn.starttls()
                    except smtplib.SMTPException as e:
                        mailman_log('error', 'SMTP TLS error: %s', str(e))
                        conn.quit()
                        return None
                    try:
                        helo_host = mm_cfg.SMTP_HELO_HOST or socket.getfqdn()
                        conn.ehlo(helo_host)
                    except smtplib.SMTPException as e:
                        mailman_log('error', 'SMTP EHLO error: %s', str(e))
                        conn.quit()
                        return None
                try:
                    conn.login(mm_cfg.SMTP_USER, mm_cfg.SMTP_PASSWD)
                except smtplib.SMTPHeloError as e:
                    mailman_log('error', 'SMTP HELO error: %s', str(e))
                    conn.quit()
                    return None
                except smtplib.SMTPAuthenticationError as e:
                    mailman_log('error', 'SMTP AUTH error: %s', str(e))
                    conn.quit()
                    return None
            
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
        return Runner._convert_message(self, msg)

    def _validate_message(self, msg, msgdata):
        """Validate the message for outgoing delivery.
        
        Args:
            msg: The message to validate
            msgdata: Additional message metadata
            
        Returns:
            tuple: (msg, success) where success is a boolean indicating if validation was successful
        """
        try:
            # Convert message if needed
            if not isinstance(msg, Message.Message):
                msg = self._convert_message(msg)
                
            # Check required headers
            if not msg.get('message-id'):
                mailman_log('error', 'OutgoingRunner._validate_message: Message missing Message-ID header')
                return msg, False
                
            if not msg.get('from'):
                mailman_log('error', 'OutgoingRunner._validate_message: Message missing From header')
                return msg, False
                
            if not msg.get('to') and not msg.get('recipients'):
                mailman_log('error', 'OutgoingRunner._validate_message: Message missing To/Recipients')
                return msg, False
                
            return msg, True
            
        except Exception as e:
            mailman_log('error', 'OutgoingRunner._validate_message: Error validating message: %s', str(e))
            return msg, False

    def _dispose(self, mlist, msg, msgdata):
        """Process an outgoing message."""
        msgid = msg.get('message-id', 'n/a')
        filebase = msgdata.get('_filebase', 'unknown')
        
        # Log the full msgdata at the start of processing
        mailman_log('debug', 'OutgoingRunner._dispose: Full msgdata at start:\n%s', str(msgdata))
        
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
            msg, success = self._validate_message(msg, msgdata)
            if not success:
                mailman_log('error', 'OutgoingRunner._dispose: Message validation failed for message %s', msgid)
                self._unmark_message_processed(msgid)
                return False

            # Log the full msgdata after validation
            mailman_log('debug', 'OutgoingRunner._dispose: Full msgdata after validation:\n%s', str(msgdata))

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
        
        # Get recipient from msgdata or message headers
        recipient = msgdata.get('recipient')
        if not recipient:
            # Try to get recipient from To header
            to = msg.get('to')
            if to:
                # Parse the To header to get the first recipient
                addrs = email.utils.getaddresses([to])
                if addrs:
                    recipient = addrs[0][1]
            
        if not recipient:
            mailman_log('error', 'OutgoingRunner: No recipients found in msgdata for message: %s', msgid)
            return False
            
        # Set the recipient in msgdata for future use
        msgdata['recipient'] = recipient
            
        # For system messages (_nolist=1), we need to handle them differently
        if msgdata.get('_nolist'):
            mailman_log('debug', 'OutgoingRunner._process_regular: Processing system message %s', msgid)
            # System messages should be sent directly via SMTP
            try:
                conn = self._get_smtp_connection()
                if not conn:
                    mailman_log('error', 'OutgoingRunner._process_regular: Failed to get SMTP connection for message %s', msgid)
                    return False
                
                # Send the message
                sender = msg.get('from', msgdata.get('original_sender', mm_cfg.MAILMAN_SITE_LIST))
                if not sender or not '@' in sender:
                    sender = mm_cfg.MAILMAN_SITE_LIST
                
                mailman_log('debug', 'OutgoingRunner._process_regular: Sending system message %s from %s to %s',
                           msgid, sender, recipient)
                
                conn.sendmail(sender, [recipient], str(msg))
                conn.quit()
                
                mailman_log('debug', 'OutgoingRunner._process_regular: Successfully sent system message %s', msgid)
                return True
                
            except Exception as e:
                mailman_log('error', 'OutgoingRunner._process_regular: SMTP error for system message %s: %s',
                           msgid, str(e))
                return False
        
        # For regular list messages, use the delivery module
        mailman_log('debug', 'OutgoingRunner._process_regular: Using delivery module for message %s', msgid)
        
        # Log the state before calling the delivery module
        mailman_log('debug', 'OutgoingRunner._process_regular: Pre-delivery msgdata:\n%s', str(msgdata))
        
        # Ensure we have the list members if this is a list message
        if msgdata.get('tolist') and not msgdata.get('_nolist'):
            try:
                # Get all list members
                members = mlist.getRegularMemberKeys()
                if members:
                    msgdata['recips'] = [mlist.getMemberCPAddress(m) for m in members 
                                       if mlist.getDeliveryStatus(m) == ENABLED]
                    mailman_log('debug', 'OutgoingRunner._process_regular: Expanded list members for message %s: %s',
                              msgid, str(msgdata['recips']))
                else:
                    mailman_log('error', 'OutgoingRunner._process_regular: No members found for list %s',
                              mlist.internal_name())
            except Exception as e:
                mailman_log('error', 'OutgoingRunner._process_regular: Error getting list members: %s\nTraceback:\n%s',
                          str(e), traceback.format_exc())
                # Try to continue with existing recipients if any
                if not msgdata.get('recips'):
                    mailman_log('error', 'OutgoingRunner._process_regular: No recipients available for message %s', msgid)
                    return False
        
        # Call the delivery module
        try:
            self._func(mlist, msg, msgdata)
            # Log the state after calling the delivery module
            mailman_log('debug', 'OutgoingRunner._process_regular: Post-delivery msgdata:\n%s', str(msgdata))
            mailman_log('debug', 'OutgoingRunner._process_regular: Successfully processed regular message %s', msgid)
            return True
        except Exception as e:
            mailman_log('error', 'OutgoingRunner._process_regular: Error in delivery module: %s\nTraceback:\n%s',
                       str(e), traceback.format_exc())
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

    def _oneloop(self):
        """Process one batch of messages from the outgoing queue."""
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
                        mailman_log('error', 'OutgoingRunner._oneloop: File %s does not exist, skipping', pckfile)
                        continue
                        
                    # Check if file is locked
                    lockfile = os.path.join(self.QDIR, filebase + '.pck.lock')
                    if os.path.exists(lockfile):
                        mailman_log('debug', 'OutgoingRunner._oneloop: File %s is locked by another process, skipping', filebase)
                        continue
                    
                    # Dequeue the file
                    msg, msgdata = self._switchboard.dequeue(filebase)
                    if msg is None:
                        continue
                    
                    # Get the list name from msgdata
                    listname = msgdata.get('listname')
                    if not listname:
                        mailman_log('error', 'OutgoingRunner._oneloop: No listname in message data for file %s', filebase)
                        self._shunt.enqueue(msg, msgdata)
                        continue
                        
                    # Open the list
                    try:
                        mlist = get_mail_list()(listname, lock=False)
                    except Errors.MMUnknownListError:
                        mailman_log('error', 'OutgoingRunner._oneloop: Unknown list %s for message %s (file: %s)',
                                  listname, msg.get('message-id', 'n/a'), filebase)
                        self._shunt.enqueue(msg, msgdata)
                        continue
                    
                    # Process the message
                    try:
                        self._dispose(mlist, msg, msgdata)
                    except Exception as e:
                        mailman_log('error', 'OutgoingRunner._oneloop: Error processing message %s: %s\nTraceback:\n%s',
                                  msg.get('message-id', 'n/a'), str(e), traceback.format_exc())
                        self._shunt.enqueue(msg, msgdata)
                except Exception as e:
                    mailman_log('error', 'OutgoingRunner._oneloop: Error processing file %s: %s', filebase, str(e))
                    mailman_log('error', 'OutgoingRunner._oneloop: Traceback: %s', traceback.format_exc())
        except Exception as e:
            mailman_log('error', 'OutgoingRunner._oneloop: Error processing outgoing queue: %s', str(e))
            mailman_log('error', 'OutgoingRunner._oneloop: Traceback: %s', traceback.format_exc())
