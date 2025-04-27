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
        """Deliver the message to the list's members."""
        try:
            # Deliver the message
            mlist.deliver(msg, msgdata)
        except Exception as e:
            # Get message details for better error reporting
            msgid = msg.get('message-id', 'n/a')
            sender = msg.get('from', 'unknown')
            subject = msg.get('subject', 'no subject')
            
            # Log detailed error information
            mailman_log('error', 
                'Error delivering message to list %s\n'
                'Message-ID: %s\n'
                'From: %s\n'
                'Subject: %s\n'
                'Error: %s\n'
                'Potential causes:\n'
                '1. List configuration error\n'
                '2. SMTP server connection issue\n'
                '3. Message format error\n'
                '4. Permission denied\n'
                '5. Network timeout',
                mlist.internal_name(), msgid, sender, subject, str(e))
            raise

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
