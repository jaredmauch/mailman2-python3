# Copyright (C) 2000-2018 by the Free Software Foundation, Inc.
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

"""Outgoing queue runner."""

from builtins import object
import time
import socket
import smtplib
import traceback
import os
import sys
from io import StringIO

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
from Mailman import i18n
from Mailman.Message import Message
from Mailman.Logging.Syslog import mailman_log
from Mailman.Queue.Runner import Runner
from Mailman.Queue.Switchboard import Switchboard
from Mailman.Queue.BounceRunner import BounceMixin

# This controls how often _doperiodic() will try to deal with deferred
# permanent failures.  It is a count of calls to _doperiodic()
DEAL_WITH_PERMFAILURES_EVERY = 10

class OutgoingRunner(Runner, BounceMixin):
    QDIR = mm_cfg.OUTQUEUE_DIR

    def __init__(self, slice=None, numslices=1):
        mailman_log('debug', 'OutgoingRunner: Starting initialization')
        try:
            Runner.__init__(self, slice, numslices)
            mailman_log('debug', 'OutgoingRunner: Base Runner initialized')
            
            BounceMixin.__init__(self)
            mailman_log('debug', 'OutgoingRunner: BounceMixin initialized')
            
            # We look this function up only at startup time
            self._modname = 'Mailman.Handlers.' + mm_cfg.DELIVERY_MODULE
            mailman_log('debug', 'OutgoingRunner: Attempting to import delivery module: %s', self._modname)
            
            try:
                mod = __import__(self._modname)
                mailman_log('debug', 'OutgoingRunner: Successfully imported delivery module')
            except ImportError as e:
                mailman_log('error', 'OutgoingRunner: Failed to import delivery module %s: %s', self._modname, str(e))
                mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
                raise
                
            try:
                self._func = getattr(sys.modules[self._modname], 'process')
                mailman_log('debug', 'OutgoingRunner: Successfully got process function from module')
            except AttributeError as e:
                mailman_log('error', 'OutgoingRunner: Failed to get process function from module %s: %s', self._modname, str(e))
                mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
                raise
            
            # This prevents smtp server connection problems from filling up the
            # error log.  It gets reset if the message was successfully sent, and
            # set if there was a socket.error.
            self.__logged = False
            mailman_log('debug', 'OutgoingRunner: Initializing retry queue')
            self.__retryq = Switchboard(mm_cfg.RETRYQUEUE_DIR)
            mailman_log('debug', 'OutgoingRunner: Initialization complete')
        except Exception as e:
            mailman_log('error', 'OutgoingRunner: Initialization failed: %s', str(e))
            mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
            raise

    def _dispose(self, mlist, msg, msgdata):
        """Deliver a message to its intended recipients."""
        msgid = msg.get('message-id', 'n/a')
        sender = msg.get('from', 'n/a')
        subject = msg.get('subject', 'n/a')
        mailman_log('info', 'OutgoingRunner: Starting delivery - msgid: %s, list: %s, sender: %s, subject: %s',
                   msgid, mlist.internal_name(), sender, subject)

        # See if we should retry delivery of this message again
        deliver_after = msgdata.get('deliver_after', 0)
        if time.time() < deliver_after:
            mailman_log('debug', 'OutgoingRunner: Message not ready for delivery yet, waiting until %s',
                       time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(deliver_after)))
            return True

        # Make sure we have the most up-to-date state
        mailman_log('debug', 'OutgoingRunner: Loading list state')
        mlist.Load()

        try:
            pid = os.getpid()
            mailman_log('debug', 'OutgoingRunner: Attempting to deliver message')
            self._func(mlist, msg, msgdata)
            # Failsafe -- a child may have leaked through
            if pid != os.getpid():
                mailman_log('error', 'OutgoingRunner: child process leaked thru: %s', self._modname)
                os._exit(1)
            self.__logged = False
            mailman_log('info', 'OutgoingRunner: Message delivered successfully - msgid: %s, list: %s',
                   msgid, mlist.internal_name())
            return False

        except socket.error:
            # There was a problem connecting to the SMTP server.  Log this
            # once, but crank up our sleep time so we don't fill the error
            # log.
            port = mm_cfg.SMTPPORT
            if port == 0:
                port = 'smtp'
            # Log this just once.
            if not self.__logged:
                mailman_log('error', 'OutgoingRunner: Cannot connect to SMTP server %s on port %s for msgid: %s',
                       mm_cfg.SMTPHOST, port, msgid)
                self.__logged = True
            self._snooze(0)
            return True

        except Errors.SomeRecipientsFailed as e:
            mailman_log('info', 'OutgoingRunner: Some recipients failed for msgid: %s - %s', msgid, str(e))
            # Handle local rejects of probe messages differently.
            if msgdata.get('probe_token') and e.permfailures:
                mailman_log('debug', 'OutgoingRunner: Handling probe bounce for msgid: %s', msgid)
                self._probe_bounce(mlist, msgdata['probe_token'])
            else:
                # Delivery failed at SMTP time for some or all of the
                # recipients.  Permanent failures are registered as bounces,
                # but temporary failures are retried for later.
                if e.permfailures:
                    mailman_log('info', 'OutgoingRunner: Queueing permanent failures as bounces for msgid: %s', msgid)
                    self._queue_bounces(mlist.internal_name(), e.permfailures, msg)
                # Move temporary failures to the qfiles/retry queue which will
                # occasionally move them back here for another shot at
                # delivery.
                if e.tempfailures:
                    mailman_log('info', 'OutgoingRunner: Queueing temporary failures for retry for msgid: %s', msgid)
                    now = time.time()
                    recips = e.tempfailures
                    last_recip_count = msgdata.get('last_recip_count', 0)
                    deliver_until = msgdata.get('deliver_until', now)
                    if len(recips) == last_recip_count:
                        # We didn't make any progress, so don't attempt
                        # delivery any longer.  BAW: is this the best
                        # disposition?
                        if now > deliver_until:
                            mailman_log('info', 'OutgoingRunner: No progress made, giving up on msgid: %s', msgid)
                            return False
                    else:
                        # Keep trying to delivery this message for a while
                        deliver_until = now + mm_cfg.DELIVERY_RETRY_PERIOD
                    # Don't retry delivery too soon.
                    deliver_after = now + mm_cfg.DELIVERY_RETRY_WAIT
                    msgdata['deliver_after'] = deliver_after
                    msgdata['last_recip_count'] = len(recips)
                    msgdata['deliver_until'] = deliver_until
                    msgdata['recips'] = recips
                    self.__retryq.enqueue(msg, msgdata)

        except Exception as e:
            mailman_log('error', 'OutgoingRunner: Unexpected error during message processing for msgid: %s - %s', msgid, str(e))
            mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
            raise
        # We've successfully completed handling of this message
        return False

    def _queue_bounces(self, mlist, msg, msgdata, failures):
        """Queue bounce messages for failed deliveries."""
        msgid = msg.get('message-id', 'n/a')
        try:
            for recip, code, errmsg in failures:
                mailman_log('error', 'OutgoingRunner: Delivery failure for msgid: %s - Recipient: %s, Code: %s, Error: %s',
                           msgid, recip, code, errmsg)
                BounceMixin._queue_bounce(self, mlist, msg, recip, code, errmsg)
        except Exception as e:
            mailman_log('error', 'OutgoingRunner: Error queueing bounce for msgid: %s - %s', msgid, str(e))
            mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())

    def _cleanup(self):
        """Clean up resources."""
        mailman_log('debug', 'OutgoingRunner: Starting cleanup')
        BounceMixin._cleanup(self)
        Runner._cleanup(self)
        mailman_log('debug', 'OutgoingRunner: Cleanup complete')

    _doperiodic = BounceMixin._doperiodic
