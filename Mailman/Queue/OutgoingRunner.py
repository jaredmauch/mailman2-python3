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

import os
import sys
import copy
import time
import socket
import traceback

import email

from Mailman import mm_cfg
from Mailman import Message
from Mailman import Errors
from Mailman import LockFile
from Mailman.Queue.Runner import Runner
from Mailman.Queue.Switchboard import Switchboard
from Mailman.Queue.BounceRunner import BounceMixin
from Mailman.Logging.Syslog import syslog

# This controls how often _doperiodic() will try to deal with deferred
# permanent failures.  It is a count of calls to _doperiodic()
DEAL_WITH_PERMFAILURES_EVERY = 10


class OutgoingRunner(Runner, BounceMixin):
    QDIR = mm_cfg.OUTQUEUE_DIR

    def __init__(self, slice=None, numslices=1):
        syslog('debug', 'OutgoingRunner: Starting initialization')
        try:
            Runner.__init__(self, slice, numslices)
            syslog('debug', 'OutgoingRunner: Base Runner initialized')
            
            BounceMixin.__init__(self)
            syslog('debug', 'OutgoingRunner: BounceMixin initialized')
            
            # We look this function up only at startup time
            modname = 'Mailman.Handlers.' + mm_cfg.DELIVERY_MODULE
            syslog('debug', 'OutgoingRunner: Attempting to import delivery module: %s', modname)
            
            try:
                mod = __import__(modname)
                syslog('debug', 'OutgoingRunner: Successfully imported delivery module')
            except ImportError as e:
                syslog('error', 'OutgoingRunner: Failed to import delivery module %s: %s', modname, str(e))
                syslog('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
                raise
                
            try:
                self._func = getattr(sys.modules[modname], 'process')
                syslog('debug', 'OutgoingRunner: Successfully got process function from module')
            except AttributeError as e:
                syslog('error', 'OutgoingRunner: Failed to get process function from module %s: %s', modname, str(e))
                syslog('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
                raise
                
            # This prevents smtp server connection problems from filling up the
            # error log.  It gets reset if the message was successfully sent, and
            # set if there was a socket.error.
            self.__logged = False
            syslog('debug', 'OutgoingRunner: Initializing retry queue')
            self.__retryq = Switchboard(mm_cfg.RETRYQUEUE_DIR)
            syslog('debug', 'OutgoingRunner: Initialization complete')
        except Exception as e:
            syslog('error', 'OutgoingRunner: Initialization failed: %s', str(e))
            syslog('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
            raise

    def _dispose(self, mlist, msg, msgdata):
        syslog('debug', 'OutgoingRunner: Processing message for list %s', mlist.internal_name())
        # See if we should retry delivery of this message again.
        deliver_after = msgdata.get('deliver_after', 0)
        if time.time() < deliver_after:
            syslog('debug', 'OutgoingRunner: Message not ready for delivery yet, waiting until %s', 
                   time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(deliver_after)))
            return True
        # Make sure we have the most up-to-date state
        syslog('debug', 'OutgoingRunner: Loading list state')
        mlist.Load()
        try:
            pid = os.getpid()
            syslog('debug', 'OutgoingRunner: Attempting to deliver message')
            self._func(mlist, msg, msgdata)
            # Failsafe -- a child may have leaked through.
            if pid != os.getpid():
                syslog('error', 'OutgoingRunner: child process leaked thru: %s', modname)
                os._exit(1)
            self.__logged = False
            syslog('debug', 'OutgoingRunner: Message delivered successfully')
        except socket.error:
            # There was a problem connecting to the SMTP server.  Log this
            # once, but crank up our sleep time so we don't fill the error
            # log.
            port = mm_cfg.SMTPPORT
            if port == 0:
                port = 'smtp'
            # Log this just once.
            if not self.__logged:
                syslog('error', 'OutgoingRunner: Cannot connect to SMTP server %s on port %s',
                       mm_cfg.SMTPHOST, port)
                self.__logged = True
            self._snooze(0)
            return True
        except Errors.SomeRecipientsFailed as e:
            syslog('debug', 'OutgoingRunner: Some recipients failed: %s', str(e))
            # Handle local rejects of probe messages differently.
            if msgdata.get('probe_token') and e.permfailures:
                syslog('debug', 'OutgoingRunner: Handling probe bounce')
                self._probe_bounce(mlist, msgdata['probe_token'])
            else:
                # Delivery failed at SMTP time for some or all of the
                # recipients.  Permanent failures are registered as bounces,
                # but temporary failures are retried for later.
                if e.permfailures:
                    syslog('debug', 'OutgoingRunner: Queueing permanent failures as bounces')
                    self._queue_bounces(mlist.internal_name(), e.permfailures,
                                        msg)
                # Move temporary failures to the qfiles/retry queue which will
                # occasionally move them back here for another shot at
                # delivery.
                if e.tempfailures:
                    syslog('debug', 'OutgoingRunner: Queueing temporary failures for retry')
                    now = time.time()
                    recips = e.tempfailures
                    last_recip_count = msgdata.get('last_recip_count', 0)
                    deliver_until = msgdata.get('deliver_until', now)
                    if len(recips) == last_recip_count:
                        # We didn't make any progress, so don't attempt
                        # delivery any longer.  BAW: is this the best
                        # disposition?
                        if now > deliver_until:
                            syslog('debug', 'OutgoingRunner: No progress made, giving up on message')
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
            syslog('error', 'OutgoingRunner: Unexpected error during message processing: %s', str(e))
            syslog('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
            raise
        # We've successfully completed handling of this message
        return False

    _doperiodic = BounceMixin._doperiodic

    def _cleanup(self):
        syslog('debug', 'OutgoingRunner: Starting cleanup')
        BounceMixin._cleanup(self)
        Runner._cleanup(self)
        syslog('debug', 'OutgoingRunner: Cleanup complete')
