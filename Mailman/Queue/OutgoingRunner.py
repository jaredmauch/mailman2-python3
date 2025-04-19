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

"""Outgoing queue runner.

This module provides the OutgoingRunner class which handles the delivery of
outgoing messages to recipients. It manages SMTP connections, retries failed
deliveries, and handles bounce messages.
"""

import os
import sys
import copy
import time
import socket
import logging
from typing import Any, Dict, List, Optional, Tuple, Union, NoReturn, Type

import email
from email.message import Message

from Mailman import mm_cfg
from Mailman import Message as MailmanMessage
from Mailman import Errors
from Mailman import LockFile
from Mailman.Queue.Runner import Runner
from Mailman.Queue.Switchboard import Switchboard
from Mailman.Queue.BounceRunner import BounceMixin
from Mailman.Logging.Syslog import syslog

# This controls how often _doperiodic() will try to deal with deferred
# permanent failures.  It is a count of calls to _doperiodic()
DEAL_WITH_PERMFAILURES_EVERY = 10

try:
    import dns.resolver
    from dns.exception import DNSException
    dns_resolver = True
except ImportError:
    dns_resolver = False


class OutgoingRunner(Runner, BounceMixin):
    """Runner for outgoing messages.
    
    This class handles the delivery of outgoing messages to recipients.
    It inherits from Runner and BounceMixin to provide message delivery
    and bounce handling functionality.
    
    Attributes:
        QDIR: The directory for outgoing messages
        logger: Logger instance for this class
        __retryq: Switchboard for retry queue
        __logged: Flag to prevent duplicate logging
    """
    
    QDIR: str = mm_cfg.OUTQUEUE_DIR

    def __init__(self, slice: Optional[int] = None, numslices: int = 1) -> None:
        """Initialize the outgoing runner.
        
        Args:
            slice: Optional slice number for parallel processing
            numslices: Total number of slices for parallel processing
            
        Raises:
            ImportError: If the delivery module cannot be imported
        """
        Runner.__init__(self, slice, numslices)
        BounceMixin.__init__(self)
        # We look this function up only at startup time
        modname = f'Mailman.Handlers.{mm_cfg.DELIVERY_MODULE}'
        try:
            mod = __import__(modname)
            self._func = getattr(sys.modules[modname], 'process')
        except (ImportError, AttributeError) as e:
            self.logger.error('Failed to import delivery module %s: %s', 
                            modname, e)
            raise ImportError(f'Cannot import delivery module {modname}: {e}')
            
        # This prevents smtp server connection problems from filling up the
        # error log.  It gets reset if the message was successfully sent, and
        # set if there was a socket.error.
        self.__logged: bool = False
        self.__retryq: Switchboard = Switchboard(mm_cfg.RETRYQUEUE_DIR)
        self.logger: logging.Logger = logging.getLogger('mailman.outgoing')

    def _dispose(self, mlist: Any, msg: Message, msgdata: Dict[str, Any]) -> bool:
        """Dispose of a message by delivering it or retrying.
        
        Args:
            mlist: The mailing list object
            msg: The email message
            msgdata: Additional message metadata
            
        Returns:
            bool: True if message should be retried, False if handled
            
        Raises:
            socket.error: If there are SMTP connection problems
            Errors.SomeRecipientsFailed: If delivery fails for some recipients
            OSError: If there are process-related errors
        """
        # See if we should retry delivery of this message again.
        deliver_after = msgdata.get('deliver_after', 0)
        if time.time() < deliver_after:
            return True
            
        # Make sure we have the most up-to-date state
        try:
            mlist.Load()
        except Exception as e:
            self.logger.error('Failed to load mailing list: %s', e)
            return True
        
        try:
            pid = os.getpid()
            self._func(mlist, msg, msgdata)
            # Failsafe -- a child may have leaked through.
            if pid != os.getpid():
                self.logger.error('Child process leaked through: %s', modname)
                syslog('error', 'child process leaked thru: %s', modname)
                os._exit(1)
            self.__logged = False
            
        except socket.error as e:
            # There was a problem connecting to the SMTP server.  Log this
            # once, but crank up our sleep time so we don't fill the error
            # log.
            port = mm_cfg.SMTPPORT
            if port == 0:
                port = 'smtp'
            # Log this just once.
            if not self.__logged:
                self.logger.error('Cannot connect to SMTP server %s on port %s: %s',
                                mm_cfg.SMTPHOST, port, e)
                syslog('error', 'Cannot connect to SMTP server %s on port %s: %s',
                       mm_cfg.SMTPHOST, port, e)
                self.__logged = True
            self._snooze(0)
            return True
            
        except Errors.SomeRecipientsFailed as e:
            # Handle local rejects of probe messages differently.
            if msgdata.get('probe_token') and e.permfailures:
                self._probe_bounce(mlist, msgdata['probe_token'])
            else:
                # Delivery failed at SMTP time for some or all of the
                # recipients.  Permanent failures are registered as bounces,
                # but temporary failures are retried for later.
                if e.permfailures:
                    self._queue_bounces(mlist.internal_name(), e.permfailures,
                                        msg)
                # Move temporary failures to the qfiles/retry queue which will
                # occasionally move them back here for another shot at
                # delivery.
                if e.tempfailures:
                    now = time.time()
                    recips = e.tempfailures
                    last_recip_count = msgdata.get('last_recip_count', 0)
                    deliver_until = msgdata.get('deliver_until', now)
                    if len(recips) == last_recip_count:
                        # We didn't make any progress, so don't attempt
                        # delivery any longer.
                        if now > deliver_until:
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
                    try:
                        self.__retryq.enqueue(msg, msgdata)
                    except Exception as e:
                        self.logger.error('Failed to enqueue message for retry: %s', e)
                        return True
        # We've successfully completed handling of this message
        return False

    _doperiodic = BounceMixin._doperiodic

    def _cleanup(self) -> None:
        """Clean up resources.
        
        This method ensures that all resources are properly cleaned up when
        the runner is shut down. It calls the cleanup methods of both the
        BounceMixin and Runner parent classes.
        """
        try:
            BounceMixin._cleanup(self)
            Runner._cleanup(self)
        except Exception as e:
            self.logger.error('Error during cleanup: %s', e)
            raise
