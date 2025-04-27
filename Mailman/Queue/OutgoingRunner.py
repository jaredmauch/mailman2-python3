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

import Mailman.mm_cfg
import Mailman.Message
import Mailman.Errors
import Mailman.LockFile
import Mailman.Queue.Runner
import Mailman.Queue.Switchboard
import Mailman.Queue.BounceRunner
import Mailman.Logging.Syslog

# This controls how often _doperiodic() will try to deal with deferred
# permanent failures.  It is a count of calls to _doperiodic()
DEAL_WITH_PERMFAILURES_EVERY = 10


class OutgoingRunner(Mailman.Queue.Runner.Runner, Mailman.Queue.BounceRunner.BounceMixin):
    QDIR = Mailman.mm_cfg.OUTQUEUE_DIR

    def __init__(self, slice=None, numslices=1):
        Mailman.Logging.Syslog.mailman_log('debug', 'OutgoingRunner: Starting initialization')
        try:
            Mailman.Queue.Runner.Runner.__init__(self, slice, numslices)
            Mailman.Logging.Syslog.mailman_log('debug', 'OutgoingRunner: Base Runner initialized')
            
            Mailman.Queue.BounceRunner.BounceMixin.__init__(self)
            Mailman.Logging.Syslog.mailman_log('debug', 'OutgoingRunner: BounceMixin initialized')
            
            # We look this function up only at startup time
            self._modname = 'Mailman.Handlers.' + Mailman.mm_cfg.DELIVERY_MODULE
            Mailman.Logging.Syslog.mailman_log('debug', 'OutgoingRunner: Attempting to import delivery module: %s', self._modname)
            
            try:
                mod = __import__(self._modname)
                Mailman.Logging.Syslog.mailman_log('debug', 'OutgoingRunner: Successfully imported delivery module')
            except ImportError as e:
                Mailman.Logging.Syslog.mailman_log('error', 'OutgoingRunner: Failed to import delivery module %s: %s', self._modname, str(e))
                Mailman.Logging.Syslog.mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
                raise
                
            try:
                self._func = getattr(sys.modules[self._modname], 'process')
                Mailman.Logging.Syslog.mailman_log('debug', 'OutgoingRunner: Successfully got process function from module')
            except AttributeError as e:
                Mailman.Logging.Syslog.mailman_log('error', 'OutgoingRunner: Failed to get process function from module %s: %s', self._modname, str(e))
                Mailman.Logging.Syslog.mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
                raise
                
            # This prevents smtp server connection problems from filling up the
            # error log.  It gets reset if the message was successfully sent, and
            # set if there was a socket.error.
            self.__logged = False
            Mailman.Logging.Syslog.mailman_log('debug', 'OutgoingRunner: Initializing retry queue')
            self.__retryq = Mailman.Queue.Switchboard.Switchboard(Mailman.mm_cfg.RETRYQUEUE_DIR)
            Mailman.Logging.Syslog.mailman_log('debug', 'OutgoingRunner: Initialization complete')
        except Exception as e:
            Mailman.Logging.Syslog.mailman_log('error', 'OutgoingRunner: Initialization failed: %s', str(e))
            Mailman.Logging.Syslog.mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
            raise

    def _dispose(self, mlist, msg, msgdata):
        # Log message details before processing
        msgid = msg.get('message-id', 'n/a')
        sender = msg.get('from', 'n/a')
        subject = msg.get('subject', 'n/a')
        Mailman.Logging.Syslog.mailman_log('info', 'OutgoingRunner: Starting delivery - msgid: %s, list: %s, sender: %s, subject: %s',
               msgid, mlist.internal_name(), sender, subject)
               
        # See if we should retry delivery of this message again.
        deliver_after = msgdata.get('deliver_after', 0)
        if time.time() < deliver_after:
            Mailman.Logging.Syslog.mailman_log('debug', 'OutgoingRunner: Message not ready for delivery yet, waiting until %s', 
                   time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(deliver_after)))
            return True
        # Make sure we have the most up-to-date state
        Mailman.Logging.Syslog.mailman_log('debug', 'OutgoingRunner: Loading list state')
        mlist.Load()
        try:
            pid = os.getpid()
            Mailman.Logging.Syslog.mailman_log('debug', 'OutgoingRunner: Attempting to deliver message')
            self._func(mlist, msg, msgdata)
            # Failsafe -- a child may have leaked through.
            if pid != os.getpid():
                Mailman.Logging.Syslog.mailman_log('error', 'OutgoingRunner: child process leaked thru: %s', self._modname)
                os._exit(1)
            self.__logged = False
            Mailman.Logging.Syslog.mailman_log('info', 'OutgoingRunner: Message delivered successfully - msgid: %s, list: %s',
                   msgid, mlist.internal_name())
        except socket.error:
            # There was a problem connecting to the SMTP server.  Log this
            # once, but crank up our sleep time so we don't fill the error
            # log.
            port = Mailman.mm_cfg.SMTPPORT
            if port == 0:
                port = 'smtp'
            # Log this just once.
            if not self.__logged:
                Mailman.Logging.Syslog.mailman_log('error', 'OutgoingRunner: Cannot connect to SMTP server %s on port %s for msgid: %s',
                       Mailman.mm_cfg.SMTPHOST, port, msgid)
                self.__logged = True
            self._snooze(0)
            return True
        except Mailman.Errors.SomeRecipientsFailed as e:
            Mailman.Logging.Syslog.mailman_log('info', 'OutgoingRunner: Some recipients failed for msgid: %s - %s', msgid, str(e))
            # Handle local rejects of probe messages differently.
            if msgdata.get('probe_token') and e.permfailures:
                Mailman.Logging.Syslog.mailman_log('debug', 'OutgoingRunner: Handling probe bounce for msgid: %s', msgid)
                self._probe_bounce(mlist, msgdata['probe_token'])
            else:
                # Delivery failed at SMTP time for some or all of the
                # recipients.  Permanent failures are registered as bounces,
                # but temporary failures are retried for later.
                if e.permfailures:
                    Mailman.Logging.Syslog.mailman_log('info', 'OutgoingRunner: Queueing permanent failures as bounces for msgid: %s', msgid)
                    self._queue_bounces(mlist.internal_name(), e.permfailures,
                                        msg)
                # Move temporary failures to the qfiles/retry queue which will
                # occasionally move them back here for another shot at
                # delivery.
                if e.tempfailures:
                    Mailman.Logging.Syslog.mailman_log('info', 'OutgoingRunner: Queueing temporary failures for retry for msgid: %s', msgid)
                    now = time.time()
                    recips = e.tempfailures
                    last_recip_count = msgdata.get('last_recip_count', 0)
                    deliver_until = msgdata.get('deliver_until', now)
                    if len(recips) == last_recip_count:
                        # We didn't make any progress, so don't attempt
                        # delivery any longer.  BAW: is this the best
                        # disposition?
                        if now > deliver_until:
                            Mailman.Logging.Syslog.mailman_log('info', 'OutgoingRunner: No progress made, giving up on msgid: %s', msgid)
                            return False
                    else:
                        # Keep trying to delivery this message for a while
                        deliver_until = now + Mailman.mm_cfg.DELIVERY_RETRY_PERIOD
                    # Don't retry delivery too soon.
                    deliver_after = now + Mailman.mm_cfg.DELIVERY_RETRY_WAIT
                    msgdata['deliver_after'] = deliver_after
                    msgdata['last_recip_count'] = len(recips)
                    msgdata['deliver_until'] = deliver_until
                    msgdata['recips'] = recips
                    self.__retryq.enqueue(msg, msgdata)
        except Exception as e:
            Mailman.Logging.Syslog.mailman_log('error', 'OutgoingRunner: Unexpected error during message processing for msgid: %s - %s', msgid, str(e))
            Mailman.Logging.Syslog.mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
            raise
        # We've successfully completed handling of this message
        return False

    _doperiodic = Mailman.Queue.BounceRunner.BounceMixin._doperiodic

    def _cleanup(self):
        Mailman.Logging.Syslog.mailman_log('debug', 'OutgoingRunner: Starting cleanup')
        Mailman.Queue.BounceRunner.BounceMixin._cleanup(self)
        Mailman.Queue.Runner.Runner._cleanup(self)
        Mailman.Logging.Syslog.mailman_log('debug', 'OutgoingRunner: Cleanup complete')
