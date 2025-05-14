# Copyright (C) 2001-2018 by the Free Software Foundation, Inc.
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

"""Bounce queue runner.

This module is responsible for processing bounce messages.
"""

from builtins import object, str
import os
import re
import time
import pickle
import email
from email.utils import getaddresses
from email.iterators import body_line_iterator
import traceback
from io import StringIO
import sys

from email.mime.text import MIMEText
from email.mime.message import MIMEMessage
from email.utils import parseaddr

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import LockFile
from Mailman import Errors
from Mailman import i18n
from Mailman.Errors import NotAMemberError
from Mailman.Message import Message, UserNotification
from Mailman.Bouncer import _BounceInfo
from Mailman.Bouncers import BouncerAPI
from Mailman.Queue.Runner import Runner
from Mailman.Queue.sbcache import get_switchboard
from Mailman.Logging.Syslog import syslog
from Mailman.i18n import _

# Lazy import to avoid circular dependency
def get_mail_list():
    from Mailman.MailList import MailList
    return MailList

COMMASPACE = ', '

class BounceMixin:
    def __init__(self):
        """Initialize the bounce mixin."""
        self._bouncecnt = 0
        self._next_action = time.time()
        syslog('debug', 'BounceMixin: Initialized with next action time: %s',
               time.ctime(self._next_action))

    def _register_bounces(self, mlist, bounces):
        """Register bounce information for a list."""
        try:
            for address, info in bounces.items():
                syslog('debug', 'BounceMixin._register_bounces: Registering bounce for list %s, address %s',
                       mlist.internal_name(), address)
                
                # Write bounce data to file
                filename = os.path.join(mlist.bounce_dir, address)
                try:
                    with open(filename, 'w') as fp:
                        fp.write(str(info))
                    syslog('debug', 'BounceMixin._register_bounces: Successfully wrote bounce data to %s', filename)
                except Exception as e:
                    syslog('error', 'BounceMixin._register_bounces: Failed to write bounce data to %s: %s\nTraceback:\n%s',
                           filename, str(e), traceback.format_exc())
                    continue
                
        except Exception as e:
            syslog('error', 'BounceMixin._register_bounces: Error registering bounce: %s\nTraceback:\n%s',
                   str(e), traceback.format_exc())

    def _cleanup(self):
        """Clean up bounce processing."""
        try:
            syslog('debug', 'BounceMixin._cleanup: Processing %d pending bounces', self._bouncecnt)
            # ... cleanup logic ...
        except Exception as e:
            syslog('error', 'BounceMixin._cleanup: Error during cleanup: %s', str(e))

    def _doperiodic(self):
        """Do periodic bounce processing."""
        try:
            now = time.time()
            if now >= self._next_action:
                syslog('debug', 'BounceMixin._doperiodic: Processing bounces, next action scheduled for %s',
                       time.ctime(self._next_action))
                # ... periodic processing logic ...
        except Exception as e:
            syslog('error', 'BounceMixin._doperiodic: Error during periodic processing: %s', str(e))

    def _queue_bounces(self, listname, addrs, msg):
        today = time.localtime()[:3]
        if self._bounce_events_fp is None:
            omask = os.umask(0o006)
            try:
                self._bounce_events_fp = open(self._bounce_events_file, 'ab')
            finally:
                os.umask(omask)
        for addr in addrs:
            # Use protocol 4 for Python 3 compatibility and fix_imports for Python 2/3 compatibility
            pickle.dump((listname, addr, today, msg),
                       self._bounce_events_fp, protocol=4, fix_imports=True)
        self._bounce_events_fp.flush()
        os.fsync(self._bounce_events_fp.fileno())
        self._bouncecnt += len(addrs)

    def _probe_bounce(self, mlist, token):
        locked = mlist.Locked()
        if not locked:
            mlist.Lock()
        try:
            op, addr, bmsg = mlist.pend_confirm(token)
            # For Python 2.4 compatibility we need an inner try because
            # try: ... except: ... finally: requires Python 2.5+
            try:
                info = mlist.getBounceInfo(addr)
                if not info:
                    # info was deleted before probe bounce was received.
                    # Just create a new info.
                    info = _BounceInfo(addr,
                                       0.0,
                                       time.localtime()[:3],
                                       mlist.bounce_you_are_disabled_warnings
                                       )
                mlist.disableBouncingMember(addr, info, bmsg)
                # Only save the list if we're unlocking it
                if not locked:
                    mlist.Save()
            except NotAMemberError:
                # Member was removed before probe bounce returned.
                # Just ignore it.
                pass
        finally:
            if not locked:
                mlist.Unlock()


class BounceRunner(Runner, BounceMixin):
    QDIR = mm_cfg.BOUNCEQUEUE_DIR

    # Enable message tracking for bounce messages
    _track_messages = True
    _max_processed_messages = 10000
    _max_retry_times = 10000
    
    # Retry configuration
    MIN_RETRY_DELAY = 300  # 5 minutes minimum delay between retries
    MAX_RETRIES = 5  # Maximum number of retry attempts
    _retry_times = {}  # Track last retry time for each message
    
    # Cleanup configuration
    _cleanup_interval = 3600  # Clean up every hour
    _last_cleanup = 0  # Last cleanup time

    def __init__(self, slice=None, numslices=1):
        syslog('debug', 'BounceRunner: Starting initialization')
        try:
            Runner.__init__(self, slice, numslices)
            BounceMixin.__init__(self)
            
            # Initialize bounce events file
            self._bounce_events_file = os.path.join(mm_cfg.DATA_DIR, 'bounce_events')
            self._bounce_events_fp = None
            
            # Initialize processed messages tracking
            self._processed_messages = set()
            self._last_cleanup = time.time()
            
            syslog('debug', 'BounceRunner: Initialization complete')
        except Exception as e:
            syslog('error', 'BounceRunner: Initialization failed: %s\nTraceback:\n%s',
                   str(e), traceback.format_exc())
            raise

    def _dispose(self, mlist, msg, msgdata):
        """Process a bounce message."""
        try:
            # Get the message ID
            msgid = msg.get('message-id', 'n/a')
            filebase = msgdata.get('_filebase', 'unknown')
            
            # Ensure we have a MailList object
            if isinstance(mlist, str):
                try:
                    mlist = get_mail_list()(mlist, lock=0)
                    should_unlock = True
                except Errors.MMUnknownListError:
                    syslog('error', 'BounceRunner: Unknown list %s', mlist)
                    self._shunt.enqueue(msg, msgdata)
                    return True
            else:
                should_unlock = False
            
            try:
                syslog('debug', 'BounceRunner._dispose: Starting to process bounce message %s (file: %s) for list %s',
                       msgid, filebase, mlist.internal_name())
                
                # Check retry delay
                if not self._check_retry_delay(msgid, filebase):
                    syslog('debug', 'BounceRunner._dispose: Message %s failed retry delay check, skipping', msgid)
                    return True
                
                # Process the bounce
                # ... bounce processing logic ...
                
                return False
                
            finally:
                if should_unlock:
                    mlist.Unlock()
                
        except Exception as e:
            syslog('error', 'BounceRunner._dispose: Error processing bounce message %s: %s\nTraceback:\n%s',
                   msgid, str(e), traceback.format_exc())
            return True

    def _extract_bounce_info(self, msg):
        """Extract bounce information from a message."""
        try:
            # Log the message structure for debugging
            syslog('debug', 'BounceRunner._extract_bounce_info: Message structure:')
            syslog('debug', '  Headers: %s', dict(msg.items()))
            syslog('debug', '  Content-Type: %s', msg.get('content-type', 'unknown'))
            syslog('debug', '  Is multipart: %s', msg.is_multipart())

            # Extract bounce information based on message structure
            bounce_info = {}
            
            # Try to get recipient from various headers
            for header in ['X-Failed-Recipients', 'X-Original-To', 'To']:
                if msg.get(header):
                    bounce_info['recipient'] = msg[header]
                    syslog('debug', 'BounceRunner._extract_bounce_info: Found recipient in %s header: %s',
                              header, bounce_info['recipient'])
                    break

            # Try to get error information
            if msg.is_multipart():
                for part in msg.get_payload():
                    if part.get_content_type() == 'message/delivery-status':
                        bounce_info['error'] = part.get_payload()
                        syslog('debug', 'BounceRunner._extract_bounce_info: Found delivery status in multipart message')
                        break

            if not bounce_info.get('recipient'):
                syslog('error', 'BounceRunner._extract_bounce_info: Could not find recipient in bounce message')
                return None

            return bounce_info

        except Exception as e:
            syslog('error', 'BounceRunner._extract_bounce_info: Error extracting bounce information: %s\nTraceback:\n%s',
                       str(e), traceback.format_exc())
            return None

    def _cleanup(self):
        """Clean up resources."""
        syslog('debug', 'BounceRunner: Starting cleanup')
        try:
            BounceMixin._cleanup(self)
            Runner._cleanup(self)
        except Exception as e:
            syslog('error', 'BounceRunner: Cleanup failed: %s\nTraceback:\n%s',
                       str(e), traceback.format_exc())
        syslog('debug', 'BounceRunner: Cleanup complete')

    _doperiodic = BounceMixin._doperiodic


def verp_bounce(mlist, msg):
    try:
        bmailbox, bdomain = Utils.ParseEmail(mlist.GetBouncesEmail())
        vals = []
        for header in ('to', 'delivered-to', 'envelope-to', 'apparently-to'):
            vals.extend(msg.get_all(header, []))
        for field in vals:
            to = parseaddr(field)[1]
            if not to:
                continue
            try:
                mo = re.search(mm_cfg.VERP_REGEXP, to)
                if not mo:
                    continue
                if bmailbox != mo.group('bounces'):
                    continue
                addr = '%s@%s' % mo.group('mailbox', 'host')
                return [addr]
            except IndexError:
                syslog('error', "VERP_REGEXP doesn't yield the right match groups: %s",
                           mm_cfg.VERP_REGEXP)
                continue
            except Exception as e:
                syslog('error', "Error processing VERP bounce: %s", str(e))
                continue
    except Exception as e:
        syslog('error', "Error in verp_bounce: %s", str(e))
    return []


def verp_probe(mlist, msg):
    bmailbox, bdomain = Utils.ParseEmail(mlist.GetBouncesEmail())
    # Sadly not every MTA bounces VERP messages correctly, or consistently.
    # Fall back to Delivered-To: (Postfix), Envelope-To: (Exim) and
    # Apparently-To:, and then short-circuit if we still don't have anything
    # to work with.  Note that there can be multiple Delivered-To: headers so
    # we need to search them all (and we don't worry about false positives for
    # forwarded email, because only one should match VERP_REGEXP).
    vals = []
    for header in ('to', 'delivered-to', 'envelope-to', 'apparently-to'):
        vals.extend(msg.get_all(header, []))
    for field in vals:
        to = parseaddr(field)[1]
        if not to:
            continue                          # empty header
        mo = re.search(mm_cfg.VERP_PROBE_REGEXP, to)
        if not mo:
            continue                          # no match of regexp
        try:
            if bmailbox != mo.group('bounces'):
                continue                      # not a bounce to our list
            # Extract the token and see if there's an entry
            token = mo.group('token')
            data = mlist.pend_confirm(token, expunge=False)
            if data is not None:
                return token
        except IndexError:
            syslog(
                'error',
                "VERP_PROBE_REGEXP doesn't yield the right match groups: %s",
                mm_cfg.VERP_PROBE_REGEXP)
    return None


def maybe_forward(mlist, msg):
    # Does the list owner want to get non-matching bounce messages?
    # If not, simply discard it.
    if mlist.bounce_unrecognized_goes_to_list_owner:
        adminurl = mlist.GetScriptURL('admin', absolute=1) + '/bounce'
        mlist.ForwardMessage(msg,
                             text=_("""\
The attached message was received as a bounce, but either the bounce format
was not recognized, or no member addresses could be extracted from it.  This
mailing list has been configured to send all unrecognized bounce messages to
the list administrator(s).

For more information see:
%(adminurl)s

"""),
                             subject=_('Uncaught bounce notification'),
                             tomoderators=0)
        syslog('bounce',
               '%s: forwarding unrecognized, message-id: %s',
               mlist.internal_name(),
               msg.get('message-id', 'n/a'))
    else:
        syslog('bounce',
               '%s: discarding unrecognized, message-id: %s',
               mlist.internal_name(),
               msg.get('message-id', 'n/a'))
