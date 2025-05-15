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

from builtins import object
import copy
import time
import socket
import smtplib
from smtplib import SMTPException
from base64 import b64encode
import traceback
import os
import errno
import pickle
import email.message
from email.message import Message

import Mailman.mm_cfg
import Mailman.Utils
import Mailman.Errors
from Mailman.Message import Message
from Mailman.Handlers.Decorate import decorate
from Mailman.Logging.Syslog import mailman_log
import Mailman.SafeDict
from Mailman.Queue.sbcache import get_switchboard

import email
from email.utils import formataddr
from email.header import Header
from email.charset import Charset

DOT = '.'

# Manage a connection to the SMTP server
class Connection(object):
    def __init__(self):
        self.__conn = None

    def __connect(self):
        try:
            self.__conn = smtplib.SMTP()
            self.__conn.set_debuglevel(Mailman.mm_cfg.SMTPLIB_DEBUG_LEVEL)
            # Ensure we have a valid hostname for TLS
            helo_host = Mailman.mm_cfg.SMTP_HELO_HOST
            if not helo_host or helo_host.startswith('.'):
                helo_host = Mailman.mm_cfg.SMTPHOST
            if not helo_host or helo_host.startswith('.'):
                # If we still don't have a valid hostname, use localhost
                helo_host = 'localhost'
            mailman_log('smtp', 'Connecting to SMTP server %s:%s with HELO %s', 
                   Mailman.mm_cfg.SMTPHOST, Mailman.mm_cfg.SMTPPORT, helo_host)
            self.__conn.connect(Mailman.mm_cfg.SMTPHOST, Mailman.mm_cfg.SMTPPORT)
            # Set the hostname for TLS
            self.__conn._host = helo_host
            if Mailman.mm_cfg.SMTP_AUTH:
                if Mailman.mm_cfg.SMTP_USE_TLS:
                    mailman_log('smtp', 'Using TLS with hostname: %s', helo_host)
                    try:
                        # Use native TLS support
                        self.__conn.starttls()
                    except SMTPException as e:
                        mailman_log('smtp-failure', 'SMTP TLS error: %s\nTraceback:\n%s', 
                               str(e), traceback.format_exc())
                        self.quit()
                        raise
                try:
                    self.__conn.login(Mailman.mm_cfg.SMTP_USER, Mailman.mm_cfg.SMTP_PASSWD)
                except smtplib.SMTPHeloError as e:
                    mailman_log('smtp-failure', 'SMTP HELO error: %s\nTraceback:\n%s', 
                           str(e), traceback.format_exc())
                    self.quit()
                    raise
                except smtplib.SMTPAuthenticationError as e:
                    mailman_log('smtp-failure', 'SMTP AUTH error: %s\nTraceback:\n%s', 
                           str(e), traceback.format_exc())
                    self.quit()
                except smtplib.SMTPException as e:
                    mailman_log('smtp-failure',
                           'SMTP - no suitable authentication method found: %s\nTraceback:\n%s', 
                           str(e), traceback.format_exc())
                    self.quit()
                    raise
        except (socket.error, smtplib.SMTPException) as e:
            mailman_log('smtp-failure', 'SMTP connection error: %s\nTraceback:\n%s', 
                   str(e), traceback.format_exc())
            self.quit()
            raise

        self.__numsessions = Mailman.mm_cfg.SMTP_MAX_SESSIONS_PER_CONNECTION

    def sendmail(self, envsender, recips, msgtext):
        if self.__conn is None:
            self.__connect()
        try:
            # Convert message to string if it's a Message object
            if isinstance(msgtext, Message):
                msgtext = msgtext.as_string()
            # Ensure msgtext is properly encoded as UTF-8
            if isinstance(msgtext, str):
                msgtext = msgtext.encode('utf-8')
            # Convert recips to list if it's not already
            if not isinstance(recips, list):
                recips = [recips]
            # Ensure envsender is a string
            if isinstance(envsender, bytes):
                envsender = envsender.decode('utf-8')
            results = self.__conn.sendmail(envsender, recips, msgtext)
        except smtplib.SMTPException as e:
            # For safety, close this connection.  The next send attempt will
            # automatically re-open it.  Pass the exception on up.
            mailman_log('smtp-failure', 'SMTP sendmail error: %s\nTraceback:\n%s', 
                   str(e), traceback.format_exc())
            self.quit()
            raise
        # This session has been successfully completed.
        self.__numsessions -= 1
        # By testing exactly for equality to 0, we automatically handle the
        # case for SMTP_MAX_SESSIONS_PER_CONNECTION <= 0 meaning never close
        # the connection.  We won't worry about wraparound <wink>.
        if self.__numsessions == 0:
            self.quit()
        return results

    def quit(self):
        if self.__conn is None:
            return
        try:
            self.__conn.quit()
        except smtplib.SMTPException:
            pass
        self.__conn = None


def process(mlist, msg, msgdata):
    """Process the message for delivery.

    This is the main entry point for the SMTPDirect handler.
    """
    t0 = time.time()
    refused = {}
    envsender = msgdata.get('envsender', msg.get_sender())
    if envsender is None:
        envsender = mlist.GetBouncesEmail()
    # Get the list of recipients
    recips = msgdata.get('recipients', [])
    if not recips:
        # Get message details for logging
        msgid = msg.get('message-id', 'unknown')
        sender = msg.get('from', 'unknown')
        subject = msg.get('subject', 'no subject')
        to = msg.get('to', 'no to')
        cc = msg.get('cc', 'no cc')
        
        mailman_log('error', 
            'No recipients found in msgdata for message:\n'
            '  Message-ID: %s\n'
            '  From: %s\n'
            '  Subject: %s\n'
            '  To: %s\n'
            '  Cc: %s\n'
            '  List: %s',
            msgid, sender, subject, to, cc, mlist.internal_name())
        return

    # Check for spam headers first
    if msg.get('x-google-group-id'):
        mailman_log('error', 'Silently dropping message with X-Google-Group-Id header: %s',
                   msg.get('message-id', 'unknown'))
        # Add all recipients to refused list with 550 error
        for r in recips:
            refused[r] = (550, 'Message rejected due to spam detection')
        # Update failures dict
        msgdata['failures'] = refused
        # Silently return without raising an exception
        return

    # Chunkify the recipients
    chunks = chunkify(recips, Mailman.mm_cfg.SMTP_MAX_RCPTS_PER_CHUNK)
    # Choose the delivery function based on VERP settings
    if msgdata.get('verp'):
        deliveryfunc = verpdeliver
    else:
        deliveryfunc = bulkdeliver

    try:
        origrecips = msgdata['recips']
        origsender = msgdata.get('original_sender', msg.get_sender())
        conn = Connection()
        try:
            msgdata['undelivered'] = chunks
            while chunks:
                chunk = chunks.pop()
                msgdata['recips'] = chunk
                try:
                    deliveryfunc(mlist, msg, msgdata, envsender, refused, conn)
                except Mailman.Errors.RejectMessage as e:
                    # Handle message rejection gracefully
                    mailman_log('error', 'Message rejected: %s', str(e))
                    # Add all recipients in this chunk to refused list
                    for r in chunk:
                        refused[r] = (550, str(e))
                    continue
                except Exception as e:
                    mailman_log('error', 
                        'Delivery error for chunk: %s\nError: %s\n%s',
                        chunk, str(e), traceback.format_exc())
                    chunks.append(chunk)
                    raise
            del msgdata['undelivered']
        finally:
            conn.quit()
            msgdata['recips'] = origrecips

        # Log the successful post
        t1 = time.time()
        listname = mlist.internal_name()
        if isinstance(listname, bytes):
            listname = listname.decode('latin-1')
        d = Mailman.SafeDict.MsgSafeDict(msg, {'time'    : t1-t0,
                          'size'    : len(msg.as_string()),
                          '#recips' : len(recips),
                          '#refused': len(refused),
                          'listname': listname,
                          'sender'  : origsender,
                          })
        if Mailman.mm_cfg.SMTP_LOG_EVERY_MESSAGE:
            mailman_log(Mailman.mm_cfg.SMTP_LOG_EVERY_MESSAGE[0],
                    Mailman.mm_cfg.SMTP_LOG_EVERY_MESSAGE[1] % d.copy())

    except Exception as e:
        mailman_log('error', 'Error in SMTPDirect.process: %s\nTraceback:\n%s',
               str(e), traceback.format_exc())
        raise


def chunkify(recips, chunksize):
    # First do a simple sort on top level domain.  It probably doesn't buy us
    # much to try to sort on MX record -- that's the MTA's job.  We're just
    # trying to avoid getting a max recips error.  Split the chunks along
    # these lines (as suggested originally by Chuq Von Rospach and slightly
    # elaborated by BAW).
    chunkmap = {'com': 1,
                'net': 2,
                'org': 3,
                'edu': 4,
                'us' : 5,
                'ca' : 6,
                'uk' : 7,
                'jp' : 8,
                'au' : 9,
                }
    # Need to sort by domain name.  if we split to chunks it is possible
    # some well-known domains will be interspersed as we sort by
    # userid by default instead of by domain.  (jared mauch)
    buckets = {}
    for r in recips:
        tld = None
        i = r.rfind('.')
        if i >= 0:
            tld = r[i+1:]
        # Use get() with default value of 0 for unknown TLDs
        bin = chunkmap.get(tld, 0)
        bucket = buckets.get(bin, [])
        bucket.append(r)
        buckets[bin] = bucket
    # Now start filling the chunks
    chunks = []
    currentchunk = []
    chunklen = 0
    for bin in list(buckets.values()):
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
        try:
            # We now need to stitch together the message with its header and
            # footer.  If we're VERPIng, we have to calculate the envelope sender
            # for each recipient.  Note that the list of recipients must be of
            # length 1.
            msgdata['recips'] = [recip]
            # Make a copy of the message and decorate + delivery that
            msgcopy = copy.deepcopy(msg)
            decorate(mlist, msgcopy, msgdata)
            # Calculate the envelope sender, which we may be VERPing
            if msgdata.get('verp'):
                try:
                    bmailbox, bdomain = Mailman.Utils.ParseEmail(envsender)
                    rmailbox, rdomain = Mailman.Utils.ParseEmail(recip)
                    if rdomain is None:
                        # The recipient address is not fully-qualified.  We can't
                        # deliver it to this person, nor can we craft a valid verp
                        # header.  I don't think there's much we can do except ignore
                        # this recipient.
                        mailman_log('smtp', 'Skipping VERP delivery to unqual recip: %s',
                               recip)
                        continue
                    d = {'bounces': bmailbox,
                         'mailbox': rmailbox,
                         'host'   : DOT.join(rdomain),
                         }
                    envsender = '%s@%s' % ((Mailman.mm_cfg.VERP_FORMAT % d), DOT.join(bdomain))
                except Exception as e:
                    mailman_log('error', 'Failed to parse email addresses for VERP: %s', e)
                    continue
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
                    charset = Mailman.Utils.GetCharSet(mlist.getMemberLanguage(recip))
                    if charset == 'us-ascii':
                        # Since Header already tries both us-ascii and utf-8,
                        # let's add something a bit more useful.
                        charset = 'iso-8859-1'
                    charset = Charset(charset)
                    codec = charset.input_codec or 'ascii'
                    if not isinstance(name, str):
                        name = str(name, codec, 'replace')
                    name = Header(name, charset).encode()
                    msgcopy['To'] = formataddr((name, recip))
                else:
                    msgcopy['To'] = recip
            # We can flag the mail as a duplicate for each member, if they've
            # already received this message, as calculated by Message-ID.  See
            # AvoidDuplicates.py for details.
            if 'x-mailman-copy' in msgcopy:
                del msgcopy['x-mailman-copy']
            if recip in msgdata.get('add-dup-header', {}):
                msgcopy['X-Mailman-Copy'] = 'yes'
            # If desired, add the RCPT_BASE64_HEADER_NAME header
            if len(Mailman.mm_cfg.RCPT_BASE64_HEADER_NAME) > 0:
                del msgcopy[Mailman.mm_cfg.RCPT_BASE64_HEADER_NAME]
                msgcopy[Mailman.mm_cfg.RCPT_BASE64_HEADER_NAME] = b64encode(recip)
            # For the final delivery stage, we can just bulk deliver to a party of
            # one. ;)
            bulkdeliver(mlist, msgcopy, msgdata, envsender, failures, conn)
        except Exception as e:
            mailman_log('error', 'Failed to process VERP delivery: %s', e)
            continue


def bulkdeliver(mlist, msg, msgdata, envsender, failures, conn):
    # Initialize recips and refused at the start
    recips = []
    refused = {}
    try:
        # Get the list of recipients
        recips = msgdata.get('recipients', [])
        if not recips:
            mailman_log('error', 'No recipients found in msgdata')
            return

        # Convert email.message.Message to Mailman.Message if needed
        if isinstance(msg, email.message.Message) and not isinstance(msg, Message):
            mailman_msg = Message()
            # Copy all attributes from the original message
            for key, value in msg.items():
                mailman_msg[key] = value
            # Copy the payload with proper MIME handling
            if msg.is_multipart():
                for part in msg.get_payload():
                    if isinstance(part, email.message.Message):
                        mailman_msg.attach(part)
                    else:
                        newpart = Message()
                        newpart.set_payload(part)
                        mailman_msg.attach(newpart)
            else:
                mailman_msg.set_payload(msg.get_payload())
            msg = mailman_msg

        # Do some final cleanup of the message header
        del msg['errors-to']
        msg['Errors-To'] = envsender
        if mlist.include_sender_header:
            del msg['sender']
            msg['Sender'] = '"%s" <%s>' % (mlist.real_name, envsender)

        # Get the plain, flattened text of the message
        msgtext = msg.as_string(mangle_from_=False)
        # Ensure the message text is properly encoded as UTF-8
        if isinstance(msgtext, str):
            msgtext = msgtext.encode('utf-8')

        msgid = msg.get('Message-ID', 'n/a')
        # Ensure msgid is a string
        if isinstance(msgid, bytes):
            try:
                msgid = msgid.decode('utf-8', 'replace')
            except UnicodeDecodeError:
                msgid = msgid.decode('latin-1', 'replace')
        elif not isinstance(msgid, str):
            msgid = str(msgid)
        try:
            # Send the message
            refused = conn.sendmail(envsender, recips, msgtext)
        except smtplib.SMTPRecipientsRefused as e:
            mailman_log('smtp-failure', 'All recipients refused: %s, msgid: %s',
                   e, msgid)
            refused = e.recipients
            # Move message to bad queue since all recipients were refused
            badq = get_switchboard(Mailman.mm_cfg.BADQUEUE_DIR)
            badq.enqueue(msg, msgdata)
        except smtplib.SMTPResponseException as e:
            mailman_log('smtp-failure', 'SMTP session failure: %s, %s, msgid: %s',
                   e.smtp_code, e.smtp_error, msgid)
            # Properly handle permanent vs temporary failures
            if e.smtp_code >= 500 and e.smtp_code != 552:
                # Permanent failure - add to refused and move to bad queue
                for r in recips:
                    refused[r] = (e.smtp_code, e.smtp_error)
                badq = get_switchboard(Mailman.mm_cfg.BADQUEUE_DIR)
                badq.enqueue(msg, msgdata)
            else:
                # Temporary failure - don't add to refused
                mailman_log('smtp-failure', 'Temporary SMTP failure, will retry: %s', e.smtp_error)
        except (socket.error, IOError, smtplib.SMTPException) as e:
            # MTA not responding or other socket problems
            mailman_log('smtp-failure', 'Low level smtp error: %s, msgid: %s', e, msgid)
            error = str(e)
            for r in recips:
                refused[r] = (-1, error)
            # Move message to bad queue for low level errors
            badq = get_switchboard(Mailman.mm_cfg.BADQUEUE_DIR)
            badq.enqueue(msg, msgdata)
        failures.update(refused)
    except Exception as e:
        mailman_log('error', 'Error in bulkdeliver: %s\nTraceback:\n%s',
               str(e), traceback.format_exc())
        raise
