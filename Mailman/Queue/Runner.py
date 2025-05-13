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

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
from Mailman import MailList
from Mailman import i18n
from Mailman.Message import Message
from Mailman.Logging.Syslog import syslog
from Mailman.Queue.Switchboard import Switchboard

import email.errors


class Runner:
    QDIR = None
    SLEEPTIME = mm_cfg.QRUNNER_SLEEP_TIME
    
    # Message tracking configuration - can be overridden by subclasses
    _track_messages = False  # Whether to track processed messages
    _max_processed_messages = 10000  # Maximum number of messages to track
    _max_retry_times = 10000  # Maximum number of retry times to track
    _processed_messages = set()  # Set of processed message IDs
    _processed_lock = threading.Lock()  # Lock for thread safety
    _retry_times = {}  # Dictionary of retry times
    _last_cleanup = time.time()  # Last cleanup time
    _cleanup_interval = 3600  # Cleanup interval in seconds

    def __init__(self, slice=None, numslices=1):
        syslog('debug', 'Runner: Starting initialization')
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
            
            syslog('debug', 'Runner: Initialization complete')
        except Exception as e:
            syslog('error', 'Runner: Initialization failed: %s\nTraceback:\n%s',
                   str(e), traceback.format_exc())
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

    def log_error(self, error_type, error, msg=None, mlist=None, **context):
        """Structured error logging with context."""
        context.update({
            'runner': self.__class__.__name__,
            'list': mlist.internal_name() if mlist else 'N/A',
            'msg_id': msg.get('message-id', 'N/A') if msg else 'N/A',
            'error_type': error_type,
            'error': str(error)
        })
        syslog('error', '%(runner)s: %(error_type)s - list: %(list)s, msg: %(msg_id)s, error: %(error)s',
            context)

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
        """Process one batch of messages from the queue."""
        try:
            # Get the list of files to process
            files = self._switchboard.files()
            filecnt = len(files)
            
            # Only log at debug level if we found files to process
            if filecnt > 0:
                syslog('debug', 'Runner._oneloop: Found %d files to process', filecnt)
            
            # Process each file
            for filebase in files:
                try:
                    # Dequeue the file
                    msg, msgdata = self._switchboard.dequeue(filebase)
                    if msg is None:
                        continue
                        
                    syslog('info', 'Runner._oneloop: Successfully dequeued file %s', filebase)
                    
                    # Process the message
                    try:
                        # Get the list name from the message data
                        listname = msgdata.get('listname', mm_cfg.MAILMAN_SITE_LIST)
                        
                        # Process the message
                        result = self._dispose(listname, msg, msgdata)
                        
                        # If the message should be kept in the queue, requeue it
                        if result:
                            self._switchboard.enqueue(msg, msgdata)
                            syslog('info', 'Runner._oneloop: Message requeued for later processing: %s', filebase)
                        else:
                            syslog('info', 'Runner._oneloop: Message processing complete, moving to shunt queue %s (msgid: %s)',
                                  filebase, msg.get('message-id', 'n/a'))
                            
                    except Exception as e:
                        syslog('error', 'Runner._oneloop: Error processing message: %s\n%s',
                              str(e), traceback.format_exc())
                        # Move to shunt queue on error
                        self._shunt.enqueue(msg, msgdata)
                        
                except Exception as e:
                    syslog('error', 'Runner._oneloop: Error dequeuing file %s: %s\n%s',
                          filebase, str(e), traceback.format_exc())
                    
            # Only log completion at debug level if we processed files
            if filecnt > 0:
                syslog('debug', 'Runner._oneloop: Loop complete, processed %d files', filecnt)
                
        except Exception as e:
            syslog('error', 'Runner._oneloop: Unexpected error in main loop: %s\n%s',
                  str(e), traceback.format_exc())

    def _validate_message(self, msg, msgdata):
        """Validate and convert message if needed.
        
        Returns a tuple of (msg, success) where success is a boolean indicating
        if validation was successful.
        """
        msgid = msg.get('message-id', 'n/a')
        try:
            # Convert message if needed
            if not isinstance(msg, Message):
                syslog('debug', 'Runner._validate_message: Converting message %s to Mailman.Message', msgid)
                msg = Message(msg)
            
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
                
            syslog('debug', 'Runner._validate_message: Message %s validation successful', msgid)
            return msg, True
            
        except Exception as e:
            syslog('error', 'Runner._validate_message: Error validating message %s: %s\nTraceback:\n%s',
                   msgid, str(e), traceback.format_exc())
            return msg, False

    def _onefile(self, msg, msgdata):
        # Validate message type first
        msg, success = self._validate_message(msg, msgdata)
        if not success:
            syslog('error', 'Message validation failed, moving to shunt queue')
            self._shunt.enqueue(msg, msgdata)
            return

        # Do some common sanity checking on the message metadata
        try:
            # Check for duplicate messages early
            msgid = msg.get('message-id', 'n/a')
            if hasattr(self, '_processed_messages') and msgid in self._processed_messages:
                syslog('error', 'Duplicate message detected early: %s (file: %s)', msgid, msgdata.get('_filebase', 'unknown'))
                self._shunt.enqueue(msg, msgdata)
                return

            sender = msg.get_sender()
            listname = msgdata.get('listname', mm_cfg.MAILMAN_SITE_LIST)
            mlist = self._open_list(listname)
            if not mlist:
                self.log_error('missing_list', 'List not found', msg=msg, listname=listname)
                self._shunt.enqueue(msg, msgdata)
                return
            # Now process this message, keeping track of any subprocesses that may
            # have been spawned.  We'll reap those later.
            #
            # We also want to set up the language context for this message.  The
            # context will be the preferred language for the user if a member of
            # the list, or the list's preferred language.  However, we must take
            # special care to reset the defaults, otherwise subsequent messages
            # may be translated incorrectly.  BAW: I'm not sure I like this
            # approach, but I can't think of anything better right now.
            otranslation = i18n.get_translation()
            if mlist:
                lang = mlist.getMemberLanguage(sender)
            else:
                lang = mm_cfg.DEFAULT_SERVER_LANGUAGE
            i18n.set_language(lang)
            msgdata['lang'] = lang
            try:
                keepqueued = self._dispose(mlist, msg, msgdata)
            finally:
                i18n.set_translation(otranslation)
            # Keep tabs on any child processes that got spawned.
            kids = msgdata.get('_kids')
            if kids:
                self._kids.update(kids)
            if keepqueued:
                self._switchboard.enqueue(msg, msgdata)
        except Exception as e:
            self._handle_error(e, msg=msg, mlist=mlist)

    def _open_list(self, listname):
        # We no longer cache the list instances.  Because of changes to
        # MailList.py needed to avoid not reloading an updated list, caching
        # is not as effective as it once was.  Also, with OldStyleMemberships
        # as the MemberAdaptor, there was a self-reference to the list which
        # kept all lists in the cache.  Changing this reference to a
        # weakref.proxy created other issues.
        try:
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

    def _snooze(self, filecnt):
        """Sleep for a while, but check for stop flag periodically."""
        syslog('debug', 'Runner._snooze: Sleeping for %d seconds', self.SLEEPTIME)
        for _ in range(self.SLEEPTIME):
            if self._stop:
                syslog('debug', 'Runner._snooze: Stop flag detected, waking up')
                return
            time.sleep(1)
        syslog('debug', 'Runner._snooze: Sleep complete')

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
        syslog('debug', 'Runner: Starting cleanup')
        try:
            self._cleanup_old_messages()
            # Clean up any stale locks
            self._switchboard.cleanup_stale_locks()
        except Exception as e:
            syslog('error', 'Runner: Cleanup failed: %s\nTraceback:\n%s',
                   str(e), traceback.format_exc())
        syslog('debug', 'Runner: Cleanup complete')

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
            syslog('debug', 'Runner._check_retry_delay: Message %s (file: %s) retry delay not met. Last retry: %s, Now: %s, Delay needed: %s',
                   msgid, filebase, time.ctime(last_retry), time.ctime(now), self.MIN_RETRY_DELAY)
            return False
        
        syslog('debug', 'Runner._check_retry_delay: Message %s (file: %s) retry delay met. Last retry: %s, Now: %s',
               msgid, filebase, time.ctime(last_retry), time.ctime(now))
        return True

    def _mark_message_processed(self, msgid):
        """Mark a message as processed."""
        with self._processed_lock:
            self._processed_messages.add(msgid)
            syslog('debug', 'Runner._mark_message_processed: Marked message %s as processed', msgid)

    def _unmark_message_processed(self, msgid):
        """Remove a message from the processed set."""
        with self._processed_lock:
            if msgid in self._processed_messages:
                self._processed_messages.remove(msgid)
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
                    syslog('debug', '%s: Clearing processed messages set (size: %d)',
                           self.__class__.__name__, len(self._processed_messages))
                    self._processed_messages.clear()
                if len(self._retry_times) > self._max_retry_times:
                    syslog('debug', '%s: Clearing retry times dict (size: %d)',
                           self.__class__.__name__, len(self._retry_times))
                    self._retry_times.clear()
                self._last_cleanup = now
        except Exception as e:
            syslog('error', '%s: Error during message cleanup: %s',
                   self.__class__.__name__, str(e))
