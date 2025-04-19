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
from typing import Any, Dict, List, Optional, Tuple, Union, IO
import email
from email.message import Message
import logging
import mailbox
import shutil
from io import StringIO

from email.parser import Parser
from email.generator import Generator
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.message import MIMEMessage
from email.utils import getaddresses, formatdate
from email.header import decode_header, make_header, Header
from email.charset import Charset

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Message as MailmanMessage
from Mailman import i18n
from Mailman import Errors
from Mailman.Mailbox import Mailbox
from Mailman.MemberAdaptor import ENABLED
from Mailman.Handlers.Decorate import decorate
from Mailman.Queue.sbcache import get_switchboard
from Mailman.Handlers.Scrubber import process as scrubber
from Mailman.Logging.Syslog import syslog

_ = i18n._

UEMPTYSTRING = ''
EMPTYSTRING = ''

try:
    import dns.resolver
    from dns.exception import DNSException
    dns_resolver = True
except ImportError:
    dns_resolver = False


def to_cset_out(text, lcset):
    # Convert text from unicode or lcset to output cset.
    ocset = Charset(lcset).get_output_charset() or lcset
    if isinstance(text, unicode):
        return text.encode(ocset, 'replace')
    else:
        return text.decode(lcset, 'replace').encode(ocset, 'replace')


def _encode_header(h, charset):
    """Encode a header value using the specified charset."""
    if isinstance(h, str):
        return h
    return h.encode(charset, 'replace')


def process(mlist: Any, msg: Message, msgdata: Dict[str, Any]) -> None:
    """Process a message for digest delivery."""
    try:
        if not mlist.digestable:
            return

        # Create the digest directory if it doesn't exist
        digest_dir = os.path.join(mlist.data_path, 'digest')
        try:
            os.makedirs(digest_dir, exist_ok=True)
        except OSError as e:
            syslog('error', 'Failed to create digest directory: %s', e)
            return

        # Process the message
        mbox_path = os.path.join(digest_dir, 'digest.mbox')
        try:
            with mailbox.mbox(mbox_path) as mbox:
                mbox.add(msg)
        except Exception as e:
            syslog('error', 'Failed to add message to digest: %s', e)
            return

        # Check if it's time to send the digest
        if should_send_digest(mlist):
            send_digests(mlist)

    except Exception as e:
        syslog('error', 'Error processing digest message: %s', e)
        traceback.print_exc()


def should_send_digest(mlist: Any) -> bool:
    """Check if it's time to send the digest based on list settings."""
    now = time.time()
    last_digest = mlist.last_digest_volume
    frequency = mlist.digest_volume_frequency
    return (now - last_digest) >= frequency


def send_digests(mlist: Any) -> None:
    """Send the current digest to all subscribers."""
    try:
        # Get the digest messages
        digest_dir = os.path.join(mlist.data_path, 'digest')
        mbox_path = os.path.join(digest_dir, 'digest.mbox')
        
        if not os.path.exists(mbox_path):
            return

        # Create the digest message
        digest_msg = create_digest_message(mlist, mbox_path)
        if not digest_msg:
            return

        # Send to all digest subscribers
        for member in mlist.getDigestMemberKeys():
            try:
                mlist.send_digest(digest_msg, member)
            except Exception as e:
                syslog('error', 'Failed to send digest to %s: %s', member, e)

        # Clean up
        os.remove(mbox_path)
        mlist.last_digest_volume = time.time()

    except Exception as e:
        syslog('error', 'Error sending digests: %s', e)
        traceback.print_exc()


def create_digest_message(mlist: Any, mbox_path: str) -> Optional[Message]:
    """Create a digest message from the mailbox contents."""
    try:
        with mailbox.mbox(mbox_path) as mbox:
            if len(mbox) == 0:
                return None

            # Create the digest message
            digest_msg = MIMEMultipart('mixed')
            digest_msg['Subject'] = f'{mlist.real_name} Digest, Vol {mlist.volume}, Issue {mlist.next_digest_number}'
            digest_msg['From'] = mlist.GetRequestEmail()
            digest_msg['To'] = mlist.GetListEmail()
            digest_msg['Date'] = formatdate(localtime=True)
            digest_msg['Message-ID'] = Utils.unique_message_id(mlist)

            # Add the table of contents
            toc = create_table_of_contents(mbox)
            digest_msg.attach(MIMEText(toc, 'plain'))

            # Add each message
            for msg in mbox:
                digest_msg.attach(MIMEMessage(msg))

            return digest_msg

    except Exception as e:
        syslog('error', 'Error creating digest message: %s', e)
        return None


def create_table_of_contents(mbox: mailbox.mbox) -> str:
    """Create a table of contents from the mailbox messages."""
    toc = StringIO()
    print(_("Today's Topics:\n"), file=toc)
    
    for msg in mbox:
        subject = msg.get('Subject', '')
        if subject:
            print(f"- {subject}", file=toc)
    
    return toc.getvalue()


def main():
    doc = Document()
    try:
        mlist = MailList.MailList(listname, lock=0)
    except Errors.MMListError as e:
        # Avoid cross-site scripting attacks
        safelistname = Utils.websafe(listname)
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('No such list <em>%(safelistname)s</em>')))
        # Send this with a 404 status
        print('Status: 404 Not Found')
        print(doc.Format())
        return

    # Must be authenticated to get any farther
    cgidata = cgi.FieldStorage()
    try:
        cgidata.getfirst('adminpw', '')
    except TypeError:
        # Someone crafted a POST with a bad Content-Type:.
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script.')))
        # Send this with a 400 status.
        print('Status: 400 Bad Request')
        print(doc.Format())
        return

    # CSRF check
    safe_params = ['VARHELP', 'adminpw', 'admlogin']
    params = list(cgidata.keys())
    if set(params) - set(safe_params):
        csrf_checked = csrf_check(mlist, cgidata.getfirst('csrf_token'),
                                  'admin')
    else:
        csrf_checked = True
    # if password is present, void cookie to force password authentication.
    if cgidata.getfirst('adminpw'):
        os.environ['HTTP_COOKIE'] = ''
        csrf_checked = True

    # Editing the html for a list is limited to the list admin and site admin.
    if not mlist.WebAuthenticate((mm_cfg.AuthListAdmin,
                                  mm_cfg.AuthSiteAdmin),
                                 cgidata.getfirst('adminpw', '')):
        if 'admlogin' in cgidata:
            # This is a re-authorization attempt
            msg = Bold(FontSize('+1', _('Authorization failed.'))).Format()
            remote = os.environ.get('HTTP_FORWARDED_FOR',
                     os.environ.get('HTTP_X_FORWARDED_FOR',
                     os.environ.get('REMOTE_ADDR',
                                    'unidentified origin')))
            syslog('security',
                   'Authorization failed (todigest): list=%s: remote=%s',
                   listname, remote)
        else:
            msg = ''
        Auth.loginpage(mlist, 'admin', msg=msg)
        return

    # Create the list directory with proper permissions
    oldmask = os.umask(0o007)
    try:
        os.makedirs(mlist.fullpath(), mode=0o2775)
    except (IOError, OSError) as e:
        if e.errno != errno.EEXIST:
            raise
    finally:
        os.umask(oldmask)


class ToDigest:
    """Handler for digesting messages."""
    
    def __init__(self, mlist: Any) -> None:
        """Initialize the handler.
        
        Args:
            mlist: The mailing list object
        """
        self.mlist = mlist
        self.logger = logging.getLogger('mailman.digest')
        
    def process(self, msg: Message, msgdata: Dict[str, Any]) -> None:
        """Process a message for digesting.
        
        Args:
            msg: The email message
            msgdata: Additional message metadata
        """
        # Check if digesting is enabled
        if not self.mlist.digest:
            return
            
        # Get the digest directory
        digest_dir = os.path.join(mm_cfg.DIGEST_DIR, self.mlist.internal_name())
        
        # Create the digest directory if it doesn't exist
        if not os.path.exists(digest_dir):
            try:
                os.makedirs(digest_dir)
            except OSError as e:
                self.logger.error('Failed to create digest directory: %s', e)
                syslog('error', 'Failed to create digest directory: %s', e)
                return
                
        # Get the digest file path
        digest_file = os.path.join(digest_dir, 'digest.mbox')
        
        try:
            # Open the digest file
            mbox = mailbox.mbox(digest_file)
            
            # Add the message to the digest
            mbox.add(msg)
            
            # Close the digest file
            mbox.close()
            
        except (OSError, mailbox.Error) as e:
            self.logger.error('Failed to digest message: %s', e)
            syslog('error', 'Failed to digest message: %s', e)
            
    def reject(self, msg: Message, msgdata: Dict[str, Any], reason: str) -> None:
        """Reject a message from being digested.
        
        Args:
            msg: The email message
            msgdata: Additional message metadata
            reason: Reason for rejection
        """
        self.logger.warning('Rejected message from digesting: %s', reason)
        syslog('warning', 'Rejected message from digesting: %s', reason)
