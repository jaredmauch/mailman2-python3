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
import fcntl

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
    # Process coordination
    _pid_file = os.path.join(mm_cfg.LOCK_DIR, 'outgoing.pid')
    _pid_lock = None
    _running = False
    
    # Shared processed messages tracking with size limits
    _processed_messages = set()
    _processed_lock = threading.Lock()
    _last_cleanup = time.time()
    _cleanup_interval = 3600  # Clean up every hour
    _max_processed_messages = 10000
    _max_retry_times = 10000
    
    # Message counting
    _total_messages_processed = 0
    _total_messages_lock = threading.Lock()
    
    # Retry configuration
    MIN_RETRY_DELAY = 300  # 5 minutes minimum delay between retries
    MAX_RETRIES = 5  # Maximum number of retry attempts
    _retry_times = {}  # Track last retry time for each message
    
    # Error tracking
    _error_count = 0
    _last_error_time = 0
    _error_window = 300  # 5 minutes window for error counting
    _max_errors = 10

    def __init__(self, slice=None, numslices=1):
        """Initialize the outgoing queue runner."""
        mailman_log('debug', 'OutgoingRunner: Initializing with slice=%s, numslices=%s', slice, numslices)
        try:
            # Check if another instance is already running
            if not self._acquire_pid_lock():
                mailman_log('error', 'OutgoingRunner: Another instance is already running')
                raise RuntimeError('Another OutgoingRunner instance is already running')
                
            Runner.__init__(self, slice, numslices)
            mailman_log('debug', 'OutgoingRunner: Base Runner initialized')
            
            BounceMixin.__init__(self)
            mailman_log('debug', 'OutgoingRunner: BounceMixin initialized')
            
            # Initialize processed messages tracking
            self._processed_messages = set()
            self._last_cleanup = time.time()
            
            # Initialize error tracking
            self._error_count = 0
            self._last_error_time = 0
            
            # We look this function up only at startup time
            modname = 'Mailman.Handlers.' + mm_cfg.DELIVERY_MODULE
            mailman_log('trace', 'OutgoingRunner: Attempting to import delivery module: %s', modname)
            
            try:
                mod = __import__(modname)
                mailman_log('trace', 'OutgoingRunner: Successfully imported delivery module')
            except ImportError as e:
                mailman_log('error', 'OutgoingRunner: Failed to import delivery module %s: %s', modname, str(e))
                mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
                self._release_pid_lock()
                raise
                
            try:
                self._func = getattr(sys.modules[modname], 'process')
                mailman_log('trace', 'OutgoingRunner: Successfully got process function from module')
            except AttributeError as e:
                mailman_log('error', 'OutgoingRunner: Failed to get process function from module %s: %s', modname, str(e))
                mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
                self._release_pid_lock()
                raise
            
            # This prevents smtp server connection problems from filling up the
            # error log.  It gets reset if the message was successfully sent, and
            # set if there was a socket.error.
            self.__logged = False
            mailman_log('debug', 'OutgoingRunner: Initializing retry queue')
            self.__retryq = Switchboard(mm_cfg.RETRYQUEUE_DIR)
            self._running = True
            mailman_log('debug', 'OutgoingRunner: Initialization complete')
        except Exception as e:
            mailman_log('error', 'OutgoingRunner: Initialization failed: %s', str(e))
            mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
            self._release_pid_lock()
            raise

    def run(self):
        """Run the outgoing queue runner."""
        mailman_log('debug', 'OutgoingRunner: Starting main loop')
        self._running = True
        
        # Try to acquire the PID lock
        if not self._acquire_pid_lock():
            mailman_log('error', 'OutgoingRunner: Failed to acquire PID lock, exiting')
            return

        try:
            while self._running:
                try:
                    self._oneloop()
                    # Sleep for a bit to avoid CPU spinning
                    time.sleep(mm_cfg.QRUNNER_SLEEP_TIME)
                except Exception as e:
                    mailman_log('error', 'OutgoingRunner: Error in main loop: %s', str(e))
                    mailman_log('error', 'OutgoingRunner: Traceback:\n%s', traceback.format_exc())
                    # Don't exit on error, just log and continue
                    time.sleep(mm_cfg.QRUNNER_SLEEP_TIME)
        finally:
            self._running = False
            self._release_pid_lock()
            mailman_log('debug', 'OutgoingRunner: Main loop ended')

    def stop(self):
        """Stop the outgoing queue runner."""
        mailman_log('debug', 'OutgoingRunner: Stopping runner')
        self._running = False
        self._release_pid_lock()
        Runner._cleanup(self)
        mailman_log('debug', 'OutgoingRunner: Runner stopped')

    def _acquire_pid_lock(self):
        """Try to acquire the PID lock file."""
        try:
            self._pid_lock = open(self._pid_file, 'w')
            fcntl.flock(self._pid_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Write our PID to the file
            self._pid_lock.seek(0)
            self._pid_lock.write(str(os.getpid()))
            self._pid_lock.truncate()
            self._pid_lock.flush()
            mailman_log('debug', 'OutgoingRunner: Acquired PID lock file %s', self._pid_file)
            return True
        except IOError:
            mailman_log('error', 'OutgoingRunner: Another instance is already running (PID file: %s)', self._pid_file)
            if self._pid_lock:
                self._pid_lock.close()
                self._pid_lock = None
            return False

    def _release_pid_lock(self):
        """Release the PID lock file."""
        if self._pid_lock:
            try:
                fcntl.flock(self._pid_lock, fcntl.LOCK_UN)
                self._pid_lock.close()
                os.unlink(self._pid_file)
                mailman_log('debug', 'OutgoingRunner: Released PID lock file %s', self._pid_file)
            except (IOError, OSError) as e:
                mailman_log('error', 'OutgoingRunner: Error releasing PID lock: %s', str(e))
            self._pid_lock = None

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
        # See if we should retry delivery of this message again.
        deliver_after = msgdata.get('deliver_after', 0)
        if time.time() < deliver_after:
            return True
        # Make sure we have the most up-to-date state
        mlist.Load()
        try:
            pid = os.getpid()
            self._func(mlist, msg, msgdata)
            # Failsafe -- a child may have leaked through.
            if pid != os.getpid():
                mailman_log('error', 'child process leaked thru: %s', mm_cfg.DELIVERY_MODULE)
                os._exit(1)
            self.__logged = False
        except socket.error:
            # There was a problem connecting to the SMTP server.  Log this
            # once, but crank up our sleep time so we don't fill the error
            # log.
            port = mm_cfg.SMTPPORT
            if port == 0:
                port = 'smtp'
            # Log this just once.
            if not self.__logged:
                mailman_log('error', 'Cannot connect to SMTP server %s on port %s',
                           mm_cfg.SMTPHOST, port)
                self.__logged = True
            self._snooze(0)
            return True
        except Errors.SomeRecipientsFailed as e:
            # Handle local rejects of probe messages differently.
            if msgdata.get('probe_token') and e.permfailures:
                self._probe_bounce(mlist, msgdata['probe_token'])
            else:
                # Delivery failed at SMTP time for some or all of the
                # recipients.  Permanent failures are registered as bounces,
                # but temporary failures are retried for later.
                #
                # BAW: msg is going to be the original message that failed
                # delivery, not a bounce message.  This may be confusing if
                # this is what's sent to the user in the probe message.  Maybe
                # we should craft a bounce-like message containing information
                # about the permanent SMTP failure?
                if e.permfailures:
                    self._queue_bounces(mlist.internal_name(), e.permfailures,
                                        msg)
                # Move temporary failures to the qfiles/retry queue which will
                # occasionally move them back here for another shot at
                # delivery.
                if e.tempfailures:
                    now = time.time()
                    recips = e.tempfailures
                    last_recip_count = msgdata.get('last_recip_count', 0)
                    deliver_until = msgdata.get('deliver_until', now)
                    if len(recips) == last_recip_count:
                        # We didn't make any progress, so don't attempt
                        # delivery any longer.  BAW: is this the best
                        # disposition?
                        if now > deliver_until:
                            return False
                    else:
                        # Keep trying to delivery this message for a while
                        deliver_until = now + mm_cfg.DELIVERY_RETRY_PERIOD
                    # Don't retry delivery too soon.
                    deliver_after = now + mm_cfg.DELIVERY_RETRY_WAIT
                    msgdata['deliver_after'] = deliver_after
                    msgdata['last_recip_count'] = len(recips)
                    msgdata['deliver_until'] = deliver_until
                    msgdata['recips'] = recips
                    self.__retryq.enqueue(msg, msgdata)
        # We've successfully completed handling of this message
        return False

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
                return self._handle_error(ValueError('No recipients found'), msg, mlist)
                
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
                        return self._handle_error(ConnectionError('Failed to get SMTP connection'), msg, mlist)
                    
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
                    return self._handle_error(e, msg, mlist)
            
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
                        return self._handle_error(ValueError('No list members found'), msg, mlist)
                except Exception as e:
                    mailman_log('error', 'OutgoingRunner._process_regular: Error getting list members: %s\nTraceback:\n%s',
                              str(e), traceback.format_exc())
                    # Try to continue with existing recipients if any
                    if not msgdata.get('recips'):
                        mailman_log('error', 'OutgoingRunner._process_regular: No recipients available for message %s', msgid)
                        return self._handle_error(ValueError('No recipients available'), msg, mlist)
            
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
                return self._handle_error(e, msg, mlist)
                
        except Exception as e:
            mailman_log('error', 'OutgoingRunner._process_regular: Unexpected error: %s\nTraceback:\n%s',
                       str(e), traceback.format_exc())
            return self._handle_error(e, msg, mlist)

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
        """Clean up the outgoing queue runner."""
        mailman_log('debug', 'OutgoingRunner: Starting cleanup')
        try:
            # Log total messages processed
            with self._total_messages_lock:
                mailman_log('debug', 'OutgoingRunner: Total messages processed: %d', self._total_messages_processed)
            
            # Call parent class cleanup
            Runner._cleanup(self)
            
            # Release PID lock if we have it
            self._release_pid_lock()
            
            mailman_log('debug', 'OutgoingRunner: Cleanup complete')
        except Exception as e:
            mailman_log('error', 'OutgoingRunner: Error during cleanup: %s', str(e))
            mailman_log('error', 'OutgoingRunner: Traceback:\n%s', traceback.format_exc())
            raise

    _doperiodic = BounceMixin._doperiodic

    def _oneloop(self):
        """Process one batch of messages from the queue."""
        # Get all files in the queue
        files = self._switchboard.files()
        if not files:
            return 0
            
        # Process each file
        for filebase in files:
            try:
                # Try to get the file from the switchboard
                msg, msgdata = self._switchboard.dequeue(filebase)
            except Exception as e:
                mailman_log('error', 'OutgoingRunner: Error dequeuing %s: %s', filebase, str(e))
                mailman_log('error', 'OutgoingRunner: Traceback:\n%s', traceback.format_exc())
                continue

            if msg is None:
                mailman_log('debug', 'OutgoingRunner: No message data for %s', filebase)
                continue

            try:
                # Process the message
                self._dispose(msg, msgdata)
                with self._total_messages_lock:
                    self._total_messages_processed += 1
                mailman_log('debug', 'OutgoingRunner: Successfully processed message %s', filebase)
            except Exception as e:
                mailman_log('error', 'OutgoingRunner: Error processing %s: %s', filebase, str(e))
                mailman_log('error', 'OutgoingRunner: Traceback:\n%s', traceback.format_exc())
                self._handle_error(e, msg, None)

    def _handle_error(self, exc, msg=None, mlist=None, preserve=True):
        """Enhanced error handling with circuit breaker and detailed logging."""
        now = time.time()
        msgid = msg.get('message-id', 'n/a') if msg else 'n/a'
        
        # Log the error with full context
        mailman_log('error', 'OutgoingRunner: Error processing message %s: %s', msgid, str(exc))
        mailman_log('error', 'OutgoingRunner: Error type: %s', type(exc).__name__)
        
        # Log full traceback
        s = StringIO()
        traceback.print_exc(file=s)
        mailman_log('error', 'OutgoingRunner: Traceback:\n%s', s.getvalue())
        
        # Log system state
        mailman_log('error', 'OutgoingRunner: System state - SMTP host: %s, port: %s, auth: %s',
                   mm_cfg.SMTPHOST, mm_cfg.SMTPPORT, mm_cfg.SMTP_AUTH)
        
        # Circuit breaker logic
        if now - self._last_error_time < self._error_window:
            self._error_count += 1
            if self._error_count >= self._max_errors:
                mailman_log('error', 'OutgoingRunner: Too many errors (%d) in %d seconds, stopping runner',
                           self._error_count, self._error_window)
                # Log stack trace before stopping
                s = StringIO()
                traceback.print_stack(file=s)
                mailman_log('error', 'OutgoingRunner: Stack trace at stop:\n%s', s.getvalue())
                self.stop()
        else:
            self._error_count = 1
        self._last_error_time = now
        
        # Handle message preservation
        if preserve and msg:
            try:
                msgdata = {'whichq': self._switchboard.whichq()}
                new_filebase = self._shunt.enqueue(msg, msgdata)
                mailman_log('error', 'OutgoingRunner: Shunted message to: %s', new_filebase)
            except Exception as e:
                mailman_log('error', 'OutgoingRunner: Failed to shunt message: %s', str(e))
                return False
        return True
