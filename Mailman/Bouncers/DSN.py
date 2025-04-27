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

"""Parse RFC 3464 (i.e. DSN) bounce formats.

RFC 3464 obsoletes 1894 which was the old DSN standard.  This module has not
been audited for differences between the two.
"""

from email.iterators import typed_subpart_iterator
from email.utils import parseaddr
from io import StringIO
import re
import ipaddress

from Mailman.Bouncers.BouncerAPI import Stop

def process(msg):
    # Iterate over each message/delivery-status subpart
    addrs = []
    for part in typed_subpart_iterator(msg, 'message', 'delivery-status'):
        if not part.is_multipart():
            # Huh?
            continue
        # Each message/delivery-status contains a list of Message objects
        # which are the header blocks.  Iterate over those too.
        for msgblock in part.get_payload():
            # We try to dig out the Original-Recipient (which is optional) and
            # Final-Recipient (which is mandatory, but may not exactly match
            # an address on our list).  Some MTA's also use X-Actual-Recipient
            # as a synonym for Original-Recipient, but some apparently use
            # that for other purposes :(
            #
            # Also grok out Action so we can do something with that too.
            action = msgblock.get('action', '').lower()
            # Some MTAs have been observed that put comments on the action.
            if action.startswith('delayed'):
                return Stop
            # opensmtpd uses non-compliant Action: error.
            if not (action.startswith('fail') or action.startswith('error')):
                # Some non-permanent failure, so ignore this block
                continue
            params = []
            foundp = False
            for header in ('original-recipient', 'final-recipient'):
                for k, v in msgblock.get_params([], header):
                    if k.lower() == 'rfc822':
                        foundp = True
                    else:
                        params.append(k)
                if foundp:
                    # Note that params should already be unquoted.
                    addrs.extend(params)
                    break
                else:
                    # MAS: This is a kludge, but SMTP-GATEWAY01.intra.home.dk
                    # has a final-recipient with an angle-addr and no
                    # address-type parameter at all. Non-compliant, but ...
                    for param in params:
                        if param.startswith('<') and param.endswith('>'):
                            addrs.append(param[1:-1])

    # Extract IP address from Received headers
    ip = None
    for header in msg.get_all('Received', []):
        if isinstance(header, bytes):
            header = header.decode('us-ascii', errors='replace')
        # Look for IP addresses in Received headers
        # Support both IPv4 and IPv6 formats
        ip_match = re.search(r'\[([0-9a-fA-F:.]+)\]', header)
        if ip_match:
            ip = ip_match.group(1)
            break
            
    if ip:
        try:
            if have_ipaddress:
                ip_obj = ipaddress.ip_address(ip)
                if isinstance(ip_obj, ipaddress.IPv4Address):
                    # For IPv4, drop last octet
                    parts = str(ip_obj).split('.')
                    ip = '.'.join(parts[:-1])
                else:
                    # For IPv6, drop last 16 bits
                    expanded = ip_obj.exploded.replace(':', '')
                    ip = expanded[:-4]
            else:
                # Fallback for systems without ipaddress module
                if ':' in ip:
                    # IPv6 address
                    parts = ip.split(':')
                    if len(parts) <= 8:
                        # Pad with zeros and drop last 16 bits
                        expanded = ''.join(part.zfill(4) for part in parts)
                        ip = expanded[:-4]
                else:
                    # IPv4 address
                    parts = ip.split('.')
                    if len(parts) == 4:
                        ip = '.'.join(parts[:-1])
        except (ValueError, IndexError):
            ip = None
            
    # Uniquify
    rtnaddrs = {}
    for a in addrs:
        if a is not None:
            realname, a = parseaddr(a)
            rtnaddrs[a] = True
    return list(rtnaddrs.keys())
