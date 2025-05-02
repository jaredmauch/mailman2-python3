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

"""Incoming message queue runner.

This qrunner handles messages that are posted to the mailing list.  It is
responsible for running the message through the pipeline of handlers.
"""

# A typical Mailman list exposes nine aliases which point to seven different
# wrapped scripts.  E.g. for a list named `mylist', you'd have:
#
# mylist-bounces -> bounces (-admin is a deprecated alias)
# mylist-confirm -> confirm
# mylist-join    -> join    (-subscribe is an alias)
# mylist-leave   -> leave   (-unsubscribe is an alias)
# mylist-owner   -> owner
# mylist         -> post
# mylist-request -> request
#
# -request, -join, and -leave are a robot addresses; their sole purpose is to
# process emailed commands in a Majordomo-like fashion (although the latter
# two are hardcoded to subscription and unsubscription requests).  -bounces is
# the automated bounce processor, and all messages to list members have their
# return address set to -bounces.  If the bounce processor fails to extract a
# bouncing member address, it can optionally forward the message on to the
# list owners.
#
# -owner is for reaching a human operator with minimal list interaction
# (i.e. no bounce processing).  -confirm is another robot address which
# processes replies to VERP-like confirmation notices.
#
# So delivery flow of messages look like this:
#
# joerandom ---> mylist ---> list members
#    |                           |
#    |                           |[bounces]
#    |        mylist-bounces <---+ <-------------------------------+
#    |              |                                              |
#    |              +--->[internal bounce processing]              |
#    |              ^                |                             |
#    |              |                |    [bounce found]           |
#    |         [bounces *]           +--->[register and discard]   |
#    |              |                |                      |      |
#    |              |                |                      |[*]   |
#    |        [list owners]          |[no bounce found]     |      |
#    |              ^                |                      |      |
#    |              |                |                      |      |
#    +-------> mylist-owner <--------+                      |      |
#    |                                                      |      |
#    |           data/owner-bounces.mbox <--[site list] <---+      |
#    |                                                             |
#    +-------> mylist-join--+                                      |
#    |                      |                                      |
#    +------> mylist-leave--+                                      |
#    |                      |                                      |
#    |                      v                                      |
#    +-------> mylist-request                                      |
#    |              |                                              |
#    |              +---> [command processor]                      |
#    |                            |                                |
#    +-----> mylist-confirm ----> +---> joerandom                  |
#                                           |                      |
#                                           |[bounces]             |
#                                           +----------------------+
#
# A person can send an email to the list address (for posting), the -owner
# address (to reach the human operator), or the -confirm, -join, -leave, and
# -request mailbots.  Message to the list address are then forwarded on to the
# list membership, with bounces directed to the -bounces address.
#
# [*] Messages sent to the -owner address are forwarded on to the list
# owner/moderators.  All -owner destined messages have their bounces directed
# to the site list -bounces address, regardless of whether a human sent the
# message or the message was crafted internally.  The intention here is that
# the site owners want to be notified when one of their list owners' addresses
# starts bouncing (yes, the will be automated in a future release).
#
# Any messages to site owners has their bounces directed to a special
# "loop-killer" address, which just dumps the message into
# data/owners-bounces.mbox.
#
# Finally, message to any of the mailbots causes the requested action to be
# performed.  Results notifications are sent to the author of the message,
# which all bounces pointing back to the -bounces address.

import os
import sys
import time
import traceback
from io import StringIO
import random
import signal
import os
from email import message_from_string
from Mailman.Message import Message

from Mailman import mm_cfg
from Mailman import Errors
from Mailman import LockFile
from Mailman.Queue.Runner import Runner
from Mailman.Logging.Syslog import mailman_log
from Mailman.Utils import reap
from Mailman import Utils


class PipelineError(Exception):
    """Exception raised when pipeline processing fails."""
    pass


class IncomingRunner(Runner):
    QDIR = mm_cfg.INQUEUE_DIR

    def __init__(self, slice=None, numslices=1):
        Runner.__init__(self, slice, numslices)
        # Track processed messages to prevent duplicates
        self._processed_messages = set()
        # Clean up old messages periodically
        self._last_cleanup = time.time()
        self._cleanup_interval = 3600  # Clean up every hour

    def _dispose(self, listname, msg, msgdata):
        # Import MailList here to avoid circular imports
        from Mailman.MailList import MailList

        # Track message ID to prevent duplicates
        msgid = msg.get('message-id', 'n/a')
        if msgid in self._processed_messages:
            mailman_log('error', 'Duplicate message detected: %s (file: %s)', msgid, msgdata.get('_filebase', 'unknown'))
            # Move to shunt queue and remove from original queue
            self._shunt.enqueue(msg, msgdata)
            # Get the filebase from msgdata and finish processing it
            filebase = msgdata.get('_filebase')
            if filebase:
                self._switchboard.finish(filebase)
            return 0

        # Clean up old message IDs periodically
        current_time = time.time()
        if current_time - self._last_cleanup > self._cleanup_interval:
            self._processed_messages.clear()
            self._last_cleanup = current_time

        # Get the MailList object for the list name
        try:
            mlist = MailList(listname, lock=False)
        except Errors.MMListError as e:
            mailman_log('error', 'Failed to get list %s: %s', listname, str(e))
            return 0

        # Try to get the list lock with shorter timeout
        try:
            mlist.Lock(timeout=mm_cfg.LIST_LOCK_TIMEOUT / 2)
        except LockFile.TimeOutError:
            mailman_log('error', 'Lock timeout for %s', listname)
            return 1
        except Exception as e:
            mailman_log('error', 'Unexpected error acquiring lock for %s: %s',
                      listname, str(e))
            return 1

        # Process the message through a handler pipeline
        try:
            pipeline = self._get_pipeline(mlist, msg, msgdata)
            msgdata['pipeline'] = pipeline
            more = self._dopipeline(mlist, msg, msgdata, pipeline)
            if not more:
                del msgdata['pipeline']
                # Only add to processed messages if processing completed successfully
                self._processed_messages.add(msgid)
            mlist.Save()
            return more
        except Exception as e:
            mailman_log('error', 'Error processing message for %s: %s\nTraceback:\n%s', listname, str(e), traceback.format_exc())
            return 1
        finally:
            try:
                mlist.Unlock()
            except Exception as e:
                mailman_log('error', 'Error unlocking %s: %s', listname, str(e))

    def _get_pipeline(self, mlist, msg, msgdata):
        # We must return a copy of the list, otherwise, the first message that
        # flows through the pipeline will empty it out!
        return msgdata.get('pipeline',
                           getattr(mlist, 'pipeline',
                                   mm_cfg.GLOBAL_PIPELINE))[:]

    def _dopipeline(self, mlist, msg, msgdata, pipeline):
        # Validate pipeline state
        if not pipeline:
            mailman_log('error', 'Empty pipeline for message %s', msg.get('message-id', 'n/a'))
            return 0
        if 'pipeline' in msgdata and msgdata['pipeline'] != pipeline:
            mailman_log('error', 'Pipeline state mismatch for message %s', msg.get('message-id', 'n/a'))
            return 0

        # Ensure message is a Mailman.Message
        if not isinstance(msg, Message):
            try:
                mailman_log('info', 'Converting email.message.Message to Mailman.Message')
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
                # Update msgdata references if needed
                if 'msg' in msgdata:
                    msgdata['msg'] = msg
            except Exception as e:
                mailman_log('error', 'Failed to convert message to Mailman.Message: %s\nTraceback:\n%s',
                       str(e), traceback.format_exc())
                return 0

        # Validate required Mailman.Message methods
        required_methods = ['get_sender', 'get', 'items', 'is_multipart', 'get_payload']
        for method in required_methods:
            if not hasattr(msg, method):
                mailman_log('error', 'Message object missing required method %s', method)
                return 0

        while pipeline:
            handler = pipeline.pop(0)
            modname = 'Mailman.Handlers.' + handler
            __import__(modname)
            try:
                # Store original PID and track child processes
                original_pid = os.getpid()
                child_pids = set()
                
                # Process the message
                sys.modules[modname].process(mlist, msg, msgdata)
                
                # Check for process leaks
                current_pid = os.getpid()
                if current_pid != original_pid:
                    mailman_log('error', 'Child process leaked through in handler %s: original_pid=%d, current_pid=%d',
                          modname, original_pid, current_pid)
                    # Try to clean up any child processes
                    try:
                        os.kill(original_pid, signal.SIGTERM)
                    except:
                        pass
                    os._exit(1)
                
                # Clean up any child processes
                try:
                    while True:
                        pid, status = os.waitpid(-1, os.WNOHANG)
                        if pid == 0:
                            break
                        child_pids.add(pid)
                except ChildProcessError:
                    pass
                
                if child_pids:
                    mailman_log('debug', 'Cleaned up %d child processes from handler %s: %s',
                          len(child_pids), modname, child_pids)
                    
            except Errors.DiscardMessage:
                # Throw the message away; we need do nothing else with it.
                pipeline.insert(0, handler)
                mailman_log('vette', """Message discarded, msgid: %s'
        list: %s,
        handler: %s""",
                       msg.get('message-id', 'n/a'),
                       mlist.internal_name(), handler)
                return 0
            except Errors.HoldMessage:
                # Let the approval process take it from here
                return 0
            except Errors.RejectMessage as e:
                pipeline.insert(0, handler)
                mailman_log('vette', """Message rejected, msgid: %s
        list: %s,
        handler: %s,
        reason: %s""",
                       msg.get('message-id', 'n/a'),
                       mlist.internal_name(), handler, e.notice())
                mlist.BounceMessage(msg, msgdata, e)
                return 0
            except Exception as e:
                # Log the full traceback for debugging
                mailman_log('error', 'Error in handler %s: %s\n%s', modname, str(e),
                      ''.join(traceback.format_exc()))
                pipeline.insert(0, handler)
                raise
        return 0

    def _cleanup(self):
        """Clean up any resources used by the pipeline."""
        # Clean up child processes
        reap(self._kids, once=True)
        # Close any open file descriptors
        for fd in range(3, 1024):  # Skip stdin, stdout, stderr
            try:
                os.close(fd)
            except OSError:
                pass

    def _oneloop(self):
        # First, list all the files in our queue directory.
        # Switchboard.files() is guaranteed to hand us the files in FIFO
        # order.  Return an integer count of the number of files that were
        # available for this qrunner to process.
        files = self._switchboard.files()
        for filebase in files:
            try:
                # Log that we're starting to process this file
                mailman_log('incoming', 'Starting to process queue file: %s', filebase)
                
                # Ask the switchboard for the message and metadata objects
                # associated with this filebase.
                msg, msgdata = self._switchboard.dequeue(filebase)
                # Process the message
                more = self._dispose(msgdata['listname'], msg, msgdata)
                if more:
                    # The message needs more processing, so enqueue it at the
                    # end of the self._switchboard's queue.
                    self._switchboard.enqueue(msg, msgdata)
                else:
                    # The message is done being processed by this qrunner, so
                    # shunt it off to the next queue.
                    self._shunt.enqueue(msg, msgdata)
            except Exception as e:
                # Log the error and requeue the message for later processing
                mailman_log('error', 'Error processing queue file %s: %s', filebase, str(e))
                try:
                    self._switchboard.enqueue(msg, msgdata)
                except:
                    pass
        return len(files)
