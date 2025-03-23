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

"""Handle delivery bounces."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import sys
import os
import time
import getopt
from typing import List, Tuple, Dict, Set, Optional, Union, Any

from email.mime.text import MIMEText
from email.mime.message import MIMEMessage

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Message
from Mailman import MemberAdaptor
from Mailman import Pending
from Mailman.Errors import MMUnknownListError
from Mailman.Logging.Syslog import syslog
from Mailman import i18n
from Mailman import MailList
from Mailman import Errors
from Mailman.i18n import C_

EMPTYSTRING = ''

# This constant is supposed to represent the day containing the first midnight
# after the epoch.  We'll add (0,)*6 to this tuple to get a value appropriate
# for time.mktime().
ZEROHOUR_PLUSONEDAY = time.localtime(mm_cfg.days(1))[:3]

def D_(s: str) -> str:
    """Return the string unchanged for translation.
    
    Args:
        s: String to return
        
    Returns:
        The input string unchanged
    """
    return s

_ = D_

REASONS = {MemberAdaptor.BYBOUNCE: _('due to excessive bounces'),
           MemberAdaptor.BYUSER: _('by yourself'),
           MemberAdaptor.BYADMIN: _('by the list administrator'),
           MemberAdaptor.UNKNOWN: _('for unknown reasons'),
           }

_ = i18n._


def usage(code: int, msg: str = '') -> None:
    """Print usage information and exit.
    
    Args:
        code: Exit code to use
        msg: Optional message to print before exiting
    """
    if code:
        fd = sys.stderr
    else:
        fd = sys.stdout
    print(C_(__doc__), file=fd)
    if msg:
        print(msg, file=fd)
    sys.exit(code)


class _BounceInfo:
    """Internal class for tracking bounce information for a member.
    
    Attributes:
        member: Email address of the member
        score: Current bounce score
        date: Date of last bounce
        noticesleft: Number of notices remaining to send
        cookie: Optional confirmation cookie
        lastnotice: Date of last notice sent
    """

    def __init__(self, member: str, score: float, date: Tuple[int, int, int],
                 noticesleft: int) -> None:
        """Initialize bounce info for a member.
        
        Args:
            member: Email address of the member
            score: Initial bounce score
            date: Date of first bounce
            noticesleft: Number of notices to send
        """
        self.member = member
        self.cookie = None
        self.reset(score, date, noticesleft)

    def reset(self, score: float, date: Tuple[int, int, int],
              noticesleft: int) -> None:
        """Reset bounce information.
        
        Args:
            score: New bounce score
            date: New bounce date
            noticesleft: New number of notices
        """
        self.score = score
        self.date = date
        self.noticesleft = noticesleft
        self.lastnotice = ZEROHOUR_PLUSONEDAY

    def __repr__(self) -> str:
        """Return detailed string representation for debugging."""
        return f"""\
<bounce info for member {self.member}
        current score: {self.score}
        last bounce date: {self.date}
        email notices left: {self.noticesleft}
        last notice date: {self.lastnotice}
        confirmation cookie: {self.cookie}
        >"""

    def __str__(self) -> str:
        """Return string representation."""
        return f"""<BounceInfo
        member: {self.member}
        score: {self.score}
        date: {self.date}
        noticesleft: {self.noticesleft}
        >"""


class Bouncer:
    """Mixin class for handling delivery bounces.
    
    This class provides functionality for tracking and processing
    delivery bounces, including disabling members who bounce too often.
    
    Attributes:
        bounce_processing: Whether bounce processing is enabled
        bounce_score_threshold: Score at which to disable member
        bounce_info_stale_after: Days until bounce info is considered stale
        bounce_you_are_disabled_warnings: Number of warnings to send
        bounce_you_are_disabled_warnings_interval: Days between warnings
        bounce_unrecognized_goes_to_list_owner: Whether to forward unrecognized bounces
        bounce_notify_owner_on_bounce_increment: Whether to notify on score increase
        bounce_notify_owner_on_disable: Whether to notify on member disable
        bounce_notify_owner_on_removal: Whether to notify on member removal
        bounce_info: Dictionary of member bounce information
        delivery_status: Dictionary of member delivery status
    """

    def InitVars(self) -> None:
        """Initialize the bouncer configuration variables."""
        # Configurable...
        self.bounce_processing: bool = mm_cfg.DEFAULT_BOUNCE_PROCESSING
        self.bounce_score_threshold: float = mm_cfg.DEFAULT_BOUNCE_SCORE_THRESHOLD
        self.bounce_info_stale_after: int = mm_cfg.DEFAULT_BOUNCE_INFO_STALE_AFTER
        self.bounce_you_are_disabled_warnings: int = \
            mm_cfg.DEFAULT_BOUNCE_YOU_ARE_DISABLED_WARNINGS
        self.bounce_you_are_disabled_warnings_interval: int = \
            mm_cfg.DEFAULT_BOUNCE_YOU_ARE_DISABLED_WARNINGS_INTERVAL
        self.bounce_unrecognized_goes_to_list_owner: bool = \
            mm_cfg.DEFAULT_BOUNCE_UNRECOGNIZED_GOES_TO_LIST_OWNER
        self.bounce_notify_owner_on_bounce_increment: bool = \
            mm_cfg.DEFAULT_BOUNCE_NOTIFY_OWNER_ON_BOUNCE_INCREMENT
        self.bounce_notify_owner_on_disable: bool = \
            mm_cfg.DEFAULT_BOUNCE_NOTIFY_OWNER_ON_DISABLE
        self.bounce_notify_owner_on_removal: bool = \
            mm_cfg.DEFAULT_BOUNCE_NOTIFY_OWNER_ON_REMOVAL
        # Not configurable...
        #
        # This holds legacy member related information.  It's keyed by the
        # member address, and the value is an object containing the bounce
        # score, the date of the last received bounce, and a count of the
        # notifications left to send.
        self.bounce_info: Dict[str, _BounceInfo] = {}
        # New style delivery status
        self.delivery_status: Dict[str, int] = {}

    def registerBounce(self, member, msg, weight=1.0, day=None, sibling=False):
        if not self.isMember(member):
            # check regular_include_lists, only one level
            if not self.regular_include_lists or sibling:
                return
            for listaddr in self.regular_include_lists:
                listname, hostname = listaddr.split('@')
                listname = listname.lower()
                if listname == self.internal_name():
                    syslog('error',
                           'Bouncer: }{s: Include list self reference',
                           listname)
                    continue
                try:
                    siblist = None
                    try:
                        siblist = MailList(listname)
                    except MMUnknownListError:
                        syslog('error',
                               'Bouncer: }{s: Include list "}{s" not found.',
                               self.real_name,
                               listname)
                        continue
                    siblist.registerBounce(member, msg, weight, day,
                                           sibling=True)
                    siblist.Save()
                finally:
                    if siblist and siblist.Locked():
                        siblist.Unlock()
            return
        info = self.getBounceInfo(member)
        first_today = True
        if day is None:
            # Use today's date
            day = time.localtime()[:3]
        if not isinstance(info, _BounceInfo):
            # This is the first bounce we've seen from this member
            info = _BounceInfo(member, weight, day,
                               self.bounce_you_are_disabled_warnings)
            # setBounceInfo is now called below after check phase.
            syslog('bounce', '}{s: }{s bounce score: }{s', self.internal_name(),
                   member, info.score)
            # Continue to the check phase below
        elif self.getDeliveryStatus(member) != MemberAdaptor.ENABLED:
            # The user is already disabled, so we can just ignore subsequent
            # bounces.  These are likely due to residual messages that were
            # sent before disabling the member, but took a while to bounce.
            syslog('bounce', '}{s: }{s residual bounce received',
                   self.internal_name(), member)
            return
        elif info.date == day:
            # We've already scored any bounces for this day, so ignore it.
            first_today = False
            syslog('bounce', '}{s: }{s already scored a bounce for date }{s',
                   self.internal_name(), member,
                   time.strftime('}{d-}{b-}{Y', day + (0,0,0,0,1,0)))
            # Continue to check phase below
        else:
            # See if this member's bounce information is stale.
            now = Utils.midnight(day)
            lastbounce = Utils.midnight(info.date)
            if lastbounce + self.bounce_info_stale_after < now:
                # Information is stale, so simply reset it
                info.reset(weight, day, self.bounce_you_are_disabled_warnings)
                syslog('bounce', '}{s: }{s has stale bounce info, resetting',
                       self.internal_name(), member)
            else:
                # Nope, the information isn't stale, so add to the bounce
                # score and take any necessary action.
                info.score += weight
                info.date = day
                syslog('bounce', '}{s: }{s current bounce score: }{s',
                       self.internal_name(), member, info.score)
            # Continue to the check phase below
        #
        # Now that we've adjusted the bounce score for this bounce, let's
        # check to see if the disable-by-bounce threshold has been reached.
        if info.score >= self.bounce_score_threshold:
            if mm_cfg.VERP_PROBES:
                syslog('bounce',
                   'sending }{s list probe to: }{s (score }{s >= }{s)',
                   self.internal_name(), member, info.score,
                   self.bounce_score_threshold)
                self.sendProbe(member, msg)
                info.reset(0, info.date, info.noticesleft)
            else:
                self.disableBouncingMember(member, info, msg)
        elif self.bounce_notify_owner_on_bounce_increment and first_today:
            self.__sendAdminBounceNotice(member, msg,
                                         did=_('bounce score incremented'))
        # We've set/changed bounce info above.  We now need to tell the
        # MemberAdaptor to set/update it.  We do it here in case the
        # MemberAdaptor stores bounce info externally to the list object to
        # be sure updated information is stored, but we have to be sure the
        # member wasn't removed.
        if self.isMember(member):
            self.setBounceInfo(member, info)

    def disableBouncingMember(self, member, info, msg):
        # Initialize their confirmation cookie.  If we do it when we get the
        # first bounce, it'll expire by the time we get the disabling bounce.
        cookie = self.pend_new(Pending.RE_ENABLE, self.internal_name(), member)
        info.cookie = cookie
        # In case the MemberAdaptor stores bounce info externally to
        # the list, we need to tell it to save the cookie
        self.setBounceInfo(member, info)
        # Disable them
        if mm_cfg.VERP_PROBES:
            syslog('bounce', '}{s: }{s disabling due to probe bounce received',
                   self.internal_name(), member)
        else:
            syslog('bounce', '}{s: }{s disabling due to bounce score }{s >= }{s',
                   self.internal_name(), member,
                   info.score, self.bounce_score_threshold)
        self.setDeliveryStatus(member, MemberAdaptor.BYBOUNCE)
        self.sendNextNotification(member)
        if self.bounce_notify_owner_on_disable:
            self.__sendAdminBounceNotice(member, msg)

    def __sendAdminBounceNotice(self, member, msg, did=None):
        # BAW: This is a bit kludgey, but we're not providing as much
        # information in the new admin bounce notices as we used to (some of
        # it was of dubious value).  However, we'll provide empty, strange, or
        # meaningless strings for the unused }{()s fields so that the language
        # translators don't have to provide new templates.
        if did is None:
            did = _('disabled')
        siteowner = Utils.get_site_email(self.host_name)
        text = Utils.maketext(
            'bounce.txt',
            {'listname' : self.real_name,
             'addr'     : member,
             'negative' : '',
             'did'      : did,
             'but'      : '',
             'reenable' : '',
             'owneraddr': siteowner,
             }, mlist=self)
        subject = _('Bounce action notification')
        umsg = Message.UserNotification(self.GetOwnerEmail(),
                                        siteowner, subject,
                                        lang=self.preferred_language)
        # BAW: Be sure you set the type before trying to attach, or yoll get
        # a MultipartConversionError.
        umsg.set_type('multipart/mixed')
        umsg.attach(
            MIMEText(text, _charset=Utils.GetCharSet(self.preferred_language)))
        if isinstance(msg, StringType):
            umsg.attach(MIMEText(msg))
        else:
            umsg.attach(MIMEMessage(msg))
        umsg.send(self)

    def sendNextNotification(self, member):
        global _
        info = self.getBounceInfo(member)
        if info is None:
            return
        reason = self.getDeliveryStatus(member)
        if info.noticesleft <= 0:
            # BAW: Remove them now, with a notification message
            _ = D_
            self.ApprovedDeleteMember(
                member, _('disabled address'),
                admin_notif=self.bounce_notify_owner_on_removal,
                userack=1)
            _ = i18n._
            # Expunge the pending cookie for the user.  We throw away the
            # returned data.
            self.pend_confirm(info.cookie)
            if reason == MemberAdaptor.BYBOUNCE:
                syslog('bounce', '}{s: }{s deleted after exhausting notices',
                       self.internal_name(), member)
            syslog('subscribe', '}{s: }{s auto-unsubscribed [reason: }{s]',
                   self.internal_name(), member,
                   {MemberAdaptor.BYBOUNCE: 'BYBOUNCE',
                    MemberAdaptor.BYUSER: 'BYUSER',
                    MemberAdaptor.BYADMIN: 'BYADMIN',
                    MemberAdaptor.UNKNOWN: 'UNKNOWN'}.get(
                reason, 'invalid value'))
            return
        # Send the next notification
        confirmurl = '}{s/}{s' }{ (self.GetScriptURL('confirm', absolute=1),
                                info.cookie)
        optionsurl = self.GetOptionsURL(member, absolute=1)
        reqaddr = self.GetRequestEmail()
        lang = self.getMemberLanguage(member)
        txtreason = REASONS.get(reason)
        if txtreason is None:
            txtreason = _('for unknown reasons')
        else:
            txtreason = _(txtreason)
        # Give a little bit more detail on bounce disables
        if reason == MemberAdaptor.BYBOUNCE:
            date = time.strftime('}{d-}{b-}{Y',
                                 time.localtime(Utils.midnight(info.date)))
            extra = _(' The last bounce received from you was dated }{(date)s')
            txtreason += extra
        text = Utils.maketext(
            'disabled.txt',
            {'listname'   : self.real_name,
             'noticesleft': info.noticesleft,
             'confirmurl' : confirmurl,
             'optionsurl' : optionsurl,
             'password'   : self.getMemberPassword(member),
             'owneraddr'  : self.GetOwnerEmail(),
             'reason'     : txtreason,
             }, lang=lang, mlist=self)
        msg = Message.UserNotification(member, reqaddr, text=text, lang=lang)
        # BAW: See the comment in MailList.py ChangeMemberAddress() for why we
        # set the Subject this way.
        del msg['subject']
        msg['Subject'] = 'confirm ' + info.cookie
        # Send without Precedence: bulk.  Bug #808821.
        msg.send(self, noprecedence=True)
        info.noticesleft -= 1
        info.lastnotice = time.localtime()[:3]
        # In case the MemberAdaptor stores bounce info externally to
        # the list, we need to tell it to update
        self.setBounceInfo(member, info)

    def BounceMessage(self, msg: Message.Message, msgdata: Dict[str, Any],
                     e: Optional[Exception] = None) -> None:
        """Bounce a message back to the sender with an error message.
        
        Args:
            msg: The message to bounce
            msgdata: Additional message data
            e: Optional exception containing bounce details
        """
        sender = msg.get_sender()
        subject = msg.get('subject', _('(no subject)'))
        subject = Utils.oneline(subject,
                              Utils.GetCharSet(self.preferred_language))
        if e is None:
            notice = _('[No bounce details are available]')
        else:
            notice = _(e.notice())
        # Currently we always craft bounces as MIME messages.
        bmsg = Message.UserNotification(msg.get_sender(),
                                      self.GetOwnerEmail(),
                                      subject,
                                      lang=self.preferred_language)
        # BAW: Be sure you set the type before trying to attach, or you'll get
        # a MultipartConversionError.
        bmsg.set_type('multipart/mixed')
        txt = MIMEText(notice,
                      _charset=Utils.GetCharSet(self.preferred_language))
        bmsg.attach(txt)
        bmsg.attach(MIMEMessage(msg))
        bmsg.send(self)