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

This module handles the bounce queue for messages that have bounced back
to the list. It processes bounces and updates member bounce information.
"""

import os
import re
import time
import pickle
import logging
from typing import Any, Dict, List, Optional, Tuple, Union, NoReturn, Iterator

from email.mime.text import MIMEText
from email.mime.message import MIMEMessage
from email.utils import parseaddr
from email.message import Message

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import LockFile
from Mailman.Errors import NotAMemberError
from Mailman.Message import UserNotification
from Mailman.Bouncer import _BounceInfo
from Mailman.Bouncers import BouncerAPI
from Mailman.Queue.Runner import Runner
from Mailman.Queue.sbcache import get_switchboard
from Mailman.Logging.Syslog import syslog
from Mailman.i18n import _

COMMASPACE = ', '


class BounceMixin:
    """Mixin class for bounce handling.
    
    This class provides bounce handling functionality for runners that
    need to process bounce messages.
    
    Attributes:
        _bounce_events_file: Path to the bounce events file
        _bounce_events_fp: File pointer for bounce events
        _bouncecnt: Count of queued bounces
        _nextaction: Time of next bounce processing
        logger: Logger instance for this class
    """
    
    def __init__(self) -> None:
        """Initialize the bounce mixin.
        
        Sets up the bounce event file and counters.
        """
        self._bounce_events_file = os.path.join(
            mm_cfg.DATA_DIR, f'bounce-events-{os.getpid():05d}.pck')
        self._bounce_events_fp: Optional[Any] = None
        self._bouncecnt: int = 0
        self._nextaction: float = time.time() + mm_cfg.REGISTER_BOUNCES_EVERY
        self.logger: logging.Logger = logging.getLogger('mailman.bounce')

    def _queue_bounces(self, listname: str, addrs: List[str], msg: Message) -> None:
        """Queue bounce events for later processing.
        
        Args:
            listname: Name of the list
            addrs: List of addresses that bounced
            msg: The bounce message
            
        Raises:
            IOError: If the bounce events file cannot be opened
        """
        today = time.localtime()[:3]
        if self._bounce_events_fp is None:
            omask = os.umask(0o006)
            try:
                self._bounce_events_fp = open(self._bounce_events_file, 'a+b')
            except IOError as e:
                self.logger.error('Failed to open bounce events file: %s', e)
                raise
            finally:
                os.umask(omask)
        try:
            for addr in addrs:
                pickle.dump((listname, addr, today, msg),
                           self._bounce_events_fp, 1)
            self._bounce_events_fp.flush()
            os.fsync(self._bounce_events_fp.fileno())
            self._bouncecnt += len(addrs)
        except Exception as e:
            self.logger.error('Failed to queue bounce events: %s', e)
            raise

    def _register_bounces(self) -> None:
        """Register all queued bounces with their respective lists.
        
        Raises:
            IOError: If the bounce events file cannot be read
            pickle.UnpicklingError: If the bounce events file is corrupted
        """
        self.logger.info('Processing %s queued bounces', self._bouncecnt)
        syslog('bounce', '%s processing %s queued bounces',
               self, self._bouncecnt)
        # Read all the records from the bounce file, then unlink it.  Sort the
        # records by listname for more efficient processing.
        events: Dict[str, List[Tuple[str, Tuple[int, int, int], Message]]] = {}
        try:
            self._bounce_events_fp.seek(0)
            while True:
                try:
                    listname, addr, day, msg = pickle.load(
                        self._bounce_events_fp, fix_imports=True, encoding='latin1')
                except ValueError as e:
                    self.logger.error('Error reading bounce events: %s', e)
                    syslog('bounce', 'Error reading bounce events: %s', e)
                    continue
                except EOFError:
                    break
                events.setdefault(listname, []).append((addr, day, msg))
        except Exception as e:
            self.logger.error('Failed to read bounce events: %s', e)
            raise
        finally:
            if self._bounce_events_fp is not None:
                self._bounce_events_fp.close()
                self._bounce_events_fp = None
                try:
                    os.unlink(self._bounce_events_file)
                except OSError as e:
                    self.logger.error('Failed to unlink bounce events file: %s', e)
            self._bouncecnt = 0

        # Now register all events sorted by list
        for listname in list(events.keys()):
            try:
                mlist = self._open_list(listname)
                mlist.Lock()
                try:
                    for addr, day, msg in events[listname]:
                        mlist.registerBounce(addr, msg, day=day)
                    mlist.Save()
                finally:
                    mlist.Unlock()
            except Exception as e:
                self.logger.error('Failed to register bounces for list %s: %s',
                                listname, e)
                continue

    def _cleanup(self) -> None:
        """Clean up any remaining bounce events.
        
        This method ensures that all remaining bounce events are processed
        before the runner is shut down.
        """
        if self._bouncecnt > 0:
            try:
                self._register_bounces()
            except Exception as e:
                self.logger.error('Error during bounce cleanup: %s', e)
                raise

    def _doperiodic(self) -> None:
        """Periodically process queued bounces.
        
        This method is called periodically to process any queued bounces.
        It checks if it's time to process bounces and if there are any
        bounces to process.
        """
        now = time.time()
        if self._nextaction > now or self._bouncecnt == 0:
            return
        # Let's go ahead and register the bounces we've got stored up
        self._nextaction = now + mm_cfg.REGISTER_BOUNCES_EVERY
        try:
            self._register_bounces()
        except Exception as e:
            self.logger.error('Error during periodic bounce processing: %s', e)
            raise

    def _probe_bounce(self, mlist: Any, token: str) -> None:
        """Process a probe bounce message.
        
        Args:
            mlist: The mailing list object
            token: The probe token
            
        Raises:
            NotAMemberError: If the member is no longer in the list
        """
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
                self.logger.info('Member %s was removed before probe bounce returned',
                               addr)
        except Exception as e:
            self.logger.error('Error processing probe bounce: %s', e)
            raise
        finally:
            if not locked:
                mlist.Unlock()


class BounceRunner(Runner, BounceMixin):
    """Runner for bounce queue.
    
    This class handles the bounce queue for messages that have bounced back
    to the list. It processes bounces and updates member bounce information.
    
    Attributes:
        QDIR: The directory for bounce messages
        logger: Logger instance for this class
    """
    
    QDIR: str = mm_cfg.BOUNCEQUEUE_DIR

    def __init__(self, slice: Optional[int] = None, numslices: int = 1) -> None:
        """Initialize the bounce runner.
        
        Args:
            slice: Optional slice number for parallel processing
            numslices: Total number of slices for parallel processing
        """
        Runner.__init__(self, slice, numslices)
        BounceMixin.__init__(self)
        self.logger = logging.getLogger('mailman.bounce')

    def _dispose(self, mlist: Any, msg: Message, msgdata: Dict[str, Any]) -> bool:
        """Dispose of a bounce message.
        
        Args:
            mlist: The mailing list object
            msg: The bounce message
            msgdata: Additional message metadata
            
        Returns:
            bool: True if message should be retried, False if handled
            
        Raises:
            IOError: If the message cannot be processed
            NotAMemberError: If the member is no longer in the list
        """
        # Make sure we have the most up-to-date state
        try:
            mlist.Load()
        except Exception as e:
            self.logger.error('Failed to load mailing list: %s', e)
            return True
            
        outq = get_switchboard(mm_cfg.OUTQUEUE_DIR)
        # There are a few possibilities here:
        #
        # - the message could have been VERP'd in which case, we know exactly
        #   who the message was destined for.  That make our job easy.
        # - the message could have been originally destined for a list owner,
        #   but a list owner address itself bounced.  That's bad, and for now
        #   we'll simply attempt to deliver the message to the site list
        #   owner.
        #   Note that this means that automated bounce processing doesn't work
        #   for the site list.  Because we can't reliably tell to what address
        #   a non-VERP'd bounce was originally sent, we have to treat all
        #   bounces sent to the site list as potential list owner bounces.
        # - the list owner could have set list-bounces (or list-admin) as the
        #   owner address.  That's really bad as it results in a loop of ever
        #   growing unrecognized bounce messages.  We detect this based on the
        #   address.  We then send this to the site list owner instead.
        # Notices to list-owner have their envelope sender and From: set to
        # the site-bounces address.  Check if this is this a bounce for a
        # message to a list owner, coming to site-bounces, or a looping
        # message sent directly to the -bounces address.  We have to do these
        # cases separately, because sending to site-owner will reset the
        # envelope sender.
        # Is this a site list bounce?
        if (mlist.internal_name().lower() ==
                mm_cfg.MAILMAN_SITE_LIST.lower()):
            # Send it on to the site owners, but craft the envelope sender to
            # be the -loop detection address, so if /they/ bounce, we won't
            # get stuck in a bounce loop.
            try:
                outq.enqueue(msg, msgdata,
                           recips=mlist.owner,
                           envsender=Utils.get_site_email(extra='loop'),
                           nodecorate=1,
                           )
            except Exception as e:
                self.logger.error('Failed to enqueue site list bounce: %s', e)
                return True
            return False
        # Is this a possible looping message sent directly to a list-bounces
        # address other than the site list?
        # Check From: because unix_from might be VERP'd.
        # Also, check the From: that Message.OwnerNotification uses.
        if (msg.get('from') ==
                Utils.get_site_email(mlist.host_name, 'bounces')):
            # Just send it to the sitelist-owner address.  If that bounces
            # we'll handle it above.
            try:
                outq.enqueue(msg, msgdata,
                           recips=[Utils.get_site_email(extra='owner')],
                           envsender=Utils.get_site_email(extra='loop'),
                           nodecorate=1,
                           )
            except Exception as e:
                self.logger.error('Failed to enqueue loop bounce: %s', e)
                return True
            return False
        # List isn't doing bounce processing?
        if not mlist.bounce_processing:
            return False
        # Try VERP detection first, since it's quick and easy
        addrs = verp_bounce(mlist, msg)
        if addrs:
            # We have an address, but check if the message is non-fatal.
            if BouncerAPI.ScanMessages(mlist, msg) is BouncerAPI.Stop:
                return False
        else:
            # See if this was a probe message.
            token = verp_probe(mlist, msg)
            if token:
                try:
                    self._probe_bounce(mlist, token)
                except Exception as e:
                    self.logger.error('Failed to process probe bounce: %s', e)
                    return True
                return False
            # That didn't give us anything useful, so try the old fashion
            # bounce matching modules.
            addrs = BouncerAPI.ScanMessages(mlist, msg)
            if addrs is BouncerAPI.Stop:
                # This is a recognized, non-fatal notice. Ignore it.
                return False
        # If that still didn't return us any useful addresses, then send it on
        # or discard it.
        addrs = [_f for _f in addrs if _f]
        if not addrs:
            self.logger.warning('Bounce message with no discernable addresses: %s',
                              msg.get('message-id', 'n/a'))
            syslog('bounce',
                   '%s: bounce message w/no discernable addresses: %s',
                   mlist.internal_name(),
                   msg.get('message-id', 'n/a'))
            try:
                maybe_forward(mlist, msg)
            except Exception as e:
                self.logger.error('Failed to forward bounce message: %s', e)
                return True
            return False
        try:
            self._queue_bounces(mlist.internal_name(), addrs, msg)
        except Exception as e:
            self.logger.error('Failed to queue bounces: %s', e)
            return True
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


def verp_bounce(mlist, msg):
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
        mo = re.search(mm_cfg.VERP_REGEXP, to)
        if not mo:
            continue                          # no match of regexp
        try:
            if bmailbox != mo.group('bounces'):
                continue                      # not a bounce to our list
            # All is good
            addr = '%s@%s' % mo.group('mailbox', 'host')
        except IndexError:
            syslog('error',
                   "VERP_REGEXP doesn't yield the right match groups: %s",
                   mm_cfg.VERP_REGEXP)
            return []
        return [addr]


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
