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
from urllib.parse import parse_qs
from Mailman.Utils import reap
from Mailman import Utils

from Mailman import mm_cfg
from Mailman import Errors
from Mailman import LockFile
from Mailman.Queue.Runner import Runner
from Mailman.Logging.Syslog import mailman_log


class PipelineError(Exception):
    """Exception raised when pipeline processing fails."""
    pass


class IncomingRunner(Runner):
    QDIR = mm_cfg.INQUEUE_DIR

    def __init__(self, slice=None, numslices=1):
        mailman_log('qrunner', 'IncomingRunner: Initializing with slice=%s, numslices=%s', slice, numslices)
        Runner.__init__(self, slice, numslices)
        # Track processed messages to prevent duplicates
        self._processed_messages = set()
        # Clean up old messages periodically
        self._last_cleanup = time.time()
        self._cleanup_interval = 3600  # Clean up every hour
        mailman_log('qrunner', 'IncomingRunner: Initialization complete')

    def _dispose(self, listname, msg, msgdata):
        # Import MailList here to avoid circular imports
        from Mailman.MailList import MailList

        # Track message ID to prevent duplicates
        msgid = msg.get('message-id', 'n/a')
        filebase = msgdata.get('_filebase', 'unknown')
        
        mailman_log('qrunner', 'IncomingRunner._dispose: Starting to process message %s (file: %s) for list %s',
                   msgid, filebase, listname)
        
        # Check retry delay and duplicate processing
        if not self._check_retry_delay(msgid, filebase):
            mailman_log('qrunner', 'IncomingRunner._dispose: Message %s failed retry delay check, moving to shunt queue',
                       msgid)
            # Move to shunt queue and remove from original queue
            self._shunt.enqueue(msg, msgdata)
            # Get the filebase from msgdata and finish processing it
            if filebase:
                self._switchboard.finish(filebase)
            return 0

        # Get the MailList object for the list name
        try:
            mlist = MailList(listname, lock=False)
            mailman_log('qrunner', 'IncomingRunner._dispose: Successfully got MailList object for %s', listname)
        except Errors.MMListError as e:
            mailman_log('qrunner', 'IncomingRunner._dispose: Failed to get list %s: %s', listname, str(e))
            self._unmark_message_processed(msgid)
            return 0

        # Get the pipeline for this message
        try:
            pipeline = self._get_pipeline(mlist, msg, msgdata)
            mailman_log('qrunner', 'IncomingRunner._dispose: Got pipeline for message %s: %s', msgid, str(pipeline))
        except Exception as e:
            mailman_log('qrunner', 'IncomingRunner._dispose: Failed to get pipeline for message %s: %s', msgid, str(e))
            return 0

        # Process the message through the pipeline
        try:
            result = self._dopipeline(mlist, msg, msgdata, pipeline)
            mailman_log('qrunner', 'IncomingRunner._dispose: Pipeline processing complete for message %s, result: %s', msgid, result)
            return result
        except Exception as e:
            mailman_log('qrunner', 'IncomingRunner._dispose: Error in pipeline processing for message %s: %s\n%s', 
                       msgid, str(e), traceback.format_exc())
            return 0

    def _get_pipeline(self, mlist, msg, msgdata):
        # We must return a copy of the list, otherwise, the first message that
        # flows through the pipeline will empty it out!
        pipeline = msgdata.get('pipeline',
                           getattr(mlist, 'pipeline',
                                   mm_cfg.GLOBAL_PIPELINE))[:]
        mailman_log('qrunner', 'IncomingRunner._get_pipeline: Got pipeline for message %s: %s',
                   msg.get('message-id', 'n/a'), str(pipeline))
        return pipeline

    def _dopipeline(self, mlist, msg, msgdata, pipeline):
        msgid = msg.get('message-id', 'n/a')
        mailman_log('qrunner', 'IncomingRunner._dopipeline: Starting pipeline processing for message %s', msgid)
        
        # Validate pipeline state
        if not pipeline:
            mailman_log('qrunner', 'IncomingRunner._dopipeline: Empty pipeline for message %s', msgid)
            return 0
        if 'pipeline' in msgdata and msgdata['pipeline'] != pipeline:
            mailman_log('qrunner', 'IncomingRunner._dopipeline: Pipeline state mismatch for message %s', msgid)
            return 0

        # Log message details for debugging
        mailman_log('qrunner', 'IncomingRunner._dopipeline: Message details for %s:', msgid)
        mailman_log('qrunner', '  From: %s', msg.get('from', 'unknown'))
        mailman_log('qrunner', '  To: %s', msg.get('to', 'unknown'))
        mailman_log('qrunner', '  Subject: %s', msg.get('subject', '(no subject)'))
        mailman_log('qrunner', '  Message type: %s', type(msg).__name__)
        mailman_log('qrunner', '  Message data: %s', str(msgdata))

        while pipeline:
            handler = pipeline.pop(0)
            modname = 'Mailman.Handlers.' + handler
            try:
                mailman_log('qrunner', 'IncomingRunner._dopipeline: Processing message %s through handler %s', 
                           msgid, handler)
                __import__(modname)
                
                # Store original PID and track child processes
                original_pid = os.getpid()
                child_pids = set()
                
                # Process the message
                sys.modules[modname].process(mlist, msg, msgdata)
                
                # Check for process leaks
                current_pid = os.getpid()
                if current_pid != original_pid:
                    mailman_log('qrunner', 'IncomingRunner._dopipeline: Child process leaked through in handler %s: original_pid=%d, current_pid=%d',
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
                    mailman_log('qrunner', 'IncomingRunner._dopipeline: Cleaned up %d child processes from handler %s: %s',
                          len(child_pids), modname, child_pids)
                
                mailman_log('qrunner', 'IncomingRunner._dopipeline: Successfully processed message %s through handler %s',
                           msgid, handler)
                    
            except Errors.DiscardMessage:
                # Throw the message away; we need do nothing else with it.
                pipeline.insert(0, handler)
                mailman_log('qrunner', """IncomingRunner._dopipeline: Message discarded, msgid: %s
        list: %s,
        handler: %s,
        reason: DiscardMessage exception""",
                       msgid, mlist.internal_name(), handler)
                return 0
            except Errors.HoldMessage:
                # Message is being held for moderation, no need to requeue
                mailman_log('qrunner', """IncomingRunner._dopipeline: Message held for moderation, msgid: %s
        list: %s,
        handler: %s,
        reason: HoldMessage exception""",
                       msgid, mlist.internal_name(), handler)
                # Add message ID to processed set to prevent duplicate processing
                self._processed_messages.add(msgid)
                return 0
            except Errors.RejectMessage as e:
                pipeline.insert(0, handler)
                mailman_log('qrunner', """IncomingRunner._dopipeline: Message rejected, msgid: %s
        list: %s,
        handler: %s,
        reason: %s""",
                       msgid, mlist.internal_name(), handler, e.notice())
                mlist.BounceMessage(msg, msgdata, e)
                return 0
            except Exception as e:
                # Log the full traceback for debugging
                mailman_log('qrunner', 'IncomingRunner._dopipeline: Error in handler %s for message %s: %s\n%s', 
                           modname, msgid, str(e), traceback.format_exc())
                # Put the handler back in the pipeline
                pipeline.insert(0, handler)
                # Try to move message to shunt queue
                try:
                    self._shunt.enqueue(msg, msgdata)
                    mailman_log('qrunner', 'IncomingRunner._dopipeline: Moved failed message %s to shunt queue (reason: handler error)', msgid)
                except Exception as shunt_error:
                    mailman_log('qrunner', 'IncomingRunner._dopipeline: Failed to move message %s to shunt queue: %s', 
                               msgid, str(shunt_error))
                raise
        return 0

    def _cleanup(self):
        """Clean up any resources used by the pipeline."""
        mailman_log('qrunner', 'IncomingRunner._cleanup: Starting cleanup')
        # Clean up child processes
        reap(self._kids, once=True)
        # Close any open file descriptors
        for fd in range(3, 1024):  # Skip stdin, stdout, stderr
            try:
                os.close(fd)
            except OSError:
                pass
        mailman_log('qrunner', 'IncomingRunner._cleanup: Cleanup complete')

    def _oneloop(self):
        mailman_log('qrunner', 'IncomingRunner._oneloop: Starting loop')
        # First, list all the files in our queue directory.
        # Switchboard.files() is guaranteed to hand us the files in FIFO
        # order.  Return an integer count of the number of files that were
        # available for this qrunner to process.
        files = self._switchboard.files()
        mailman_log('qrunner', 'IncomingRunner._oneloop: Found %d files to process', len(files))
        
        for filebase in files:
            try:
                # Log that we're starting to process this file
                mailman_log('qrunner', 'IncomingRunner._oneloop: Starting to process queue file: %s', filebase)
                
                # Ask the switchboard for the message and metadata objects
                # associated with this filebase.
                try:
                    msg, msgdata = self._switchboard.dequeue(filebase)
                    if msg is None or msgdata is None:
                        mailman_log('qrunner', 'IncomingRunner._oneloop: Failed to dequeue file %s - invalid message data', filebase)
                        # Move to shunt queue
                        try:
                            src = os.path.join(self._switchboard.whichq(), filebase + '.bak')
                            dst = os.path.join(mm_cfg.BADQUEUE_DIR, filebase + '.psv')
                            if not os.path.exists(mm_cfg.BADQUEUE_DIR):
                                os.makedirs(mm_cfg.BADQUEUE_DIR, 0o770)
                            os.rename(src, dst)
                            mailman_log('qrunner', 'IncomingRunner._oneloop: Moved invalid file to shunt queue: %s -> %s (reason: null message or metadata)', filebase, dst)
                        except Exception as e:
                            mailman_log('qrunner', 'IncomingRunner._oneloop: Failed to move invalid file to shunt queue: %s', str(e))
                        continue
                        
                    mailman_log('qrunner', 'IncomingRunner._oneloop: Successfully dequeued file %s', filebase)
                    
                    # Validate message data structure
                    if not isinstance(msgdata, dict):
                        mailman_log('qrunner', 'IncomingRunner._oneloop: Invalid message data structure for file %s: expected dict, got %s', 
                                  filebase, type(msgdata))
                        # Move to shunt queue
                        try:
                            src = os.path.join(self._switchboard.whichq(), filebase + '.bak')
                            dst = os.path.join(mm_cfg.BADQUEUE_DIR, filebase + '.psv')
                            if not os.path.exists(mm_cfg.BADQUEUE_DIR):
                                os.makedirs(mm_cfg.BADQUEUE_DIR, 0o770)
                            os.rename(src, dst)
                            mailman_log('qrunner', 'IncomingRunner._oneloop: Moved invalid file to shunt queue: %s -> %s (reason: invalid message data structure)', filebase, dst)
                        except Exception as e:
                            mailman_log('qrunner', 'IncomingRunner._oneloop: Failed to move invalid file to shunt queue: %s', str(e))
                        continue
                        
                    # Validate required message data fields
                    required_fields = ['listname']
                    missing_fields = [field for field in required_fields if field not in msgdata]
                    if missing_fields:
                        mailman_log('qrunner', 'IncomingRunner._oneloop: Missing required fields in message data for file %s: %s', 
                                  filebase, ', '.join(missing_fields))
                        # Move to shunt queue
                        try:
                            src = os.path.join(self._switchboard.whichq(), filebase + '.bak')
                            dst = os.path.join(mm_cfg.BADQUEUE_DIR, filebase + '.psv')
                            if not os.path.exists(mm_cfg.BADQUEUE_DIR):
                                os.makedirs(mm_cfg.BADQUEUE_DIR, 0o770)
                            os.rename(src, dst)
                            mailman_log('qrunner', 'IncomingRunner._oneloop: Moved invalid file to shunt queue: %s -> %s (reason: missing required fields: %s)', 
                                      filebase, dst, ', '.join(missing_fields))
                        except Exception as e:
                            mailman_log('qrunner', 'IncomingRunner._oneloop: Failed to move invalid file to shunt queue: %s', str(e))
                        continue
                    
                except Exception as e:
                    mailman_log('qrunner', 'IncomingRunner._oneloop: Failed to dequeue file %s: %s', filebase, str(e))
                    continue
                    
                # Process the message
                more = self._dispose(msgdata['listname'], msg, msgdata)
                if more:
                    # The message needs more processing, so enqueue it at the
                    # end of the self._switchboard's queue.
                    mailman_log('qrunner', 'IncomingRunner._oneloop: Message needs more processing, requeuing %s', filebase)
                    self._switchboard.enqueue(msg, msgdata)
                else:
                    # The message is done being processed by this qrunner, so
                    # shunt it off to the next queue.
                    msgid = msg.get('message-id', 'n/a')
                    mailman_log('qrunner', 'IncomingRunner._oneloop: Message processing complete, moving to shunt queue %s (msgid: %s)', filebase, msgid)
                    self._shunt.enqueue(msg, msgdata)
            except Exception as e:
                # Log the error and requeue the message for later processing
                mailman_log('qrunner', 'IncomingRunner._oneloop: Error processing queue file %s: %s\n%s', 
                           filebase, str(e), traceback.format_exc())
                if msg is not None and msgdata is not None:
                    try:
                        self._switchboard.enqueue(msg, msgdata)
                        mailman_log('qrunner', 'IncomingRunner._oneloop: Successfully requeued file %s', filebase)
                    except Exception as e2:
                        mailman_log('qrunner', 'IncomingRunner._oneloop: Failed to requeue file %s: %s', filebase, str(e2))
        
        mailman_log('qrunner', 'IncomingRunner._oneloop: Loop complete, processed %d files', len(files))
        return len(files)

