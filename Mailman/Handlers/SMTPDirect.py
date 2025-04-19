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

"""Local SMTP direct drop-off.

This module delivers messages via SMTP to a locally specified daemon.  This
should be compatible with any modern SMTP server.  It is expected that the MTA
handles all final delivery.  We have to play tricks so that the list object
isn't locked while delivery occurs synchronously.

Note: This file only handles single threaded delivery.  See SMTPThreaded.py
for a threaded implementation.
"""

from __future__ import division
from __future__ import print_function

from typing import Any, Dict, List, Optional, Set, Tuple, Union
import sys
import copy
import time
import socket
import smtplib
from base64 import b64encode
from typing import UnicodeType

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
from Mailman.Handlers import Decorate
from Mailman.Logging.Syslog import syslog
from Mailman.SafeDict import MsgSafeDict
from Mailman.MailList import MailList

import email
from email.utils import formataddr
from email.header import Header
from email.charset import Charset
from email.message import Message

DOT = '.'


# Manage a connection to the SMTP server
class Connection:
    """Manage a connection to the SMTP server."""
    
    def __init__(self) -> None:
        """Initialize the connection."""
        self.__conn: Optional[smtplib.SMTP] = None
        self.__numsessions: int = 0

    def __connect(self) -> None:
        """Establish connection to SMTP server.
        
        Raises:
            smtplib.SMTPException: For any SMTP-related errors
        """
        self.__conn = smtplib.SMTP()
        self.__conn.set_debuglevel(mm_cfg.SMTPLIB_DEBUG_LEVEL)
        
        try:
            self.__conn.connect(mm_cfg.SMTPHOST, mm_cfg.SMTPPORT)
        except (socket.error, smtplib.SMTPException) as e:
            syslog('smtp-failure', 'SMTP connection failed: %s', str(e))
            self.quit()
            raise
            
        if mm_cfg.SMTP_AUTH:
            if mm_cfg.SMTP_USE_TLS:
                try:
                    self.__conn.starttls()
                    self.__conn.ehlo(mm_cfg.SMTP_HELO_HOST)
                except smtplib.SMTPException as e:
                    syslog('smtp-failure', 'SMTP TLS/EHLO error: %s', str(e))
                    self.quit()
                    raise
                    
            try:
                self.__conn.login(mm_cfg.SMTP_USER, mm_cfg.SMTP_PASSWD)
            except smtplib.SMTPException as e:
                syslog('smtp-failure', 'SMTP authentication error: %s', str(e))
                self.quit()
                raise

        self.__numsessions = mm_cfg.SMTP_MAX_SESSIONS_PER_CONNECTION

    def sendmail(self, envsender: str, recips: List[str], msgtext: str) -> Dict[str, str]:
        """Send mail via SMTP.
        
        Args:
            envsender: Envelope sender address
            recips: List of recipient addresses
            msgtext: Message text to send
            
        Returns:
            Dictionary of failed recipients and error messages
            
        Raises:
            smtplib.SMTPException: For any SMTP-related errors
        """
        if self.__conn is None:
            self.__connect()
            
        try:
            results = self.__conn.sendmail(envsender, recips, msgtext)
        except smtplib.SMTPException:
            # For safety close this connection. The next send attempt will
            # automatically re-open it.
            self.quit()
            raise
            
        # This session has been successfully completed
        self.__numsessions -= 1
        
        # Close connection if max sessions reached
        if self.__numsessions == 0:
            self.quit()
            
        return results

    def quit(self) -> None:
        """Close the SMTP connection safely."""
        if self.__conn is None:
            return
            
        try:
            self.__conn.quit()
        except smtplib.SMTPException:
            pass
        finally:
            self.__conn = None


def process(mlist: MailList, msg: Message, msgdata: Dict[str, Any]) -> None:
    """Process a message for SMTP delivery.
    
    Args:
        mlist: The mailing list
        msg: The message to process
        msgdata: Message metadata
        
    Raises:
        Errors.SomeRecipientsFailed: If any recipients failed
    """
    if not msgdata.get('recipients'):
        # Nobody to deliver to!
        return
        
    # Get message info
    recips = msgdata['recipients']
    envsender = msgdata.get('envsender', mlist.GetBouncesEmail())
    msgtext = msg.as_string()
    refused: Dict[str, str] = {}
    
    # Split recipients into chunks
    chunks = chunkify(recips, mm_cfg.SMTP_MAX_RCPTS)
    
    # Select delivery function
    if msgdata.get('verp'):
        deliveryfunc = verpdeliver
    elif msgdata.get('personalize'):
        deliveryfunc = bulkdeliver
    else:
        deliveryfunc = deliver
        
    # Select connection type
    if (not msgdata.get('verp') and 
            mm_cfg.SMTP_MAX_SESSIONS_PER_CONNECTION > 0):
        connection = BulkConnection()
    else:
        connection = Connection()
        
    # Attempt delivery
    try:
        for chunk in chunks:
            try:
                deliveryfunc(mlist, msg, msgdata, envsender, refused, connection)
            except Exception:
                # If anything goes wrong, push the last chunk back and re-raise
                chunks.append(chunk)
                raise
    finally:
        connection.quit()
        
    if refused:
        raise Errors.SomeRecipientsFailed(refused)


def chunkify(recips: List[str], chunksize: int) -> List[List[str]]:
    """Split recipients into chunks by domain.
    
    Args:
        recips: List of recipient addresses
        chunksize: Maximum size of each chunk
        
    Returns:
        List of recipient chunks
    """
    # Sort by top level domain
    chunkmap = {
        'com': 1,
        'net': 2,
        'org': 2,
        'edu': 3,
        'us': 3,
        'ca': 3,
    }
    
    buckets: Dict[int, List[str]] = {}
    for r in recips:
        tld = None
        i = r.rfind('.')
        if i >= 0:
            tld = r[i+1:]
        bin = chunkmap.get(tld, 0)
        bucket = buckets.get(bin, [])
        bucket.append(r)
        buckets[bin] = bucket
        
    # Fill chunks
    chunks: List[List[str]] = []
    currentchunk: List[str] = []
    chunklen = 0
    for bin in buckets.values():
        for r in bin:
            currentchunk.append(r)
            chunklen = chunklen + 1
            if chunklen >= chunksize:
                chunks.append(currentchunk)
                currentchunk = []
                chunklen = 0
        if currentchunk:
            chunks.append(currentchunk)
            currentchunk = []
            chunklen = 0
    return chunks


def verpdeliver(mlist, msg, msgdata, envsender, failures, conn):
    for recip in msgdata['recips']:
        # We now need to stitch together the message with its header and
        # footer.  If we're VERPIng, we have to calculate the envelope sender
        # for each recipient.  Note that the list of recipients must be of
        # length 1.
        #
        # BAW: ezmlm includes the message number in the envelope, used when
        # sending a notification to the user telling her how many messages
        # they missed due to bouncing.  Neat idea.
        msgdata['recips'] = [recip]
        # Make a copy of the message and decorate + delivery that
        msgcopy = copy.deepcopy(msg)
        Decorate.process(mlist, msgcopy, msgdata)
        # Calculate the envelope sender, which we may be VERPing
        if msgdata.get('verp'):
            bmailbox, bdomain = Utils.ParseEmail(envsender)
            rmailbox, rdomain = Utils.ParseEmail(recip)
            if rdomain is None:
                # The recipient address is not fully-qualified.  We can't
                # deliver it to this person, nor can we craft a valid verp
                # header.  I don't think there's much we can do except ignore
                # this recipient.
                syslog('smtp', 'Skipping VERP delivery to unqual recip: %s', recip)
                continue
            d = {'bounces': bmailbox,
                 'mailbox': rmailbox,
                 'host'   : DOT.join(rdomain),
                 }
            envsender = '%s@%s' % ((mm_cfg.VERP_FORMAT % d), DOT.join(bdomain))
        if mlist.personalize == 2:
            # When fully personalizing, we want the To address to point to the
            # recipient, not to the mailing list
            del msgcopy['to']
            name = None
            if mlist.isMember(recip):
                name = mlist.getMemberName(recip)
            if name:
                # Convert the name to an email-safe representation.  If the
                # name is a byte string, convert it first to Unicode, given
                # the character set of the member's language, replacing bad
                # characters for which we can do nothing about.  Once we have
                # the name as Unicode, we can create a Header instance for it
                # so that it's properly encoded for email transport.
                charset = Utils.GetCharSet(mlist.getMemberLanguage(recip))
                if charset == 'us-ascii':
                    # Since Header already tries both us-ascii and utf-8,
                    # let's add something a bit more useful.
                    charset = 'iso-8859-1'
                charset = Charset(charset)
                codec = charset.input_codec or 'ascii'
                if not isinstance(name, UnicodeType):
                    name = str(name, codec, 'replace')
                name = Header(name, charset).encode()
                msgcopy['To'] = formataddr((name, recip))
            else:
                msgcopy['To'] = recip
        # We can flag the mail as a duplicate for each member, if they've
        # already received this message, as calculated by Message-ID.  See
        # AvoidDuplicates.py for details.
        del msgcopy['x-mailman-copy']
        if msgdata.get('add-dup-header', {}).has_key(recip):
            msgcopy['X-Mailman-Copy'] = 'yes'
        # If desired, add the RCPT_BASE64_HEADER_NAME header
        if len(mm_cfg.RCPT_BASE64_HEADER_NAME) > 0:
            del msgcopy[mm_cfg.RCPT_BASE64_HEADER_NAME]
            msgcopy[mm_cfg.RCPT_BASE64_HEADER_NAME] = b64encode(recip)
        # For the final delivery stage, we can just bulk deliver to a party of
        # one. ;)
        bulkdeliver(mlist, msgcopy, msgdata, envsender, failures, conn)


def bulkdeliver(mlist, msg, msgdata, envsender, failures, conn):
    # Do some final cleanup of the message header.  Start by blowing away
    # any the Sender: and Errors-To: headers so remote MTAs won't be
    # tempted to delivery bounces there instead of our envelope sender
    #
    # BAW An interpretation of RFCs 2822 and 2076 could argue for not touching
    # the Sender header at all.  Brad Knowles points out that MTAs tend to
    # wipe existing Return-Path headers, and old MTAs may still honor
    # Errors-To while new ones will at worst ignore the header.
    #
    # With some MUAs (eg. Outlook 2003) rewriting the Sender header with our
    # envelope sender causes more problems than it solves, because some will 
    # include the Sender address in a reply-to-all, which is not only 
    # confusing to subscribers, but can actually disable/unsubscribe them from
    # lists, depending on how often they accidentally reply to it.  Also, when
    # forwarding mail inline, the sender is replaced with the string "Full 
    # Name (on behalf bounce@addr.ess)", essentially losing the original
    # sender address.  To partially mitigate this, we add the list name as a
    # display-name in the Sender: header that we add.
    # 
    # The drawback of not touching the Sender: header is that some MTAs might
    # still send bounces to it, so by not trapping it, we can miss bounces.
    # (Or worse, MTAs might send bounces to the From: address if they can't
    # find a Sender: header.)  So instead of completely disabling the sender
    # rewriting, we offer an option to disable it.
    del msg['errors-to']
    msg['Errors-To'] = envsender
    if mlist.include_sender_header:
        del msg['sender']
        msg['Sender'] = '"%s" <%s>' % (mlist.real_name, envsender)
    # Get the plain, flattened text of the message, sans unixfrom
    # using our as_string() method to not mangle From_ and not fold
    # sub-part headers possibly breaking signatures.
    msgtext = msg.as_string(mangle_from_=False)
    refused = {}
    recips = msgdata['recips']
    msgid = msg['message-id']
    try:
        # Send the message
        refused = conn.sendmail(envsender, recips, msgtext)
    except smtplib.SMTPRecipientsRefused as e:
        syslog('smtp-failure', 'All recipients refused: %s, msgid: %s', e, msgid)
        refused = e.recipients
    except smtplib.SMTPResponseException as e:
        syslog('smtp-failure', 'SMTP session failure: %s, %s, msgid: %s',
               e.smtp_code, e.smtp_error, msgid)
        # If this was a permanent failure, don't add the recipients to the
        # refused, because we don't want them to be added to failures.
        # Otherwise, if the MTA rejects the message because of the message
        # content (e.g. it's spam, virii, or has syntactic problems), then
        # this will end up registering a bounce score for every recipient.
        # Definitely /not/ what we want.
        if e.smtp_code < 500 or e.smtp_code == 552:
            # It's a temporary failure
            for r in recips:
                refused[r] = (e.smtp_code, e.smtp_error)
    except (socket.error, smtplib.SMTPException) as e:
        # MTA not responding, or other socket problems, or any other kind of
        # SMTPException.  In that case, nothing got delivered, so treat this
        # as a temporary failure.
        syslog('smtp-failure', 'Low level smtp error: %s, msgid: %s', e, msgid)
        error = str(e)
        for r in recips:
            refused[r] = (-1, error)
    failures.update(refused)


def _encode_name(name, charset):
    """Encode a name for use in email headers."""
    if isinstance(name, str):
        try:
            name = name.encode('ascii')
        except UnicodeEncodeError:
            name = str(Header(name, charset))
    return name

def _encode_recipient(recip):
    """Encode a recipient email address if needed."""
    if isinstance(recip, bytes):
        recip = recip.decode('utf-8')
    return recip
