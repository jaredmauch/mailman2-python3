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

scre = re.compile('did not reach the following recipient')
ecre = re.compile('MSEXCH:')
a1cre = re.compile('SMTP=(?P<addr>[^;]+); on ')
a2cre = re.compile('(?P<addr>[^ ]+) on ')


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
        mo = a1cre.search(line)
        if not mo:
            mo = a2cre.search(line)
        if mo:
            addrs[mo.group('addr')] = 1
    return list(addrs.keys())
