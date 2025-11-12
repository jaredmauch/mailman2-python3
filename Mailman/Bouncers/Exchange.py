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

"""Recognizes (some) Microsoft Exchange formats."""

import re
import email
from email.iterators import body_line_iterator
from email.header import decode_header

from Mailman import mm_cfg
from Mailman import Utils
from Mailman.Logging.Syslog import syslog
from Mailman.Handlers.CookHeaders import change_header

# Patterns for different Exchange/Office 365 bounce formats
scre = re.compile('did not reach the following recipient|Your message to .* couldn\'t be delivered')
ecre = re.compile('MSEXCH:|Action Required')
a1cre = re.compile('SMTP=(?P<addr>[^;]+); on ')
a2cre = re.compile('(?P<addr>[^ ]+) on ')
a3cre = re.compile('Your message to (?P<addr>[^ ]+) couldn\'t be delivered')
a4cre = re.compile('(?P<addr>[^ ]+) wasn\'t found at ')


def process(msg):
    addrs = {}
    it = body_line_iterator(msg)
    # Find the start line
    for line in it:
        if scre.search(line):
            break
    else:
        return []
    # Search each line until we hit the end line
    for line in it:
        if ecre.search(line):
            break
        # Try all patterns
        for pattern in [a1cre, a2cre, a3cre, a4cre]:
            mo = pattern.search(line)
            if mo:
                addr = mo.group('addr')
                # Clean up the address if needed
                if '@' not in addr and 'at' in line:
                    # Handle cases where domain is on next line
                    next_line = next(it, '')
                    if 'at' in next_line:
                        domain = next_line.split('at')[-1].strip()
                        addr = f"{addr}@{domain}"
                addrs[addr] = 1
                break
    return list(addrs.keys())
