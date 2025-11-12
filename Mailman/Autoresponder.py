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

"""MailList mixin class managing the autoresponder.
"""

from builtins import object
from Mailman import mm_cfg
from Mailman.i18n import _
import time



class Autoresponder(object):
    def InitVars(self):
        # configurable
        self.autorespond_postings = 0
        self.autorespond_admin = 0
        # this value can be
        #  0 - no autoresponse on the -request line
        #  1 - autorespond, but discard the original message
        #  2 - autorespond, and forward the message on to be processed
        self.autorespond_requests = 0
        self.autoresponse_postings_text = ''
        self.autoresponse_admin_text = ''
        self.autoresponse_request_text = ''
        self.autoresponse_graceperiod = 90 # days
        # non-configurable
        self.postings_responses = {}
        self.admin_responses = {}
        self.request_responses = {}

    def autorespondToSender(self, sender, lang):
        """Check if we should autorespond to this sender.
        
        Args:
            sender: The email address of the sender
            lang: The language to use for the response
            
        Returns:
            True if we should autorespond, False otherwise
        """
        # Check if we're in the grace period
        now = time.time()
        graceperiod = self.autoresponse_graceperiod
        if graceperiod > 0:
            # Check the appropriate response dictionary based on the type of message
            if self.autorespond_admin:
                quiet_until = self.admin_responses.get(sender, 0)
            elif self.autorespond_requests:
                quiet_until = self.request_responses.get(sender, 0)
            else:
                quiet_until = self.postings_responses.get(sender, 0)
            if quiet_until > now:
                return False
                
        # Update the appropriate response dictionary
        if self.autorespond_admin:
            self.admin_responses[sender] = now + graceperiod
        elif self.autorespond_requests:
            self.request_responses[sender] = now + graceperiod
        else:
            self.postings_responses[sender] = now + graceperiod
            
        return True

