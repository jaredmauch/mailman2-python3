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

"""Yahoo! has its own weird format for bounces."""

import re
import email
from email.Utils import parseaddr

tcre = (re.compile(r'message\s+from\s+yahoo\.\S+', re.IGNORECASE),
        re.compile(r'Sorry, we were unable to deliver your message to '
                   r'the following address(\(es\))?\.',
                   re.IGNORECASE),
        )
acre = re.compile(r'<(?P<addr>[^>]*)>:')
ecre = (re.compile(r'--- Original message follows'),
        re.compile(r'--- Below this line is a copy of the message'),
        )



def process(msg):
    # Yahoo! bounces seem to have a known subject value and something called
    # an x-uidl: header, the value of which seems unimportant.
    sender = parseaddr(msg.get('from', '').lower())[1] or ''
    if not sender.startswith('mailer-daemon@yahoo'):
        return None
    addrs = []
    # simple state machine
    #     0 == nothing seen
    #     1 == tag line seen
    #     2 == end line seen
    state = 0
    for line in email.Iterators.body_line_iterator(msg):
        line = line.strip()
        if state == 0:
            for cre in tcre:
                if cre.match(line):
                    state = 1
                    break
        elif state == 1:
            mo = acre.match(line)
            if mo:
                addrs.append(mo.group('addr'))
                continue
            for cre in ecre:
                mo = cre.match(line)
                if mo:
                    # we're at the end of the error response
                    state = 2
                    break
        elif state == 2:
            break
    return addrs
