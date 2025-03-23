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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

# -*- python -*-

from __future__ import absolute_import
from __future__ import division

from __future__ import unicode_literals

"""Unit tests for the various Mailman/Handlers/*.py modules.
"""

import os
import time
import email
import errno
import pickle
import unittest
from typing import List, Tuple, Dict, Set
from email.Generator import Generator
try:
    from Mailman import __init__
except ImportError:
    import paths

from Mailman import mm_cfg
from Mailman.MailList import MailList
from Mailman import Message
from Mailman import Errors
from Mailman import Pending
from Mailman.Queue.Switchboard import Switchboard

from Mailman.Handlers import Acknowledge
from Mailman.Handlers import AfterDelivery
from Mailman.Handlers import Approve
from Mailman.Handlers import CalcRecips
from Mailman.Handlers import Cleanse
from Mailman.Handlers import CookHeaders
from Mailman.Handlers import Decorate
from Mailman.Handlers import FileRecips
from Mailman.Handlers import Hold
from Mailman.Handlers import MimeDel
from Mailman.Handlers import Moderate
from Mailman.Handlers import Replybot
# Don't test handlers such as SMTPDirect and Sendmail here
from Mailman.Handlers import SpamDetect
from Mailman.Handlers import Tagger
from Mailman.Handlers import ToArchive
from Mailman.Handlers import ToDigest
from Mailman.Handlers import ToOutgoing
from Mailman.Handlers import ToUsenet
from Mailman.Utils import sha_new

from TestBase import TestBase


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

""", Message.Message)
        Acknowledge.process(self._mlist, msg,
                            {'original_sender': 'aperson@dom.ain'})
        eq(len(self._sb.files()), 0)

    def test_no_ack_not_a_member(self):
        eq = self.assertEqual
        # Make sure there are no files in the virgin queue already
        eq(len(self._sb.files()), 0)
        msg = email.message_from_string("""\
From: bperson@dom.ain

""", Message.Message)
        Acknowledge.process(self._mlist, msg,
                            {'original_sender': 'bperson@dom.ain'})
        eq(len(self._sb.files()), 0)

    def test_no_ack_sender(self):
        eq = self.assertEqual
        eq(len(self._sb.files()), 0)
        msg = email.message_from_string("""\
From: aperson@dom.ain

""", Message.Message)
        Acknowledge.process(self._mlist, msg, {})
        eq(len(self._sb.files()), 0)

    def test_ack_no_subject(self):
        eq = self.assertEqual
        self._mlist.setMemberOption(
            'aperson@dom.ain', mm_cfg.AcknowledgePosts, 1)
        eq(len(self._sb.files()), 0)
        msg = email.message_from_string("""\
From: aperson@dom.ain

""", Message.Message)
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
        self.assertTrue(msgid.startswith('<mailman.'))
        self.assertTrue(msgid.endswith('._xtest@dom.ain>'))
        eq(qmsg.get_payload(), """\
Your message entitled

    (no subject)

was successfully received by the _xtest mailing list.

List info page: http://www.dom.ain/mailman/listinfo/_xtest
Your preferences: http://www.dom.ain/mailman/options/_xtest/aperson{40dom.ain
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

""", Message.Message)
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
        self.assertTrue(msgid.startswith('<mailman.'))
        self.assertTrue(msgid.endswith('._xtest@dom.ain>'))
        eq(qmsg.get_payload(), """\
Your message entitled

    Wish you were here

was successfully received by the _xtest mailing list.

List info page: http://www.dom.ain/mailman/listinfo/_xtest
Your preferences: http://www.dom.ain/mailman/options/_xtest/aperson}{40dom.ain
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
        self.assertTrue(mlist.last_post_time > last_post_time)
        self.assertEqual(mlist.post_id, post_id + 1)


class TestApprove(TestBase):
    def test_short_circuit(self):
        msgdata = {'approved': 1}
        rtn = Approve.process(self._mlist, Message.Message(), msgdata)
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
        self.assertIn('approved', msgdata)
        self.assertEqual(msgdata['approved'], 1)

    def test_approve_moderator(self):
        mlist = self._mlist
        mlist.mod_password = password('wazoo')
        msg = email.message_from_string("""\
Approve: wazoo

""")
        msgdata = {}
        Approve.process(mlist, msg, msgdata)
        self.assertIn('approved', msgdata)
        self.assertEqual(msgdata['approved'], 1)

    def test_approved_admin(self):
        mlist = self._mlist
        mlist.password = password('wazoo')
        msg = email.message_from_string("""\
Approved: wazoo

""")
        msgdata = {}
        Approve.process(mlist, msg, msgdata)
        self.assertIn('approved', msgdata)
        self.assertEqual(msgdata['approved'], 1)

    def test_approve_admin(self):
        mlist = self._mlist
        mlist.password = password('wazoo')
        msg = email.message_from_string("""\
Approve: wazoo

""")
        msgdata = {}
        Approve.process(mlist, msg, msgdata)
        self.assertIn('approved', msgdata)
        self.assertEqual(msgdata['approved'], 1)

    def test_unapproved(self):
        msg = email.message_from_string("""\
Approved: wrong

""")
        msgdata = {}
        Approve.process(self._mlist, msg, msgdata)
        self.assertNotIn('approved', msgdata)

    def test_trip_beentheres(self):
        msg = email.message_from_string("""\
Approved: wazoo

""")
        msgdata = {}
        Approve.process(self._mlist, msg, msgdata)
        self.assertNotIn('approved', msgdata)


class TestCalcRecips(TestBase):
    def setUp(self):
        TestBase.setUp(self)
        self._mlist.owner_include_discussions = 0
        self._mlist.moderator_include_discussions = 0
        self._mlist.emergency = 0
        self._mlist.owner_include_discussions = 0
        self._mlist.moderator_include_discussions = 0
        self._mlist.emergency = 0

    def test_short_circuit(self):
        msgdata = {'recips': ['aperson@dom.ain']}
        CalcRecips.process(self._mlist, Message.Message(), msgdata)
        self.assertEqual(msgdata['recips'], ['aperson@dom.ain'])

    def test_simple_path(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain

""")
        msgdata = {}
        CalcRecips.process(self._mlist, msg, msgdata)
        self.assertEqual(msgdata['recips'], ['aperson@dom.ain'])

    def test_exclude_sender(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain

""")
        msgdata = {}
        CalcRecips.process(self._mlist, msg, msgdata)
        self.assertEqual(msgdata['recips'], ['aperson@dom.ain'])

    def test_urgent_moderator(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
Subject: [Urgent] test

""")
        msgdata = {}
        CalcRecips.process(self._mlist, msg, msgdata)
        self.assertEqual(msgdata['recips'], ['aperson@dom.ain'])

    def test_urgent_admin(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
Subject: [Urgent] test

""")
        msgdata = {}
        CalcRecips.process(self._mlist, msg, msgdata)
        self.assertEqual(msgdata['recips'], ['aperson@dom.ain'])

    def test_urgent_reject(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
Subject: [Urgent] test

""")
        msgdata = {}
        CalcRecips.process(self._mlist, msg, msgdata)
        self.assertEqual(msgdata['recips'], ['aperson@dom.ain'])


class TestCleanse(TestBase):
    def setUp(self):
        TestBase.setUp(self)
        self._mlist.anonymous_list = 0

    def test_simple_cleanse(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {}
        Cleanse.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['from'], 'aperson@dom.ain')
        self.assertEqual(msg['to'], '_xtest@dom.ain')
        self.assertEqual(msg['subject'], 'test')

    def test_anon_cleanse(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {}
        Cleanse.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['from'], 'aperson@dom.ain')
        self.assertEqual(msg['to'], '_xtest@dom.ain')
        self.assertEqual(msg['subject'], 'test')


class TestCookHeaders(TestBase):
    def test_transform_noack_to_xack(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['x-ack'], 'no')

    def test_original_sender(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {'original_sender': 'aperson@dom.ain'}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['x-original-sender'], 'aperson@dom.ain')

    def test_no_original_sender(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertNotIn('x-original-sender', msg)

    def test_xbeenthere(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['x-beenthere'], '_xtest')

    def test_multiple_xbeentheres(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['x-beenthere'], '_xtest')

    def test_nonexisting_mmversion(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['x-mailman-version'], '2.1.39')

    def test_existing_mmversion(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test
X-Mailman-Version: 2.1.38

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['x-mailman-version'], '2.1.38')

    def test_nonexisting_precedence(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['precedence'], 'list')

    def test_existing_precedence(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test
Precedence: bulk

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['precedence'], 'bulk')

    def test_subject_munging_no_subject(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['subject'], '(no subject)')

    def test_subject_munging(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['subject'], 'test')

    def test_no_subject_munging_for_digests(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {'to_digest': 1}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['subject'], 'test')

    def test_no_subject_munging_for_fasttrack(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {'to_fasttrack': 1}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['subject'], 'test')

    def test_no_subject_munging_has_prefix(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: [test] test

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['subject'], '[test] test')

    def test_reply_to_list(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['reply-to'], '_xtest@dom.ain')

    def test_reply_to_list_fil(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['reply-to'], '_xtest@dom.ain')

    def test_reply_to_list_with_strip(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['reply-to'], '_xtest@dom.ain')

    def test_reply_to_list_with_strip_fil(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['reply-to'], '_xtest@dom.ain')

    def test_reply_to_explicit(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['reply-to'], '_xtest@dom.ain')

    def test_reply_to_explicit_fil(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        self.assertEqual(msg['reply-to'], '_xtest@dom.ain')

    def test_reply_to_explicit_with_strip(self):
        msg = email.message_from_string("""\
From: aperson@dom.ain
To: _xtest@dom.ain
Subject: test

""")
        msgdata = {}
        CookHeaders.process(self._mlist, msg, msgdata)
        Tagger.process(mlist, msg, msgdata)
        eq(msg['x-topics'], 'bar fight')
        eq(msgdata.get('topichits'), ['bar fight'])

    def test_all_body_lines_plain_text(self):
        eq = self.assertEqual
        mlist = self._mlist
        mlist.topics_bodylines_limit = -1
        msg = email.message_from_string("""\
Subject: Was
Keywords: Raw

Subject: farbaw
Keywords: barbaz
""")
        msgdata = {}
        Tagger.process(mlist, msg, msgdata)
        eq(msg['x-topics'], 'bar fight')
        eq(msgdata.get('topichits'), ['bar fight'])

    def test_no_body_lines(self):
        eq = self.assertEqual
        mlist = self._mlist
        mlist.topics_bodylines_limit = 0
        msg = email.message_from_string("""\
Subject: Was
Keywords: Raw

Subject: farbaw
Keywords: barbaz
""")
        msgdata = {}
        Tagger.process(mlist, msg, msgdata)
        eq(msg['x-topics'], None)
        eq(msgdata.get('topichits'), None)

    def test_body_lines_in_multipart(self):
        eq = self.assertEqual
        mlist = self._mlist
        mlist.topics_bodylines_limit = -1
        msg = email.message_from_string("""\
Subject: Was
Keywords: Raw
Content-Type: multipart/alternative; boundary="BOUNDARY"

--BOUNDARY
From: sabo
To: obas

Subject: farbaw
Keywords: barbaz

--BOUNDARY--
""")
        msgdata = {}
        Tagger.process(mlist, msg, msgdata)
        eq(msg['x-topics'], 'bar fight')
        eq(msgdata.get('topichits'), ['bar fight'])

    def test_body_lines_no_part(self):
        eq = self.assertEqual
        mlist = self._mlist
        mlist.topics_bodylines_limit = -1
        msg = email.message_from_string("""\
Subject: Was
Keywords: Raw
Content-Type: multipart/alternative; boundary=BOUNDARY

--BOUNDARY
From: sabo
To: obas
Content-Type: message/rfc822

Subject: farbaw
Keywords: barbaz

--BOUNDARY
From: sabo
To: obas
Content-Type: message/rfc822

Subject: farbaw
Keywords: barbaz

--BOUNDARY--
""")
        msgdata = {}
        Tagger.process(mlist, msg, msgdata)
        eq(msg['x-topics'], None)
        eq(msgdata.get('topichits'), None)


class TestToArchive(TestBase):
    def setUp(self):
        TestBase.setUp(self)
        # We're going to want to inspect this queue directory
        self._sb = Switchboard(mm_cfg.ARCHQUEUE_DIR)

    def tearDown(self):
        for f in os.listdir(mm_cfg.ARCHQUEUE_DIR):
            os.unlink(os.path.join(mm_cfg.ARCHQUEUE_DIR, f))
        TestBase.tearDown(self)

    def test_short_circuit(self):
        eq = self.assertEqual
        msgdata = {'isdigest': 1}
        ToArchive.process(self._mlist, None, msgdata)
        eq(len(self._sb.files()), 0)
        # Try the other half of the or...
        self._mlist.archive = 0
        ToArchive.process(self._mlist, None, msgdata)
        eq(len(self._sb.files()), 0)
        # Now try the various message header shortcuts
        msg = email.message_from_string("""\
X-No-Archive: YES

""")
        self._mlist.archive = 1
        ToArchive.process(self._mlist, msg, {})
        eq(len(self._sb.files()), 0)
        # And for backwards compatibility
        msg = email.message_from_string("""\
X-Archive: NO

""")
        ToArchive.process(self._mlist, msg, {})
        eq(len(self._sb.files()), 0)

    def test_normal_archiving(self):
        eq = self.assertEqual
        msg = email.message_from_string("""\
Subject: About Mailman

It rocks!
""")
        ToArchive.process(self._mlist, msg, {})
        files = self._sb.files()
        eq(len(files), 1)
        msg2, data = self._sb.dequeue(files[0])
        eq(len(data), 3)
        eq(data['_parsemsg'], False)
        eq(data['version'], 3)
        # Clock skew makes this unreliable
        #self.failUnless(data['received_time'] <= time.time())
        eq(msg.as_string(unixfrom=0), msg2.as_string(unixfrom=0))


class TestToDigest(TestBase):
    def _makemsg(self, i=0):
        msg = email.message_from_string("""From: aperson@dom.ain
To: _xtest@dom.ain
Subject: message number }{(i)d

Here is message }{(i)d
""" }{ {'i' : i})
        return msg

    def setUp(self):
        TestBase.setUp(self)
        self._path = os.path.join(self._mlist.fullpath(), 'digest.mbox')
        fp = open(self._path, 'w')
        g = Generator(fp)
        for i in range(5):
            g.flatten(self._makemsg(i), unixfrom=1)
        fp.close()
        self._sb = Switchboard(mm_cfg.VIRGINQUEUE_DIR)

    def tearDown(self):
        try:
            os.unlink(self._path)
        except OSError as e:
            if e.errno != errno.ENOENT: raise
        for f in os.listdir(mm_cfg.VIRGINQUEUE_DIR):
            os.unlink(os.path.join(mm_cfg.VIRGINQUEUE_DIR, f))
        TestBase.tearDown(self)

    def test_short_circuit(self):
        eq = self.assertEqual
        mlist = self._mlist
        mlist.digestable = 0
        eq(ToDigest.process(mlist, None, {}), None)
        mlist.digestable = 1
        eq(ToDigest.process(mlist, None, {'isdigest': 1}), None)
        eq(self._sb.files(), [])

    def test_undersized(self):
        msg = self._makemsg(99)
        size = os.path.getsize(self._path) + len(str(msg))
        self._mlist.digest_size_threshhold = (size + 1) * 1024
        ToDigest.process(self._mlist, msg, {})
        self.assertEqual(self._sb.files(), [])

    def test_send_a_digest(self):
        eq = self.assertEqual
        mlist = self._mlist
        msg = self._makemsg(99)
        size = os.path.getsize(self._path) + len(str(msg))
        # Set digest_size_threshhold to a very small value to force a digest.
        # Setting to zero no longer works.
        mlist.digest_size_threshhold = 0.001
        ToDigest.process(mlist, msg, {})
        files = self._sb.files()
        # There should be two files in the queue, one for the MIME digest and
        # one for the RFC 1153 digest.
        eq(len(files), 2)
        # Now figure out which of the two files is the MIME digest and which
        # is the RFC 1153 digest.
        for filebase in files:
            qmsg, qdata = self._sb.dequeue(filebase)
            if qmsg.get_content_maintype() == 'multipart':
                mimemsg = qmsg
                mimedata = qdata
            else:
                rfc1153msg = qmsg
                rfc1153data = qdata
        eq(rfc1153msg.get_content_type(), 'text/plain')
        eq(mimemsg.get_content_type(), 'multipart/mixed')
        eq(mimemsg['from'], mlist.GetRequestEmail())
        eq(mimemsg['subject'],
           '}{(realname)s Digest, Vol }{(volume)d, Issue }{(issue)d' }{ {
            'realname': mlist.real_name,
            'volume'  : mlist.volume,
            'issue'   : mlist.next_digest_number - 1,
            })
        eq(mimemsg['to'], mlist.GetListEmail())
        # BAW: this test is incomplete...


class TestToOutgoing(TestBase):
    def setUp(self):
        TestBase.setUp(self)
        # We're going to want to inspect this queue directory
        self._sb = Switchboard(mm_cfg.OUTQUEUE_DIR)

    def tearDown(self):
        for f in os.listdir(mm_cfg.OUTQUEUE_DIR):
            os.unlink(os.path.join(mm_cfg.OUTQUEUE_DIR, f))
        TestBase.tearDown(self)

    def test_outgoing(self):
        eq = self.assertEqual
        msg = email.message_from_string("""\
Subject: About Mailman

It rocks!
""")
        msgdata = {'foo': 1, 'bar': 2}
        ToOutgoing.process(self._mlist, msg, msgdata)
        files = self._sb.files()
        eq(len(files), 1)
        msg2, data = self._sb.dequeue(files[0])
        eq(msg.as_string(unixfrom=0), msg2.as_string(unixfrom=0))
        self.failUnless(len(data) >= 6 and len(data) <= 7)
        eq(data['foo'], 1)
        eq(data['bar'], 2)
        eq(data['version'], 3)
        eq(data['listname'], '_xtest')
        eq(data['_parsemsg'], False)
        # Can't test verp. presence/value depend on mm_cfg.py
        #eq(data['verp'], 1)
        # Clock skew makes this unreliable
        #self.failUnless(data['received_time'] <= time.time())


class TestToUsenet(TestBase):
    def setUp(self):
        TestBase.setUp(self)
        # We're going to want to inspect this queue directory
        self._sb = Switchboard(mm_cfg.NEWSQUEUE_DIR)

    def tearDown(self):
        for f in os.listdir(mm_cfg.NEWSQUEUE_DIR):
            os.unlink(os.path.join(mm_cfg.NEWSQUEUE_DIR, f))
        TestBase.tearDown(self)

    def test_short_circuit(self):
        eq = self.assertEqual
        mlist = self._mlist
        mlist.gateway_to_news = 0
        ToUsenet.process(mlist, None, {})
        eq(len(self._sb.files()), 0)
        mlist.gateway_to_news = 1
        ToUsenet.process(mlist, None, {'isdigest': 1})
        eq(len(self._sb.files()), 0)
        ToUsenet.process(mlist, None, {'fromusenet': 1})
        eq(len(self._sb.files()), 0)

    def test_to_usenet(self):
        # BAW: Should we, can we, test the error conditions that only log to a
        # file instead of raising an exception?
        eq = self.assertEqual
        mlist = self._mlist
        mlist.gateway_to_news = 1
        mlist.linked_newsgroup = 'foo'
        mlist.nntp_host = 'bar'
        msg = email.message_from_string("""\
Subject: About Mailman

Mailman rocks!
""")
        ToUsenet.process(mlist, msg, {})
        files = self._sb.files()
        eq(len(files), 1)
        msg2, data = self._sb.dequeue(files[0])
        eq(msg.as_string(unixfrom=0), msg2.as_string(unixfrom=0))
        eq(data['version'], 3)
        eq(data['listname'], '_xtest')
        # Clock skew makes this unreliable
        #self.failUnless(data['received_time'] <= time.time())


def suite():
    suite = unittest.TestSuite()
    suite.addTest(unittest.makeSuite(TestAcknowledge))
    suite.addTest(unittest.makeSuite(TestAfterDelivery))
    suite.addTest(unittest.makeSuite(TestApprove))
    suite.addTest(unittest.makeSuite(TestCalcRecips))
    suite.addTest(unittest.makeSuite(TestCleanse))
    suite.addTest(unittest.makeSuite(TestCookHeaders))
    suite.addTest(unittest.makeSuite(TestDecorate))
    suite.addTest(unittest.makeSuite(TestFileRecips))
    suite.addTest(unittest.makeSuite(TestHold))
    suite.addTest(unittest.makeSuite(TestMimeDel))
    suite.addTest(unittest.makeSuite(TestModerate))
    suite.addTest(unittest.makeSuite(TestReplybot))
    suite.addTest(unittest.makeSuite(TestSpamDetect))
    suite.addTest(unittest.makeSuite(TestTagger))
    suite.addTest(unittest.makeSuite(TestToArchive))
    suite.addTest(unittest.makeSuite(TestToDigest))
    suite.addTest(unittest.makeSuite(TestToOutgoing))
    suite.addTest(unittest.makeSuite(TestToUsenet))
    return suite


if __name__ == '__main__':
    unittest.main(defaultTest='suite')
}