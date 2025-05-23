# Copyright (C) 2001-2018 by the Free Software Foundation, Inc.
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

"""LLNL's custom Sendmail bounce message."""

import re
import email
from email.iterators import body_line_iterator

acre = re.compile(r',\s*(?P<addr>\S+@[^,]+),', re.IGNORECASE)


def process(msg):
    for line in body_line_iterator(msg):
        mo = acre.search(line)
        if mo:
            return [mo.group('addr')]
    return []
