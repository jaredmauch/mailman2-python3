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

import time

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
from Mailman import i18n
from Mailman.Message import UserNotification
from Mailman.Logging.Syslog import syslog
from Mailman.i18n import _
from Mailman.SafeDict import SafeDict

# Set up i18n
_ = i18n._
i18n.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)

def process(mlist, msg, msgdata):
    """Process a message through the replybot handler.
    
    Args:
        mlist: The MailList object
        msg: The message to process
        msgdata: Additional message metadata
        
    Returns:
        bool: True if message should be discarded, False otherwise
    """
    # Get the sender
    sender = msg.get_sender()
    if not sender:
        return False
        
    # Check if we should autorespond
    if not mlist.autorespondToSender(sender, msgdata.get('lang', mlist.preferred_language)):
        return False
        
    # Create the response message
    outmsg = UserNotification(sender, mlist.GetBouncesEmail(),
                            _('Automatic response from %(listname)s') % {'listname': mlist.real_name},
                            lang=msgdata.get('lang', mlist.preferred_language))
                            
    # Set the message content
    outmsg.set_type('text/plain')
    outmsg.set_payload(_("""\
This message is an automatic response from %(listname)s.

Your message has been received and will be processed by the list
administrators.  Please do not send this message again.

If you have any questions, please contact the list administrator at
%(adminaddr)s.

Thank you for your interest in the %(listname)s mailing list.
""") % {'listname': mlist.real_name,
        'adminaddr': mlist.GetOwnerEmail()})
        
    # Send the response
    outmsg.send(mlist, msgdata=msgdata)
    
    # Return True to indicate the original message should be discarded
    return True
