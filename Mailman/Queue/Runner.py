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

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
from Mailman import MailList
from Mailman import i18n
from Mailman.Message import Message
from Mailman.Logging.Syslog import mailman_log as log
from Mailman.Queue.Switchboard import Switchboard

import email.errors


class Runner:
    QDIR = None
    SLEEPTIME = mm_cfg.QRUNNER_SLEEP_TIME

    def __init__(self, slice=None, numslices=1):
        self._kids = {}
        # Create our own switchboard.  Don't use the switchboard cache because
        # we want to provide slice and numslice arguments.
        self._switchboard = Switchboard(self.QDIR, slice, numslices, True)
        # Create the shunt switchboard
        self._shunt = Switchboard(mm_cfg.SHUNTQUEUE_DIR)
        self._stop = False
        self.status = 0  # Add status attribute initialized to 0
        self._error_count = 0  # Track consecutive errors
        self._last_error_time = 0  # Track time of last error

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
        log('error', '%(runner)s: %(error_type)s - list: %(list)s, msg: %(msg_id)s, error: %(error)s',
            context)

    def log_warning(self, warning_type, msg=None, mlist=None, **context):
        """Structured warning logging with context."""
        context.update({
            'runner': self.__class__.__name__,
            'list': mlist.internal_name() if mlist else 'N/A',
            'msg_id': msg.get('message-id', 'N/A') if msg else 'N/A',
            'warning_type': warning_type
        })
        log('warning', '%(runner)s: %(warning_type)s - list: %(list)s, msg: %(msg_id)s',
            context)

    def log_info(self, info_type, msg=None, mlist=None, **context):
        """Structured info logging with context."""
        context.update({
            'runner': self.__class__.__name__,
            'list': mlist.internal_name() if mlist else 'N/A',
            'msg_id': msg.get('message-id', 'N/A') if msg else 'N/A',
            'info_type': info_type
        })
        log('info', '%(runner)s: %(info_type)s - list: %(list)s, msg: %(msg_id)s',
            context)

    def _handle_error(self, exc, msg=None, mlist=None, preserve=True):
        """Centralized error handling with circuit breaker."""
        now = time.time()
        
        # Log the error with full context
        self.log_error('unhandled_exception', exc, msg=msg, mlist=mlist)
        
        # Log full traceback
        s = StringIO()
        traceback.print_exc(file=s)
        log('error', 'Traceback: %s', s.getvalue())
        
        # Circuit breaker logic
        if now - self._last_error_time < 60:  # Within last minute
            self._error_count += 1
            if self._error_count >= 10:  # Too many errors in short time
                log('error', '%s: Too many errors, stopping runner', self.__class__.__name__)
                self.stop()
        else:
            self._error_count = 1
        self._last_error_time = now
        
        # Handle message preservation
        if preserve:
            try:
                msgdata = {'whichq': self._switchboard.whichq()}
                new_filebase = self._shunt.enqueue(msg, msgdata)
                log('error', '%s: Shunted message to: %s', self.__class__.__name__, new_filebase)
            except Exception as e:
                log('error', '%s: Failed to shunt message: %s', self.__class__.__name__, str(e))
                return False
        return True

    def _oneloop(self):
        # First, list all the files in our queue directory.
        # Switchboard.files() is guaranteed to hand us the files in FIFO
        # order.  Return an integer count of the number of files that were
        # available for this qrunner to process.
        files = self._switchboard.files()
        for filebase in files:
            try:
                # Ask the switchboard for the message and metadata objects
                # associated with this filebase.
                msg, msgdata = self._switchboard.dequeue(filebase)
            except (email.errors.MessageParseError, ValueError) as e:
                # Handle message parsing errors
                self.log_error('message_parse_error', e, filebase=filebase)
                preserve = mm_cfg.QRUNNER_SAVE_BAD_MESSAGES
                self._switchboard.finish(filebase, preserve=preserve)
                continue
            except Exception as e:
                # Handle other unexpected errors
                self._handle_error(e, filebase=filebase)
                self._switchboard.finish(filebase, preserve=True)
                continue
            try:
                self._onefile(msg, msgdata)
                self._switchboard.finish(filebase)
            except Exception as e:
                if not self._handle_error(e, msg=msg, mlist=msgdata.get('listname')):
                    self._switchboard.finish(filebase, preserve=True)
            # Other work we want to do each time through the loop
            Utils.reap(self._kids, once=True)
            self._doperiodic()
            if self._shortcircuit():
                break
        return len(files)

    def _onefile(self, msg, msgdata):
        # Do some common sanity checking on the message metadata.  It's got to
        # be destined for a particular mailing list.  This switchboard is used
        # to shunt off badly formatted messages.  We don't want to just trash
        # them because they may be fixable with human intervention.  Just get
        # them out of our site though.
        #
        # Find out which mailing list this message is destined for.
        try:
            # Convert email.message.Message to Mailman.Message if needed
            if not isinstance(msg, Message):
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

            # Check for duplicate messages early
            msgid = msg.get('message-id', 'n/a')
            if hasattr(self, '_processed_messages') and msgid in self._processed_messages:
                log('error', 'Duplicate message detected early: %s', msgid)
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
        """Sleep for a little while.

        filecnt is the number of messages in the queue the last time through.
        Sub-runners can decide to continue to do work, or sleep for a while
        based on this value.  By default, we only snooze if there was nothing
        to do last time around.
        """
        if filecnt or self.SLEEPTIME <= 0:
            return
        time.sleep(self.SLEEPTIME)

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
        """Clean up upon exit from the main processing loop.

        Called when the Runner's main loop is stopped, this should perform
        any necessary resource deallocation.  Its return value is irrelevant.
        """
        Utils.reap(self._kids)

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
