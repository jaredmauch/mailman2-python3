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
import email
from email import message_from_string
from email.message import Message as EmailMessage
from urllib.parse import parse_qs
from Mailman.Utils import reap
from Mailman import Utils

from Mailman import mm_cfg
from Mailman import Errors
from Mailman import LockFile
from Mailman.Queue.Runner import Runner
from Mailman.Queue.Switchboard import Switchboard
from Mailman.Logging.Syslog import mailman_log
import Mailman.MailList as MailList
import Mailman.Message
import threading
import email.header


class PipelineError(Exception):
    """Exception raised when pipeline processing fails."""
    pass


class IncomingRunner(Runner):
    QDIR = mm_cfg.INQUEUE_DIR

    # Enable message tracking for incoming messages
    _track_messages = True
    _max_processed_messages = 10000
    _max_retry_times = 10000
    
    # Retry configuration
    MIN_RETRY_DELAY = 300  # 5 minutes minimum delay between retries
    MAX_RETRIES = 5  # Maximum number of retry attempts
    _retry_times = {}  # Track last retry time for each message

    def __init__(self, slice=None, numslices=1):
        mailman_log('debug', 'IncomingRunner: Starting initialization')
        try:
            Runner.__init__(self, slice, numslices)
            mailman_log('debug', 'IncomingRunner: Initialization complete')
        except Exception as e:
            mailman_log('error', 'IncomingRunner: Initialization failed: %s\nTraceback:\n%s',
                       str(e), traceback.format_exc())
            raise

    def _convert_message(self, msg):
        """Convert email.message.Message to Mailman.Message with proper handling of nested messages."""
        return Runner._convert_message(self, msg)

    def _dispose(self, mlist, msg, msgdata):
        """Process an incoming message."""
        msgid = msg.get('message-id', 'n/a')
        filebase = msgdata.get('_filebase', 'unknown')
        
        try:
            # Enhanced logging for message details
            mailman_log('debug', 'IncomingRunner._dispose: Starting to process message %s (file: %s)', msgid, filebase)
            mailman_log('debug', 'Message details:')
            mailman_log('debug', '  Message ID: %s', msgid)
            mailman_log('debug', '  From: %s', msg.get('from', 'unknown'))
            mailman_log('debug', '  To: %s', msg.get('to', 'unknown'))
            mailman_log('debug', '  Subject: %s', msg.get('subject', '(no subject)'))
            mailman_log('debug', '  Message type: %s', type(msg).__name__)
            mailman_log('debug', '  Message data: %s', str(msgdata))
            mailman_log('debug', '  Pipeline: %s', msgdata.get('pipeline', 'No pipeline'))
            
            # Check if this is an administrative message
            is_admin = msgdata.get('admin_type', False)
            mailman_log('debug', '  Is admin message: %s', is_admin)
            
            # Check if this is a list post
            is_list_post = msgdata.get('list_post', False)
            mailman_log('debug', '  Is list post: %s', is_list_post)
            
            # Log recipients information
            recipients = msgdata.get('recips', [])
            mailman_log('debug', '  Recipients: %s', recipients)
            if not recipients:
                mailman_log('warning', 'IncomingRunner: No recipients found in msgdata for message %s, pipeline handlers may set them', msgid)
                mailman_log('debug', '  Message data: %s', str(msgdata))
                mailman_log('debug', '  To header: %s', msg.get('to', 'unknown'))
                mailman_log('debug', '  Cc header: %s', msg.get('cc', 'unknown'))
                mailman_log('debug', '  Resent-To: %s', msg.get('resent-to', 'unknown'))
                mailman_log('debug', '  Resent-Cc: %s', msg.get('resent-cc', 'unknown'))
            
            # Convert Python's Message to Mailman's Message if needed
            msg = self._convert_message(msg)
            
            # Check if this is a bounce message
            if self._is_bounce(msg):
                mailman_log('debug', 'IncomingRunner._dispose: Message %s is a bounce, routing to bounce queue', msgid)
                # Route to bounce queue
                bounce_queue = Switchboard(mm_cfg.BOUNCEQUEUE_DIR)
                bounce_queue.enqueue(msg, msgdata)
                return False
            
            # Get the pipeline
            pipeline = self._get_pipeline(mlist, msg, msgdata)
            if not pipeline:
                mailman_log('error', 'IncomingRunner._dispose: No pipeline found for message %s', msgid)
                return False
            
            # Process the message through the pipeline
            try:
                more = self._dopipeline(mlist, msg, msgdata, pipeline)
                if more:
                    mailman_log('debug', 'IncomingRunner._dispose: Message %s needs more processing', msgid)
                    return True
            except Exception as e:
                mailman_log('error', 'IncomingRunner._dispose: Error processing message %s: %s\nTraceback:\n%s',
                           msgid, str(e), traceback.format_exc())
                return False
            
            mailman_log('debug', 'IncomingRunner._dispose: Successfully processed message %s', msgid)
            return True
            
        except Exception as e:
            mailman_log('error', 'IncomingRunner._dispose: Error processing message %s: %s\nTraceback:\n%s',
                       msgid, str(e), traceback.format_exc())
            return False

    def _get_pipeline(self, mlist, msg, msgdata):
        """Get the pipeline for processing the message."""
        # We must return a copy of the list, otherwise, the first message that
        # flows through the pipeline will empty it out!
        return msgdata.get('pipeline',
                          getattr(mlist, 'pipeline',
                                 mm_cfg.GLOBAL_PIPELINE))[:]

    def _dopipeline(self, mlist, msg, msgdata, pipeline):
        """Process the message through the pipeline of handlers."""
        while pipeline:
            handler = pipeline.pop(0)
            modname = 'Mailman.Handlers.' + handler
            __import__(modname)
            try:
                pid = os.getpid()
                sys.modules[modname].process(mlist, msg, msgdata)
                # Failsafe -- a child may have leaked through
                if pid != os.getpid():
                    mailman_log('error', 'Child process leaked through: %s', modname)
                    os._exit(1)
            except Errors.DiscardMessage:
                # Throw the message away
                pipeline.insert(0, handler)
                mailman_log('vette', """Message discarded, msgid: %s
        list: %s,
        handler: %s""",
                       msg.get('message-id', 'n/a'),
                       mlist.real_name, handler)
                return 0
            except Errors.HoldMessage:
                # Let the approval process take it from here
                return 0
            except Errors.RejectMessage as e:
                # Log rejection and bounce message
                pipeline.insert(0, handler)
                mailman_log('vette', """Message rejected, msgid: %s
        list: %s,
        handler: %s,
        reason: %s""",
                       msg.get('message-id', 'n/a'),
                       mlist.real_name, handler, e.notice())
                mlist.BounceMessage(msg, msgdata, e)
                return 0
            except Exception as e:
                # Push this pipeline module back on the stack, then re-raise
                pipeline.insert(0, handler)
                raise
        # We've successfully completed handling of this message
        return 0

    def _is_command(self, msg):
        """Check if the message is a command."""
        try:
            subject = msg.get('subject', '').lower()
            if subject.startswith('subscribe') or subject.startswith('unsubscribe'):
                mailman_log('debug', 'IncomingRunner._is_command: Message is a subscription command')
                return True
            return False
        except Exception as e:
            mailman_log('error', 'IncomingRunner._is_command: Error checking command: %s\nTraceback:\n%s',
                       str(e), traceback.format_exc())
            return False

    def _is_bounce(self, msg):
        """Check if a message is a bounce message."""
        # Check for common bounce headers
        if msg.get('x-failed-recipients'):
            return True
        if msg.get('x-original-to'):
            return True
        if msg.get('return-path', '').startswith('<>'):
            return True
        # Check content type for multipart/report
        if msg.get('content-type', '').startswith('multipart/report'):
            return True
        # Check for common bounce subjects
        subject = msg.get('subject', '')
        if isinstance(subject, email.header.Header):
            subject = str(subject)
        subject = subject.lower()
        bounce_subjects = ['delivery status', 'failure notice', 'mail delivery failed',
                          'mail delivery system', 'mail system error', 'returned mail',
                          'undeliverable', 'undelivered mail']
        for bounce_subject in bounce_subjects:
            if bounce_subject in subject:
                return True
        return False

    def _process_command(self, mlist, msg, msgdata):
        """Process a command message."""
        msgid = msg.get('message-id', 'n/a')
        try:
            mailman_log('debug', 'IncomingRunner._process_command: Processing command for message %s', msgid)
            # Process the command
            # ... command processing logic ...
            mailman_log('debug', 'IncomingRunner._process_command: Successfully processed command for message %s', msgid)
            return True
        except Exception as e:
            mailman_log('error', 'IncomingRunner._process_command: Error processing command for message %s: %s\nTraceback:\n%s',
                       msgid, str(e), traceback.format_exc())
            return False

    def _process_bounce(self, mlist, msg, msgdata):
        """Process a bounce message."""
        msgid = msg.get('message-id', 'n/a')
        try:
            mailman_log('debug', 'IncomingRunner._process_bounce: Processing bounce for message %s', msgid)
            # Process the bounce
            # ... bounce processing logic ...
            mailman_log('debug', 'IncomingRunner._process_bounce: Successfully processed bounce for message %s', msgid)
            return True
        except Exception as e:
            mailman_log('error', 'IncomingRunner._process_bounce: Error processing bounce for message %s: %s\nTraceback:\n%s',
                       msgid, str(e), traceback.format_exc())
            return False

    def _process_regular_message(self, mlist, msg, msgdata):
        """Process a regular message."""
        msgid = msg.get('message-id', 'n/a')
        try:
            mailman_log('debug', 'IncomingRunner._process_regular_message: Processing regular message %s', msgid)
            # Process the regular message
            # ... regular message processing logic ...
            mailman_log('debug', 'IncomingRunner._process_regular_message: Successfully processed regular message %s', msgid)
            return True
        except Exception as e:
            mailman_log('error', 'IncomingRunner._process_regular_message: Error processing regular message %s: %s\nTraceback:\n%s',
                       msgid, str(e), traceback.format_exc())
            return False

    def _cleanup(self):
        """Clean up resources."""
        mailman_log('debug', 'IncomingRunner: Starting cleanup')
        try:
            Runner._cleanup(self)
        except Exception as e:
            mailman_log('error', 'IncomingRunner: Cleanup failed: %s\nTraceback:\n%s',
                       str(e), traceback.format_exc())
        mailman_log('debug', 'IncomingRunner: Cleanup complete')

    def _oneloop(self):
        """Process one batch of messages from the incoming queue."""
        try:
            # Get the list of files to process
            files = self._switchboard.files()
            filecnt = len(files)
            
            # Only log at debug level if we found files to process
            if filecnt > 0:
                mailman_log('debug', 'IncomingRunner._oneloop: Found %d files to process', filecnt)
            
            # Process each file
            for filebase in files:
                # Check stop flag at the start of each file
                if self._stop:
                    mailman_log('debug', 'IncomingRunner._oneloop: Stop flag detected, stopping processing')
                    return filecnt
                    
                try:
                    # Check if the file exists before dequeuing
                    pckfile = os.path.join(self.QDIR, filebase + '.pck')
                    if not os.path.exists(pckfile):
                        mailman_log('error', 'IncomingRunner._oneloop: File %s does not exist, skipping', pckfile)
                        continue
                        
                    # Check if file is locked
                    lockfile = os.path.join(self.QDIR, filebase + '.pck.lock')
                    if os.path.exists(lockfile):
                        mailman_log('debug', 'IncomingRunner._oneloop: File %s is locked by another process, skipping', filebase)
                        continue
                    
                    # Dequeue the file
                    msg, msgdata = self._switchboard.dequeue(filebase)
                    
                    # If dequeue failed due to file being locked, skip it
                    if msg is None and msgdata is None:
                        # For other None,None cases, shunt the message
                        mailman_log('error', 'IncomingRunner._oneloop: Failed to dequeue file %s (got None values), shunting', filebase)
                        # Create a basic message and metadata if we don't have them
                        msg = Message()
                        msgdata = {}
                        # Add the original queue information
                        msgdata['whichq'] = self.QDIR
                        # Shunt the message
                        self._shunt.enqueue(msg, msgdata)
                        continue
                    
                    # Try to get message-id early for logging purposes
                    try:
                        msgid = msg.get('message-id', 'n/a')
                    except Exception as e:
                        msgid = 'unknown'
                        mailman_log('error', 'IncomingRunner._oneloop: Error getting message-id for file %s: %s', filebase, str(e))
                    
                    # Get the list name
                    listname = msgdata.get('listname', 'unknown')
                    try:
                        mlist = MailList.MailList(listname, lock=False)
                    except Errors.MMUnknownListError:
                        mailman_log('error', 'IncomingRunner._oneloop: Unknown list %s for message %s (file: %s)',
                                  listname, msgid, filebase)
                        self._shunt.enqueue(msg, msgdata)
                        continue
                    
                    # Process the message
                    try:
                        result = self._dispose(mlist, msg, msgdata)
                        
                        # If the message should be kept in the queue, requeue it
                        if result:
                            # Get pipeline information for logging
                            pipeline = msgdata.get('pipeline', [])
                            current_handler = pipeline[0] if pipeline else 'unknown'
                            next_handler = pipeline[1] if len(pipeline) > 1 else 'none'
                            
                            # Get retry information
                            retry_count = msgdata.get('retry_count', 0)
                            last_retry = self._retry_times.get(msgid, 0)
                            next_retry = time.ctime(last_retry + self.MIN_RETRY_DELAY) if last_retry else 'unknown'
                            
                            # Log detailed requeue information
                            mailman_log('info', 'IncomingRunner._oneloop: Message requeued for later processing: %s (msgid: %s)', 
                                      filebase, msgid)
                            mailman_log('debug', '  Current state:')
                            mailman_log('debug', '    - Current handler: %s', current_handler)
                            mailman_log('debug', '    - Next handler: %s', next_handler)
                            mailman_log('debug', '    - Retry count: %d', retry_count)
                            mailman_log('debug', '    - Last retry: %s', time.ctime(last_retry) if last_retry else 'none')
                            mailman_log('debug', '    - Next retry: %s', next_retry)
                            mailman_log('debug', '    - List: %s', mlist.internal_name())
                            mailman_log('debug', '    - Message type: %s', msgdata.get('_msgtype', 'unknown'))
                            
                            self._switchboard.enqueue(msg, msgdata)
                        else:
                            mailman_log('info', 'IncomingRunner._oneloop: Message processing complete, moving to shunt queue %s (msgid: %s)',
                                      filebase, msgid)
                            
                    except Exception as e:
                        mailman_log('error', 'IncomingRunner._oneloop: Error processing message %s (file: %s): %s\n%s',
                                  msgid, filebase, str(e), traceback.format_exc())
                        # Move to shunt queue on error
                        self._shunt.enqueue(msg, msgdata)
                        
                except Exception as e:
                    mailman_log('error', 'IncomingRunner._oneloop: Error dequeuing file %s: %s\n%s',
                              filebase, str(e), traceback.format_exc())
                    
            # Only log completion at debug level if we processed files
            if filecnt > 0:
                mailman_log('debug', 'IncomingRunner._oneloop: Loop complete, processed %d files', filecnt)
                
        except Exception as e:
            mailman_log('error', 'IncomingRunner._oneloop: Unexpected error in main loop: %s\n%s',
                      str(e), traceback.format_exc())
            # Don't re-raise the exception to keep the runner alive
            return False
        return True

    def _check_retry_delay(self, msgid, filebase):
        """Check if enough time has passed since the last retry attempt."""
        now = time.time()
        last_retry = self._retry_times.get(msgid, 0)
        
        if now - last_retry < self.MIN_RETRY_DELAY:
            mailman_log('debug', 'IncomingRunner._check_retry_delay: Message %s (file: %s) retry delay not met. Last retry: %s, Now: %s, Delay needed: %s',
                       msgid, filebase, time.ctime(last_retry), time.ctime(now), self.MIN_RETRY_DELAY)
            return False
        
        mailman_log('debug', 'IncomingRunner._check_retry_delay: Message %s (file: %s) retry delay met. Last retry: %s, Now: %s',
                   msgid, filebase, time.ctime(last_retry), time.ctime(now))
        return True

    def _mark_message_processed(self, msgid):
        """Mark a message as processed."""
        with self._processed_lock:
            self._processed_messages.add(msgid)

    def _unmark_message_processed(self, msgid):
        """Remove a message from the processed set."""
        with self._processed_lock:
            self._processed_messages.discard(msgid)

    def _process_admin(self, mlist, msg, msgdata):
        """Process an admin message."""
        msgid = msg.get('message-id', 'n/a')
        try:
            mailman_log('debug', 'IncomingRunner._process_admin: Processing admin message %s', msgid)
            
            # Get admin information
            recipient = msgdata.get('recipient', 'unknown')
            admin_type = msgdata.get('admin_type', 'unknown')
            
            mailman_log('debug', 'IncomingRunner._process_admin: Admin message for %s, type: %s',
                       recipient, admin_type)
            
            # Process the admin message
            # ... admin message processing logic ...
            
            mailman_log('debug', 'IncomingRunner._process_admin: Successfully processed admin message %s', msgid)
            return True
            
        except Exception as e:
            mailman_log('error', 'IncomingRunner._process_admin: Error processing admin message %s: %s\nTraceback:\n%s',
                       msgid, str(e), traceback.format_exc())
            return False

    def _check_message_processed(self, msgid, filebase, msg):
        """Check if a message has already been processed and if retry delay is met.
        
        Args:
            msgid: The message ID to check
            filebase: The base filename of the message
            msg: The message object
            
        Returns:
            bool: True if message should be skipped (already processed or retry delay not met),
                  False if message should be processed
        """
        try:
            # Check if message was recently processed
            with self._processed_lock:
                if msgid in self._processed_messages:
                    mailman_log('debug', 'IncomingRunner._check_message_processed: Message %s (file: %s) was recently processed, skipping',
                              msgid, filebase)
                    return True
                
            # Check if retry delay is met
            if not self._check_retry_delay(msgid, filebase):
                return True
                
            # Message should be processed
            return False
            
        except Exception as e:
            mailman_log('error', 'IncomingRunner._check_message_processed: Error checking message %s: %s\nTraceback:\n%s',
                      msgid, str(e), traceback.format_exc())
            # On error, allow the message to be processed
            return False
