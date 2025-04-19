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

"""Decorate a message by sticking the header and footer around it."""

import re
import email.mime.multipart

from email.mime.text import MIMEText

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
from Mailman.Message import Message
from Mailman.i18n import _
from Mailman.SafeDict import SafeDict
from Mailman.Logging.Syslog import syslog

try:
    import dns.resolver
    from dns.exception import DNSException
    dns_resolver = True
except ImportError:
    dns_resolver = False


def _encode_header(h, charset):
    """Encode a header value using the specified charset."""
    if isinstance(h, bytes):
        return h
    return h.encode(charset, 'replace')

def process(mlist, msg, msgdata):
    # Digests and Mailman-craft messages should not get additional headers
    if msgdata.get('isdigest') or msgdata.get('nodecorate'):
        return
    d = {}
    if msgdata.get('personalize'):
        # Calculate the extra personalization dictionary.  Note that the
        # length of the recips list better be exactly 1.
        recips = msgdata.get('recips')
        assert isinstance(recips, list) and len(recips) == 1
        member = recips[0].lower()
        d['user_address'] = member
        try:
            d['user_delivered_to'] = mlist.getMemberCPAddress(member)
            # BAW: Hmm, should we allow this?
            d['user_password'] = mlist.getMemberPassword(member)
            d['user_language'] = mlist.getMemberLanguage(member)
            username = mlist.getMemberName(member) or None
            try:
                username = username.encode(Utils.GetCharSet(d['user_language']))
            except (AttributeError, UnicodeError):
                username = member
            d['user_name'] = username
            d['user_optionsurl'] = mlist.GetOptionsURL(member)
        except Errors.NotAMemberError:
            pass
    # These strings are descriptive for the log file and shouldn't be i18n'd
    d.update(msgdata.get('decoration-data', {}))
    header = decorate(mlist, mlist.msg_header, 'non-digest header', d)
    footer = decorate(mlist, mlist.msg_footer, 'non-digest footer', d)
    # Escape hatch if both the footer and header are empty
    if not header and not footer:
        return
    # Get the list's charset
    lcset = Utils.GetCharSet(mlist.preferred_language)
    
    # Get the message's content type and charset
    msgtype = msg.get_content_type()
    mcset = msg.get_content_charset() or lcset
    
    # Try to keep the message plain by converting header/footer/oldpayload
    # into unicode and encode with mcset/lcset
    wrap = True
    if not msg.is_multipart() and msgtype == 'text/plain':
        try:
            # Get the header and footer
            header = mlist.getHeader()
            footer = mlist.getFooter()
            
            # Convert header to unicode if needed
            if isinstance(header, str):
                uheader = header
            else:
                uheader = header.decode(lcset, 'ignore')
                
            # Convert footer to unicode if needed
            if isinstance(footer, str):
                ufooter = footer
            else:
                ufooter = footer.decode(lcset, 'ignore')
                
            # Get and decode the message payload
            try:
                oldpayload = msg.get_payload(decode=True).decode(mcset)
            except (UnicodeError, LookupError):
                oldpayload = msg.get_payload(decode=True).decode('utf-8', 'replace')
                
            # Add appropriate separators
            frontsep = '\n' if header and not header.endswith('\n') else ''
            endsep = '\n' if footer and not oldpayload.endswith('\n') else ''
            
            # Combine the parts
            payload = uheader + frontsep + oldpayload + endsep + ufooter
            
            # Try to encode with list charset first
            try:
                payload = payload.encode(lcset)
                newcset = lcset
            except UnicodeError:
                if lcset != mcset:
                    # If that fails, try message charset
                    payload = payload.encode(mcset)
                    newcset = mcset
                else:
                    raise
                    
            # Update message headers and payload
            format = msg.get_param('format')
            delsp = msg.get_param('delsp')
            del msg['content-transfer-encoding']
            del msg['content-type']
            msg.set_payload(payload, newcset)
            if format:
                msg.set_param('Format', format)
            if delsp:
                msg.set_param('DelSp', delsp)
            wrap = False
            
        except (LookupError, UnicodeError):
            # If we can't handle it as plain text, wrap it
            pass
            
    if wrap:
        # Wrap the message in a multipart/alternative
        outer = email.mime.multipart.MIMEMultipart('alternative')
        for header, value in msg.items():
            outer[header] = value
        outer.attach(msg)
        msg = outer


def decorate(mlist, template, what, extradict=None):
    # `what' is just a descriptive phrase used in the log message
    
    # If template is only whitespace, ignore it.
    if len(re.sub(r'\s', '', template)) == 0:
        return ''

    # BAW: We've found too many situations where Python can be fooled into
    # interpolating too much revealing data into a format string.  For
    # example, a footer of "% silly %(real_name)s" would give a header
    # containing all list attributes.  While we've previously removed such
    # really bad ones like `password' and `passwords', it's much better to
    # provide a whitelist of known good attributes, then to try to remove a
    # blacklist of known bad ones.
    d = SafeDict({'real_name'     : mlist.real_name,
                  'list_name'     : mlist.internal_name(),
                  # For backwards compatibility
                  '_internal_name': mlist.internal_name(),
                  'host_name'     : mlist.host_name,
                  'web_page_url'  : mlist.web_page_url,
                  'description'   : mlist.description,
                  'info'          : mlist.info,
                  'cgiext'        : mm_cfg.CGIEXT,
                  })
    if extradict is not None:
        d.update(extradict)
    # Using $-strings?
    if getattr(mlist, 'use_dollar_strings', 0):
        template = Utils.to_percent(template)
    # Interpolate into the template
    try:
        text = re.sub(r'(?m)(?<!^--) +(?=\n)', '',
                      re.sub(r'\r\n', r'\n', template % d))
    except (ValueError, TypeError) as e:
        syslog('error', 'Exception while calculating %s:\n%s', what, e)
        text = template
    # Ensure text ends with new-line
    if not text.endswith('\n'):
        text += '\n'
    return text
