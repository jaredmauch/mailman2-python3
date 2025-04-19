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


def verpdeliver(mlist: MailList, msg: Message, msgdata: Dict[str, Any], 
                envsender: str, failures: Dict[str, str], conn: Connection) -> None:
    """Deliver a message with VERP (Variable Envelope Return Path).
    
    Args:
        mlist: The mailing list
        msg: The message to deliver
        msgdata: Message metadata
        envsender: Envelope sender address
        failures: Dictionary to store failed deliveries
        conn: SMTP connection to use
    """
    # Make a copy of the message for each recipient
    for recip in msgdata['recipients']:
        try:
            # Create VERP envelope sender
            verp_sender = f"{mlist.GetBouncesEmail()}+{recip}@{mlist.host_name}"
            
            # Send the message
            refused = conn.sendmail(verp_sender, [recip], msg.as_string())
            if refused:
                failures.update(refused)
                
        except Exception as e:
            failures[recip] = str(e)
            syslog('smtp-failure', 'VERP delivery failed for %s: %s', recip, e)

def bulkdeliver(mlist: MailList, msg: Message, msgdata: Dict[str, Any],
                envsender: str, failures: Dict[str, str], conn: Connection) -> None:
    """Deliver a message in bulk mode.
    
    Args:
        mlist: The mailing list
        msg: The message to deliver
        msgdata: Message metadata
        envsender: Envelope sender address
        failures: Dictionary to store failed deliveries
        conn: SMTP connection to use
    """
    # Clean up message headers
    if not mm_cfg.SMTP_SENDER_REWRITE:
        msg['Sender'] = None
    else:
        sender = formataddr((mlist.real_name, mlist.GetBouncesEmail()))
        msg['Sender'] = sender
        
    msg['Errors-To'] = None
    
    try:
        # Send the message
        refused = conn.sendmail(envsender, msgdata['recipients'], msg.as_string())
        if refused:
            failures.update(refused)
            
    except Exception as e:
        for recip in msgdata['recipients']:
            failures[recip] = str(e)
        syslog('smtp-failure', 'Bulk delivery failed: %s', e)

def _encode_name(name: str, charset: str) -> str:
    """Encode a name using the specified charset.
    
    Args:
        name: Name to encode
        charset: Character set to use
        
    Returns:
        Encoded name string
    """
    if not name:
        return ''
        
    try:
        return name.encode(charset, 'replace').decode(charset)
    except (UnicodeError, LookupError):
        return name

def _encode_recipient(recip: str) -> str:
    """Encode a recipient address.
    
    Args:
        recip: Recipient address to encode
        
    Returns:
        Encoded recipient string
    """
    try:
        name, addr = email.utils.parseaddr(recip)
        if name:
            charset = Charset('utf-8')
            name = _encode_name(name, charset)
            return formataddr((name, addr))
        return addr
    except Exception:
        return recip
