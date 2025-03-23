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

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import sys
import os
import time
import getopt
from typing import Dict, Optional, Union

import paths
from Mailman import mm_cfg
from Mailman import Utils
from Mailman import MailList
from Mailman import Errors
from Mailman import Message
from Mailman.i18n import C_

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


class Autoresponder:
    """Mixin class for managing mailing list autoresponders.
    
    This class provides functionality for handling automated responses
    to postings, admin requests, and general list requests.
    
    Attributes:
        autorespond_postings: Whether to autorespond to postings
        autorespond_admin: Whether to autorespond to admin requests
        autorespond_requests: Autoresponse mode for -request line
            (0: no response, 1: respond and discard, 2: respond and forward)
        autoresponse_postings_text: Text for posting autoresponses
        autoresponse_admin_text: Text for admin autoresponses
        autoresponse_request_text: Text for request autoresponses
        autoresponse_graceperiod: Days between autoresponses to same address
        postings_responses: Dictionary tracking posting autoresponses
        admin_responses: Dictionary tracking admin autoresponses
        request_responses: Dictionary tracking request autoresponses
    """

    def InitVars(self) -> None:
        """Initialize the autoresponder configuration variables.
        
        This method sets up default values for the autoresponder
        configuration and response tracking.
        """
        # Configurable
        self.autorespond_postings: bool = False
        self.autorespond_admin: bool = False
        # This value can be:
        #  0 - no autoresponse on the -request line
        #  1 - autorespond, but discard the original message
        #  2 - autorespond, and forward the message on to be processed
        self.autorespond_requests: int = 0
        self.autoresponse_postings_text: str = ''
        self.autoresponse_admin_text: str = ''
        self.autoresponse_request_text: str = ''
        self.autoresponse_graceperiod: int = 90  # days
        
        # Non-configurable
        self.postings_responses: Dict[str, float] = {}
        self.admin_responses: Dict[str, float] = {}
        self.request_responses: Dict[str, float] = {}

