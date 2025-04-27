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

"""Add the message to the list's current digest and possibly send it."""

from __future__ import absolute_import, print_function, unicode_literals

# Messages are accumulated to a Unix mailbox compatible file containing all
# the messages destined for the digest.  This file must be parsable by the
# mailbox.UnixMailbox class (i.e. it must be ^From_ quoted).
#
# When the file reaches the size threshold, it is moved to the qfiles/digest
# directory and the DigestRunner will craft the MIME, rfc1153, and
# (eventually) URL-subject linked digests from the mbox.

import os
import re
import copy
import time
import traceback
from io import StringIO, BytesIO

from email.parser import Parser
from email.generator import Generator
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.message import MIMEMessage
from email.utils import getaddresses, formatdate
from email.header import decode_header, make_header, Header
from email.charset import Charset
import email.message

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import i18n
from Mailman import Errors
import Mailman.Message
from Mailman.Mailbox import Mailbox
from Mailman.MemberAdaptor import ENABLED
from Mailman.Handlers.Decorate import decorate
from Mailman.Queue.sbcache import get_switchboard
from Mailman.Mailbox import Mailbox
from Mailman.Handlers.Scrubber import process as scrubber
from Mailman.Logging.Syslog import syslog

_ = i18n._

UEMPTYSTRING = u''
EMPTYSTRING = ''

def decode_header_value(value, lcset):
    """Decode an email header value properly."""
    if not value:
        return ''
    try:
        # Handle encoded-word format
        decoded = []
        for part, charset in decode_header(value):
            if isinstance(part, bytes):
                try:
                    decoded.append(part.decode(charset or lcset, 'replace'))
                except (UnicodeError, LookupError):
                    decoded.append(part.decode('utf-8', 'replace'))
            else:
                decoded.append(part)
        return ''.join(decoded)
    except Exception:
        return str(value)

def to_cset_out(text, lcset):
    """Convert text to output charset.
    
    Handles both str and bytes input, ensuring proper encoding for output.
    Returns a properly encoded string, not bytes.
    """
    if text is None:
        return ''
        
    ocset = Charset(lcset).get_output_charset() or lcset
    
    if isinstance(text, str):
        try:
            return text
        except (UnicodeError, LookupError):
            return text.encode('utf-8', 'replace').decode('utf-8')
    elif isinstance(text, bytes):
        try:
            return text.decode(lcset, 'replace')
        except (UnicodeError, LookupError):
            try:
                return text.decode('utf-8', 'replace')
            except (UnicodeError, LookupError):
                return str(text)
    else:
        return str(text)

def process_message_body(msg, lcset):
    """Process a message body, handling MIME parts and encoding properly."""
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            if part.get_content_maintype() == 'multipart':
                continue
            try:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = part.get_content_charset(lcset)
                    try:
                        text = payload.decode(charset or lcset, 'replace')
                    except (UnicodeError, LookupError):
                        text = payload.decode('utf-8', 'replace')
                else:
                    text = str(payload)
                parts.append(text)
            except Exception as e:
                parts.append('[Part could not be decoded]')
        return '\n\n'.join(parts)
    else:
        try:
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                charset = msg.get_content_charset(lcset)
                try:
                    return payload.decode(charset or lcset, 'replace')
                except (UnicodeError, LookupError):
                    return payload.decode('utf-8', 'replace')
            return str(payload)
        except Exception:
            return '[Message body could not be decoded]'

def process(mlist, msg, msgdata):
    """Process a message for digest delivery.
    
    This function handles adding messages to the digest and sending the digest
    when appropriate. All file operations use proper encoding handling.
    """
    # Short circuit non-digestable lists
    if not mlist.digestable or msgdata.get('isdigest'):
        return
        
    mboxfile = os.path.join(mlist.fullpath(), 'digest.mbox')
    omask = os.umask(0o007)
    
    try:
        # Open file in binary mode for proper handling of line endings
        with open(mboxfile, 'ab+') as mboxfp:
            # Pass the file path to Mailbox, not the file object
            mbox = Mailbox(mboxfile)
            mbox.AppendMessage(msg)
            
            # Calculate size and check threshold
            mboxfp.flush()
            size = os.path.getsize(mboxfile)
            if (mlist.digest_size_threshhold > 0 and
                size / 1024.0 >= mlist.digest_size_threshhold):
                try:
                    send_digests(mlist, mboxfile)  # Pass path instead of file object
                except Exception as e:
                    syslog('error', 'Error sending digest: %s', str(e))
                    syslog('error', 'Traceback: %s', traceback.format_exc())
    finally:
        os.umask(omask)

def send_digests(mlist, mboxpath):
    """Send digests for the mailing list."""
    # Set up the digest state
    volume = mlist.volume
    issue = mlist.next_digest_number
    digestid = _('%(realname)s Digest, Vol %(volume)d, Issue %(issue)d')
    
    # Get the list's preferred language and charset
    lang = mlist.preferred_language
    lcset = Utils.GetCharSet(lang)
    lcset_out = Charset(lcset).output_charset or lcset
    
    # Create the digest messages
    mimemsg = email.message.Message()
    mimemsg['Content-Type'] = 'multipart/mixed'
    mimemsg['MIME-Version'] = '1.0'
    mimemsg['From'] = mlist.GetRequestEmail()
    mimemsg['Subject'] = Header(digestid, lcset, header_name='Subject')
    mimemsg['To'] = mlist.GetListEmail()
    mimemsg['Reply-To'] = mlist.GetListEmail()
    mimemsg['Date'] = formatdate(localtime=1)
    mimemsg['Message-ID'] = Utils.unique_message_id(mlist)
    
    # Set up the RFC 1153 digest
    plainmsg = StringIO()  # Use StringIO for text output
    rfc1153msg = email.message.Message()
    rfc1153msg['From'] = mlist.GetRequestEmail()
    rfc1153msg['Subject'] = Header(digestid, lcset, header_name='Subject')
    rfc1153msg['To'] = mlist.GetListEmail()
    rfc1153msg['Reply-To'] = mlist.GetListEmail()
    rfc1153msg['Date'] = formatdate(localtime=1)
    rfc1153msg['Message-ID'] = Utils.unique_message_id(mlist)
    
    # Create the digest content
    separator70 = '-' * 70
    separator30 = '-' * 30
    
    # Add masthead
    mastheadtxt = Utils.maketext(
        'masthead.txt',
        {'real_name': mlist.real_name,
         'got_list_email': mlist.GetListEmail(),
         'got_listinfo_url': mlist.GetScriptURL('listinfo', absolute=1),
         'got_request_email': mlist.GetRequestEmail(),
         'got_owner_email': mlist.GetOwnerEmail(),
        },
        lang=lang,
        mlist=mlist)
        
    # Add masthead to both digest formats
    mimemsg.attach(MIMEText(mastheadtxt, _charset=lcset))
    plainmsg.write(to_cset_out(mastheadtxt, lcset_out))
    plainmsg.write('\n')
    
    # Process the mbox
    mbox = Mailbox(mboxpath)  # Use path instead of file object
    
    # Add a table of contents for RFC 1153 digests
    plainmsg.write(to_cset_out(separator70, lcset_out))
    plainmsg.write('\n')
    plainmsg.write(to_cset_out(_('Today\'s Topics:\n'), lcset_out))
    
    # Process each message
    msg_num = 1
    for msg in mbox:
        if msg is None:
            continue
            
        try:
            subject = decode_header_value(msg.get('subject', _('(no subject)')), lcset)
            subject = Utils.oneline(subject, lcset)
            
            # Add to table of contents
            plainmsg.write('%2d. %s\n' % (msg_num, to_cset_out(subject, lcset_out)))
            
            # Add the message to both digest formats
            mimemsg.attach(MIMEMessage(msg))
            
            # Add message header
            plainmsg.write('\n')
            plainmsg.write(to_cset_out(separator30, lcset_out))
            plainmsg.write('\n')
            plainmsg.write(to_cset_out(_('Message %d\n' % msg_num), lcset_out))
            plainmsg.write(to_cset_out(separator30, lcset_out))
            plainmsg.write('\n')
            
            # Add message metadata
            for header in ('date', 'from', 'subject'):
                value = decode_header_value(msg.get(header, ''), lcset)
                plainmsg.write('%s: %s\n' % (header.capitalize(), to_cset_out(value, lcset_out)))
            plainmsg.write('\n')
            
            # Add message body
            try:
                body = process_message_body(msg, lcset)
                plainmsg.write(to_cset_out(body, lcset_out))
                plainmsg.write('\n')
            except Exception as e:
                plainmsg.write(to_cset_out(_('[Message body could not be decoded]\n'), lcset_out))
                syslog('error', 'Message %d digest payload error: %s', msg_num, str(e))
            
            msg_num += 1
            
        except Exception as e:
            syslog('error', 'Digest message %d processing error: %s', msg_num, str(e))
            syslog('error', 'Traceback: %s', traceback.format_exc())
            continue
    
    # Finish up the RFC 1153 digest
    plainmsg.write('\n')
    plainmsg.write(to_cset_out(separator70, lcset_out))
    plainmsg.write('\n')
    plainmsg.write(to_cset_out(_('End of Digest\n'), lcset_out))
    
    # Set the RFC 1153 message body
    rfc1153msg.set_payload(plainmsg.getvalue(), charset=lcset)
    plainmsg.close()
    
    # Send both digests
    send_digest_final(mlist, mimemsg, rfc1153msg, volume, issue)
    
    # Clean up
    mlist.next_digest_number += 1
    mlist.Save()
    
    # Remove the mbox file
    try:
        os.unlink(os.path.join(mlist.fullpath(), 'digest.mbox'))
    except OSError as e:
        syslog('error', 'Failed to remove digest.mbox: %s', str(e))

def send_digest_final(mlist, mimemsg, rfc1153msg, volume, issue):
    """Send the actual digest messages.
    
    This function handles the final preparation and sending of both digest formats.
    """
    # Send to MIME digest members
    mime_members = mlist.getMemberCPAddresses(digest=True, mime=True)
    if mime_members:
        outq = get_switchboard(mm_cfg.OUTQUEUE_DIR)
        outq.enqueue(mimemsg,
                    recips=mime_members,
                    listname=mlist.internal_name(),
                    fromnode='digest')
    
    # Send to RFC 1153 digest members
    rfc1153_members = mlist.getMemberCPAddresses(digest=True, mime=False)
    if rfc1153_members:
        outq = get_switchboard(mm_cfg.OUTQUEUE_DIR)
        outq.enqueue(rfc1153msg,
                    recips=rfc1153_members,
                    listname=mlist.internal_name(),
                    fromnode='digest')
