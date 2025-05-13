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

from builtins import str
import os
import re
import errno
import time
import traceback
from io import StringIO
import email
from email.utils import getaddresses, parsedate_tz, mktime_tz, parseaddr
from email.iterators import body_line_iterator

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
from Mailman import i18n
from Mailman.Message import Message
from Mailman.Logging.Syslog import syslog
from Mailman.Queue.Runner import Runner
from Mailman.Queue.sbcache import get_switchboard

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
            self._dir = os.path.join(mm_cfg.MAILDIR_DIR, 'new')
            self._cur = os.path.join(mm_cfg.MAILDIR_DIR, 'cur')
            if not os.path.exists(self._dir):
                os.makedirs(self._dir)
            if not os.path.exists(self._cur):
                os.makedirs(self._cur)
            syslog('debug', 'MaildirRunner: Initialization complete')
        except Exception as e:
            syslog('error', 'MaildirRunner: Initialization failed: %s\nTraceback:\n%s',
                   str(e), traceback.format_exc())
            raise

    def _oneloop(self):
        """Process one batch of messages from the maildir."""
        # Refresh this each time through the list
        listnames = Utils.list_names()
        try:
            files = os.listdir(self._dir)
        except OSError as e:
            if e.errno != errno.ENOENT:
                syslog('error', 'Error listing maildir directory: %s', str(e))
                raise
            # Nothing's been delivered yet
            return 0

        for file in files:
            srcname = os.path.join(self._dir, file)
            dstname = os.path.join(self._cur, file + ':1,P')
            xdstname = os.path.join(self._cur, file + ':1,X')
            
            try:
                os.rename(srcname, dstname)
            except OSError as e:
                if e.errno == errno.ENOENT:
                    # Some other MaildirRunner beat us to it
                    continue
                syslog('error', 'Could not rename maildir file: %s', srcname)
                raise

            try:
                # Read and parse the message
                with open(dstname, 'rb') as fp:
                    msg = email.message_from_binary_file(fp)

                # Figure out which queue of which list this message was destined for
                vals = []
                for header in ('delivered-to', 'envelope-to', 'apparently-to'):
                    vals.extend(msg.get_all(header, []))
                
                for field in vals:
                    to = parseaddr(field)[1]
                    if not to:
                        continue
                    mo = lre.match(to)
                    if not mo:
                        # This isn't an address we care about
                        continue
                    listname, subq = mo.group('listname', 'subq')
                    if listname in listnames:
                        break
                else:
                    # As far as we can tell, this message isn't destined for
                    # any list on the system
                    syslog('error', 'Message apparently not for any list: %s',
                           xdstname)
                    os.rename(dstname, xdstname)
                    continue

                # Determine which queue to use based on the subqueue
                msgdata = {'listname': listname}
                if subq in ('bounces', 'admin'):
                    queue = get_switchboard(mm_cfg.BOUNCEQUEUE_DIR)
                elif subq == 'confirm':
                    msgdata['toconfirm'] = 1
                    queue = get_switchboard(mm_cfg.CMDQUEUE_DIR)
                elif subq in ('join', 'subscribe'):
                    msgdata['tojoin'] = 1
                    queue = get_switchboard(mm_cfg.CMDQUEUE_DIR)
                elif subq in ('leave', 'unsubscribe'):
                    msgdata['toleave'] = 1
                    queue = get_switchboard(mm_cfg.CMDQUEUE_DIR)
                elif subq == 'owner':
                    msgdata.update({
                        'toowner': 1,
                        'envsender': Utils.get_site_email(extra='bounces'),
                        'pipeline': mm_cfg.OWNER_PIPELINE,
                        })
                    queue = get_switchboard(mm_cfg.INQUEUE_DIR)
                elif subq is None:
                    msgdata['tolist'] = 1
                    queue = get_switchboard(mm_cfg.INQUEUE_DIR)
                elif subq == 'request':
                    msgdata['torequest'] = 1
                    queue = get_switchboard(mm_cfg.CMDQUEUE_DIR)
                else:
                    syslog('error', 'Unknown sub-queue: %s', subq)
                    os.rename(dstname, xdstname)
                    continue

                # Enqueue the message and clean up
                queue.enqueue(msg, msgdata)
                os.unlink(dstname)
                syslog('debug', 'Successfully processed maildir message: %s', file)

            except Exception as e:
                syslog('error', 'Error processing maildir file %s: %s\nTraceback:\n%s',
                       file, str(e), traceback.format_exc())
                try:
                    os.rename(dstname, xdstname)
                except OSError:
                    pass

        return len(files)

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
