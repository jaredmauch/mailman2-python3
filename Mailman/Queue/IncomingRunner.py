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

from Mailman import mm_cfg
from Mailman import Errors
from Mailman import LockFile
from Mailman.Queue.Runner import Runner
from Mailman.Logging.Syslog import mailman_log
from Mailman.Utils import Utils


class PipelineError(Exception):
    """Exception raised when pipeline processing fails."""
    pass


class IncomingRunner(Runner):
    QDIR = mm_cfg.INQUEUE_DIR

    def _dispose(self, mlist, msg, msgdata):
        # Try to get the list lock with timeout
        try:
            mlist.Lock(timeout=mm_cfg.LIST_LOCK_TIMEOUT)
        except LockFile.TimeOutError:
            mailman_log('warning', 'List lock timeout for %s', mlist.real_name)
            return 1

        try:
            # Get and validate the pipeline
            pipeline = self._get_pipeline(mlist, msg, msgdata)
            self._validate_pipeline(pipeline)
            
            msgdata['pipeline'] = pipeline
            more = self._dopipeline(mlist, msg, msgdata, pipeline)
            
            if not more:
                del msgdata['pipeline']
                
            try:
                mlist.Save(timeout=mm_cfg.SAVE_TIMEOUT)
            except Exception as e:
                mailman_log('error', 'Failed to save list %s: %s\nTraceback:\n%s', 
                          mlist.real_name, str(e), traceback.format_exc())
                raise
                
            return more
        finally:
            try:
                mlist.Unlock()
            except Exception as e:
                mailman_log('error', 'Failed to unlock list %s: %s\nTraceback:\n%s', 
                          mlist.real_name, str(e), traceback.format_exc())

    def _get_pipeline(self, mlist, msg, msgdata):
        # Return a copy of the pipeline to prevent modification of the original
        return msgdata.get('pipeline',
                          getattr(mlist, 'pipeline',
                                 mm_cfg.GLOBAL_PIPELINE))[:]

    def _validate_pipeline(self, pipeline):
        """Validate that all handlers in the pipeline exist and are importable."""
        for handler in pipeline:
            modname = 'Mailman.Handlers.' + handler
            try:
                __import__(modname)
            except ImportError as e:
                mailman_log('error', 'Invalid pipeline handler: %s\nError: %s\nTraceback:\n%s', 
                          handler, str(e), traceback.format_exc())
                raise PipelineError('Invalid handler: %s' % handler)

    def _dopipeline(self, mlist, msg, msgdata, pipeline):
        retry_count = 0
        max_retries = getattr(mm_cfg, 'MAX_PIPELINE_RETRIES', 3)
        
        # Log inbound message details
        msgid = msg.get('message-id', 'n/a')
        sender = msg.get('from', 'n/a')
        subject = msg.get('subject', 'n/a')
        mailman_log('info', 'Inbound message received - msgid: %s, list: %s, sender: %s, subject: %s',
               msgid, mlist.real_name, sender, subject)
        
        while pipeline and retry_count < max_retries:
            handler = pipeline.pop(0)
            modname = 'Mailman.Handlers.' + handler
            
            try:
                __import__(modname)
                pid = os.getpid()
                sys.modules[modname].process(mlist, msg, msgdata)
                
                if pid != os.getpid():
                    mailman_log('error', 'child process leaked thru: %s', modname)
                    # Clean up child processes before exiting
                    Utils.reap(self._kids, once=True)
                    os._exit(1)
                    
            except Errors.DiscardMessage:
                pipeline.insert(0, handler)
                mailman_log('info', 'Message discarded, msgid: %s, list: %s, handler: %s',
                       msg.get('message-id', 'n/a'), mlist.real_name, handler)
                return 0
                
            except Errors.HoldMessage:
                mailman_log('info', 'Message held for approval, msgid: %s, list: %s',
                       msg.get('message-id', 'n/a'), mlist.real_name)
                return 0
                
            except Errors.RejectMessage as e:
                pipeline.insert(0, handler)
                mailman_log('info', 'Message rejected, msgid: %s, list: %s, handler: %s, reason: %s',
                       msg.get('message-id', 'n/a'), mlist.real_name, handler, e.notice())
                mlist.BounceMessage(msg, msgdata, e)
                return 0
                
            except Exception as e:
                pipeline.insert(0, handler)
                mailman_log('error', 'Pipeline error in handler %s: %s\nTraceback:\n%s', 
                          handler, str(e), traceback.format_exc())
                retry_count += 1
                if retry_count >= max_retries:
                    mailman_log('error', 'Max retries exceeded for msgid: %s', msgid)
                    raise
                time.sleep(1)  # Brief pause before retry
                
        # Log completion status
        if len(pipeline) == 0:
            mailman_log('info', 'Message processing completed - msgid: %s, list: %s', msgid, mlist.real_name)
        else:
            mailman_log('info', 'Message processing paused - msgid: %s, list: %s, remaining handlers: %s',
                   msgid, mlist.real_name, ', '.join(pipeline))
            
        return len(pipeline) > 0

    def _cleanup(self):
        """Clean up any resources used by the pipeline."""
        # Clean up child processes
        Utils.reap(self._kids, once=True)
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
        mailman_log('debug', 'IncomingRunner: Found %d files in queue directory %s', len(files), self.QDIR)
        for filebase in files:
            try:
                # Log the queue file being processed
                mailman_log('debug', 'IncomingRunner: Processing queue file: %s', filebase)
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
                mailman_log('error', 'Error processing queue file %s: %s\nTraceback:\n%s', 
                          filebase, str(e), traceback.format_exc())
                # Requeue the message for later processing
                try:
                    self._switchboard.enqueue(msg, msgdata)
                except Exception as e:
                    mailman_log('error', 'Failed to requeue message %s: %s\nTraceback:\n%s', 
                              filebase, str(e), traceback.format_exc())
        return len(files)
