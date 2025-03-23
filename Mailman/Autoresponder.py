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

import paths
from Mailman import mm_cfg
from Mailman import Utils
from Mailman import MailList
from Mailman import Errors
from Mailman import Message
from Mailman.i18n import C_

def usage(code, msg=''):
    if code:
        fd = sys.stderr
    else:
        fd = sys.stdout
    print(C_(__doc__), file=fd)
    if msg:
        print(msg, file=fd)
    sys.exit(code)


class Autoresponder:
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

