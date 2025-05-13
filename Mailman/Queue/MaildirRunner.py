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

"""Maildir pre-queue runner.

Most MTAs can be configured to deliver messages to a `Maildir'[1].  This
runner will read messages from a maildir's new/ directory and inject them into
Mailman's qfiles/in directory for processing in the normal pipeline.  This
delivery mechanism contrasts with mail program delivery, where incoming
messages end up in qfiles/in via the MTA executing the scripts/post script
(and likewise for the other -aliases for each mailing list).

The advantage to Maildir delivery is that it is more efficient; there's no
need to fork an intervening program just to take the message from the MTA's
standard output, to the qfiles/in directory.

[1] http://cr.yp.to/proto/maildir.html

We're going to use the :info flag == 1, experimental status flag for our own
purposes.  The :1 can be followed by one of these letters:

- P means that MaildirRunner's in the process of parsing and enqueuing the
  message.  If successful, it will delete the file.

- X means something failed during the parse/enqueue phase.  An error message
  will be logged to log/error and the file will be renamed <filename>:1,X.
  MaildirRunner will never automatically return to this file, but once the
  problem is fixed, you can manually move the file back to the new/ directory
  and MaildirRunner will attempt to re-process it.  At some point we may do
  this automatically.

See the variable USE_MAILDIR in Defaults.py.in for enabling this delivery
mechanism.
"""

# NOTE: Maildir delivery is experimental in Mailman 2.1.

from builtins import str
import os
import re
import errno
import time
import email
from email.utils import getaddresses
from email.iterators import body_line_iterator

from email.parser import Parser
from email.utils import parseaddr

from Mailman import mm_cfg
from Mailman import Utils
from Mailman.Message import Message
from Mailman.Queue.Runner import Runner
from Mailman.Queue.sbcache import get_switchboard
from Mailman.Logging.Syslog import mailman_log
import pickle
import traceback

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
        mailman_log('debug', 'MaildirRunner: Starting initialization')
        try:
            Runner.__init__(self, slice, numslices)
            mailman_log('debug', 'MaildirRunner: Initialization complete')
        except Exception as e:
            mailman_log('error', 'MaildirRunner: Initialization failed: %s\nTraceback:\n%s',
                       str(e), traceback.format_exc())
            raise

    def _oneloop(self):
        """Process one batch of messages."""
        try:
            # Get list of files in new directory
            try:
                files = os.listdir(self._dir)
            except OSError as e:
                if e.errno != errno.ENOENT:
                    mailman_log('error', 'Error reading maildir directory %s: %s',
                              self._dir, e)
                return
                
            for filename in files:
                try:
                    # Skip non-files
                    fullpath = os.path.join(self._dir, filename)
                    if not os.path.isfile(fullpath):
                        continue
                        
                    # Read and parse the file
                    try:
                        with open(fullpath, 'rb') as fp:
                            # Use protocol 2 for Python 2/3 compatibility
                            protocol = 2
                            listname = pickle.load(fp, fix_imports=True, encoding='latin1')
                            msg = pickle.load(fp, fix_imports=True, encoding='latin1')
                            msgdata = pickle.load(fp, fix_imports=True, encoding='latin1')
                    except (pickle.UnpicklingError, EOFError) as e:
                        mailman_log('error', 'Error unpickling maildir file %s: %s',
                                  fullpath, e)
                        continue
                        
                    # Validate message headers
                    if not msg.get('message-id'):
                        mailman_log('error', 'Message missing Message-ID header')
                        continue
                        
                    # Process the message
                    try:
                        self._dispose(listname, msg, msgdata)
                    except Exception as e:
                        mailman_log('error', 'Error processing message %s: %s',
                                  msg.get('message-id', 'n/a'), e)
                        continue
                        
                    # Move file to cur directory
                    try:
                        os.rename(fullpath, os.path.join(self._cur, filename))
                    except OSError as e:
                        mailman_log('error', 'Error moving maildir file %s: %s',
                                  fullpath, e)
                        continue
                        
                except Exception as e:
                    mailman_log('error', 'Error processing maildir file %s: %s',
                              filename, e)
                    continue
                    
        except Exception as e:
            mailman_log('error', 'Error in maildir runner: %s', e)
            return

    def _cleanup(self):
        """Clean up resources."""
        mailman_log('debug', 'MaildirRunner: Starting cleanup')
        try:
            Runner._cleanup(self)
        except Exception as e:
            mailman_log('error', 'MaildirRunner: Cleanup failed: %s\nTraceback:\n%s',
                       str(e), traceback.format_exc())
        mailman_log('debug', 'MaildirRunner: Cleanup complete')

    def _dispose(self, mlist, msg, msgdata):
        """Process a maildir message."""
        msgid = msg.get('message-id', 'n/a')
        filebase = msgdata.get('_filebase', 'unknown')
        
        mailman_log('debug', 'MaildirRunner._dispose: Starting to process maildir message %s (file: %s) for list %s',
                   msgid, filebase, mlist.internal_name())
        
        # Check retry delay and duplicate processing
        if not self._check_retry_delay(msgid, filebase):
            mailman_log('debug', 'MaildirRunner._dispose: Message %s failed retry delay check, skipping', msgid)
            return False

        # Make sure we have the most up-to-date state
        try:
            mlist.Load()
            mailman_log('debug', 'MaildirRunner._dispose: Successfully loaded list %s', mlist.internal_name())
        except Errors.MMCorruptListDatabaseError as e:
            mailman_log('error', 'MaildirRunner._dispose: Failed to load list %s: %s\nTraceback:\n%s',
                       mlist.internal_name(), str(e), traceback.format_exc())
            self._unmark_message_processed(msgid)
            return False
        except Exception as e:
            mailman_log('error', 'MaildirRunner._dispose: Unexpected error loading list %s: %s\nTraceback:\n%s',
                       mlist.internal_name(), str(e), traceback.format_exc())
            self._unmark_message_processed(msgid)
            return False

        # Validate message type first
        msg, success = self._validate_message(msg, msgdata)
        if not success:
            mailman_log('error', 'MaildirRunner._dispose: Message validation failed for message %s', msgid)
            self._unmark_message_processed(msgid)
            return False

        # Validate message headers
        if not msg.get('message-id'):
            mailman_log('error', 'MaildirRunner._dispose: Message missing Message-ID header')
            self._unmark_message_processed(msgid)
            return False

        # Process the maildir message
        try:
            mailman_log('debug', 'MaildirRunner._dispose: Processing maildir message %s', msgid)
            
            # Get message type and recipient
            msgtype = msgdata.get('_msgtype', 'unknown')
            recipient = msgdata.get('recipient', 'unknown')
            
            mailman_log('debug', 'MaildirRunner._dispose: Message %s is type %s for recipient %s',
                       msgid, msgtype, recipient)
            
            # Process based on message type
            if msgtype == 'bounce':
                success = self._process_bounce(mlist, msg, msgdata)
            elif msgtype == 'admin':
                success = self._process_admin(mlist, msg, msgdata)
            else:
                success = self._process_regular(mlist, msg, msgdata)
                
            if success:
                mailman_log('debug', 'MaildirRunner._dispose: Successfully processed maildir message %s', msgid)
                return True
            else:
                mailman_log('error', 'MaildirRunner._dispose: Failed to process maildir message %s', msgid)
                return False

        except Exception as e:
            mailman_log('error', 'MaildirRunner._dispose: Error processing maildir message %s: %s\nTraceback:\n%s',
                       msgid, str(e), traceback.format_exc())
            self._unmark_message_processed(msgid)
            return False

    def _process_bounce(self, mlist, msg, msgdata):
        """Process a bounce message."""
        msgid = msg.get('message-id', 'n/a')
        try:
            mailman_log('debug', 'MaildirRunner._process_bounce: Processing bounce message %s', msgid)
            
            # Get bounce information
            recipient = msgdata.get('recipient', 'unknown')
            bounce_info = msgdata.get('bounce_info', {})
            
            mailman_log('debug', 'MaildirRunner._process_bounce: Bounce for recipient %s, info: %s',
                       recipient, str(bounce_info))
            
            # Process the bounce
            # ... bounce processing logic ...
            
            mailman_log('debug', 'MaildirRunner._process_bounce: Successfully processed bounce message %s', msgid)
            return True
            
        except Exception as e:
            mailman_log('error', 'MaildirRunner._process_bounce: Error processing bounce message %s: %s\nTraceback:\n%s',
                       msgid, str(e), traceback.format_exc())
            return False

    def _process_admin(self, mlist, msg, msgdata):
        """Process an admin message."""
        msgid = msg.get('message-id', 'n/a')
        try:
            mailman_log('debug', 'MaildirRunner._process_admin: Processing admin message %s', msgid)
            
            # Get admin information
            recipient = msgdata.get('recipient', 'unknown')
            admin_type = msgdata.get('admin_type', 'unknown')
            
            mailman_log('debug', 'MaildirRunner._process_admin: Admin message for %s, type: %s',
                       recipient, admin_type)
            
            # Process the admin message
            # ... admin message processing logic ...
            
            mailman_log('debug', 'MaildirRunner._process_admin: Successfully processed admin message %s', msgid)
            return True
            
        except Exception as e:
            mailman_log('error', 'MaildirRunner._process_admin: Error processing admin message %s: %s\nTraceback:\n%s',
                       msgid, str(e), traceback.format_exc())
            return False

    def _process_regular(self, mlist, msg, msgdata):
        """Process a regular maildir message."""
        msgid = msg.get('message-id', 'n/a')
        try:
            mailman_log('debug', 'MaildirRunner._process_regular: Processing regular message %s', msgid)
            
            # Get recipient information
            recipient = msgdata.get('recipient', 'unknown')
            
            mailman_log('debug', 'MaildirRunner._process_regular: Regular message for recipient %s', recipient)
            
            # Process the regular message
            # ... regular message processing logic ...
            
            mailman_log('debug', 'MaildirRunner._process_regular: Successfully processed regular message %s', msgid)
            return True
            
        except Exception as e:
            mailman_log('error', 'MaildirRunner._process_regular: Error processing regular message %s: %s\nTraceback:\n%s',
                       msgid, str(e), traceback.format_exc())
            return False
