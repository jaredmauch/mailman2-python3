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
import threading
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
        # Rate limiting
        self._message_times = {}
        self._message_lock = threading.Lock()
        self._max_messages_per_hour = 1000
        self._message_window = 3600  # 1 hour
        # Cleanup
        self._last_cleanup = time.time()
        self._cleanup_interval = 3600  # Clean up every hour
        self._max_message_age = 7 * 24 * 3600  # 7 days max message age
        mailman_log('qrunner', 'IncomingRunner: Initialization complete')

    def _cleanup_old_messages(self):
        """Clean up old message files."""
        try:
            current_time = time.time()
            if current_time - self._last_cleanup < self._cleanup_interval:
                return
                
            with self._message_lock:
                # Clean up old message files
                for filename in os.listdir(self.QDIR):
                    if filename.endswith('.pck'):
                        filepath = os.path.join(self.QDIR, filename)
                        try:
                            if current_time - os.path.getmtime(filepath) > self._max_message_age:
                                os.unlink(filepath)
                        except OSError:
                            pass
                            
                # Clean up old message times
                cutoff = current_time - self._message_window
                self._message_times = {k: v for k, v in self._message_times.items() 
                                    if v > cutoff}
                                    
                self._last_cleanup = current_time
        except Exception as e:
            mailman_log('error', 'Error cleaning up old messages: %s', str(e))

    def _check_rate_limit(self, sender):
        """Check if sender has exceeded rate limit."""
        with self._message_lock:
            current_time = time.time()
            # Clean up old entries
            cutoff = current_time - self._message_window
            self._message_times = {k: v for k, v in self._message_times.items() 
                                if v > cutoff}
                                
            # Count messages in window
            count = sum(1 for t in self._message_times.values() if t > cutoff)
            if count >= self._max_messages_per_hour:
                return False
                
            # Add new message
            self._message_times[sender] = current_time
            return True

    def _validate_message(self, msg, msgdata):
        """Validate message format."""
        try:
            # Check required headers
            if not msg.get('from'):
                return False
            if not msg.get('to'):
                return False
            if not msg.get('message-id'):
                return False
                
            # Check message size
            if len(str(msg)) > mm_cfg.MAX_MESSAGE_SIZE:
                return False
                
            # Check for valid message format
            if not msg.get('content-type', '').startswith(('text/', 'multipart/')):
                return False
                
            return True
        except Exception:
            return False

    def _dispose(self, listname, msg, msgdata):
        """Process an incoming message with proper validation and rate limiting."""
        # Import MailList here to avoid circular imports
        from Mailman.MailList import MailList

        # Track message ID to prevent duplicates
        msgid = msg.get('message-id', 'n/a')
        filebase = msgdata.get('_filebase', 'unknown')
        sender = msg.get_sender()
        
        mailman_log('qrunner', 'IncomingRunner._dispose: Starting to process message %s (file: %s) for list %s',
                   msgid, filebase, listname)
        
        # Validate message
        if not self._validate_message(msg, msgdata):
            mailman_log('error', 'Invalid message format for %s', msgid)
            return False
            
        # Check rate limit
        if not self._check_rate_limit(sender):
            mailman_log('error', 'Rate limit exceeded for sender %s', sender)
            return False

        # Get the MailList object for the list name
        try:
            mlist = MailList(listname, lock=False)
            mailman_log('qrunner', 'IncomingRunner._dispose: Successfully got MailList object for %s', listname)
        except Errors.MMListError as e:
            mailman_log('error', 'Failed to get list %s: %s', listname, str(e))
            return False
        except Exception as e:
            mailman_log('error', 'Unexpected error loading list %s: %s', listname, str(e))
            return False

        try:
            # Log start of processing
            mailman_log('qrunner', 'IncomingRunner: Starting to process message %s (file: %s) for list %s',
                       msgid, filebase, mlist.internal_name())
            
            # Get the pipeline
            pipeline = self._get_pipeline(mlist, msg, msgdata)
            mailman_log('qrunner', 'IncomingRunner._dispose: Got pipeline for message %s: %s', 
                       msgid, str(pipeline))
            
            # Process the message through the pipeline
            result = self._dopipeline(mlist, msg, msgdata, pipeline)
            
            # Log successful completion
            mailman_log('qrunner', 'IncomingRunner: Successfully processed message %s (file: %s) for list %s',
                       msgid, filebase, mlist.internal_name())
            return result
        except Exception as e:
            # Enhanced error logging with more context
            mailman_log('error', 'Error processing message %s for list %s: %s',
                   msgid, mlist.internal_name(), str(e))
            mailman_log('error', 'Message details:')
            mailman_log('error', '  Message ID: %s', msgid)
            mailman_log('error', '  From: %s', msg.get('from', 'unknown'))
            mailman_log('error', '  To: %s', msg.get('to', 'unknown'))
            mailman_log('error', '  Subject: %s', msg.get('subject', '(no subject)'))
            mailman_log('error', '  Message type: %s', type(msg).__name__)
            mailman_log('error', '  Message data: %s', str(msgdata))
            mailman_log('error', 'Traceback:\n%s', traceback.format_exc())
            return False
        finally:
            self._cleanup_old_messages()

    def _get_pipeline(self, mlist, msg, msgdata):
        """Get the pipeline for message processing."""
        # We must return a copy of the list, otherwise, the first message that
        # flows through the pipeline will empty it out!
        pipeline = msgdata.get('pipeline',
                           getattr(mlist, 'pipeline',
                                   mm_cfg.GLOBAL_PIPELINE))[:]
        mailman_log('qrunner', 'IncomingRunner._get_pipeline: Got pipeline for message %s: %s',
                   msg.get('message-id', 'n/a'), str(pipeline))
        return pipeline

    def _dopipeline(self, mlist, msg, msgdata, pipeline):
        """Process message through pipeline with proper error handling."""
        msgid = msg.get('message-id', 'n/a')
        mailman_log('qrunner', 'IncomingRunner._dopipeline: Starting pipeline processing for message %s', msgid)
        
        # Validate pipeline state
        if not pipeline:
            mailman_log('error', 'Empty pipeline for message %s', msgid)
            return 0
        if 'pipeline' in msgdata and msgdata['pipeline'] != pipeline:
            mailman_log('error', 'Pipeline state mismatch for message %s', msgid)
            return 0

        # Ensure message is a Mailman.Message
        if not isinstance(msg, Message):
            try:
                mailman_log('qrunner', 'Converting email.message.Message to Mailman.Message for %s', msgid)
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
                mailman_log('qrunner', 'Successfully converted message %s', msgid)
            except Exception as e:
                mailman_log('error', 'Failed to convert message to Mailman.Message: %s\nTraceback:\n%s',
                       str(e), traceback.format_exc())
                return 0

        # Process through pipeline
        try:
            for handler in pipeline:
                try:
                    handler.process(mlist, msg, msgdata)
                except Exception as e:
                    mailman_log('error', 'Handler %s failed for message %s: %s',
                           handler.__name__, msgid, str(e))
                    raise PipelineError(str(e))
            return 1
        except PipelineError as e:
            mailman_log('error', 'Pipeline processing failed for message %s: %s',
                   msgid, str(e))
            return 0
        except Exception as e:
            mailman_log('error', 'Unexpected error in pipeline for message %s: %s',
                   msgid, str(e))
            return 0

    def _cleanup(self):
        """Clean up resources."""
        try:
            self._cleanup_old_messages()
        except Exception as e:
            mailman_log('error', 'Error in message cleanup: %s', str(e))

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
                            mailman_log('qrunner', 'IncomingRunner._oneloop: Moved invalid file to shunt queue: %s -> %s', filebase, dst)
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
                            mailman_log('qrunner', 'IncomingRunner._oneloop: Moved invalid file to shunt queue: %s -> %s', filebase, dst)
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
                            mailman_log('qrunner', 'IncomingRunner._oneloop: Moved invalid file to shunt queue: %s -> %s', filebase, dst)
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
                    mailman_log('qrunner', 'IncomingRunner._oneloop: Message processing complete, moving to shunt queue %s', filebase)
                    self._shunt.enqueue(msg, msgdata)
            except Exception as e:
                # Log the error and requeue the message for later processing
                mailman_log('qrunner', 'IncomingRunner._oneloop: Error processing queue file %s: %s', filebase, str(e))
                if msg is not None and msgdata is not None:
                    try:
                        self._switchboard.enqueue(msg, msgdata)
                        mailman_log('qrunner', 'IncomingRunner._oneloop: Successfully requeued file %s', filebase)
                    except Exception as e2:
                        mailman_log('qrunner', 'IncomingRunner._oneloop: Failed to requeue file %s: %s', filebase, str(e2))
        
        mailman_log('qrunner', 'IncomingRunner._oneloop: Loop complete, processed %d files', len(files))
        return len(files)

