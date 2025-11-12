# Copyright (C) 2006-2018 by the Free Software Foundation, Inc.
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

"""Remove any "DomainKeys" (or similar) header lines.

The values contained in these header lines are intended to be used by the
recipient to detect forgery or tampering in transit, and the modifications
made by Mailman to the headers and body of the message will cause these keys
to appear invalid.  Removing them will at least avoid this misleading result,
and it will also give the MTA the opportunity to regenerate valid keys
originating at the Mailman server for the outgoing message.
"""

from __future__ import absolute_import, print_function, unicode_literals

from Mailman import mm_cfg


def process(mlist, msg, msgdata):
    if not (mm_cfg.REMOVE_DKIM_HEADERS or mlist.anonymous_list):
        # We want to remove these headers from posts to anonymous lists.
        # There can be interaction with the next test, but anonymous_list
        # and Munge From are not compatible anyway, so don't worry.
        return
    if (mm_cfg.REMOVE_DKIM_HEADERS == 1 and not
           # The following means 'Munge From' applies to this message.
           # So this whole stanza means if RDH is 1 and we're not Munging,
           # return and don't remove the headers.  See Defaults.py.
           (msgdata.get('from_is_list') == 1 or
            (mlist.from_is_list == 1 and msgdata.get('from_is_list') != 2)
           )
       ):
        return
    if (mm_cfg.REMOVE_DKIM_HEADERS == 3):
        # Save original headers before removing them
        for header in ('domainkey-signature', 'dkim-signature', 'authentication-results'):
            values = msg.get_all(header, [])
            if values:
                # Store original values in X-Mailman-Original-* headers
                for value in values:
                    msg.add_header('X-Mailman-Original-' + header.title().replace('-', ''), value)
                # Remove the original headers
                while header in msg:
                    del msg[header]
    else:
        # Just remove the headers without saving them
        for header in ('domainkey-signature', 'dkim-signature', 'authentication-results'):
            while header in msg:
                del msg[header]

