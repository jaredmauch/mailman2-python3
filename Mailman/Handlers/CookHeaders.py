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

"""Cook a message's Subject header.
Also do other manipulations of From:, Reply-To: and Cc: depending on
list configuration.
"""

from __future__ import absolute_import, print_function, unicode_literals

import re
from email.charset import Charset
from email.header import Header, decode_header, make_header
from email.utils import parseaddr, formataddr, getaddresses
from email.errors import HeaderParseError
from email.iterators import body_line_iterator

from Mailman import i18n
from Mailman import mm_cfg
from Mailman import Utils
from Mailman.i18n import _
from Mailman.Logging.Syslog import mailman_log

CONTINUATION = ',\n '
COMMASPACE = ', '
MAXLINELEN = 78

def _isunicode(s):
    return isinstance(s, str)

nonascii = re.compile(r'[^\s!-~]')

def uheader(mlist, s, header_name=None, continuation_ws=' ', maxlinelen=None):
    """Create a Header object from a string with proper charset handling.
    
    This function ensures proper handling of both str and bytes input,
    and uses the list's preferred charset for encoding.
    """
    # Get the charset to encode the string in
    charset = Utils.GetCharSet(mlist.preferred_language)
    
    # Convert input to str if it's bytes
    if isinstance(s, bytes):
        try:
            s = s.decode('ascii')
        except UnicodeDecodeError:
            try:
                s = s.decode(charset)
            except UnicodeDecodeError:
                s = s.decode('utf-8', 'replace')
    
    # If there are non-ASCII characters, use the list's charset
    if nonascii.search(s):
        if charset == 'us-ascii':
            charset = 'utf-8'
    else:
        charset = 'us-ascii'
        
    try:
        return Header(s, charset, maxlinelen, header_name, continuation_ws)
    except UnicodeError:
        mailman_log('error', 'list: %s: cannot encode "%s" as %s',
                   mlist.internal_name(), s, charset)
        # Fall back to ASCII with replacement characters
        return Header(s.encode('ascii', 'replace').decode('ascii'),
                     'us-ascii', maxlinelen, header_name, continuation_ws)

def change_header(name, value, mlist, msg, msgdata, delete=True, repl=True):
    """Change or add a message header.
    
    This function handles header changes in a Python 3 compatible way,
    properly dealing with encodings and header values.
    """
    if ((msgdata.get('from_is_list') == 2 or
        (msgdata.get('from_is_list') == 0 and mlist.from_is_list == 2)) and 
        not msgdata.get('_fasttrack')
       ) or name.lower() in ('from', 'reply-to', 'cc'):
        # Store the header in msgdata for later use
        msgdata.setdefault('add_header', {})[name] = value
        # Also add the header to the message if it's not From, Reply-To, or Cc
        if name.lower() not in ('from', 'reply-to', 'cc'):
            if delete:
                del msg[name]
            if isinstance(value, Header):
                msg[name] = value
            else:
                try:
                    msg[name] = str(value)
                except UnicodeEncodeError:
                    msg[name] = Header(value,
                                     Utils.GetCharSet(mlist.preferred_language))
    elif repl or name not in msg:
        if delete:
            del msg[name]
        if isinstance(value, Header):
            msg[name] = value
        else:
            try:
                msg[name] = str(value)
            except UnicodeEncodeError:
                msg[name] = Header(value,
                                 Utils.GetCharSet(mlist.preferred_language))

def process(mlist, msg, msgdata):
    """Process the message by cooking its headers."""
    msgid = msg.get('message-id', 'n/a')
    
    # Log start of processing with enhanced details
    mailman_log('debug', 'CookHeaders: Starting to process message %s for list %s',
               msgid, mlist.internal_name())
    mailman_log('debug', 'CookHeaders: Message details:')
    mailman_log('debug', '  Message ID: %s', msgid)
    mailman_log('debug', '  From: %s', msg.get('from', 'unknown'))
    mailman_log('debug', '  To: %s', msg.get('to', 'unknown'))
    mailman_log('debug', '  Subject: %s', msg.get('subject', '(no subject)'))
    mailman_log('debug', '  Message type: %s', type(msg).__name__)
    mailman_log('debug', '  Message data: %s', str(msgdata))
    mailman_log('debug', '  Pipeline: %s', msgdata.get('pipeline', 'No pipeline'))
    
    # Set the "X-Ack: no" header if noack flag is set
    if msgdata.get('noack'):
        mailman_log('debug', 'CookHeaders: Setting X-Ack: no for message %s', msgid)
        change_header('X-Ack', 'no', mlist, msg, msgdata)
    
    # Save original sender for later
    if 'original_sender' not in msgdata:
        msgdata['original_sender'] = msg.get_sender()
        mailman_log('debug', 'CookHeaders: Saved original sender %s for message %s',
                   msgdata['original_sender'], msgid)
    
    # Handle subject prefix and other headers
    fasttrack = msgdata.get('_fasttrack')
    if not msgdata.get('isdigest') and not fasttrack:
        try:
            mailman_log('debug', 'CookHeaders: Adding subject prefix for message %s', msgid)
            prefix_subject(mlist, msg, msgdata)
        except (UnicodeError, ValueError) as e:
            mailman_log('error', 'CookHeaders: Error adding subject prefix for message %s: %s',
                       msgid, str(e))
    
    # Mark message as processed
    mailman_log('debug', 'CookHeaders: Adding X-BeenThere header for message %s', msgid)
    change_header('X-BeenThere', mlist.GetListEmail(),
                 mlist, msg, msgdata, delete=False)
    
    # Add standard headers
    mailman_log('debug', 'CookHeaders: Adding standard headers for message %s', msgid)
    change_header('X-Mailman-Version', mm_cfg.VERSION,
                 mlist, msg, msgdata, repl=False)
    change_header('Precedence', 'list',
                 mlist, msg, msgdata, repl=False)
    
    # Handle From: header munging if needed
    if (msgdata.get('from_is_list') or mlist.from_is_list) and not fasttrack:
        mailman_log('debug', 'CookHeaders: Munging From header for message %s', msgid)
        munge_from_header(mlist, msg, msgdata)
    
    mailman_log('debug', 'CookHeaders: Finished processing message %s', msgid)

def munge_from_header(mlist, msg, msgdata):
    """Munge the From: header for the list.
    
    This is separated into its own function to make the logic clearer
    and handle all the encoding issues in one place.
    """
    # Get the original From: addresses
    faddrs = getaddresses(msg.get_all('from', []))
    faddrs = [x for x in faddrs if x[1].find('@') > 0]
    
    if len(faddrs) == 1:
        realname, email = faddrs[0]
    else:
        realname = ''
        email = msgdata['original_sender']
    
    # Get or create realname
    if not realname:
        if mlist.isMember(email):
            realname = mlist.getMemberName(email) or email
        else:
            realname = email
    
    # Remove domain from realname if it looks like an email
    realname = re.sub(r'@([^ .]+\.)+[^ .]+$', '---', realname)
    
    # Convert realname to unicode
    charset = Utils.GetCharSet(mlist.preferred_language)
    if isinstance(realname, bytes):
        try:
            realname = realname.decode(charset)
        except UnicodeDecodeError:
            realname = realname.decode('utf-8', 'replace')
    
    # Format the new From: header
    via = _('%(realname)s via %(listname)s')
    listname = mlist.real_name
    if isinstance(listname, bytes):
        listname = listname.decode(charset, 'replace')
    
    display_name = via % {'realname': realname, 'listname': listname}
    
    # Create the new From: header value
    new_from = formataddr((display_name, mlist.GetListEmail()))
    change_header('From', new_from, mlist, msg, msgdata)

def prefix_subject(mlist, msg, msgdata):
    """Add the list's subject prefix to the message's Subject: header."""
    # Get the subject and charset
    subject = msg.get('subject', '')
    if not subject:
        return
    
    # Get the list's charset
    cset = mlist.preferred_language
    
    # Get the prefix
    prefix = mlist.subject_prefix.strip()
    if not prefix:
        return
        
    # Handle the subject encoding
    try:
        # If subject is already a string, use it directly
        if isinstance(subject, str):
            subject_str = subject
        # If subject is a Header object, convert it to string
        elif isinstance(subject, Header):
            subject_str = str(subject)
        else:
            # Try to decode the subject
            try:
                subject_str = str(subject, cset)
            except (UnicodeError, LookupError):
                # If that fails, try utf-8
                subject_str = str(subject, 'utf-8', 'replace')
    except Exception as e:
        mailman_log('error', 'Error decoding subject: %s', str(e))
        return
        
    # Add the prefix if it's not already there
    if not subject_str.startswith(prefix):
        msg['Subject'] = prefix + ' ' + subject_str

def ch_oneline(headerstr):
    # Decode header string in one line and convert into single charset
    # copied and modified from ToDigest.py and Utils.py
    # return (string, cset) tuple as check for failure
    try:
        # Ensure headerstr is a string, not bytes
        if isinstance(headerstr, bytes):
            try:
                headerstr = headerstr.decode('utf-8')
            except UnicodeDecodeError:
                headerstr = headerstr.decode('us-ascii', 'replace')
        
        d = decode_header(headerstr)
        # at this point, we should rstrip() every string because some
        # MUA deliberately add trailing spaces when composing return
        # message.
        d = [(s.rstrip(), c) for (s,c) in d]
        cset = 'us-ascii'
        for x in d:
            # search for no-None charset
            if x[1]:
                cset = x[1]
                break
        h = make_header(d)
        ustr = str(h)
        oneline = u''.join(ustr.splitlines())
        return oneline.encode(cset, 'replace'), cset
    except (LookupError, UnicodeError, ValueError, HeaderParseError):
        # possibly charset problem. return with undecoded string in one line.
        if isinstance(headerstr, bytes):
            try:
                headerstr = headerstr.decode('utf-8')
            except UnicodeDecodeError:
                headerstr = headerstr.decode('us-ascii', 'replace')
        return ''.join(headerstr.splitlines()), 'us-ascii'
