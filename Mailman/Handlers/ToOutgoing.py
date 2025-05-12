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

"""Re-queue the message to the outgoing queue.

This module is only for use by the IncomingRunner for delivering messages
posted to the list membership.  Anything else that needs to go out to some
recipient should just be placed in the out queue directly.
"""

from Mailman import mm_cfg
from Mailman.Queue.sbcache import get_switchboard
import traceback
from Mailman.Logging.Syslog import mailman_log


def process(mlist, msg, msgdata):
    """Process the message by moving it to the outgoing queue."""
    msgid = msg.get('message-id', 'n/a')
    
    # Log the start of processing
    mailman_log('info', 'ToOutgoing: Starting to process message %s for list %s',
               msgid, mlist.internal_name())
    
    # Log message details
    mailman_log('debug', 'ToOutgoing: Message details:')
    mailman_log('debug', '  Message ID: %s', msgid)
    mailman_log('debug', '  From: %s', msg.get('from', 'unknown'))
    mailman_log('debug', '  To: %s', msg.get('to', 'unknown'))
    mailman_log('debug', '  Subject: %s', msg.get('subject', '(no subject)'))
    mailman_log('debug', '  Message type: %s', type(msg).__name__)
    mailman_log('debug', '  Message data: %s', str(msgdata))
    
    # Get the outgoing queue
    try:
        outgoingq = get_switchboard(mm_cfg.OUTQUEUE_DIR)
        mailman_log('debug', 'ToOutgoing: Got outgoing queue for message %s', msgid)
    except Exception as e:
        mailman_log('error', 'ToOutgoing: Failed to get outgoing queue for message %s: %s', msgid, str(e))
        raise
    
    # Add the message to the outgoing queue
    try:
        outgoingq.enqueue(msg, msgdata, listname=mlist.internal_name())
        mailman_log('info', 'ToOutgoing: Successfully queued message %s for list %s',
                   msgid, mlist.internal_name())
    except Exception as e:
        mailman_log('error', 'ToOutgoing: Failed to enqueue message %s: %s', msgid, str(e))
        mailman_log('error', 'ToOutgoing: Traceback:\n%s', traceback.format_exc())
        raise
