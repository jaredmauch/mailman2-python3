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

"""Generic queue runner class.
"""

from builtins import object
import time
import traceback
from io import StringIO
from functools import wraps
import threading
import os

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
import Mailman.MailList as MailList
from Mailman import i18n
import Mailman.Message as Message
from Mailman.Logging.Syslog import syslog
from Mailman.Queue.Switchboard import Switchboard

import email.errors


class Runner:
    QDIR = None
    SLEEPTIME = mm_cfg.QRUNNER_SLEEP_TIME
    MIN_RETRY_DELAY = 300  # 5 minutes minimum delay between retries
    MAX_BACKOFF = 60  # Maximum backoff time in seconds
    INITIAL_BACKOFF = 1  # Initial backoff time in seconds
    
    # Message tracking configuration - can be overridden by subclasses
    _track_messages = False  # Whether to track processed messages
    _max_processed_messages = 10000  # Maximum number of messages to track
    _max_retry_times = 10000  # Maximum number of retry times to track
    _processed_messages = set()  # Set of processed message IDs
    _processed_lock = threading.Lock()  # Lock for thread safety
    _retry_times = {}  # Dictionary of retry times
    _last_cleanup = time.time()  # Last cleanup time
    _cleanup_interval = 3600  # Cleanup interval in seconds
    _current_backoff = INITIAL_BACKOFF  # Current backoff time in seconds
    _last_mtime = 0  # Last directory modification time

    def __init__(self, slice=None, numslices=1):
        syslog('debug', '%s: Starting initialization', self.__class__.__name__)
        try:
            self._stop = 0
            self._slice = slice
            self._numslices = numslices
            self._kids = {}
            # Create our own switchboard.  Don't use the switchboard cache because
            # we want to provide slice and numslice arguments.
            self._switchboard = Switchboard(self.QDIR, slice, numslices, True)
            # Create the shunt switchboard
            self._shunt = Switchboard(mm_cfg.SHUNTQUEUE_DIR)
            
            # Initialize message tracking attributes
            self._track_messages = self.__class__._track_messages
            self._max_processed_messages = self.__class__._max_processed_messages
            self._max_retry_times = self.__class__._max_retry_times
            self._processed_messages = set()
            self._processed_lock = threading.Lock()
            self._retry_times = {}
            self._last_cleanup = time.time()
            self._cleanup_interval = 3600
            
            # Initialize error tracking attributes
            self._last_error_time = 0
            self._error_count = 0
            
            self._current_backoff = self.INITIAL_BACKOFF
            self._last_mtime = 0
            
            syslog('debug', '%s: Initialization complete', self.__class__.__name__)
        except Exception as e:
            syslog('error', '%s: Initialization failed: %s\nTraceback:\n%s',
                   self.__class__.__name__, str(e), traceback.format_exc())
            raise

    def __repr__(self):
        return '<%s at %s>' % (self.__class__.__name__, id(self))

    def stop(self):
        self._stop = True

    def run(self):
        # Start the main loop for this queue runner.
        try:
            try:
                while True:
                    # Once through the loop that processes all the files in
                    # the queue directory.
                    filecnt = self._oneloop()
                    # Do the periodic work for the subclass.  BAW: this
                    # shouldn't be called here.  There should be one more
                    # _doperiodic() call at the end of the _oneloop() loop.
                    self._doperiodic()
                    # If the stop flag is set, we're done.
                    if self._stop:
                        break
                    # Give the runner an opportunity to snooze for a while,
                    # but pass it the file count so it can decide whether to
                    # do more work now or not.
                    self._snooze(filecnt)
            except KeyboardInterrupt:
                pass
        finally:
            # We've broken out of our main loop, so we want to reap all the
            # subprocesses we've created and do any other necessary cleanups.
            self._cleanup()

    def log_error(self, error_type, error_msg, **kwargs):
        """Log an error with the given type and message.
        
        Args:
            error_type: A string identifying the type of error
            error_msg: The error message to log
            **kwargs: Additional context to include in the log message
        """
        context = {
            'runner': self.__class__.__name__,
            'error_type': error_type,
            'error_msg': error_msg,
        }
        context.update(kwargs)
        
        # Format the error message
        msg_parts = ['%s: %s' % (error_type, error_msg)]
        if 'msg' in context:
            msg_parts.append('Message-ID: %s' % context['msg'].get('message-id', 'unknown'))
        if 'listname' in context:
            msg_parts.append('List: %s' % context['listname'])
        if 'traceback' in context:
            msg_parts.append('Traceback:\n%s' % context['traceback'])
            
        # Log the error
        syslog('error', ' '.join(msg_parts))

    def log_warning(self, warning_type, msg=None, mlist=None, **context):
        """Structured warning logging with context."""
        context.update({
            'runner': self.__class__.__name__,
            'list': mlist.internal_name() if mlist else 'N/A',
            'msg_id': msg.get('message-id', 'N/A') if msg else 'N/A',
            'warning_type': warning_type
        })
        syslog('warning', '%(runner)s: %(warning_type)s - list: %(list)s, msg: %(msg_id)s',
            context)

    def log_info(self, info_type, msg=None, mlist=None, **context):
        """Structured info logging with context."""
        context.update({
            'runner': self.__class__.__name__,
            'list': mlist.internal_name() if mlist else 'N/A',
            'msg_id': msg.get('message-id', 'N/A') if msg else 'N/A',
            'info_type': info_type
        })
        syslog('info', '%(runner)s: %(info_type)s - list: %(list)s, msg: %(msg_id)s',
            context)

    def _handle_error(self, exc, msg=None, mlist=None, preserve=True):
        """Centralized error handling with circuit breaker."""
        now = time.time()
        
        # Log the error with full context
        self.log_error('unhandled_exception', exc, msg=msg, mlist=mlist)
        
        # Log full traceback
        s = StringIO()
        traceback.print_exc(file=s)
        syslog('error', 'Traceback: %s', s.getvalue())
        
        # Circuit breaker logic
        if now - self._last_error_time < 60:  # Within last minute
            self._error_count += 1
            if self._error_count >= 10:  # Too many errors in short time
                syslog('error', '%s: Too many errors, stopping runner', self.__class__.__name__)
                # Log stack trace before stopping
                s = StringIO()
                traceback.print_stack(file=s)
                syslog('error', 'Stack trace at stop:\n%s', s.getvalue())
                self.stop()
        else:
            self._error_count = 1
        self._last_error_time = now
        
        # Handle message preservation
        if preserve:
            try:
                msgdata = {'whichq': self._switchboard.whichq()}
                new_filebase = self._shunt.enqueue(msg, msgdata)
                syslog('error', '%s: Shunted message to: %s', self.__class__.__name__, new_filebase)
            except Exception as e:
                syslog('error', '%s: Failed to shunt message: %s', self.__class__.__name__, str(e))
                return False
        return True

    def _oneloop(self):
        """Run one iteration of the runner's main loop.
        
        Returns:
            int: Number of files processed, or 0 if no files found
        """
        # Check if directory has been modified since last check
        try:
            st = os.stat(self.QDIR)
            current_mtime = st.st_mtime
            if current_mtime <= self._last_mtime:
                # Directory hasn't changed, use backoff
                self._snooze(self._current_backoff)
                # Double the backoff time, up to MAX_BACKOFF
                self._current_backoff = min(self._current_backoff * 2, self.MAX_BACKOFF)
                return 0
            # Directory has changed, reset backoff
            self._current_backoff = self.INITIAL_BACKOFF
            self._last_mtime = current_mtime
        except OSError as e:
            syslog('error', '%s: Error checking directory %s: %s',
                   self.__class__.__name__, self.QDIR, str(e))
            return 0

        # Process files in the directory
        files = self._switchboard.files()
        if not files:
            syslog('debug', '%s: No files to process', self.__class__.__name__)
            return 0

        # Process each file
        for filebase in files:
            if self._stop:
                break
            try:
                # Ask the switchboard for the message and metadata objects
                # associated with this filebase.
                msg, msgdata = self._switchboard.dequeue(filebase)
                self._onefile(msg, msgdata)
                self._switchboard.finish(filebase)
            except Exception as e:
                # All runners that implement _dispose() must guarantee that
                # exceptions are caught and dealt with properly.  Still, there
                # may be a bug in the infrastructure, and we do not want those
                # to cause messages to be lost.  Any uncaught exceptions will
                # cause the message to be stored in the shunt queue for human
                # intervention.
                self._log(e)
                # Put a marker in the metadata for unshunting
                msgdata['whichq'] = self._switchboard.whichq()
                # It is possible that shunting can throw an exception, e.g. a
                # permissions problem or a MemoryError due to a really large
                # message.  Try to be graceful.
                try:
                    new_filebase = self._shunt.enqueue(msg, msgdata)
                    syslog('error', 'SHUNTING: %s', new_filebase)
                    self._switchboard.finish(filebase)
                except Exception as e:
                    # The message wasn't successfully shunted.  Log the
                    # exception and try to preserve the original queue entry
                    # for possible analysis.
                    self._log(e)
                    syslog('error',
                           'SHUNTING FAILED, preserving original entry: %s',
                           filebase)
                    self._switchboard.finish(filebase, preserve=True)
            # Other work we want to do each time through the loop
            Utils.reap(self._kids, once=True)
            self._doperiodic()
            if self._shortcircuit():
                break
        return len(files)

    def _convert_message(self, msg):
        """Convert email.message.Message to Mailman.Message with proper handling of nested messages.
        
        Args:
            msg: The message to convert
            
        Returns:
            Mailman.Message: The converted message
        """
        if isinstance(msg, email.message.Message):
            mailman_msg = Message.Message()
            # Copy all attributes from the original message
            for key, value in msg.items():
                mailman_msg[key] = value
            # Copy the payload
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
            # Convert message if needed
            if not isinstance(msg, Message.Message):
                # Only log conversion if it's a significant event
                if msg.is_multipart() or len(msg.get_payload()) > 1000:
                    syslog('debug', 'Runner._validate_message: Converting complex message %s to Mailman.Message', msgid)
                msg = self._convert_message(msg)
            
            # Validate required Mailman.Message methods
            required_methods = ['get_sender', 'get', 'items', 'is_multipart', 'get_payload']
            missing_methods = []
            for method in required_methods:
                if not hasattr(msg, method):
                    missing_methods.append(method)
            
            if missing_methods:
                syslog('error', 'Runner._validate_message: Message %s missing required methods: %s', 
                       msgid, ', '.join(missing_methods))
                return msg, False
                
            # Validate message headers
            if not msg.get('message-id'):
                syslog('error', 'Runner._validate_message: Message %s missing Message-ID header', msgid)
                return msg, False
                
            if not msg.get('from'):
                syslog('error', 'Runner._validate_message: Message %s missing From header', msgid)
                return msg, False
                
            if not msg.get('to') and not msg.get('recipients'):
                syslog('error', 'Runner._validate_message: Message %s missing To/Recipients', msgid)
                return msg, False
                
            # Only log successful validation for complex messages
            if msg.is_multipart() or len(msg.get_payload()) > 1000:
                syslog('debug', 'Runner._validate_message: Complex message %s validation successful', msgid)
            return msg, True
            
        except Exception as e:
            syslog('error', 'Runner._validate_message: Error validating message %s: %s\nTraceback:\n%s',
                   msgid, str(e), traceback.format_exc())
            return msg, False

    def _onefile(self, mlist, msg, msgdata):
        """Process a single file from the queue."""
        try:
            # Get the list name from the message data
            listname = msgdata.get('listname')
            if not listname:
                syslog('error', 'Runner._onefile: No listname in message data')
                self._handle_error(ValueError('No listname in message data'), msg=msg, mlist=None)
                return False
                
            # Open the list
            try:
                mlist = self._open_list(listname)
            except Exception as e:
                self._handle_error(e, msg=msg, mlist=None)
                return False
                
            # Process the message
            try:
                result = self._dispose(mlist, msg, msgdata)
                if result:
                    # If _dispose returns True, requeue the message
                    self._switchboard.enqueue(msg, msgdata)
                    # Only log significant events
                    if msg.is_multipart() or len(msg.get_payload()) > 1000:
                        syslog('debug', 'Runner._onefile: Complex message requeued for %s', listname)
                else:
                    # If _dispose returns False, finish processing and remove the file
                    self._switchboard.finish(msgdata.get('filebase', ''))
                    # Only log significant events
                    if msg.is_multipart() or len(msg.get_payload()) > 1000:
                        syslog('debug', 'Runner._onefile: Complex message processing completed for %s', listname)
                return result
            except Exception as e:
                self._handle_error(e, msg=msg, mlist=mlist)
                return False
            finally:
                if mlist:
                    mlist.Unlock()
                    
        except Exception as e:
            self._handle_error(e, msg=msg, mlist=None)
            return False

    def _open_list(self, listname):
        try:
            import Mailman.MailList as MailList
            mlist = MailList.MailList(listname, lock=False)
        except Errors.MMListError as e:
            self.log_error('list_open_error', e, listname=listname)
            return None
        return mlist

    def _doperiodic(self):
        """Do some processing `every once in a while'.

        Called every once in a while both from the Runner's main loop, and
        from the Runner's hash slice processing loop.  You can do whatever
        special periodic processing you want here, and the return value is
        irrelevant.
        """
        pass

    def _snooze(self, secs):
        """Sleep for the specified number of seconds, but wake up if the
        stop flag is set.

        Args:
            secs: Number of seconds to sleep.
        """
        endtime = time.time() + secs
        while time.time() < endtime and not self._stop:
            time.sleep(0.1)

    def _shortcircuit(self):
        """Return a true value if the individual file processing loop should
        exit before it's finished processing each message in the current slice
        of hash space.  A false value tells _oneloop() to continue processing
        until the current snapshot of hash space is exhausted.

        You could, for example, implement a throttling algorithm here.
        """
        return self._stop

    #
    # Subclasses can override these methods.
    #
    def _cleanup(self):
        """Clean up resources."""
        syslog('debug', '%s: Starting cleanup', self.__class__.__name__)
        try:
            self._cleanup_old_messages()
            # Clean up any stale locks
            self._switchboard.cleanup_stale_locks()
        except Exception as e:
            syslog('error', '%s: Cleanup failed: %s\nTraceback:\n%s',
                   self.__class__.__name__, str(e), traceback.format_exc())
        syslog('debug', '%s: Cleanup complete', self.__class__.__name__)

    def _dispose(self, mlist, msg, msgdata):
        """Dispose of a single message destined for a mailing list.

        Called for each message that the Runner is responsible for, this is
        the primary overridable method for processing each message.
        Subclasses, must provide implementation for this method.

        mlist is the MailList instance this message is destined for.

        msg is the Message object representing the message.

        msgdata is a dictionary of message metadata.
        """
        raise NotImplementedError

    def _check_retry_delay(self, msgid, filebase):
        """Check if enough time has passed since the last retry attempt."""
        now = time.time()
        last_retry = self._retry_times.get(msgid, 0)
        
        if now - last_retry < self.MIN_RETRY_DELAY:
            # Only log if this is a significant delay
            if self.MIN_RETRY_DELAY > 300:  # 5 minutes
                syslog('debug', 'Runner._check_retry_delay: Message %s (file: %s) retry delay not met. Last retry: %s, Now: %s, Delay needed: %s',
                       msgid, filebase, time.ctime(last_retry), time.ctime(now), self.MIN_RETRY_DELAY)
            return False
        
        # Only log if this is a significant delay
        if self.MIN_RETRY_DELAY > 300:  # 5 minutes
            syslog('debug', 'Runner._check_retry_delay: Message %s (file: %s) retry delay met. Last retry: %s, Now: %s',
                   msgid, filebase, time.ctime(last_retry), time.ctime(now))
        return True

    def _mark_message_processed(self, msgid):
        """Mark a message as processed."""
        with self._processed_lock:
            self._processed_messages.add(msgid)
            # Only log if we're tracking a large number of messages
            if len(self._processed_messages) > 1000:
                syslog('debug', 'Runner._mark_message_processed: Marked message %s as processed', msgid)

    def _unmark_message_processed(self, msgid):
        """Remove a message from the processed set."""
        with self._processed_lock:
            if msgid in self._processed_messages:
                self._processed_messages.remove(msgid)
                # Only log if we're tracking a large number of messages
                if len(self._processed_messages) > 1000:
                    syslog('debug', 'Runner._unmark_message_processed: Removed message %s from processed set', msgid)

    def _cleanup_old_messages(self):
        """Clean up old message tracking data if message tracking is enabled."""
        if not self._track_messages:
            return

        try:
            now = time.time()
            if now - self._last_cleanup < self._cleanup_interval:
                return

            with self._processed_lock:
                if len(self._processed_messages) > self._max_processed_messages:
                    # Only log if we're clearing a significant number of messages
                    if len(self._processed_messages) > 1000:
                        syslog('debug', '%s: Clearing processed messages set (size: %d)',
                               self.__class__.__name__, len(self._processed_messages))
                    self._processed_messages.clear()
                if len(self._retry_times) > self._max_retry_times:
                    # Only log if we're clearing a significant number of retry times
                    if len(self._retry_times) > 1000:
                        syslog('debug', '%s: Clearing retry times dict (size: %d)',
                               self.__class__.__name__, len(self._retry_times))
                    self._retry_times.clear()
                self._last_cleanup = now
        except Exception as e:
            syslog('error', '%s: Error during message cleanup: %s',
                   self.__class__.__name__, str(e))
