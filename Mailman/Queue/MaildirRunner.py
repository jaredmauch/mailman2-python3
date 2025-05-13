# Copyright (C) 2002-2018 by the Free Software Foundation, Inc.
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

"""Maildir queue runner.

This module is responsible for processing messages from a maildir directory.
"""

from builtins import object
import time
import traceback
from io import StringIO
import os
import sys
import email
from email.utils import getaddresses
from email.iterators import body_line_iterator

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
from Mailman import i18n
from Mailman.Message import Message
from Mailman.Logging.Syslog import syslog
from Mailman.Queue.Runner import Runner

# We only care about the listname and the subq as in listname@ or
# listname-request@
lre = re.compile(r"""
 ^                        # start of string
 (?P<listname>[^+@]+?)    # listname@ or listname-subq@ (non-greedy)
 (?:                      # non-grouping
   -                      # dash separator
   (?P<subq>              # any known suffix
     admin|
     bounces|
     confirm|
     join|
     leave|
     owner|
     request|
     subscribe|
     unsubscribe
   )
 )?                       # if it exists
 [+@]                     # followed by + or @
 """, re.VERBOSE | re.IGNORECASE)


class MaildirRunner(Runner):
    # This class is much different than most runners because it pulls files
    # of a different format than what scripts/post and friends leaves.  The
    # files this runner reads are just single message files as dropped into
    # the directory by the MTA.  This runner will read the file, and enqueue
    # it in the expected qfiles directory for normal processing.
    QDIR = mm_cfg.MAILDIR_DIR

    def __init__(self, slice=None, numslices=1):
        syslog('debug', 'MaildirRunner: Starting initialization')
        try:
            Runner.__init__(self, slice, numslices)
            self._maildir = mm_cfg.MAILDIR_DIR
            if not os.path.exists(self._maildir):
                os.makedirs(self._maildir)
            syslog('debug', 'MaildirRunner: Initialization complete')
        except Exception as e:
            syslog('error', 'MaildirRunner: Initialization failed: %s\nTraceback:\n%s',
                   str(e), traceback.format_exc())
            raise

    def _oneloop(self):
        """Process one batch of messages from the maildir."""
        try:
            # Get the list of files to process
            files = []
            for filename in os.listdir(self._maildir):
                if filename.startswith('.'):
                    continue
                files.append(os.path.join(self._maildir, filename))
            
            # Process each file
            for filepath in files:
                try:
                    # Read the message
                    with open(filepath, 'rb') as fp:
                        msg = email.message_from_binary_file(fp)
                    
                    # Process the message
                    try:
                        self._process_message(msg, filepath)
                    except Exception as e:
                        syslog('error', 'Error processing message %s: %s',
                               msg.get('message-id', 'n/a'), str(e))
                        continue
                    
                    # Move the file to the processed directory
                    try:
                        os.rename(filepath, filepath + '.processed')
                    except Exception as e:
                        syslog('error', 'Error moving maildir file %s: %s',
                               filepath, str(e))
                        
                except Exception as e:
                    syslog('error', 'Error processing maildir file %s: %s',
                           filepath, str(e))
                    
        except Exception as e:
            syslog('error', 'Error in maildir runner: %s', e)

    def _cleanup(self):
        """Clean up resources."""
        syslog('debug', 'MaildirRunner: Starting cleanup')
        try:
            # Call parent cleanup
            super(MaildirRunner, self)._cleanup()
        except Exception as e:
            syslog('error', 'MaildirRunner: Cleanup failed: %s\nTraceback:\n%s',
                   str(e), traceback.format_exc())
        syslog('debug', 'MaildirRunner: Cleanup complete')

    def _dispose(self, mlist, msg, msgdata):
        """Process a maildir message."""
        try:
            # Get the message ID
            msgid = msg.get('message-id', 'n/a')
            filebase = msgdata.get('_filebase', 'unknown')
            
            syslog('debug', 'MaildirRunner._dispose: Starting to process maildir message %s (file: %s) for list %s',
                   msgid, filebase, mlist.internal_name())
            
            # Check retry delay
            if not self._check_retry_delay(msgid, filebase):
                syslog('debug', 'MaildirRunner._dispose: Message %s failed retry delay check, skipping', msgid)
                return True
            
            # Get the list object
            try:
                mlist = MailList.MailList(mlist.internal_name(), lock=False)
                syslog('debug', 'MaildirRunner._dispose: Successfully loaded list %s', mlist.internal_name())
            except Errors.MMListError as e:
                syslog('error', 'MaildirRunner._dispose: Failed to load list %s: %s\nTraceback:\n%s',
                       mlist.internal_name(), str(e), traceback.format_exc())
                return True
            except Exception as e:
                syslog('error', 'MaildirRunner._dispose: Unexpected error loading list %s: %s\nTraceback:\n%s',
                       mlist.internal_name(), str(e), traceback.format_exc())
                return True
            
            # Validate the message
            if not self._validate_message(msg, msgdata):
                syslog('error', 'MaildirRunner._dispose: Message validation failed for message %s', msgid)
                return True
            
            # Check for Message-ID
            if not msg.get('message-id'):
                syslog('error', 'MaildirRunner._dispose: Message missing Message-ID header')
                return True
            
            # Process the message
            syslog('debug', 'MaildirRunner._dispose: Processing maildir message %s', msgid)
            
            # Get the recipient
            recipient = msg.get('to', '')
            if not recipient:
                recipient = msg.get('recipients', '')
            
            # Determine message type
            if msg.get('x-mailman-command'):
                syslog('debug', 'MaildirRunner._dispose: Message %s is type %s for recipient %s',
                       msgid, 'command', recipient)
                self._process_command(mlist, msg, msgdata)
            elif msg.get('x-mailman-bounce'):
                syslog('debug', 'MaildirRunner._dispose: Message %s is type %s for recipient %s',
                       msgid, 'bounce', recipient)
                self._process_bounce(mlist, msg, msgdata)
            else:
                syslog('debug', 'MaildirRunner._dispose: Message %s is type %s for recipient %s',
                       msgid, 'regular', recipient)
                self._process_regular(mlist, msg, msgdata)
            
            syslog('debug', 'MaildirRunner._dispose: Successfully processed maildir message %s', msgid)
            return False
            
        except Exception as e:
            syslog('error', 'MaildirRunner._dispose: Failed to process maildir message %s', msgid)
            syslog('error', 'MaildirRunner._dispose: Error processing maildir message %s: %s\nTraceback:\n%s',
                   msgid, str(e), traceback.format_exc())
            return True

    def _process_bounce(self, mlist, msg, msgdata):
        """Process a bounce message."""
        try:
            msgid = msg.get('message-id', 'n/a')
            syslog('debug', 'MaildirRunner._process_bounce: Processing bounce message %s', msgid)
            
            # Get the recipient
            recipient = msg.get('to', '')
            if not recipient:
                recipient = msg.get('recipients', '')
            
            # Get bounce info
            bounce_info = msg.get('x-mailman-bounce-info', '')
            
            syslog('debug', 'MaildirRunner._process_bounce: Bounce for recipient %s, info: %s',
                   recipient, bounce_info)
            
            # Process the bounce
            mlist.process_bounce(msg, bounce_info)
            
            syslog('debug', 'MaildirRunner._process_bounce: Successfully processed bounce message %s', msgid)
            
        except Exception as e:
            syslog('error', 'MaildirRunner._process_bounce: Error processing bounce message %s: %s\nTraceback:\n%s',
                   msgid, str(e), traceback.format_exc())

    def _process_command(self, mlist, msg, msgdata):
        """Process a command message."""
        try:
            msgid = msg.get('message-id', 'n/a')
            syslog('debug', 'MaildirRunner._process_command: Processing command message %s', msgid)
            
            # Get the recipient
            recipient = msg.get('to', '')
            if not recipient:
                recipient = msg.get('recipients', '')
            
            # Get command type
            command = msg.get('x-mailman-command', '')
            
            syslog('debug', 'MaildirRunner._process_command: Command for recipient %s, type: %s',
                   recipient, command)
            
            # Process the command
            mlist.process_command(msg, command)
            
            syslog('debug', 'MaildirRunner._process_command: Successfully processed command message %s', msgid)
            
        except Exception as e:
            syslog('error', 'MaildirRunner._process_command: Error processing command message %s: %s\nTraceback:\n%s',
                   msgid, str(e), traceback.format_exc())

    def _process_regular(self, mlist, msg, msgdata):
        """Process a regular message."""
        try:
            msgid = msg.get('message-id', 'n/a')
            syslog('debug', 'MaildirRunner._process_regular: Processing regular message %s', msgid)
            
            # Get the recipient
            recipient = msg.get('to', '')
            if not recipient:
                recipient = msg.get('recipients', '')
            
            syslog('debug', 'MaildirRunner._process_regular: Regular message for recipient %s', recipient)
            
            # Process the message
            mlist.process_regular(msg)
            
            syslog('debug', 'MaildirRunner._process_regular: Successfully processed regular message %s', msgid)
            
        except Exception as e:
            syslog('error', 'MaildirRunner._process_regular: Error processing regular message %s: %s\nTraceback:\n%s',
                   msgid, str(e), traceback.format_exc())
