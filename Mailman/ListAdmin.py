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

"""Mixin class for MailList which handles administrative requests.

Two types of admin requests are currently supported: adding members to a
closed or semi-closed list, and moderated posts.

Pending subscriptions which are requiring a user's confirmation are handled
elsewhere.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import sys
import os
import time
import errno
import pickle
import marshal
import binascii
import tempfile
import shutil
from typing import List, Tuple, Dict, Set

import email
from email.mime.message import MIMEMessage
from email.generator import Generator
from email.utils import getaddresses

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Message
from Mailman import Errors
from Mailman.UserDesc import UserDesc
from Mailman.Queue.sbcache import get_switchboard
from Mailman.Logging.Syslog import syslog
from Mailman import i18n
from Mailman.i18n import C_

# This is a temporary variable used to store the current translation
# function.  It's used by the D_() function below.
_translation = None

def D_(s):
    """Return the string s if no translation is available."""
    if _translation is None:
        return s
    return _translation(s)

# Request types requiring admin approval
IGN = 0
HELDMSG = 1
SUBSCRIPTION = 2
UNSUBSCRIPTION = 3

# Return status from __handlepost()
DEFER = 0
REMOVE = 1
LOST = 2

DASH = '-'
NL = '\n'


class ListAdmin(object):
    def InitVars(self):
        # non-configurable data
        self.requests_db = None
        self.requests_db_path = None
        self.next_request_id = 1

    def InitTempVars(self):
        """Initialize temporary variables."""
        self.requests_db = None
        self.requests_db_path = None
        self.next_request_id = 1

    def __opendb(self):
        """Open the requests database."""
        if self.requests_db is None:
            self.requests_db_path = os.path.join(self.fullpath(), 'requests.pck')
            try:
                with open(self.requests_db_path, 'rb') as fp:
                    self.requests_db = pickle.load(fp)
            except (IOError, EOFError, pickle.UnpicklingError):
                self.requests_db = {}
            self.next_request_id = max(self.requests_db.keys()) + 1 if self.requests_db else 1

    def __closedb(self):
        """Close the requests database."""
        if self.requests_db is not None:
            try:
                with open(self.requests_db_path, 'wb') as fp:
                    pickle.dump(self.requests_db, fp)
            except IOError as e:
                syslog('error', 'Failed to save requests database: %s', str(e))
            self.requests_db = None
            self.requests_db_path = None

    def __nextid(self):
        """Get the next available request ID."""
        self.__opendb()
        id = self.next_request_id
        self.next_request_id += 1
        return id

    def SaveRequestsDb(self):
        """Save the requests database."""
        self.__closedb()

    def NumRequestsPending(self):
        """Return the number of pending requests."""
        self.__opendb()
        return len(self.requests_db)

    def __getmsgids(self, rtype):
        """Get message IDs for a given request type."""
        self.__opendb()
        return [id for id, record in self.requests_db.items() 
                if record[0] == rtype]

    def GetHeldMessageIds(self):
        """Get IDs of held messages."""
        return self.__getmsgids('post')

    def GetSubscriptionIds(self):
        """Get IDs of subscription requests."""
        return self.__getmsgids('subscribe')

    def GetUnsubscriptionIds(self):
        """Get IDs of unsubscription requests."""
        return self.__getmsgids('unsubscribe')

    def GetRecord(self, id):
        """Get a record by ID."""
        self.__opendb()
        return self.requests_db.get(id)

    def GetRecordType(self, id):
        """Get the type of a record by ID."""
        record = self.GetRecord(id)
        return record[0] if record else None

    def HandleRequest(self, id, value, comment=None, preserve=None,
                     forward=None, addr=None):
        """Handle a request with the given parameters."""
        record = self.GetRecord(id)
        if not record:
            raise Errors.MMUnknownRequestError
        rtype = record[0]
        if rtype == 'post':
            self.__handlepost(record, value, comment, preserve, forward, addr)
        elif rtype == 'subscribe':
            self.__handlesubscription(record, value, comment)
        elif rtype == 'unsubscribe':
            self.__handleunsubscription(record, value, comment)
        else:
            raise Errors.MMUnknownRequestError
        del self.requests_db[id]
        self.SaveRequestsDb()

    def HoldMessage(self, msg, reason, msgdata=None):
        """Hold a message for moderation."""
        # Make a copy of msgdata so that subsequent changes won't corrupt the
        # request database.
        if msgdata is None:
            msgdata = {}
        msgdata = msgdata.copy()
        # Store the message in a temporary file
        tmpdir = tempfile.mkdtemp()
        try:
            msgpath = os.path.join(tmpdir, 'msg')
            with open(msgpath, 'wb') as fp:
                msg.as_string(fp)
            # Create the record
            record = ('post', time.time(), msg.get_sender(),
                     msg.get('subject', '(no subject)'),
                     reason, msgdata)
            id = self.__nextid()
            self.requests_db[id] = record
            self.SaveRequestsDb()
            return id
        finally:
            shutil.rmtree(tmpdir)

    def __handlepost(self, record, value, comment, preserve, forward, addr):
        """Handle a held post request."""
        if value == 1:  # approve
            if preserve:
                self.Save()
            if forward:
                self.ForwardMessage(record[3], comment)
            else:
                self.ApproveMessage(record[3], comment)
        elif value == 0:  # reject
            self.__refuse(record[3], record[2], comment)
        elif value == 2:  # discard
            pass
        else:
            raise Errors.MMUnknownRequestError

    def HoldSubscription(self, addr, fullname, password, digest, lang):
        """Hold a subscription request."""
        record = ('subscribe', time.time(), addr, fullname, password, digest, lang)
        id = self.__nextid()
        self.requests_db[id] = record
        self.SaveRequestsDb()
        return id

    def __handlesubscription(self, record, value, comment):
        """Handle a subscription request."""
        if value == 1:  # approve
            self.ApproveSubscription(record[2], record[3], record[4],
                                   record[5], record[6])
        elif value == 0:  # reject
            self.__refuse(record[2], record[2], comment, lang=record[6])
        else:
            raise Errors.MMUnknownRequestError

    def HoldUnsubscription(self, addr):
        """Hold an unsubscription request."""
        record = ('unsubscribe', time.time(), addr)
        id = self.__nextid()
        self.requests_db[id] = record
        self.SaveRequestsDb()
        return id

    def __handleunsubscription(self, record, value, comment):
        """Handle an unsubscription request."""
        if value == 1:  # approve
            self.ApproveUnsubscription(record[2])
        elif value == 0:  # reject
            self.__refuse(record[2], record[2], comment)
        else:
            raise Errors.MMUnknownRequestError

    def __refuse(self, request, recip, comment, origmsg=None, lang=None):
        """Send a refusal notice."""
        if lang is None:
            lang = self.preferred_language
        text = Utils.maketext('reject.txt',
                            {'listname': self.real_name,
                             'reason': comment},
                            lang=lang, mlist=self)
        msg = Message.UserNotification(recip, self.GetBouncesEmail(),
                                     _('Your request to the %(listname)s mailing list'),
                                     text, lang=lang)
        if origmsg:
            msg.attach(origmsg)
        msg.send(self)

    def _UpdateRecords(self):
        """Update records to the latest format."""
        self.__opendb()
        updated = False
        for id, record in list(self.requests_db.items()):
            if record[0] == 'subscribe' and len(record) < 7:
                # Add language parameter
                record = record + (self.preferred_language,)
                self.requests_db[id] = record
                updated = True
            elif record[0] == 'subscribe' and len(record) < 6:
                # Add fullname parameter
                record = record[:2] + (record[2], '') + record[2:]
                self.requests_db[id] = record
                updated = True
        if updated:
            self.SaveRequestsDb()


def readMessage(path):
    """Read a message from a file."""
    try:
        with open(path, 'rb') as fp:
            return pickle.load(fp)
    except (IOError, EOFError, pickle.UnpicklingError):
        # Try reading as text file
        with open(path, 'r') as fp:
            return fp.read()
}