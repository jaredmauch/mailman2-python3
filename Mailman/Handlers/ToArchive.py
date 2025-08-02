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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301,
# USA.

"""Add the message to the archives."""

import time
from io import StringIO

from Mailman import mm_cfg
from Mailman.Queue.sbcache import get_switchboard



def process(mlist, msg, msgdata):
    # DEBUG: Log archiver processing start
    from Mailman import syslog
    syslog('debug', 'ToArchive: Starting archive processing for list %s', mlist.internal_name())
    
    # short circuits
    if msgdata.get('isdigest'):
        syslog('debug', 'ToArchive: Skipping digest message for list %s', mlist.internal_name())
        return
    if not mlist.archive:
        syslog('debug', 'ToArchive: Archiving disabled for list %s', mlist.internal_name())
        return
    
    # Common practice seems to favor "X-No-Archive: yes".  No other value for
    # this header seems to make sense, so we'll just test for it's presence.
    # I'm keeping "X-Archive: no" for backwards compatibility.
    if 'x-no-archive' in msg:
        syslog('debug', 'ToArchive: Skipping message with X-No-Archive header for list %s', mlist.internal_name())
        return
    if msg.get('x-archive', '').lower() == 'no':
        syslog('debug', 'ToArchive: Skipping message with X-Archive: no for list %s', mlist.internal_name())
        return
    
    # Send the message to the archiver queue
    archq = get_switchboard(mm_cfg.ARCHQUEUE_DIR)
    syslog('debug', 'ToArchive: Enqueuing message to archive queue for list %s', mlist.internal_name())
    # Send the message to the queue
    archq.enqueue(msg, msgdata)
    syslog('debug', 'ToArchive: Successfully enqueued message to archive queue for list %s', mlist.internal_name())
