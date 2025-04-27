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
            
            self._delivery = None
            self._load_delivery_module()
            
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

    def _load_delivery_module(self):
        """Load the delivery module with proper error handling."""
        try:
            module = Utils.import_module(mm_cfg.DELIVERY_MODULE)
            self._delivery = module.Delivery()
            mailman_log('debug', 'OutgoingRunner: Successfully loaded delivery module')
        except Exception as e:
            mailman_log('error', 'OutgoingRunner: Failed to load delivery module: %s', str(e))
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

            # Get the list of recipients
            recips = msgdata.get('recips', [])
            if not recips:
                mailman_log('warning', 'OutgoingRunner: No recipients for msgid: %s', msgid)
                return False

            # Deliver the message
            failures = self._delivery.deliver(mlist, msg, recips)
            
            # Failsafe -- a child may have leaked through
            if pid != os.getpid():
                mailman_log('error', 'OutgoingRunner: child process leaked thru')
                os._exit(1)

            if failures:
                # Handle delivery failures
                mailman_log('error', 'OutgoingRunner: Failed to deliver to %d recipients for msgid: %s',
                           len(failures), msgid)

                # Handle probe messages differently
                if msgdata.get('probe_token') and failures:
                    mailman_log('debug', 'OutgoingRunner: Handling probe bounce for msgid: %s', msgid)
                    self._probe_bounce(mlist, msgdata['probe_token'])
                    return False

                # Process permanent and temporary failures
                perm_failures = [(r, c, e) for r, c, e in failures if c.startswith('5')]
                temp_failures = [(r, c, e) for r, c, e in failures if c.startswith('4')]

                if perm_failures:
                    mailman_log('info', 'OutgoingRunner: Queueing permanent failures as bounces for msgid: %s', msgid)
                    self._queue_bounces(mlist, msg, msgdata, perm_failures)

                if temp_failures:
                    mailman_log('info', 'OutgoingRunner: Queueing temporary failures for retry for msgid: %s', msgid)
                    now = time.time()
                    last_recip_count = msgdata.get('last_recip_count', 0)
                    deliver_until = msgdata.get('deliver_until', now)

                    if len(temp_failures) == last_recip_count:
                        if now > deliver_until:
                            mailman_log('info', 'OutgoingRunner: No progress made, giving up on msgid: %s', msgid)
                            return False
                    else:
                        deliver_until = now + mm_cfg.DELIVERY_RETRY_PERIOD

                    deliver_after = now + mm_cfg.DELIVERY_RETRY_WAIT
                    msgdata['deliver_after'] = deliver_after
                    msgdata['last_recip_count'] = len(temp_failures)
                    msgdata['deliver_until'] = deliver_until
                    msgdata['recips'] = [r for r, _, _ in temp_failures]
                    self.__retryq.enqueue(msg, msgdata)
                    return True

                return False

            # Log successful delivery
            self.__logged = False
            mailman_log('info', 'OutgoingRunner: Successfully delivered msgid: %s to %d recipients',
                       msgid, len(recips))
            return False

        except smtplib.SMTPException as e:
            # Handle SMTP-specific errors
            if not self.__logged:
                mailman_log('error', 'OutgoingRunner: SMTP error for msgid: %s - %s', msgid, str(e))
                self.__logged = True
            self._snooze(0)
            return True  # Retry on temporary SMTP errors

        except socket.error as e:
            # Handle network errors
            if not self.__logged:
                mailman_log('error', 'OutgoingRunner: Network error for msgid: %s - %s', msgid, str(e))
                self.__logged = True
            self._snooze(0)
            return True  # Retry on network errors

        except Exception as e:
            # Handle other unexpected errors
            mailman_log('error', 'OutgoingRunner: Unexpected error for msgid: %s - %s', msgid, str(e))
            mailman_log('error', 'OutgoingRunner: Traceback: %s', traceback.format_exc())
            return True  # Retry on other errors

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
        if self._delivery:
            try:
                self._delivery.cleanup()
            except Exception as e:
                mailman_log('error', 'OutgoingRunner: Error during delivery cleanup: %s', str(e))
        mailman_log('debug', 'OutgoingRunner: Cleanup complete')

    _doperiodic = BounceMixin._doperiodic
