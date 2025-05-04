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


def process(mlist, msg, msgdata):
    msgid = msg.get('message-id', 'n/a')
    mailman_log('info', 'ToOutgoing: Starting to process message %s for list %s', 
                msgid, mlist.internal_name())
    
    interval = mm_cfg.VERP_DELIVERY_INTERVAL
    # Should we VERP this message?  If personalization is enabled for this
    # list and VERP_PERSONALIZED_DELIVERIES is true, then yes we VERP it.
    # Also, if personalization is /not/ enabled, but VERP_DELIVERY_INTERVAL is
    # set (and we've hit this interval), then again, this message should be
    # VERPed. Otherwise, no.
    #
    # Note that the verp flag may already be set, e.g. by mailpasswds using
    # VERP_PASSWORD_REMINDERS.  Preserve any existing verp flag.
    if 'verp' in msgdata:
        mailman_log('debug', 'ToOutgoing: Using existing VERP flag for message %s', msgid)
    elif mlist.personalize:
        if mm_cfg.VERP_PERSONALIZED_DELIVERIES:
            msgdata['verp'] = 1
            mailman_log('debug', 'ToOutgoing: Setting VERP flag for personalized message %s', msgid)
    elif interval == 0:
        # Never VERP
        mailman_log('debug', 'ToOutgoing: VERP disabled for message %s', msgid)
    elif interval == 1:
        # VERP every time
        msgdata['verp'] = 1
        mailman_log('debug', 'ToOutgoing: Setting VERP flag for message %s (interval=1)', msgid)
    else:
        # VERP every `inteval' number of times
        msgdata['verp'] = not int(mlist.post_id) % interval
        mailman_log('debug', 'ToOutgoing: Setting VERP flag for message %s based on interval %d', 
                   msgid, interval)
    
    # And now drop the message in qfiles/out
    try:
        outq = get_switchboard(mm_cfg.OUTQUEUE_DIR)
        mailman_log('debug', 'ToOutgoing: Enqueueing message %s to outgoing queue', msgid)
        outq.enqueue(msg, msgdata, listname=mlist.internal_name())
        mailman_log('info', 'ToOutgoing: Successfully enqueued message %s to outgoing queue', msgid)
    except Exception as e:
        mailman_log('error', 'ToOutgoing: Failed to enqueue message %s: %s', msgid, str(e))
        mailman_log('error', 'ToOutgoing: Traceback:\n%s', traceback.format_exc())
        raise
