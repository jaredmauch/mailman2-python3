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

"""-request robot command queue runner."""

# See the delivery diagram in IncomingRunner.py.  This module handles all
# email destined for mylist-request, -join, and -leave.  It no longer handles
# bounce messages (i.e. -admin or -bounces), nor does it handle mail to
# -owner.

import re
import sys
import email
import email.message
import email.utils
from email.header import decode_header, make_header, Header
from email.errors import HeaderParseError
from email.iterators import typed_subpart_iterator
from email.mime.text import MIMEText
from email.mime.message import MIMEMessage

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
from Mailman import i18n
from Mailman.htmlformat import *
from Mailman.Logging.Syslog import mailman_log, syslog
from Mailman.Utils import validate_ip_address
import Mailman.Handlers.Replybot as Replybot
from Mailman.Message import Message, UserNotification
from Mailman.i18n import _
from Mailman.Queue.Runner import Runner
from Mailman import LockFile
from Mailman import Pending
from Mailman import MailList
import traceback
import os

# Lazy imports to avoid circular dependencies
def get_replybot():
    import Mailman.Handlers.Replybot as Replybot
    return Replybot

def get_maillist():
    import Mailman.MailList as MailList
    return MailList.MailList

def get_usernotification():
    from Mailman.Message import UserNotification
    return UserNotification

NL = '\n'
CONTINUE = 0
STOP = 1
BADCMD = 2
BADSUBJ = 3

# List of valid commands that can be imported
VALID_COMMANDS = {
    'confirm',    # Confirm subscription/unsubscription
    'echo',       # Echo command
    'end',        # End command
    'help',       # Help command
    'info',       # List information
    'join',       # Join list
    'leave',      # Leave list
    'lists',      # List all lists
    'password',   # Password command
    'remove',     # Remove from list
    'set',        # Set options
    'stop',       # Stop command
    'subscribe',  # Subscribe to list
    'unsubscribe',# Unsubscribe from list
    'who'         # Who command
}

class Results:
    def __init__(self, mlist_obj, msg, msgdata):
        self.mlist = mlist_obj
        self.msg = msg
        self.msgdata = msgdata
        # Only set returnaddr if the response is to go to someone other than
        # the address specified in the From: header (e.g. for the password
        # command).
        self.returnaddr = None
        self.commands = []
        self.results = []
        self.ignored = []
        self.lineno = 0
        self.subjcmdretried = 0
        self.respond = True
        # Extract the subject header and do RFC 2047 decoding
        subj = msg.get('subject', '')
        try:
            # If subj is already a Header object, convert it to string first
            if isinstance(subj, Header):
                subj = str(subj)
            else:
                subj = str(make_header(decode_header(subj)))
            # TK: Currently we don't allow 8bit or multibyte in mail command.
            # MAS: However, an l10n 'Re:' may contain non-ascii so ignore it.
            subj = subj.encode('us-ascii', 'ignore').decode('us-ascii')
            # Always process the Subject: header first
            self.commands.append(subj)
        except (HeaderParseError, UnicodeError, LookupError):
            # We couldn't parse it so ignore the Subject header
            pass
        # Find the first text/plain part
        part = None
        for part in typed_subpart_iterator(msg, 'text', 'plain'):
            break
        if part is None or part is not msg:
            # Either there was no text/plain part or we ignored some
            # non-text/plain parts.
            self.results.append(_('Ignoring non-text/plain MIME parts'))
        if part is None:
            # E.g the outer Content-Type: was text/html
            return
        body = part.get_payload(decode=True)
        if (part.get_content_charset(None)):
            # Use get() with default value for lang
            lang = msgdata.get('lang', mlist_obj.preferred_language)
            body = str(body, part.get_content_charset(),
                           errors='replace').encode(
                           Utils.GetCharSet(lang),
                           errors='replace')
        # text/plain parts better have string payloads
        if not isinstance(body, (str, bytes)):
            raise TypeError(f'Invalid body type: {type(body)}, expected str or bytes')
        lines = body.splitlines()
        # Use no more lines than specified
        self.commands.extend(lines[:mm_cfg.DEFAULT_MAIL_COMMANDS_MAX_LINES])
        self.ignored.extend(lines[mm_cfg.DEFAULT_MAIL_COMMANDS_MAX_LINES:])

    def process(self):
        # Now, process each line until we find an error.  The first
        # non-command line found stops processing.
        found = BADCMD
        ret = CONTINUE
        for line in self.commands:
            if line and line.strip():
                # Ensure line is a string
                if isinstance(line, bytes):
                    try:
                        line = line.decode('utf-8')
                    except UnicodeDecodeError:
                        line = line.decode('latin-1')
                args = line.split()
                cmd = args.pop(0).lower()
                ret = self.do_command(cmd, args)
                if ret == STOP or ret == CONTINUE:
                    found = ret
            self.lineno += 1
            if ret == STOP or ret == BADCMD:
                break
        return found

    def do_command(self, cmd, args=None):
        if args is None:
            args = ()
        # Clean the command name to prevent injection
        cmd = cmd.lower().strip()
        # Only try to import valid commands
        if cmd not in VALID_COMMANDS:
            # If we're on line zero, it was the Subject: header that didn't
            # contain a command.  It's possible there's a Re: prefix (or
            # localized version thereof) on the Subject: line that's messing
            # things up.  Pop the prefix off and try again... once.
            if self.lineno != 0:
                return BADCMD
            if self.subjcmdretried < 1:
                self.subjcmdretried += 1
                if re.search('^.*:.+', cmd):
                    cmd = re.sub('.*:', '', cmd).lower().strip()
                    return self.do_command(cmd, args)
            if self.subjcmdretried < 2 and args:
                self.subjcmdretried += 1
                cmd = args.pop(0).lower().strip()
                return self.do_command(cmd, args)
            return BADSUBJ

        # Try to import a command handler module for this command
        modname = 'Mailman.Commands.cmd_' + cmd
        try:
            __import__(modname)
            handler = sys.modules[modname]
        except (ImportError, ValueError, KeyError, TypeError) as e:
            syslog('error', 'CommandRunner: Failed to import command module %s: %s',
                   modname, str(e))
            return BADCMD

        if handler.process(self, args):
            return STOP
        else:
            return CONTINUE

    def send_response(self):
        # Helper
        def indent(lines):
            """Indent each line with 4 spaces."""
            result = []
            for line in lines:
                if isinstance(line, bytes):
                    try:
                        # Try UTF-8 first
                        line = line.decode('utf-8')
                    except UnicodeDecodeError:
                        # Fall back to latin-1 if UTF-8 fails
                        line = line.decode('latin-1')
                result.append('    ' + line)
            return result
        # Quick exit for some commands which don't need a response
        if not self.respond:
            return
        resp = [Utils.wrap(_("""\
The results of your email command are provided below.
Attached is your original message.
"""))]
        if self.results:
            resp.append(_('- Results:'))
            resp.extend(indent(self.results))
        # Ignore empty lines
        unprocessed = [line for line in self.commands[self.lineno:]
                       if line and line.strip()]
        if unprocessed and mm_cfg.RESPONSE_INCLUDE_LEVEL >= 2:
            resp.append(_('\n- Unprocessed:'))
            resp.extend(indent(unprocessed))
        if not unprocessed and not self.results:
            # The user sent an empty message; return a helpful one.
            resp.append(Utils.wrap(_("""\
No commands were found in this message.
To obtain instructions, send a message containing just the word "help".
""")))
        if self.ignored and mm_cfg.RESPONSE_INCLUDE_LEVEL >= 2:
            resp.append(_('\n- Ignored:'))
            resp.extend(indent(self.ignored))
        resp.append(_('\n- Done.\n\n'))
        # Encode any strings into the list charset, so we don't try to
        # join strings and invalid ASCII.
        charset = Utils.GetCharSet(self.msgdata.get('lang', self.mlist.preferred_language))
        encoded_resp = []
        for item in resp:
            if isinstance(item, str):
                item = item.encode(charset, 'replace')
            # Convert bytes to string for joining
            if isinstance(item, bytes):
                try:
                    item = item.decode(charset, 'replace')
                except UnicodeDecodeError:
                    item = item.decode('latin-1', 'replace')
            encoded_resp.append(item)
        # Join all items as strings
        results = MIMEText(NL.join(str(item) for item in encoded_resp), _charset=charset)
        # Safety valve for mail loops with misconfigured email 'bots.  We
        # don't respond to commands sent with "Precedence: bulk|junk|list"
        # unless they explicitly "X-Ack: yes", but not all mail 'bots are
        # correctly configured, so we max out the number of responses we'll
        # give to an address in a single day.
        #
        # BAW: We wait until now to make this decision since our sender may
        # not be self.msg.get_sender(), but I'm not sure this is right.
        recip = self.returnaddr or self.msg.get_sender()
        if not self.mlist.autorespondToSender(recip, self.msgdata.get('lang', self.mlist.preferred_language)):
            return
        msg = UserNotification(
            recip,
            self.mlist.GetOwnerEmail(),
            _('The results of your email commands'),
            lang=self.msgdata.get('lang', self.mlist.preferred_language))
        msg.set_type('multipart/mixed')
        msg.attach(results)
        if mm_cfg.RESPONSE_INCLUDE_LEVEL == 1:
            self.msg.set_payload(
                _('Message body suppressed by Mailman site configuration\n'))
        if mm_cfg.RESPONSE_INCLUDE_LEVEL == 0:
            orig = MIMEText(_(
                'Original message suppressed by Mailman site configuration\n'
                ), _charset=charset)
        else:
            orig = MIMEMessage(self.msg)
        msg.attach(orig)
        # Add recipient to msgdata to ensure proper delivery
        msgdata = {'recipient': recip}
        msg.send(self.mlist, msgdata=msgdata)

class CommandRunner(Runner):
    QDIR = mm_cfg.CMDQUEUE_DIR

    def _validate_message(self, msg, msgdata):
        """Validate a command message.
        
        Args:
            msg: The message to validate
            msgdata: Additional message metadata
            
        Returns:
            tuple: (msg, success) where success is True if validation passed
        """
        try:
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

            # Check for required headers
            if not msg.get('message-id'):
                syslog('error', 'CommandRunner._validate_message: Missing Message-ID header')
                return msg, False
                
            if not msg.get('from'):
                syslog('error', 'CommandRunner._validate_message: Missing From header')
                return msg, False
                
            # Check for command type in msgdata
            if not any(key in msgdata for key in ('torequest', 'tojoin', 'toleave', 'toconfirm')):
                syslog('error', 'CommandRunner._validate_message: No command type found in msgdata')
                return msg, False
                
            return msg, True
            
        except Exception as e:
            syslog('error', 'CommandRunner._validate_message: Error validating message: %s', str(e))
            return msg, False

    def _dispose(self, mlist, msg, msgdata):
        """Process a command message.
        
        Args:
            mlist: The MailList instance this message is destined for
            msg: The Message object representing the message
            msgdata: Dictionary of message metadata
            
        Returns:
            bool: True if message should be requeued, False if processing is complete
        """
        msgid = msg.get('message-id', 'n/a')
        filebase = msgdata.get('_filebase', 'unknown')
        
        # Ensure we have a MailList object
        if isinstance(mlist, str):
            try:
                mlist = get_maillist()(mlist, lock=0)
                should_unlock = True
            except Errors.MMUnknownListError:
                syslog('error', 'CommandRunner: Unknown list %s', mlist)
                self._shunt.enqueue(msg, msgdata)
                return False
        else:
            should_unlock = False
        
        try:
            syslog('debug', 'CommandRunner._dispose: Starting to process command message %s (file: %s) for list %s',
                   msgid, filebase, mlist.internal_name())
            
            # Check retry delay and duplicate processing
            if not self._check_retry_delay(msgid, filebase):
                syslog('debug', 'CommandRunner._dispose: Message %s failed retry delay check, skipping', msgid)
                return True

            # Validate message type first
            msg, success = self._validate_message(msg, msgdata)
            if not success:
                syslog('error', 'CommandRunner._dispose: Message validation failed for message %s', msgid)
                msgdata['_validation_failure'] = 'Missing required headers'
                self._shunt.enqueue(msg, msgdata)
                return False

            # The policy here is similar to the Replybot policy.  If a message has
            # "Precedence: bulk|junk|list" and no "X-Ack: yes" header, we discard
            # it to prevent replybot response storms.
            precedence = msg.get('precedence', '').lower()
            ack = msg.get('x-ack', '').lower()
            if ack != 'yes' and precedence in ('bulk', 'junk', 'list'):
                syslog('vette', 'Precedence: %s message discarded by: %s',
                       precedence, mlist.GetRequestEmail())
                return False

            # Lock the list before any operations
            try:
                mlist.Lock(timeout=mm_cfg.LIST_LOCK_TIMEOUT)
            except LockFile.TimeOutError:
                # Oh well, try again later
                return True

            try:
                # Check if list is temporarily unavailable
                try:
                    mlist.Load()
                except Errors.MMCorruptListDatabaseError as e:
                    syslog('error', 'CommandRunner._dispose: List %s is temporarily unavailable: %s',
                           mlist.internal_name(), str(e))
                    return True
                except Exception as e:
                    syslog('error', 'CommandRunner._dispose: Error loading list %s: %s',
                           mlist.internal_name(), str(e))
                    return True

                # Do replybot for commands
                Replybot = get_replybot()
                Replybot.process(mlist, msg, msgdata)
                if mlist.autorespond_requests == 1:
                    syslog('vette', 'replied and discard')
                    # w/discard
                    return False

                # Now craft the response
                res = Results(mlist, msg, msgdata)
                # This message will have been delivered to one of mylist-request,
                # mylist-join, or mylist-leave, and the message metadata will contain
                # a key to which one was used.
                ret = BADCMD
                if msgdata.get('torequest', False):
                    ret = res.process()
                elif msgdata.get('tojoin', False):
                    ret = res.do_command('join')
                elif msgdata.get('toleave', False):
                    ret = res.do_command('leave')
                elif msgdata.get('toconfirm', False):
                    mo = re.match(mm_cfg.VERP_CONFIRM_REGEXP, msg.get('to', ''), re.IGNORECASE)
                    if mo:
                        ret = res.do_command('confirm', (mo.group('cookie'),))
                if ret == BADCMD and mm_cfg.DISCARD_MESSAGE_WITH_NO_COMMAND:
                    syslog('vette',
                           'No command, message discarded, msgid: %s',
                           msg.get('message-id', 'n/a'))
                    return False
                else:
                    res.send_response()
                    mlist.Save()
                    return False
            finally:
                mlist.Unlock()
                
        except Exception as e:
            syslog('error', 'CommandRunner._dispose: Error processing command message %s: %s\nTraceback:\n%s',
                   msgid, str(e), traceback.format_exc())
            self._shunt.enqueue(msg, msgdata)
            return False
        finally:
            if should_unlock:
                mlist.Unlock()

    def _oneloop(self):
        """Process one batch of messages from the command queue."""
        try:
            # Get the list of files to process
            files = self._switchboard.files()
            if not files:
                syslog('debug', 'CommandRunner: No files to process')
                return

            syslog('debug', 'CommandRunner: Processing %d files', len(files))
            
            # Process each file
            for filebase in files:
                try:
                    # Check if the file exists before dequeuing
                    pckfile = os.path.join(self.QDIR, filebase + '.pck')
                    if not os.path.exists(pckfile):
                        syslog('error', 'CommandRunner._oneloop: File %s does not exist, skipping', pckfile)
                        continue
                        
                    # Check if file is locked
                    lockfile = os.path.join(self.QDIR, filebase + '.pck.lock')
                    if os.path.exists(lockfile):
                        syslog('debug', 'CommandRunner._oneloop: File %s is locked by another process, skipping', filebase)
                        continue
                    
                    # Dequeue the file
                    msg, msgdata = self._switchboard.dequeue(filebase)
                    if msg is None:
                        syslog('debug', 'CommandRunner._oneloop: No message data for %s', filebase)
                        continue

                    # Get the list name from msgdata
                    listname = msgdata.get('listname')
                    if not listname:
                        syslog('error', 'CommandRunner._oneloop: No listname in message data for file %s', filebase)
                        self._shunt.enqueue(msg, msgdata)
                        continue
                        
                    # Open the list
                    try:
                        mlist = MailList.MailList(listname, lock=False)
                    except Errors.MMUnknownListError:
                        syslog('error', 'CommandRunner._oneloop: Unknown list %s for message %s (file: %s)',
                              listname, msg.get('message-id', 'n/a'), filebase)
                        self._shunt.enqueue(msg, msgdata)
                        continue

                    try:
                        # Process the message
                        self._dispose(mlist, msg, msgdata)
                        syslog('debug', 'CommandRunner: Successfully processed message %s', filebase)
                    except Exception as e:
                        syslog('error', 'CommandRunner: Error processing %s: %s', filebase, str(e))
                        syslog('error', 'CommandRunner: Traceback:\n%s', traceback.format_exc())
                        self._handle_error(e, msg, mlist)
                    finally:
                        mlist.Unlock()

                except Exception as e:
                    syslog('error', 'CommandRunner: Error processing file %s: %s', filebase, str(e))
                    syslog('error', 'CommandRunner: Traceback:\n%s', traceback.format_exc())
                    continue

        except Exception as e:
            syslog('error', 'CommandRunner: Error in _oneloop: %s', str(e))
            syslog('error', 'CommandRunner: Traceback:\n%s', traceback.format_exc())
            raise

# Set up i18n
_ = i18n._
i18n.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)
