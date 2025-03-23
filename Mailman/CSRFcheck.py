# Copyright (C) 2011-2018 by the Free Software Foundation, Inc.
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

""" Cross-Site Request Forgery checker """

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import sys
import os
import time
import getopt
import urllib
import marshal
import binascii

import paths
from Mailman import mm_cfg
from Mailman import Utils
from Mailman import MailList
from Mailman import Errors
from Mailman import Message
from Mailman.i18n import C_
from Mailman.Logging.Syslog import syslog
from Mailman.Utils import UnobscureEmail, sha_new

keydict = {
    'user':      mm_cfg.AuthUser,
    'poster':    mm_cfg.AuthListPoster,
    'moderator': mm_cfg.AuthListModerator,
    'admin':     mm_cfg.AuthListAdmin,
    'site':      mm_cfg.AuthSiteAdmin,
}

def usage(code, msg=''):
    if code:
        fd = sys.stderr
    else:
        fd = sys.stdout
    print(C_(__doc__), file=fd)
    if msg:
        print(msg, file=fd)
    sys.exit(code)

def csrf_token(mlist, contexts, user=None):
    """ create token by mailman cookie generation algorithm """
    if user is None:
        user = mlist.GetMemberName(user)
    if user is None:
        user = ''
    # Create a token that includes the list name, user name, and contexts
    token = '%s:%s:%s' % (mlist.internal_name(), user, ':'.join(contexts))
    # Hash the token with the site password
    return sha_new(token + mm_cfg.SITE_PASSWORD).hexdigest()

def csrf_check(mlist, contexts, token, user=None):
    """ check if the token is valid for the given list, contexts and user """
    if token is None:
        return False
    expected = csrf_token(mlist, contexts, user)
    return token == expected