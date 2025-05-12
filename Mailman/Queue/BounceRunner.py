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

"""Bounce queue runner."""

from builtins import object, str
import os
import re
import time
import pickle
import email
from email.utils import getaddresses
from email.iterators import body_line_iterator
from email.mime.text import MIMEText
from email.mime.message import MIMEMessage
from email.utils import parseaddr
import threading
import traceback

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import LockFile
from Mailman.Errors import NotAMemberError
from Mailman.Message import Message, UserNotification
from Mailman.Bouncer import _BounceInfo
from Mailman.Bouncers import BouncerAPI
from Mailman.Queue.Runner import Runner
from Mailman.Queue.sbcache import get_switchboard
from Mailman.Logging.Syslog import mailman_log
from Mailman.i18n import _

COMMASPACE = ', '

class BounceMixin:
    def __init__(self):
        # Registering a bounce means acquiring the list lock, and it would be
        # too expensive to do this for each message.  Instead, each bounce
        # runner maintains an event log which is essentially a file with
        # multiple pickles.  Each bounce we receive gets appended to this file
        # as a 4-tuple record: (listname, addr, today, msg)
        #
        # today is itself a 3-tuple of (year, month, day)
        #
        # Every once in a while (see _doperiodic()), the bounce runner cracks
        # open the file, reads all the records and registers all the bounces.
        # Then it truncates the file and continues on.  We don't need to lock
        # the bounce event file because bounce qrunners are single threaded
        # and each creates a uniquely named file to contain the events.
        self._bounce_events_file = os.path.join(
            mm_cfg.DATA_DIR, 'bounce-events-%05d.pck' % os.getpid())
        self._bounce_events_fp = None
        self._bouncecnt = 0
        self._nextaction = time.time() + mm_cfg.REGISTER_BOUNCES_EVERY
        self._max_bounce_size = 1024 * 1024  # 1MB max bounce size
        self._max_bounce_age = 7 * 24 * 3600  # 7 days max bounce age
        self._bounce_lock = threading.Lock()
        self._last_cleanup = time.time()
        self._cleanup_interval = 3600  # Clean up every hour

    def _cleanup_old_bounces(self):
        """Clean up old bounce files."""
        try:
            current_time = time.time()
            if current_time - self._last_cleanup < self._cleanup_interval:
                return
                
            with self._bounce_lock:
                # Clean up old bounce event files
                for filename in os.listdir(mm_cfg.DATA_DIR):
                    if filename.startswith('bounce-events-') and filename.endswith('.pck'):
                        filepath = os.path.join(mm_cfg.DATA_DIR, filename)
                        try:
                            if current_time - os.path.getmtime(filepath) > self._max_bounce_age:
                                os.unlink(filepath)
                        except OSError:
                            pass
                            
                # Clean up old bounce queue files
                for filename in os.listdir(mm_cfg.BOUNCEQUEUE_DIR):
                    if filename.endswith('.pck'):
                        filepath = os.path.join(mm_cfg.BOUNCEQUEUE_DIR, filename)
                        try:
                            if current_time - os.path.getmtime(filepath) > self._max_bounce_age:
                                os.unlink(filepath)
                        except OSError:
                            pass
                            
                self._last_cleanup = current_time
        except Exception as e:
            mailman_log('error', 'Error cleaning up old bounces: %s', str(e))

    def _validate_bounce(self, msg):
        """Validate bounce message format."""
        try:
            # Check required headers
            if not msg.get('from'):
                return False
            if not msg.get('to'):
                return False
            if not msg.get('message-id'):
                return False
                
            # Check message size
            if len(str(msg)) > self._max_bounce_size:
                return False
                
            # Check for valid bounce format
            if not msg.get('content-type', '').startswith('message/delivery-status'):
                return False
                
            return True
        except Exception:
            return False

    def _queue_bounces(self, listname, addrs, msg):
        """Queue bounce messages with proper validation and error handling."""
        if not self._validate_bounce(msg):
            mailman_log('error', 'Invalid bounce message format')
            return False
            
        today = time.localtime()[:3]
        try:
            with self._bounce_lock:
                if self._bounce_events_fp is None:
                    omask = os.umask(0o006)
                    try:
                        self._bounce_events_fp = open(self._bounce_events_file, 'ab')
                    finally:
                        os.umask(omask)
                        
                for addr in addrs:
                    # Use protocol 4 for Python 3 compatibility
                    pickle.dump((listname, addr, today, msg),
                              self._bounce_events_fp, protocol=4, fix_imports=True)
                self._bounce_events_fp.flush()
                os.fsync(self._bounce_events_fp.fileno())
                self._bouncecnt += len(addrs)
            return True
        except Exception as e:
            mailman_log('error', 'Error queueing bounces: %s', str(e))
            return False

    def _register_bounces(self, listname, addr, msg):
        """Register a bounce for a member with proper error handling."""
        try:
            # Create a unique filename
            now = time.time()
            filename = os.path.join(mm_cfg.BOUNCEQUEUE_DIR,
                                  '%d.%d.pck' % (os.getpid(), now))
            
            # Write the bounce data to the pickle file
            try:
                with open(filename, 'wb') as fp:
                    pickle.dump((listname, addr, now, msg), fp, protocol=4, fix_imports=True)
                os.chmod(filename, 0o660)
                return True
            except (IOError, OSError) as e:
                try:
                    os.unlink(filename)
                except (IOError, OSError):
                    pass
                mailman_log('error', 'Could not save bounce to %s: %s', filename, e)
                return False
        except Exception as e:
            mailman_log('error', 'Error registering bounce: %s', e)
            return False

    def _cleanup(self):
        """Clean up resources."""
        try:
            if self._bounce_events_fp is not None:
                self._bounce_events_fp.close()
                self._bounce_events_fp = None
            if self._bouncecnt > 0:
                self._register_bounces()
            self._cleanup_old_bounces()
        except Exception as e:
            mailman_log('error', 'Error in bounce cleanup: %s', str(e))

    def _doperiodic(self):
        """Periodic cleanup and processing."""
        now = time.time()
        if self._nextaction > now or self._bouncecnt == 0:
            return
        # Let's go ahead and register the bounces we've got stored up
        self._nextaction = now + mm_cfg.REGISTER_BOUNCES_EVERY
        self._register_bounces()
        self._cleanup_old_bounces()

    def _probe_bounce(self, mlist, token):
        """Process a probe bounce with proper error handling."""
        locked = mlist.Locked()
        if not locked:
            mlist.Lock()
        try:
            op, addr, bmsg = mlist.pend_confirm(token)
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
        except Exception as e:
            mailman_log('error', 'Error processing probe bounce: %s', str(e))
        finally:
            if not locked:
                mlist.Unlock()


class BounceRunner(Runner, BounceMixin):
    QDIR = mm_cfg.BOUNCEQUEUE_DIR

    def __init__(self, slice=None, numslices=1):
        Runner.__init__(self, slice, numslices)
        BounceMixin.__init__(self)

    def _dispose(self, mlist, msg, msgdata):
        """Process a bounce message with proper validation and error handling."""
        msgid = msg.get('message-id', 'n/a')
        filebase = msgdata.get('_filebase', 'unknown')
        
        # Validate bounce message
        if not self._validate_bounce(msg):
            mailman_log('error', 'Invalid bounce message format for %s', msgid)
            return False

        # Make sure we have the most up-to-date state
        try:
            mlist.Load()
        except Errors.MMCorruptListDatabaseError as e:
            mailman_log('error', 'Failed to load list %s: %s',
                       mlist.internal_name(), e)
            return False
        except Exception as e:
            mailman_log('error', 'Unexpected error loading list %s: %s',
                       mlist.internal_name(), e)
            return False

        try:
            # Log start of processing
            mailman_log('info', 'BounceRunner: Starting to process bounce message %s (file: %s) for list %s',
                       msgid, filebase, mlist.internal_name())
            
            # Process the bounce
            if not self._register_bounces(mlist.internal_name(), msg, msgdata):
                return False

            # Queue the bounce for further processing
            try:
                self._queue_bounces(mlist.internal_name(), msg, msgdata)
            except Exception as e:
                mailman_log('error', 'Error queueing bounces: %s', e)
                return False

            # Log successful completion
            mailman_log('info', 'BounceRunner: Successfully processed bounce message %s (file: %s) for list %s',
                       msgid, filebase, mlist.internal_name())
            return True
        except Exception as e:
            # Enhanced error logging with more context
            mailman_log('error', 'Error processing bounce message %s for list %s: %s',
                   msgid, mlist.internal_name(), str(e))
            mailman_log('error', 'Message details:')
            mailman_log('error', '  Message ID: %s', msgid)
            mailman_log('error', '  From: %s', msg.get('from', 'unknown'))
            mailman_log('error', '  To: %s', msg.get('to', 'unknown'))
            mailman_log('error', '  Subject: %s', msg.get('subject', '(no subject)'))
            mailman_log('error', '  Message type: %s', type(msg).__name__)
            mailman_log('error', '  Message data: %s', str(msgdata))
            mailman_log('error', 'Traceback:\n%s', traceback.format_exc())
            return False

    _doperiodic = BounceMixin._doperiodic

    def _cleanup(self):
        BounceMixin._cleanup(self)
        Runner._cleanup(self)


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
                mailman_log('error', "VERP_REGEXP doesn't yield the right match groups: %s",
                           mm_cfg.VERP_REGEXP)
                continue
            except Exception as e:
                mailman_log('error', "Error processing VERP bounce: %s", str(e))
                continue
    except Exception as e:
        mailman_log('error', "Error in verp_bounce: %s", str(e))
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
            mailman_log(
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
        mailman_log('bounce',
               '%s: forwarding unrecognized, message-id: %s',
               mlist.internal_name(),
               msg.get('message-id', 'n/a'))
    else:
        mailman_log('bounce',
               '%s: discarding unrecognized, message-id: %s',
               mlist.internal_name(),
               msg.get('message-id', 'n/a'))
