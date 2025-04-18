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

"""Unit tests for the various Message class methods.
"""

import sys
import unittest
import email
try:
    from Mailman import __init__
except ImportError:
    import paths

from Mailman import Message
from Mailman import Version
from Mailman import Errors

from EmailBase import EmailBase



class TestSentMessage1(EmailBase):
    def test_user_notification(self):
        eq = self.assertEqual
        unless = self.failUnless
        msg = Message.UserNotification(
            'aperson@dom.ain',
            '_xtest@dom.ain',
            'Your Test List',
            'About your test list')
        msg.send(self._mlist)
        qmsg = email.message_from_string(self._readmsg())
        eq(qmsg['subject'], 'Your Test List')
        eq(qmsg['from'], '_xtest@dom.ain')
        eq(qmsg['to'], 'aperson@dom.ain')
        # The Message-ID: header has some time-variant information
        msgid = qmsg['message-id']
        unless(msgid.startswith('<mailman.'))
        unless(msgid.endswith('._xtest@dom.ain>'))
        # The Sender: header is optional and addresses can be VERPed
        if self._mlist.include_sender_header:
            sender = qmsg['sender']
            unless(sender.startswith('"_xtest" <_xtest-bounces'))
            unless(sender.endswith('@dom.ain>'))
        eto = qmsg['errors-to']
        unless(eto.startswith('_xtest-bounces'))
        unless(eto.endswith('@dom.ain'))
        eq(qmsg['x-beenthere'], '_xtest@dom.ain')
        eq(qmsg['x-mailman-version'], Version.VERSION)
        eq(qmsg['precedence'], 'bulk')
        eq(qmsg['list-id'], '<_xtest.dom.ain>')
        eq(qmsg['x-list-administrivia'], 'yes')
        eq(qmsg.get_payload(), 'About your test list')

class TestSentMessage2(EmailBase):
    def test_bounce_message(self):
        eq = self.assertEqual
        unless = self.failUnless
        msg = email.message_from_string("""\
To: _xtest@dom.ain
From: nobody@dom.ain
Subject: and another thing

yadda yadda yadda
""", Message.Message)
        self._mlist.BounceMessage(msg, {})
        qmsg = email.message_from_string(self._readmsg())
        unless(qmsg.is_multipart())
        eq(len(qmsg.get_payload()), 2)
        # The first payload is the details of the bounce action, and the
        # second message is the message/rfc822 attachment of the original
        # message.
        msg1 = qmsg.get_payload(0)
        eq(msg1.get_content_type(), 'text/plain')
        eq(msg1.get_payload(), '[No bounce details are available]')
        msg2 = qmsg.get_payload(1)
        eq(msg2.get_content_type(), 'message/rfc822')
        unless(msg2.is_multipart())
        msg3 = msg2.get_payload(0)
        eq(msg3.get_payload(), 'yadda yadda yadda\n')



def suite(x):
    suite = unittest.TestSuite()
    if x == '1':
        suite.addTest(unittest.makeSuite(TestSentMessage1))
    elif x == '2':
        suite.addTest(unittest.makeSuite(TestSentMessage2))
    return suite



if __name__ == '__main__':
    # There is some issue in asyncore.py that prevents successfully running more than
    # one test at a time, so specify which of the two tests as an argument.
    if len(sys.argv) == 1:
        x = '1'
    else:
        x = sys.argv[1]
    if x not in ('1', '2'):
        print >> sys.stderr, (
            'usage: python test_message.py [n] where n = 1, 2 is the sub-test to run.')
        sys.exit(1)
    unittest.TextTestRunner(verbosity=2).run(suite(x)) 

