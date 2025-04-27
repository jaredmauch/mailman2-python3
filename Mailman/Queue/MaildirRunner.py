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
import Mailman.Message
from Mailman.Queue.Runner import Runner
from Mailman.Queue.sbcache import get_switchboard
from Mailman.Logging.Syslog import mailman_log
import pickle

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
    def __init__(self, slice=None, numslices=1):
        # Don't call the base class constructor, but build enough of the
        # underlying attributes to use the base class's implementation.
        self._stop = 0
        self._dir = os.path.join(mm_cfg.MAILDIR_DIR, 'new')
        self._cur = os.path.join(mm_cfg.MAILDIR_DIR, 'cur')
        self._parser = Parser(Mailman.Message.Message)

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
        pass

    def _dispose(self, mlist, msg, msgdata):
        """Process the maildir message."""
        try:
            # Process the maildir message
            mlist.process_maildir(msg)
        except Exception as e:
            mailman_log('error', 'Error processing maildir message for list %s: %s',
                   mlist.internal_name(), str(e))
            raise
