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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

"""Handler for auto-responses.
"""

from typing import Any, Dict, List, Optional, Tuple, Union
import time
from email.message import Message
import traceback

from Mailman import Utils
from Mailman import Message as MailmanMessage
from Mailman.i18n import _
from Mailman.SafeDict import SafeDict
from Mailman.Logging.Syslog import syslog
from Mailman.MailList import MailList


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
    """Process a message for auto-response.
    
    Args:
        mlist: The mailing list
        msg: The message to process
        msgdata: Message metadata
    """
    # Check if we should skip auto-response
    if should_skip_autoresponse(mlist, msg, msgdata):
        return
        
    # Check if we're in the grace period
    if not should_respond_now(mlist, msg, msgdata):
        return
        
    try:
        # Create and send the auto-response
        send_autoresponse(mlist, msg, msgdata)
        
        # Update the grace period database
        update_grace_period(mlist, msg, msgdata)
        
    except Exception as e:
        syslog('error', 'Failed to process auto-response: %s', e)
        traceback.print_exc()


def should_skip_autoresponse(mlist: MailList, msg: Message, msgdata: Dict[str, Any]) -> bool:
    """Check if we should skip auto-response for this message.
    
    Args:
        mlist: The mailing list
        msg: The message to check
        msgdata: Message metadata
        
    Returns:
        True if we should skip auto-response, False otherwise
    """
    # Check X-Ack header
    ack = msg.get('x-ack', '').lower()
    if ack == 'no' or msgdata.get('noack'):
        return True
        
    # Check Precedence header
    precedence = msg.get('precedence', '').lower()
    if ack != 'yes' and precedence in ('bulk', 'junk', 'list'):
        return True
        
    # Check list configuration
    toadmin = msgdata.get('toowner')
    torequest = msgdata.get('torequest') or msgdata.get('toconfirm') or \
                msgdata.get('tojoin') or msgdata.get('toleave')
                
    return ((toadmin and not mlist.autorespond_admin) or
            (torequest and not mlist.autorespond_requests) or \
            (not toadmin and not torequest and not mlist.autorespond_postings))


def should_respond_now(mlist: MailList, msg: Message, msgdata: Dict[str, Any]) -> bool:
    """Check if we should respond now or wait for grace period.
    
    Args:
        mlist: The mailing list
        msg: The message to check
        msgdata: Message metadata
        
    Returns:
        True if we should respond now, False if we should wait
    """
    sender = msg.get_sender()
    now = time.time()
    graceperiod = mlist.autoresponse_graceperiod
    
    # Always respond if grace period is disabled or X-Ack is yes
    if graceperiod <= 0 or msg.get('x-ack', '').lower() == 'yes':
        return True
        
    # Check grace period for this sender
    toadmin = msgdata.get('toowner')
    torequest = msgdata.get('torequest') or msgdata.get('toconfirm') or \
                msgdata.get('tojoin') or msgdata.get('toleave')
                
    if toadmin:
        quiet_until = mlist.admin_responses.get(sender, 0)
    elif torequest:
        quiet_until = mlist.request_responses.get(sender, 0)
    else:
        quiet_until = mlist.postings_responses.get(sender, 0)
        
    return now >= quiet_until


def send_autoresponse(mlist: MailList, msg: Message, msgdata: Dict[str, Any]) -> None:
    """Send an auto-response message.
    
    Args:
        mlist: The mailing list
        msg: The original message
        msgdata: Message metadata
    """
    sender = msg.get_sender()
    realname = mlist.real_name
    subject = _(f'Auto-response for your message to the "{realname}" mailing list')
    
    # Create the response text
    d = SafeDict({
        'listname': realname,
        'listurl': mlist.GetScriptURL('listinfo'),
        'requestemail': mlist.GetRequestEmail(),
        'adminemail': mlist.GetBouncesEmail(),
        'owneremail': mlist.GetOwnerEmail(),
    })
    
    # Get the appropriate response text
    toadmin = msgdata.get('toowner')
    torequest = msgdata.get('torequest') or msgdata.get('toconfirm') or \
                msgdata.get('tojoin') or msgdata.get('toleave')
                
    if toadmin:
        rtext = mlist.autoresponse_admin_text
    elif torequest:
        rtext = mlist.autoresponse_request_text
    else:
        rtext = mlist.autoresponse_postings_text
        
    # Convert $-strings if needed
    if getattr(mlist, 'use_dollar_strings', 0):
        rtext = Utils.to_percent(rtext)
        
    try:
        text = rtext % d
    except Exception as e:
        syslog('error', 'Bad autoreply text for list %s: %s\n%s',
               mlist.internal_name(), str(e), rtext)
        text = rtext
        
    # Wrap and send the response
    text = Utils.wrap(text)
    outmsg = MailmanMessage.UserNotification(
        sender, mlist.GetBouncesEmail(),
        subject, text, mlist.preferred_language
    )
    outmsg['X-Mailer'] = _('The Mailman Replybot')
    outmsg['X-Ack'] = 'No'  # prevent recursions and mail loops!
    
    try:
        outmsg.send(mlist)
    except Exception as e:
        syslog('error', 'Failed to send auto-response to %s: %s', sender, e)
        raise


def update_grace_period(mlist: MailList, msg: Message, msgdata: Dict[str, Any]) -> None:
    """Update the grace period database.
    
    Args:
        mlist: The mailing list
        msg: The original message
        msgdata: Message metadata
    """
    graceperiod = mlist.autoresponse_graceperiod
    if graceperiod <= 0:
        return
        
    sender = msg.get_sender()
    now = time.time()
    quiet_until = now + graceperiod * 24 * 60 * 60  # Convert days to seconds
    
    toadmin = msgdata.get('toowner')
    torequest = msgdata.get('torequest') or msgdata.get('toconfirm') or \
                msgdata.get('tojoin') or msgdata.get('toleave')
                
    if toadmin:
        mlist.admin_responses[sender] = quiet_until
    elif torequest:
        mlist.request_responses[sender] = quiet_until
    else:
        mlist.postings_responses[sender] = quiet_until
