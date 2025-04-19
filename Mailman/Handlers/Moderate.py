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

    # Get the sender and check if they're a member
    sender = get_sender(mlist, msg)
    if sender:
        handle_member_post(mlist, msg, msgdata, sender)
        return
        
    # Handle non-member posts
    handle_non_member_post(mlist, msg, msgdata)


def get_sender(mlist: MailList, msg: Message) -> Optional[str]:
    """Get the sender address and check if they're a member.
    
    Args:
        mlist: The mailing list
        msg: The message to check
        
    Returns:
        The sender's address if they're a member, None otherwise
    """
    for addr in msg.get_senders():
        if mlist.isMember(addr):
            return addr
        for equiv_addr in Utils.check_eq_domains(addr, mlist.equivalent_domains):
            if mlist.isMember(equiv_addr):
                return equiv_addr
    return None


def handle_member_post(mlist: MailList, msg: Message, msgdata: Dict[str, Any], 
                      sender: str) -> None:
    """Handle a post from a member.
    
    Args:
        mlist: The mailing list
        msg: The message to process
        msgdata: Message metadata
        sender: The sender's address
    """
    if not mlist.getMemberOption(sender, mm_cfg.Moderate):
        return
        
    action = mlist.member_moderation_action
    if not 0 <= action <= 2:
        raise ValueError(f'Invalid member_moderation_action: {action}')
        
    if action == 0:  # Hold
        msgdata['sender'] = sender
        Hold.hold_for_approval(mlist, msg, msgdata, ModeratedMemberPost)
    elif action == 1:  # Reject
        text = mlist.member_moderation_notice
        if text:
            text = Utils.wrap(text)
        raise Errors.RejectMessage(text)
    elif action == 2:  # Discard
        raise Errors.DiscardMessage


def handle_non_member_post(mlist: MailList, msg: Message, msgdata: Dict[str, Any]) -> None:
    """Handle a post from a non-member.
    
    Args:
        mlist: The mailing list
        msg: The message to process
        msgdata: Message metadata
    """
    sender = msg.get_sender()
    listname = mlist.internal_name()
    
    # Check moderation patterns
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

    # Handle by generic non-member action
    action = mlist.generic_nonmember_action
    if not 0 <= action <= 4:
        raise ValueError(f'Invalid generic_nonmember_action: {action}')
        
    if action == 0 or msgdata.get('fromusenet'):
        return  # Accept
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
    
    msg = _(f"""\
Your message has been rejected, probably because you are not subscribed to the
mailing list and the list's policy is to prohibit non-members from posting to
it. If you think that your messages are being rejected in error, contact the
mailing list owner at {listowner}.""")
    
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
    
    # Forward auto-discards to list owners if configured
    if mlist.forward_auto_discards:
        lang = mlist.preferred_language
        varhelp = f'{mlist.GetScriptURL("admin", absolute=1)}/?VARHELP=privacy/sender/discard_these_nonmembers'
        
        # Create notification message
        nmsg = MailmanMessage.UserNotification(
            mlist.GetOwnerEmail(),
            mlist.GetBouncesEmail(),
            _('Auto-discarded message from %(sender)s'),
            _(f"""\
A message from {sender} was automatically discarded because the sender was
not a member of the mailing list and the list's policy is to discard messages
from non-members.

To modify this behavior, visit the list's privacy settings at:

{varhelp}

The original message follows:

"""),
            lang
        )
        
        # Attach original message
        nmsg.attach(MIMEMessage(msg))
        
        try:
            nmsg.send(mlist)
        except Exception as e:
            syslog('error', 'Failed to send auto-discard notice: %s', e)
            
    raise Errors.DiscardMessage
