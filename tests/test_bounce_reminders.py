# Copyright (C) 2026 by the Free Software Foundation, Inc.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.

"""Test membership reminder bounce handling."""

import os
import pickle
import unittest
import email

_TESTDIR = os.path.dirname(os.path.abspath(__file__))
try:
    from Mailman import __init__
except ImportError:
    import paths

from Mailman import Utils
from Mailman.Queue.BounceRunner import is_membership_reminder_bounce



class ReminderBounceTest(unittest.TestCase):
    def test_membership_reminder_heuristic(self):
        msg = email.message_from_string("""\
Content-Type: multipart/report; boundary=\"BOUND\"

--BOUND
Content-Type: message/rfc822

From: mylist-bounces@dom.ain
To: user@example.com
Subject: Monthly membership reminder
X-List-Administrivia: yes

Reminder body.

--BOUND--
""")
        self.assertTrue(is_membership_reminder_bounce(msg))
    def test_owner_notification_is_not_reminder(self):
        msg = email.message_from_string("""\
Content-Type: multipart/report; boundary="BOUND"

--BOUND
Content-Type: text/plain

A moderation notice bounced.

--BOUND
Content-Type: message/rfc822

From: mailman-bounces@dom.ain
To: mylist-owner@dom.ain
Subject: Your message awaits moderator approval
X-List-Administrivia: yes

The held message.

--BOUND--
""")
        self.assertFalse(is_membership_reminder_bounce(msg))

    def test_explicit_header(self):
        msg = email.message_from_string("""\
Content-Type: multipart/report; boundary="BOUND"

--BOUND
Content-Type: message/rfc822

From: mailman-owner@dom.ain
To: user@example.com
Subject: anything
X-Mailman-Membership-Reminder: yes

Reminder body.

--BOUND--
""")
        self.assertTrue(is_membership_reminder_bounce(msg))



class ReminderBounceQueueTest(unittest.TestCase):
    def test_queue_reminder_bounces(self):
        from Mailman.Queue.BounceRunner import BounceMixin
        import Mailman.Queue.BounceRunner as br_mod
        from Mailman import mm_cfg

        class FakeList(object):
            bounce_processing = 1
            send_reminders = 1

            def isMember(self, addr):
                return addr == 'user@example.com'

            def getMemberOption(self, addr, option):
                return 0

        class TestMixin(BounceMixin):
            pass

        runner = TestMixin()
        msg = email.message_from_string('Subject: bounce\n\n')
        old_list_names = Utils.list_names
        old_mail_list = br_mod.MailList
        try:
            Utils.list_names = lambda: [mm_cfg.MAILMAN_SITE_LIST, 'mylist']
            br_mod.MailList = lambda name, lock=0: FakeList()
            runner._queue_reminder_bounces('user@example.com', msg)
        finally:
            Utils.list_names = old_list_names
            br_mod.MailList = old_mail_list

        runner._bounce_events_fp.seek(0)
        events = []
        while True:
            try:
                events.append(pickle.load(runner._bounce_events_fp))
            except EOFError:
                break
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], 'mylist')
        self.assertEqual(events[0][1], 'user@example.com')
        runner._bounce_events_fp.close()
        os.unlink(runner._bounce_events_file)
        runner._bounce_events_fp = None
        runner._bouncecnt = 0



def suite():
    suite = unittest.TestSuite()
    suite.addTest(unittest.makeSuite(ReminderBounceTest))
    suite.addTest(unittest.makeSuite(ReminderBounceQueueTest))
    return suite



if __name__ == '__main__':
    unittest.main(defaultTest='suite')
