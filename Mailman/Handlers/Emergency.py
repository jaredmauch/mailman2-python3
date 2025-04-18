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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

"""Put an emergency hold on all messages otherwise approved.

No notices are sent to either the sender or the list owner for emergency
holds.  I think they'd be too obnoxious.
"""

from Mailman import Errors
from Mailman.i18n import _


def _encode_header(h, charset):
    """Encode a header value using the specified charset."""
    if isinstance(h, str):
        return h
    return h.encode(charset, 'replace')


class EmergencyHold(Errors.HoldMessage):
    reason = _('Emergency hold on all list traffic is in effect')
    rejection = _('Your message was deemed inappropriate by the moderator.')


def process(mlist, msg, msgdata):
    """Process a message for emergency handling."""
    # Get the message headers
    subject = _encode_header(msg.get('subject', ''), 'utf-8')
    from_ = _encode_header(msg.get('from', ''), 'utf-8')
    to = _encode_header(msg.get('to', ''), 'utf-8')
    cc = _encode_header(msg.get('cc', ''), 'utf-8')
    if mlist.emergency and not msgdata.get('adminapproved'):
        mlist.HoldMessage(msg, _(EmergencyHold.reason), msgdata)
        raise EmergencyHold
