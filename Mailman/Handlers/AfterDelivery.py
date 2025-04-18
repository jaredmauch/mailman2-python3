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

"""Perform some bookkeeping after a successful post.

This module must appear after the delivery module in the message pipeline.
"""

import time


def _encode_header(h, charset):
    """Encode a header value using the specified charset."""
    if isinstance(h, str):
        return h
    return h.encode(charset, 'replace')

def process(mlist, msg, msgdata):
    """Process a message after delivery."""
    # Get the message headers
    subject = _encode_header(msg.get('subject', ''), 'utf-8')
    from_ = _encode_header(msg.get('from', ''), 'utf-8')
    to = _encode_header(msg.get('to', ''), 'utf-8')
    cc = _encode_header(msg.get('cc', ''), 'utf-8')
    mlist.last_post_time = time.time()
    mlist.post_id += 1
