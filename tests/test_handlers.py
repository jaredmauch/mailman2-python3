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

"""Unit tests for the various Mailman/Handlers/*.py modules.
"""
from __future__ import print_function

from builtins import str
from builtins import range
import os
import time
import email
import errno
import pickle
import unittest
from email.generator import Generator
try:
    from Mailman import __init__
except ImportError:
    import paths

from Mailman import mm_cfg
from Mailman.MailList import MailList
from Mailman.Message import Message
from Mailman import Errors
from Mailman import Pending
from Mailman.Queue.Switchboard import Switchboard
from Mailman.Handlers import Acknowledge
from Mailman.Handlers import CookHeaders
from Mailman.Handlers import ToDigest
from Mailman.Handlers import ToUsenet
from Mailman.Handlers import Decorate
from Mailman.Handlers import Cleanse
from Mailman.Handlers import CleanseDKIM
from Mailman.Handlers import AvoidDuplicates
from Mailman.Handlers import CalcRecips
from Mailman.Handlers import ToArchive
from Mailman.Handlers import AfterDelivery
from Mailman.Handlers import WrapMessage
from Mailman.Handlers import HandleBouncingAddresses
from Mailman.Handlers import Moderate
from Mailman.Handlers import Replybot
from Mailman.Handlers import Tagger
from Mailman.Handlers import FileRecips
from Mailman.Handlers import MimeDel
from Mailman.Handlers import Scrubber
from Mailman.Handlers import SMTPDirect
from Mailman.Handlers import SpamDetect
from Mailman.Handlers import Hold
from Mailman.Handlers import Approve
from Mailman.Handlers import ToOutgoing
from Mailman.Handlers import ToDigest
from Mailman.Handlers import ToArchive
from Mailman.Handlers import ToUsenet
from Mailman.Handlers import AfterDelivery
from Mailman.Handlers import WrapMessage
from Mailman.Handlers import HandleBouncingAddresses
from Mailman.Handlers import Moderate
from Mailman.Handlers import Replybot
from Mailman.Handlers import Tagger
from Mailman.Handlers import FileRecips
from Mailman.Handlers import MimeDel
from Mailman.Handlers import Scrubber
from Mailman.Handlers import SMTPDirect
from Mailman.Handlers import SpamDetect
from Mailman.Handlers import Hold
from Mailman.Handlers import Approve
from Mailman.Handlers import ToOutgoing

from Mailman.tests.test_bounces import TestBase


def password(plaintext):
    return sha_new(plaintext).hexdigest()


class TestAcknowledge(TestBase):
    def setUp(self):
        TestBase.setUp(self)
        # We're going to want to inspect this queue directory
        self._sb = Switchboard(mm_cfg.VIRGINQUEUE_DIR)
        # Add a member
        self._mlist.addNewMember('aperson@dom.ain')
        self._mlist.personalize = False
        self._mlist.dmarc_moderation_action = 0

    def tearDown(self):
        for f in os.listdir(mm_cfg.VIRGINQUEUE_DIR):
            os.unlink(os.path.join(mm_cfg.VIRGINQUEUE_DIR, f))
        TestBase.tearDown(self)

    def test_no_ack_msgdata(self):
        eq = self.assertEqual
        # Make sure there are no files in the virgin queue already
        eq(len(self._sb.files()), 0)
        msg = email.message_from_string("""\
From: aperson@dom.ain

""")
        Acknowledge.process(self._mlist, msg,
                            {'original_sender': 'aperson@dom.ain'})
        eq(len(self._sb.files()), 0)

    def test_no_ack_not_a_member(self):
        eq = self.assertEqual
        # Make sure there are no files in the virgin queue already
        eq(len(self._sb.files()), 0)
        msg = email.message_from_string("""\
From: bperson@dom.ain

""")
        Acknowledge.process(self._mlist, msg,
                            {'original_sender': 'bperson@dom.ain'})
        eq(len(self._sb.files()), 0)

    def test_no_ack_sender(self):
        eq = self.assertEqual
        eq(len(self._sb.files()), 0)
        msg = email.message_from_string("""\
From: aperson@dom.ain

""")
        Acknowledge.process(self._mlist, msg, {})
        eq(len(self._sb.files()), 0)

    def test_ack_no_subject(self):
        eq = self.assertEqual
        self._mlist.setMemberOption(
            'aperson@dom.ain', mm_cfg.AcknowledgePosts, 1)
        eq(len(self._sb.files()), 0)
        msg = email.message_from_string("""\
From: aperson@dom.ain

""")
        Acknowledge.process(self._mlist, msg, {})
        files = self._sb.files()
        eq(len(files), 1)
        qmsg, qdata = self._sb.dequeue(files[0])
        # Check the .db file
        eq(qdata.get('listname'), '_xtest')
        eq(qdata.get('recips'), ['aperson@dom.ain'])
        eq(qdata.get('version'), 3)
        # Check the .pck
        eq(str(str(qmsg['subject'])), '_xtest post acknowledgement')
        eq(qmsg['to'], 'aperson@dom.ain')
        eq(qmsg['from'], '_xtest-bounces@dom.ain')
        eq(qmsg.get_content_type(), 'text/plain')
        eq(qmsg.get_param('charset'), 'us-ascii')
        msgid = qmsg['message-id']
        self.failUnless(msgid.startswith('<mailman.'))
        self.failUnless(msgid.endswith('._xtest@dom.ain>'))
        eq(qmsg.get_payload(), """\
Your message entitled

    (no subject)

was successfully received by the _xtest mailing list.

List info page: http://www.dom.ain/mailman/listinfo/_xtest
Your preferences: http://www.dom.ain/mailman/options/_xtest/aperson%40dom.ain
""")
        # Make sure we dequeued the only message
        eq(len(self._sb.files()), 0)

    def test_ack_with_subject(self):
        eq = self.assertEqual
        self._mlist.setMemberOption(
            'aperson@dom.ain', mm_cfg.AcknowledgePosts, 1)
        eq(len(self._sb.files()), 0)
        msg = email.message_from_string("""\
From: aperson@dom.ain
Subject: Wish you were here

""")
        Acknowledge.process(self._mlist, msg, {})
        files = self._sb.files()
        eq(len(files), 1)
        qmsg, qdata = self._sb.dequeue(files[0])
        # Check the .db file
        eq(qdata.get('listname'), '_xtest')
        eq(qdata.get('recips'), ['aperson@dom.ain'])
        eq(qdata.get('version'), 3)
        # Check the .pck
        eq(str(qmsg['subject']), '_xtest post acknowledgement')
        eq(qmsg['to'], 'aperson@dom.ain')
        eq(qmsg['from'], '_xtest-bounces@dom.ain')
        eq(qmsg.get_content_type(), 'text/plain')
        eq(qmsg.get_param('charset'), 'us-ascii')
        msgid = qmsg['message-id']
        self.failUnless(msgid.startswith('<mailman.'))
        self.failUnless(msgid.endswith('._xtest@dom.ain>'))
        eq(qmsg.get_payload(), """\
Your message entitled

    Wish you were here

was successfully received by the _xtest mailing list.

List info page: http://www.dom.ain/mailman/listinfo/_xtest
Your preferences: http://www.dom.ain/mailman/options/_xtest/aperson%40dom.ain
""")
        # Make sure we dequeued the only message
        eq(len(self._sb.files()), 0)

    def test_ack_with_subject_and_headers(self):
        eq = self.assertEqual
        self._mlist.setMemberOption(
            'aperson@dom.ain', mm_cfg.AcknowledgePosts, 1)
        eq(len(self._sb.files()), 0)
        msg = email.message_from_string("""\
From: aperson@dom.ain
Subject: Wish you were here
Date: Mon, 01 Jan 2001 00:00:00 -0000
Message-ID: <test@dom.ain>

""")
        Acknowledge.process(self._mlist, msg, {})
        files = self._sb.files()
        eq(len(files), 1)
        qmsg, qdata = self._sb.dequeue(files[0])
        # Check the .db file
        eq(qdata.get('listname'), '_xtest')
        eq(qdata.get('recips'), ['aperson@dom.ain'])
        eq(qdata.get('version'), 3)
        # Check the .pck
        eq(str(qmsg['subject']), '_xtest post acknowledgement')
        eq(qmsg['to'], 'aperson@dom.ain')
        eq(qmsg['from'], '_xtest-bounces@dom.ain')
        eq(qmsg.get_content_type(), 'text/plain')
        eq(qmsg.get_param('charset'), 'us-ascii')
        msgid = qmsg['message-id']
        self.failUnless(msgid.startswith('<mailman.'))
        self.failUnless(msgid.endswith('._xtest@dom.ain>'))
        eq(qmsg.get_payload(), """\
Your message entitled

    Wish you were here

was successfully received by the _xtest mailing list.

List info page: http://www.dom.ain/mailman/listinfo/_xtest
Your preferences: http://www.dom.ain/mailman/options/_xtest/aperson%40dom.ain
""")
        # Make sure we dequeued the only message
        eq(len(self._sb.files()), 0)

    def test_ack_with_subject_and_body(self):
        eq = self.assertEqual
        self._mlist.setMemberOption(
            'aperson@dom.ain', mm_cfg.AcknowledgePosts, 1)
        eq(len(self._sb.files()), 0)
        msg = email.message_from_string("""\
From: aperson@dom.ain
Subject: Wish you were here
Date: Mon, 01 Jan 2001 00:00:00 -0000
Message-ID: <test@dom.ain>

This is the body of the message.
""")
        Acknowledge.process(self._mlist, msg, {})
        files = self._sb.files()
        eq(len(files), 1)
        qmsg, qdata = self._sb.dequeue(files[0])
        # Check the .db file
        eq(qdata.get('listname'), '_xtest')
        eq(qdata.get('recips'), ['aperson@dom.ain'])
        eq(qdata.get('version'), 3)
        # Check the .pck
        eq(str(qmsg['subject']), '_xtest post acknowledgement')
        eq(qmsg['to'], 'aperson@dom.ain')
        eq(qmsg['from'], '_xtest-bounces@dom.ain')
        eq(qmsg.get_content_type(), 'text/plain')
        eq(qmsg.get_param('charset'), 'us-ascii')
        msgid = qmsg['message-id']
        self.failUnless(msgid.startswith('<mailman.'))
        self.failUnless(msgid.endswith('._xtest@dom.ain>'))
        eq(qmsg.get_payload(), """\
Your message entitled

    Wish you were here

was successfully received by the _xtest mailing list.

List info page: http://www.dom.ain/mailman/listinfo/_xtest
Your preferences: http://www.dom.ain/mailman/options/_xtest/aperson%40dom.ain
""")
        # Make sure we dequeued the only message
        eq(len(self._sb.files()), 0)

    def test_ack_with_subject_and_body_and_headers(self):
        eq = self.assertEqual
        self._mlist.setMemberOption(
            'aperson@dom.ain', mm_cfg.AcknowledgePosts, 1)
        eq(len(self._sb.files()), 0)
        msg = email.message_from_string("""\
From: aperson@dom.ain
Subject: Wish you were here
Date: Mon, 01 Jan 2001 00:00:00 -0000
Message-ID: <test@dom.ain>
Content-Type: text/plain; charset=us-ascii

This is the body of the message.
""")
        Acknowledge.process(self._mlist, msg, {})
        files = self._sb.files()
        eq(len(files), 1)
        qmsg, qdata = self._sb.dequeue(files[0])
        # Check the .db file
        eq(qdata.get('listname'), '_xtest')
        eq(qdata.get('recips'), ['aperson@dom.ain'])
        eq(qdata.get('version'), 3)
        # Check the .pck
        eq(str(qmsg['subject']), '_xtest post acknowledgement')
        eq(qmsg['to'], 'aperson@dom.ain')
        eq(qmsg['from'], '_xtest-bounces@dom.ain')
        eq(qmsg.get_content_type(), 'text/plain')
        eq(qmsg.get_param('charset'), 'us-ascii')
        msgid = qmsg['message-id']
        self.failUnless(msgid.startswith('<mailman.'))
        self.failUnless(msgid.endswith('._xtest@dom.ain>'))
        eq(qmsg.get_payload(), """\
Your message entitled

    Wish you were here

was successfully received by the _xtest mailing list.

List info page: http://www.dom.ain/mailman/listinfo/_xtest
Your preferences: http://www.dom.ain/mailman/options/_xtest/aperson%40dom.ain
""")
        # Make sure we dequeued the only message
        eq(len(self._sb.files()), 0)

    def test_ack_with_subject_and_body_and_headers_and_attachments(self):
        eq = self.assertEqual
        self._mlist.setMemberOption(
            'aperson@dom.ain', mm_cfg.AcknowledgePosts, 1)
        eq(len(self._sb.files()), 0)
        msg = email.message_from_string("""\
From: aperson@dom.ain
Subject: Wish you were here
Date: Mon, 01 Jan 2001 00:00:00 -0000
Message-ID: <test@dom.ain>
Content-Type: multipart/mixed; boundary="===============1234567890=="

--===============1234567890==
Content-Type: text/plain; charset=us-ascii

This is the body of the message.

--===============1234567890==
Content-Type: application/octet-stream
Content-Disposition: attachment; filename="test.txt"

This is an attachment.
--===============1234567890==--
""")
        Acknowledge.process(self._mlist, msg, {})
        files = self._sb.files()
        eq(len(files), 1)
        qmsg, qdata = self._sb.dequeue(files[0])
        # Check the .db file
        eq(qdata.get('listname'), '_xtest')
        eq(qdata.get('recips'), ['aperson@dom.ain'])
        eq(qdata.get('version'), 3)
        # Check the .pck
        eq(str(qmsg['subject']), '_xtest post acknowledgement')
        eq(qmsg['to'], 'aperson@dom.ain')
        eq(qmsg['from'], '_xtest-bounces@dom.ain')
        eq(qmsg.get_content_type(), 'text/plain')
        eq(qmsg.get_param('charset'), 'us-ascii')
        msgid = qmsg['message-id']
        self.failUnless(msgid.startswith('<mailman.'))
        self.failUnless(msgid.endswith('._xtest@dom.ain>'))
        eq(qmsg.get_payload(), """\
Your message entitled

    Wish you were here

was successfully received by the _xtest mailing list.

List info page: http://www.dom.ain/mailman/listinfo/_xtest
Your preferences: http://www.dom.ain/mailman/options/_xtest/aperson%40dom.ain
""")
        # Make sure we dequeued the only message
        eq(len(self._sb.files()), 0)

    def test_ack_with_subject_and_body_and_headers_and_attachments_and_embedded(self):
        eq = self.assertEqual
        self._mlist.setMemberOption(
            'aperson@dom.ain', mm_cfg.AcknowledgePosts, 1)
        eq(len(self._sb.files()), 0)
        msg = email.message_from_string("""\
From: aperson@dom.ain
Subject: Wish you were here
Date: Mon, 01 Jan 2001 00:00:00 -0000
Message-ID: <test@dom.ain>
Content-Type: multipart/mixed; boundary="===============1234567890=="

--===============1234567890==
Content-Type: multipart/alternative; boundary="===============0987654321=="

--===============0987654321==
Content-Type: text/plain; charset=us-ascii

This is the body of the message.

--===============0987654321==
Content-Type: text/html; charset=us-ascii

<html>
<body>
This is the body of the message.
</body>
</html>

--===============0987654321==--

--===============1234567890==
Content-Type: application/octet-stream
Content-Disposition: attachment; filename="test.txt"

This is an attachment.
--===============1234567890==--
""")
        Acknowledge.process(self._mlist, msg, {})
        files = self._sb.files()
        eq(len(files), 1)
        qmsg, qdata = self._sb.dequeue(files[0])
        # Check the .db file
        eq(qdata.get('listname'), '_xtest')
        eq(qdata.get('recips'), ['aperson@dom.ain'])
        eq(qdata.get('version'), 3)
        # Check the .pck
        eq(str(qmsg['subject']), '_xtest post acknowledgement')
        eq(qmsg['to'], 'aperson@dom.ain')
        eq(qmsg['from'], '_xtest-bounces@dom.ain')
        eq(qmsg.get_content_type(), 'text/plain')
        eq(qmsg.get_param('charset'), 'us-ascii')
        msgid = qmsg['message-id']
        self.failUnless(msgid.startswith('<mailman.'))
        self.failUnless(msgid.endswith('._xtest@dom.ain>'))
        eq(qmsg.get_payload(), """\
Your message entitled

    Wish you were here

was successfully received by the _xtest mailing list.

List info page: http://www.dom.ain/mailman/listinfo/_xtest
Your preferences: http://www.dom.ain/mailman/options/_xtest/aperson%40dom.ain
""")
        # Make sure we dequeued the only message
        eq(len(self._sb.files()), 0)

    def test_ack_with_subject_and_body_and_headers_and_attachments_and_embedded_and_headers(self):
        eq = self.assertEqual
        self._mlist.setMemberOption(
            'aperson@dom.ain', mm_cfg.AcknowledgePosts, 1)
        eq(len(self._sb.files()), 0)
        msg = email.message_from_string("""\
From: aperson@dom.ain
Subject: Wish you were here
Date: Mon, 01 Jan 2001 00:00:00 -0000
Message-ID: <test@dom.ain>
Content-Type: multipart/mixed; boundary="===============1234567890=="
MIME-Version: 1.0

--===============1234567890==
Content-Type: multipart/alternative; boundary="===============0987654321=="
MIME-Version: 1.0

--===============0987654321==
Content-Type: text/plain; charset=us-ascii

This is the body of the message.

--===============0987654321==
Content-Type: text/html; charset=us-ascii

<html>
<body>
This is the body of the message.
</body>
</html>

--===============0987654321==--

--===============1234567890==
Content-Type: application/octet-stream
Content-Disposition: attachment; filename="test.txt"

This is an attachment.
--===============1234567890==--
""")
        Acknowledge.process(self._mlist, msg, {})
        files = self._sb.files()
        eq(len(files), 1)
        qmsg, qdata = self._sb.dequeue(files[0])
        # Check the .db file
        eq(qdata.get('listname'), '_xtest')
        eq(qdata.get('recips'), ['aperson@dom.ain'])
        eq(qdata.get('version'), 3)
        # Check the .pck
        eq(str(qmsg['subject']), '_xtest post acknowledgement')
        eq(qmsg['to'], 'aperson@dom.ain')
        eq(qmsg['from'], '_xtest-bounces@dom.ain')
        eq(qmsg.get_content_type(), 'text/plain')
        eq(qmsg.get_param('charset'), 'us-ascii')
        msgid = qmsg['message-id']
        self.failUnless(msgid.startswith('<mailman.'))
        self.failUnless(msgid.endswith('._xtest@dom.ain>'))
        eq(qmsg.get_payload(), """\
Your message entitled

    Wish you were here

was successfully received by the _xtest mailing list.

List info page: http://www.dom.ain/mailman/listinfo/_xtest
Your preferences: http://www.dom.ain/mailman/options/_xtest/aperson%40dom.ain
""")
        # Make sure we dequeued the only message
        eq(len(self._sb.files()), 0)

    def test_ack_with_subject_and_body_and_headers_and_attachments_and_embedded_and_headers_and_headers(self):
        eq = self.assertEqual
        self._mlist.setMemberOption(
            'aperson@dom.ain', mm_cfg.AcknowledgePosts, 1)
        eq(len(self._sb.files()), 0)
        msg = email.message_from_string("""\
From: aperson@dom.ain
Subject: Wish you were here
Date: Mon, 01 Jan 2001 00:00:00 -0000
Message-ID: <test@dom.ain>
Content-Type: multipart/mixed; boundary="===============1234567890=="
MIME-Version: 1.0
X-Mailer: Python Mailman

--===============1234567890==
Content-Type: multipart/alternative; boundary="===============0987654321=="
MIME-Version: 1.0

--===============0987654321==
Content-Type: text/plain; charset=us-ascii

This is the body of the message.

--===============0987654321==
Content-Type: text/html; charset=us-ascii

<html>
<body>
This is the body of the message.
</body>
</html>

--===============0987654321==--

--===============1234567890==
Content-Type: application/octet-stream
Content-Disposition: attachment; filename="test.txt"

This is an attachment.
--===============1234567890==--
""")
        Acknowledge.process(self._mlist, msg, {})
        files = self._sb.files()
        eq(len(files), 1)
        qmsg, qdata = self._sb.dequeue(files[0])
        # Check the .db file
        eq(qdata.get('listname'), '_xtest')
        eq(qdata.get('recips'), ['aperson@dom.ain'])
        eq(qdata.get('version'), 3)
        # Check the .pck
        eq(str(qmsg['subject']), '_xtest post acknowledgement')
        eq(qmsg['to'], 'aperson@dom.ain')
        eq(qmsg['from'], '_xtest-bounces@dom.ain')
        eq(qmsg.get_content_type(), 'text/plain')
        eq(qmsg.get_param('charset'), 'us-ascii')
        msgid = qmsg['message-id']
        self.failUnless(msgid.startswith('<mailman.'))
        self.failUnless(msgid.endswith('._xtest@dom.ain>'))
        eq(qmsg.get_payload(), """\
Your message entitled

    Wish you were here

was successfully received by the _xtest mailing list.

List info page: http://www.dom.ain/mailman/listinfo/_xtest
Your preferences: http://www.dom.ain/mailman/options/_xtest/aperson%40dom.ain
""")
        # Make sure we dequeued the only message
        eq(len(self._sb.files()), 0)


class TestAfterDelivery(TestBase):
    # Both msg and msgdata are ignored
    def test_process(self):
        mlist = self._mlist
        last_post_time = mlist.last_post_time
        post_id = mlist.post_id
        AfterDelivery.process(mlist, None, None)
        self.failUnless(mlist.last_post_time > last_post_time)
        self.assertEqual(mlist.post_id, post_id + 1)


class TestApprove(TestBase):
    def test_short_circuit(self):
        msgdata = {'approved': 1}
        rtn = Approve.process(self._mlist, Message(), msgdata)
        # Not really a great test, but there's little else to assert
        self.assertEqual(rtn, None)

    def test_approved_moderator(self):
        mlist = self._mlist
        mlist.mod_password = password('wazoo')
        msg = email.message_from_string("""\
Approved: wazoo

""")
        msgdata = {}
        Approve.process(mlist, msg, msgdata)
        self.failUnless('approved' in msgdata)
        self.assertEqual(msgdata['approved'], 1)

    def test_approve_moderator(self):
        mlist = self._mlist
        mlist.mod_password = password('wazoo')
        msg = email.message_from_string("""\
Approve: wazoo

""")
        msgdata = {}
        Approve.process(mlist, msg, msgdata)
        self.failUnless('approved' in msgdata)
        self.assertEqual(msgdata['approved'], 1)

    def test_approved_admin(self):
        mlist = self._mlist
        mlist.password = password('wazoo')
        msg = email.message_from_string("""\
Approved: wazoo

""")
        msgdata = {}
        Approve.process(mlist, msg, msgdata)
        self.failUnless('approved' in msgdata)
        self.assertEqual(msgdata['approved'], 1)

    def test_approve_admin(self):
        mlist = self._mlist
        mlist.password = password('wazoo')
        msg = email.message_from_string("""\
Approve: wazoo

""")
        msgdata = {}
        Approve.process(mlist, msg, msgdata)
        self.failUnless('approved' in msgdata)
        self.assertEqual(msgdata['approved'], 1)

    def test_unapproved(self):
        mlist = self._mlist
        mlist.password = password('zoowa')
        msg = email.message_from_string("""\
Approve: wazoo

""")
        msgdata = {}
        Approve.process(mlist, msg, msgdata)
        self.assertEqual(msgdata.get('approved'), None)

    def test_trip_beentheres(self):
        mlist = self._mlist
        msg = email.message_from_string("""\
X-BeenThere: %s

""" % mlist.GetListEmail())
        self.assertRaises(Errors.LoopError, Approve.process, mlist, msg, {})


class TestCalcRecips(TestBase):
    def setUp(self):
        TestBase.setUp(self)
        # Add a bunch of regular members
        mlist = self._mlist
        mlist.addNewMember('aperson@dom.ain')
        mlist.addNewMember('bperson@dom.ain')
        mlist.addNewMember('cperson@dom.ain')
        # And a bunch of digest members
        mlist.addNewMember('dperson@dom.ain', digest=1)
        mlist.addNewMember('eperson@dom.ain', digest=1)
        mlist.addNewMember('fperson@dom.ain', digest=1)

    def test_short_circuit(self):
        msgdata = {'recips': 1}
        rtn = CalcRecips.process(self._mlist, None, msgdata)
        # Not really a great test, but there's little else to assert
        self.assertEqual(rtn, None)

    def test_simple_path(self):
        msgdata = {}
        msg = email.message_from_string("""\
From: dperson@dom.ain

""", Message)
        CalcRecips.process(self._mlist, msg, msgdata)
        self.failUnless('recips' in msgdata)
        recips = msgdata['recips']
        recips.sort()
        self.assertEqual(recips, ['aperson@dom.ain', 'bperson@dom.ain',
                                  'cperson@dom.ain'])

    def test_exclude_sender(self):
        msgdata = {}
        msg = email.message_from_string("""\
From: cperson@dom.ain

""", Message)
        self._mlist.setMemberOption('cperson@dom.ain',
                                    mm_cfg.DontReceiveOwnPosts, 1)
        CalcRecips.process(self._mlist, msg, msgdata)
        self.failUnless('recips' in msgdata)
        recips = msgdata['recips']
        recips.sort()
        self.assertEqual(recips, ['aperson@dom.ain', 'bperson@dom.ain'])

    def test_urgent_moderator(self):
        self._mlist.mod_password = password('xxXXxx')
        msgdata = {}
        msg = email.message_from_string("""\
From: dperson@dom.ain
Urgent: xxXXxx

""", Message)
        CalcRecips.process(self._mlist, msg, msgdata)
        self.failUnless('recips' in msgdata)
        recips = msgdata['recips']
        recips.sort()
        self.assertEqual(recips, ['aperson@dom.ain', 'bperson@dom.ain',
                                  'cperson@dom.ain', 'dperson@dom.ain',
                                  'eperson@dom.ain', 'fperson@dom.ain'])

    def test_urgent_admin(self):
        self._mlist.mod_password = password('yyYYyy')
        self._mlist.password = password('xxXXxx')
        msgdata = {}
        msg = email.message_from_string("""\
From: dperson@dom.ain
Urgent: xxXXxx

""", Message)
        CalcRecips.process(self._mlist, msg, msgdata)
        self.failUnless('recips' in msgdata)
        recips = msgdata['recips']
        recips.sort()
        self.assertEqual(recips, ['aperson@dom.ain', 'bperson@dom.ain',
                                  'cperson@dom.ain', 'dperson@dom.ain',
                                  'eperson@dom.ain', 'fperson@dom.ain'])

    def test_urgent_reject(self):
        self._mlist.mod_password = password('yyYYyy')
        self._mlist.password = password('xxXXxx')
        msgdata = {}
        msg = email.message_from_string("""\
From: dperson@dom.ain
Urgent: zzZZzz

""", Message)
        self.assertRaises(Errors.RejectMessage,
                          CalcRecips.process,
                          self._mlist, msg, msgdata)

    # BAW: must test the do_topic_filters() path...


class TestCleanse(TestBase):
    def setUp(self):
        TestBase.setUp(self)

    def test_simple_cleanse(self):
        eq = self.assertEqual
        msg = email.message_from_string("""\
From: aperson@dom.ain
Approved: yes
Urgent: indeed
Reply-To: bperson@dom.ain
Sender: asystem@dom.ain
Return-Receipt-To: another@dom.ain
Disposition-Notification-To: athird@dom.ain
X-Confirm-Reading-To: afourth@dom.ain
X-PMRQC: afifth@dom.ain
Subject: a message to you

""", Message)
        Cleanse.process(self._mlist, msg, {})
        eq(msg['approved'], None)
        eq(msg['urgent'], None)
        eq(msg['return-receipt-to'], None)
        eq(msg['disposition-notification-to'], None)
        eq(msg['x-confirm-reading-to'], None)
        eq(msg['x-pmrqc'], None)
        eq(msg['from'], 'aperson@dom.ain')
        eq(msg['reply-to'], 'bperson@dom.ain')
        eq(msg['sender'], 'asystem@dom.ain')
        eq(msg['subject'], 'a message to you')

    def test_anon_cleanse(self):
        eq = self.assertEqual
        msg = email.message_from_string("""\
From: aperson@dom.ain
Approved: yes
Urgent: indeed
Reply-To: bperson@dom.ain
Sender: asystem@dom.ain
Return-Receipt-To: another@dom.ain
Disposition-Notification-To: athird@dom.ain
X-Confirm-Reading-To: afourth@dom.ain
X-PMRQC: afifth@dom.ain
Subject: a message to you

""", Message)
        self._mlist.anonymous_list = 1
        Cleanse.process(self._mlist, msg, {})
        eq(msg['approved'], None)
        eq(msg['urgent'], None)
        eq(msg['return-receipt-to'], None)
        eq(msg['disposition-notification-to'], None)
        eq(msg['x-confirm-reading-to'], None)
        eq(msg['x-pmrqc'], None)
        eq(len(msg.get_all('from')), 1)
        eq(len(msg.get_all('reply-to')), 1)
        eq(msg['from'], '_xtest@dom.ain')
        eq(msg['reply-to'], '_xtest@dom.ain')
        eq(msg['sender'], None)
        eq(msg['subject'], 'a message to you')


class TestCookHeaders(TestBase):
    def test_transform_noack_to_xack(self):
        eq = self.assertEqual
        msg = email.message_from_string("""\
X-Ack: yes

""", Message)
        CookHeaders.process(self._mlist, msg, {'noack': 1})
        eq(len(msg.get_all('x-ack')), 1)
        eq(msg['x-ack'], 'no')

    def test_original_sender(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain

""", Message)
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msgdata.get('original_sender'), 'aperson@dom.ain')

    def test_no_original_sender(self):
        msg = email.message_from_string("""\
Subject: about this message

""", Message)
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msgdata.get('original_sender'), '')

    def test_xbeenthere(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain

""", Message)
        CookHeaders.process(self._mlist, msg, {})
        self.assertEqual(msg['x-beenthere'], '_xtest@dom.ain')

    def test_multiple_xbeentheres(self):
        eq = self.assertEqual
        msg = email.message_from_string("""\
From: aperson@dom.ain
X-BeenThere: alist@another.dom.ain

""", Message)
        CookHeaders.process(self._mlist, msg, {})
        eq(len(msg.get_all('x-beenthere')), 2)
        beentheres = msg.get_all('x-beenthere')
        beentheres.sort()
        eq(beentheres, ['_xtest@dom.ain', 'alist@another.dom.ain'])

    def test_nonexisting_mmversion(self):
        eq = self.assertEqual
        msg = email.message_from_string("""\
From: aperson@dom.ain

""", Message)
        CookHeaders.process(self._mlist, msg, {})
        eq(msg['x-mailman-version'], mm_cfg.VERSION)

    def test_existing_mmversion(self):
        eq = self.assertEqual
        msg = email.message_from_string("""\
From: aperson@dom.ain
X-Mailman-Version: 3000

""", Message)
        CookHeaders.process(self._mlist, msg, {})
        eq(len(msg.get_all('x-mailman-version')), 1)
        eq(msg['x-mailman-version'], '3000')

    def test_nonexisting_precedence(self):
        eq = self.assertEqual
        msg = email.message_from_string("""\
From: aperson@dom.ain

""", Message)
        CookHeaders.process(self._mlist, msg, {})
        eq(msg['precedence'], 'list')

    def test_existing_precedence(self):
        eq = self.assertEqual
        msg = email.message_from_string("""\
From: aperson@dom.ain
Precedence: junk

""", Message)
        CookHeaders.process(self._mlist, msg, {})
        eq(len(msg.get_all('precedence')), 1)
        eq(msg['precedence'], 'junk')

    def test_subject_munging_no_subject(self):
        self._mlist.subject_prefix = '[XTEST] '
        msg = email.message_from_string("""\
From: aperson@dom.ain

""", Message)
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msgdata.get('origsubj'), '')
        self.assertEqual(str(msg['subject']), '[XTEST] (no subject)')

    def test_subject_munging(self):
        self._mlist.subject_prefix = '[XTEST] '
        msg = email.message_from_string("""\
From: aperson@dom.ain
Subject: About Mailman...

""", Message)
        CookHeaders.process(self._mlist, msg, {})
        self.assertEqual(str(msg['subject']), '[XTEST] About Mailman...')

    def test_no_subject_munging_for_digests(self):
        self._mlist.subject_prefix = '[XTEST] '
        msg = email.message_from_string("""\
From: aperson@dom.ain
Subject: About Mailman...

""", Message)
        CookHeaders.process(self._mlist, msg, {'isdigest': 1})
        self.assertEqual(msg['subject'], 'About Mailman...')

    def test_no_subject_munging_for_fasttrack(self):
        self._mlist.subject_prefix = '[XTEST] '
        msg = email.message_from_string("""\
From: aperson@dom.ain
Subject: About Mailman...

""", Message)
        CookHeaders.process(self._mlist, msg, {'_fasttrack': 1})
        self.assertEqual(msg['subject'], 'About Mailman...')

    def test_no_subject_munging_has_prefix(self):
        self._mlist.subject_prefix = '[XTEST] '
        msg = email.message_from_string("""\
From: aperson@dom.ain
Subject: Re: [XTEST] About Mailman...

""", Message)
        CookHeaders.process(self._mlist, msg, {})
        # prefixing depends on mm_cfg.py
        self.failUnless(str(msg['subject']) == 'Re: [XTEST] About Mailman...' or
                        str(msg['subject']) == '[XTEST] Re: About Mailman...')

    def test_reply_to_list(self):
        eq = self.assertEqual
        mlist = self._mlist
        mlist.reply_goes_to_list = 1
        mlist.from_is_list = 0
        msg = email.message_from_string("""\
From: aperson@dom.ain

""", Message)
        msgdata = {}
        CookHeaders.process(mlist, msg, msgdata)
        eq(msgdata['add_header']['Reply-To'], '_xtest@dom.ain')
        eq(msg.get_all('reply-to'), None)

    def test_reply_to_list_fil(self):
        eq = self.assertEqual
        mlist = self._mlist
        mlist.reply_goes_to_list = 1
        mlist.from_is_list = 1
        msg = email.message_from_string("""\
From: aperson@dom.ain

""", Message)
        msgdata = {}
        CookHeaders.process(mlist, msg, msgdata)
        eq(msgdata['add_header']['Reply-To'],
            '_xtest@dom.ain')
        eq(msgdata['add_header']['Cc'],
            'aperson@dom.ain')
        eq(msg.get_all('reply-to'), None)
        eq(msg.get_all('cc'), None)


    def test_reply_to_explicit(self):
        eq = self.assertEqual
        mlist = self._mlist
        mlist.reply_goes_to_list = 2
        mlist.from_is_list = 0
        mlist.reply_to_address = 'mlist@dom.ain'
        msg = email.message_from_string("""\
From: aperson@dom.ain

""", Message)
        msgdata = {}
        CookHeaders.process(mlist, msg, msgdata)
        eq(msgdata['add_header']['Reply-To'], 'mlist@dom.ain')
        eq(msg.get_all('reply-to'), None)

    def test_reply_to_explicit_fil(self):
        eq = self.assertEqual
        mlist = self._mlist
        mlist.reply_goes_to_list = 2
        mlist.from_is_list = 1
        mlist.reply_to_address = 'mlist@dom.ain'
        msg = email.message_from_string("""\
From: aperson@dom.ain

""", Message)
        msgdata = {}
        CookHeaders.process(mlist, msg, msgdata)
        eq(msgdata['add_header']['Reply-To'],
            'mlist@dom.ain')
        eq(msgdata['add_header']['Cc'],
            'aperson@dom.ain')
        eq(msg.get_all('reply-to'), None)
        eq(msg.get_all('cc'), None)

    def test_reply_to_explicit_with_strip(self):
        eq = self.assertEqual
        mlist = self._mlist
        mlist.reply_goes_to_list = 2
        mlist.first_strip_reply_to = 1
        mlist.from_is_list = 0
        mlist.reply_to_address = 'mlist@dom.ain'
        msg = email.message_from_string("""\
From: aperson@dom.ain
Reply-To: bperson@dom.ain

""", Message)
        msgdata = {}

        CookHeaders.process(self._mlist, msg, msgdata)
        eq(msgdata['add_header']['Reply-To'], 'mlist@dom.ain')
        eq(msg.get_all('reply-to'), ['bperson@dom.ain'])

    def test_reply_to_explicit_with_strip_fil(self):
        eq = self.assertEqual
        mlist = self._mlist
        mlist.reply_goes_to_list = 2
        mlist.first_strip_reply_to = 1
        mlist.from_is_list = 1
        mlist.reply_to_address = 'mlist@dom.ain'
        msg = email.message_from_string("""\
From: aperson@dom.ain
Reply-To: bperson@dom.ain

""", Message)
        msgdata = {}

        CookHeaders.process(self._mlist, msg, msgdata)
        eq(msgdata['add_header']['Reply-To'],
            'mlist@dom.ain')
        eq(msgdata['add_header']['Cc'],
            'aperson@dom.ain')
        eq(msg.get_all('reply-to'), ['bperson@dom.ain'])
        eq(msg.get_all('cc'), None)

    def test_reply_to_extends_to_list(self):
        eq = self.assertEqual
        mlist = self._mlist
        mlist.reply_goes_to_list = 1
        mlist.first_strip_reply_to = 0
        mlist.from_is_list = 0
        msg = email.message_from_string("""\
From: aperson@dom.ain
Reply-To: bperson@dom.ain

""", Message)
        msgdata = {}

        CookHeaders.process(mlist, msg, msgdata)
        eq(msgdata['add_header']['Reply-To'],
            'bperson@dom.ain, _xtest@dom.ain')

    def test_reply_to_extends_to_list_fil(self):
        eq = self.assertEqual
        mlist = self._mlist
        mlist.reply_goes_to_list = 1
        mlist.first_strip_reply_to = 0
        mlist.from_is_list = 1
        msg = email.message_from_string("""\
From: aperson@dom.ain
Reply-To: bperson@dom.ain

""", Message)
        msgdata = {}

        CookHeaders.process(mlist, msg, msgdata)
        eq(msgdata['add_header']['Reply-To'],
            'bperson@dom.ain, _xtest@dom.ain')
        eq(msgdata['add_header']['Cc'],
            'aperson@dom.ain')
        eq(msg.get_all('reply-to'), ['bperson@dom.ain'])
        eq(msg.get_all('cc'), None)

    def test_reply_to_extends_to_explicit(self):
        eq = self.assertEqual
        mlist = self._mlist
        mlist.reply_goes_to_list = 2
        mlist.first_strip_reply_to = 0
        mlist.from_is_list = 0
        mlist.reply_to_address = 'mlist@dom.ain'
        msg = email.message_from_string("""\
From: aperson@dom.ain
Reply-To: bperson@dom.ain

""", Message)
        msgdata = {}
        CookHeaders.process(mlist, msg, msgdata)
        eq(msgdata['add_header']['Reply-To'],
            'mlist@dom.ain, bperson@dom.ain')

    def test_reply_to_extends_to_explicit_fil(self):
        eq = self.assertEqual
        mlist = self._mlist
        mlist.reply_goes_to_list = 2
        mlist.first_strip_reply_to = 0
        mlist.from_is_list = 1
        mlist.reply_to_address = 'mlist@dom.ain'
        msg = email.message_from_string("""\
From: aperson@dom.ain
Reply-To: bperson@dom.ain

""", Message)
        msgdata = {}
        CookHeaders.process(mlist, msg, msgdata)
        eq(msgdata['add_header']['Reply-To'],
            'mlist@dom.ain, bperson@dom.ain')
        eq(msgdata['add_header']['Cc'],
            'aperson@dom.ain')
        eq(msg.get_all('reply-to'), ['bperson@dom.ain'])
        eq(msg.get_all('cc'), None)

    def test_list_headers_nolist(self):
        eq = self.assertEqual
        msg = email.message_from_string("""\
From: aperson@dom.ain

""", Message)
        CookHeaders.process(self._mlist, msg, {'_nolist': 1})
        eq(msg['list-id'], None)
        eq(msg['list-help'], None)
        eq(msg['list-unsubscribe'], None)
        eq(msg['list-subscribe'], None)
        eq(msg['list-post'], None)
        eq(msg['list-archive'], None)

    def test_list_headers(self):
        eq = self.assertEqual
        self._mlist.archive = 1
        msg = email.message_from_string("""\
From: aperson@dom.ain

""", Message)
        oldval = mm_cfg.DEFAULT_URL_HOST
        mm_cfg.DEFAULT_URL_HOST = 'www.dom.ain'
        try:
            CookHeaders.process(self._mlist, msg, {})
        finally:
            mm_cfg.DEFAULT_URL_HOST = oldval
        eq(msg['list-id'], '<_xtest.dom.ain>')
        eq(msg['list-help'], '<mailto:_xtest-request@dom.ain?subject=help>')
        eq(msg['list-unsubscribe'],
           '<http://www.dom.ain/mailman/options/_xtest>,'
           '\n <mailto:_xtest-request@dom.ain?subject=unsubscribe>')
        eq(msg['list-subscribe'],
           '<http://www.dom.ain/mailman/listinfo/_xtest>,'
           '\n <mailto:_xtest-request@dom.ain?subject=subscribe>')
        eq(msg['list-post'], '<mailto:_xtest@dom.ain>')
        eq(msg['list-archive'], '<http://www.dom.ain/pipermail/_xtest/>')
