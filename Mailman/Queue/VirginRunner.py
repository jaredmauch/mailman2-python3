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

"""Virgin message queue runner.

This qrunner handles messages that the Mailman system gives virgin birth to.
E.g. acknowledgement responses to user posts or Replybot messages.  They need
to go through some minimal processing before they can be sent out to the
recipient.
"""

from Mailman import mm_cfg
from Mailman.Queue.Runner import Runner
from Mailman.Queue.IncomingRunner import IncomingRunner
from Mailman.Logging.Syslog import mailman_log
from Mailman import MailList
import traceback


class VirginRunner(IncomingRunner):
    QDIR = mm_cfg.VIRGINQUEUE_DIR

    def _dispose(self, listname, msg, msgdata):
        msgid = msg.get('message-id', 'n/a')
        filebase = msgdata.get('_filebase', 'unknown')
        
        # Check retry delay and duplicate processing
        if not self._check_retry_delay(msgid, filebase):
            return 0

        try:
            # Get the MailList object
            try:
                mlist = MailList.MailList(listname, lock=0)
            except Exception as e:
                mailman_log('error', 'Failed to get MailList object for list %s: %s',
                           listname, str(e))
                return 0

            # Log start of processing
            mailman_log('info', 'VirginRunner: Starting to process virgin message %s (file: %s) for list %s',
                       msgid, filebase, mlist.internal_name())
            
            # We need to fasttrack this message through any handlers that touch
            # it.  E.g. especially CookHeaders.
            msgdata['_fasttrack'] = 1
            
            # Process through the pipeline
            result = IncomingRunner._dispose(self, mlist, msg, msgdata)
            
            # Log successful completion
            mailman_log('info', 'VirginRunner: Successfully processed virgin message %s (file: %s) for list %s',
                       msgid, filebase, mlist.internal_name())
            return result
        except Exception as e:
            # Enhanced error logging with more context
            mailman_log('error', 'Error processing virgin message %s for list %s: %s',
                   msgid, listname, str(e))
            mailman_log('error', 'Message details:')
            mailman_log('error', '  Message ID: %s', msgid)
            mailman_log('error', '  From: %s', msg.get('from', 'unknown'))
            mailman_log('error', '  To: %s', msg.get('to', 'unknown'))
            mailman_log('error', '  Subject: %s', msg.get('subject', '(no subject)'))
            mailman_log('error', '  Message type: %s', type(msg).__name__)
            mailman_log('error', '  Message data: %s', str(msgdata))
            mailman_log('error', 'Traceback:\n%s', traceback.format_exc())
            
            # Remove from processed messages on error
            self._unmark_message_processed(msgid)
            return 0

    def _get_pipeline(self, mlist, msg, msgdata):
        # It's okay to hardcode this, since it'll be the same for all
        # internally crafted messages.
        return ['CookHeaders', 'ToOutgoing']
