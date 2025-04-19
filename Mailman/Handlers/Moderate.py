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

"""Posting moderation filter.
"""

from typing import Any, Dict, List, Optional, Tuple, Union
import re
from email.mime.message import MIMEMessage
from email.mime.text import MIMEText
from email.utils import parseaddr
from email.message import Message

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Message as MailmanMessage
from Mailman import Errors
from Mailman.i18n import _
from Mailman.Handlers import Hold
from Mailman.Logging.Syslog import syslog
from Mailman.MailList import MailList


class ModeratedMemberPost(Hold.ModeratedPost):
    # BAW: I wanted to use the reason below to differentiate between this
    # situation and normal ModeratedPost reasons.  Greg Ward and Stonewall
    # Ballard thought the language was too harsh and mentioned offense taken
    # by some list members.  I'd still like this class's reason to be
    # different than the base class's reason, but we'll use this until someone
    # can come up with something more clever but inoffensive.
    #
    # reason = _('Posts by member are currently quarantined for moderation')
    pass


def _encode_header(h: Union[str, bytes], charset: str) -> str:
    """Encode a header value using the specified charset.
    
    Args:
        h: Header value to encode
        charset: Character set to use for encoding
        
    Returns:
        Encoded header value
    """
    if isinstance(h, str):
        return h
    return h.decode(charset, 'replace')


def process(mlist: MailList, msg: Message, msgdata: Dict[str, Any]) -> None:
    """Process a message for moderation.
    
    Args:
        mlist: The mailing list
        msg: The message to process
        msgdata: Message metadata
    """
    if msgdata.get('approved'):
        return

    # Is the poster a member or not?
    sender = None
    for addr in msg.get_senders():
        if mlist.isMember(addr):
            sender = addr
            break
        for equiv_addr in Utils.check_eq_domains(addr, mlist.equivalent_domains):
            if mlist.isMember(equiv_addr):
                sender = equiv_addr
                break
        if sender:
            break

    if sender:
        # If the member's moderation flag is on, then perform the moderation action
        if mlist.getMemberOption(sender, mm_cfg.Moderate):
            # Note that for member_moderation_action:
            # 0 = Hold
            # 1 = Reject  
            # 2 = Discard
            action = mlist.member_moderation_action
            
            if action == 0:
                # Hold
                msgdata['sender'] = sender
                Hold.hold_for_approval(mlist, msg, msgdata, ModeratedMemberPost)
                
            elif action == 1:
                # Reject
                text = mlist.member_moderation_notice
                if text:
                    text = Utils.wrap(text)
                raise Errors.RejectMessage(text)
                
            elif action == 2:
                # Discard
                raise Errors.DiscardMessage
                
            else:
                raise ValueError(f'Invalid member_moderation_action: {action}')
                
        return
    else:
        sender = msg.get_sender()

    # From here on out, we're dealing with non-members
    listname = mlist.internal_name()
    
    # Check various non-member moderation patterns
    if mlist.GetPattern(sender, mlist.accept_these_nonmembers,
                       at_list='accept_these_nonmembers'):
        return
        
    if mlist.GetPattern(sender, mlist.hold_these_nonmembers,
                       at_list='hold_these_nonmembers'):
        Hold.hold_for_approval(mlist, msg, msgdata, Hold.NonMemberPost)
        return
        
    if mlist.GetPattern(sender, mlist.reject_these_nonmembers,
                       at_list='reject_these_nonmembers'):
        do_reject(mlist)
        return
        
    if mlist.GetPattern(sender, mlist.discard_these_nonmembers,
                       at_list='discard_these_nonmembers'):
        do_discard(mlist, msg)
        return

    # Handle by way of generic non-member action
    action = mlist.generic_nonmember_action
    if not 0 <= action <= 4:
        raise ValueError(f'Invalid generic_nonmember_action: {action}')
        
    if action == 0 or msgdata.get('fromusenet'):
        # Accept
        return
    elif action == 1:
        Hold.hold_for_approval(mlist, msg, msgdata, Hold.NonMemberPost)
    elif action == 2:
        do_reject(mlist)
    elif action == 3:
        do_discard(mlist, msg)


def do_reject(mlist: MailList) -> None:
    """Reject a message from a non-member.
    
    Args:
        mlist: The mailing list
        
    Raises:
        Errors.RejectMessage: Always raised with rejection notice
    """
    listowner = mlist.GetOwnerEmail()
    if mlist.nonmember_rejection_notice:
        raise Errors.RejectMessage(Utils.wrap(_(mlist.nonmember_rejection_notice)))
    
    msg = _("""\
Your message has been rejected, probably because you are not subscribed to the
mailing list and the list's policy is to prohibit non-members from posting to
it. If you think that your messages are being rejected in error, contact the
mailing list owner at %(listowner)s.""")
    
    raise Errors.RejectMessage(Utils.wrap(msg))


def do_discard(mlist: MailList, msg: Message) -> None:
    """Discard a message from a non-member.
    
    Args:
        mlist: The mailing list
        msg: The message to discard
        
    Raises:
        Errors.DiscardMessage: Always raised after processing
    """
    sender = msg.get_sender()
    
    # Do we forward auto-discards to the list owners?
    if mlist.forward_auto_discards:
        lang = mlist.preferred_language
        varhelp = f'{mlist.GetScriptURL("admin", absolute=1)}/?VARHELP=privacy/sender/discard_these_nonmembers'
        
        # Create notification message
        nmsg = MailmanMessage.UserNotification(
            mlist.GetOwnerEmail(),
            mlist.GetBouncesEmail(),
            _('Auto-discard notification'),
            lang=lang
        )
        
        nmsg.set_type('multipart/mixed')
        
        # Add notification text
        charset = Utils.GetCharSet(lang)
        text = MIMEText(
            Utils.wrap(_('The attached message has been automatically discarded.')),
            _charset=charset
        )
        nmsg.attach(text)
        
        # Attach original message
        nmsg.attach(MIMEMessage(msg))
        
        # Send notification
        nmsg.send(mlist)
    
    # Discard the message
    raise Errors.DiscardMessage
