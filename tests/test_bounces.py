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

"""Test the bounce detection modules."""

import sys
import os
import unittest
import email
try:
    from Mailman import __init__
except ImportError:
    import paths

from Mailman.Bouncers.BouncerAPI import Stop



class BounceTest(unittest.TestCase):
    DATA = (
        # Postfix bounces
        ('Postfix', 'postfix_01.txt', ['xxxxx@local.ie']),
        ('Postfix', 'postfix_02.txt', ['yyyyy@digicool.com']),
        ('Postfix', 'postfix_03.txt', ['ttttt@ggggg.com']),
        ('Postfix', 'postfix_04.txt', ['userx@mail1.example.com']),
        ('Postfix', 'postfix_05.txt', ['userx@example.net']),
        # Exim bounces
        ('Exim', 'exim_01.txt', ['userx@its.example.nl']),
        # SimpleMatch bounces
        ('SimpleMatch', 'sendmail_01.txt', ['zzzzz@shaft.coal.nl',
                                            'zzzzz@nfg.nl']),
        ('SimpleMatch', 'simple_01.txt', ['bbbsss@example.com']),
        ('SimpleMatch', 'simple_02.txt', ['userx@example.net']),
        ('SimpleMatch', 'simple_04.txt', ['userx@example.com']),
        ('SimpleMatch', 'newmailru_01.txt', ['zzzzz@newmail.ru']),
        ('SimpleMatch', 'hotpop_01.txt', ['userx@example.com']),
        ('SimpleMatch', 'microsoft_03.txt', ['userx@example.com']),
        ('SimpleMatch', 'simple_05.txt', ['userx@example.net']),
        ('SimpleMatch', 'simple_06.txt', ['userx@example.com']),
        ('SimpleMatch', 'simple_07.txt', ['userx@example.net']),
        ('SimpleMatch', 'simple_08.txt', ['userx@example.de']),
        ('SimpleMatch', 'simple_09.txt', ['userx@example.de']),
        ('SimpleMatch', 'simple_10.txt', ['userx@example.com']),
        ('SimpleMatch', 'simple_11.txt', ['userx@example.com']),
        ('SimpleMatch', 'simple_12.txt', ['userx@example.ac.jp']),
        ('SimpleMatch', 'simple_13.txt', ['userx@example.fr']),
        ('SimpleMatch', 'simple_14.txt', ['userx@example.com',
                                          'usery@example.com']),
        ('SimpleMatch', 'simple_15.txt', ['userx@example.be']),
        ('SimpleMatch', 'simple_16.txt', ['userx@example.com']),
        ('SimpleMatch', 'simple_17.txt', ['userx@example.com']),
        ('SimpleMatch', 'simple_18.txt', ['userx@example.com']),
        ('SimpleMatch', 'simple_19.txt', ['userx@example.com.ar']),
        ('SimpleMatch', 'simple_20.txt', ['userx@example.com']),
        ('SimpleMatch', 'simple_23.txt', ['userx@example.it']),
        ('SimpleMatch', 'simple_24.txt', ['userx@example.com']),
        ('SimpleMatch', 'simple_25.txt', ['userx@example.com']),
        ('SimpleMatch', 'simple_26.txt', ['userx@example.it']),
        ('SimpleMatch', 'simple_27.txt', ['userx@example.net.py']),
        ('SimpleMatch', 'simple_29.txt', ['userx@example.com']),
        ('SimpleMatch', 'simple_30.txt', ['userx@example.com']),
        ('SimpleMatch', 'simple_31.txt', ['userx@example.fr']),
        ('SimpleMatch', 'simple_32.txt', ['userx@example.com']),
        ('SimpleMatch', 'simple_33.txt', ['userx@example.com']),
        ('SimpleMatch', 'simple_34.txt', ['roland@example.com']),
        ('SimpleMatch', 'simple_36.txt', ['userx@example.com']),
        ('SimpleMatch', 'simple_37.txt', ['user@example.edu']),
        ('SimpleMatch', 'simple_38.txt', ['userx@example.com']),
        ('SimpleMatch', 'simple_39.txt', ['userx@example.ru']),
        ('SimpleMatch', 'simple_41.txt', ['userx@example.com']),
        ('SimpleMatch', 'simple_44.txt', ['user@example.com']),
        ('SimpleMatch', 'bounce_02.txt', ['userx@example.com']),
        ('SimpleMatch', 'bounce_03.txt', ['userx@example.uk']),
        ('SimpleMatch', 'yahoo_12.txt', ['user@yahoo.com']),
        # SimpleWarning
        ('SimpleWarning', 'simple_03.txt', Stop),
        ('SimpleWarning', 'simple_21.txt', Stop),
        ('SimpleWarning', 'simple_22.txt', Stop),
        ('SimpleWarning', 'simple_28.txt', Stop),
        ('SimpleWarning', 'simple_35.txt', Stop),
        ('SimpleWarning', 'simple_40.txt', Stop),
        # GroupWise
        ('GroupWise', 'groupwise_01.txt', ['userx@example.EDU']),
        # This one really sucks 'cause it's text/html.  Just make sure it
        # doesn't throw an exception, but we won't get any meaningful
        # addresses back from it.
        ('GroupWise', 'groupwise_02.txt', []),
        # Actually, it's from Exchange, and Exchange does recognize it
        ('Exchange', 'groupwise_02.txt', ['userx@example.com']),
        # Not a bounce but has confused groupwise
        ('GroupWise', 'groupwise_03.txt', []),
        # Yale's own
        ('Yale', 'yale_01.txt', ['userx@cs.yale.edu',
                                 'userx@yale.edu']),
        # DSN, i.e. RFC 1894
        ('DSN', 'dsn_01.txt', ['userx@example.com']),
        ('DSN', 'dsn_02.txt', ['zzzzz@example.uk']),
        ('DSN', 'dsn_03.txt', ['userx@example.be']),
        ('DSN', 'dsn_04.txt', ['userx@example.ch']),
        ('DSN', 'dsn_05.txt', Stop),
        ('DSN', 'dsn_06.txt', Stop),
        ('DSN', 'dsn_07.txt', Stop),
        ('DSN', 'dsn_08.txt', Stop),
        ('DSN', 'dsn_09.txt', ['userx@example.com']),
        ('DSN', 'dsn_10.txt', ['anne.person@dom.ain']),
        ('DSN', 'dsn_11.txt', ['joem@example.com']),
        ('DSN', 'dsn_12.txt', ['userx@example.jp']),
        ('DSN', 'dsn_13.txt', ['userx@example.com']),
        ('DSN', 'dsn_14.txt', ['userx@example.com.dk']),
        ('DSN', 'dsn_15.txt', ['userx@example.com']),
        ('DSN', 'dsn_16.txt', ['userx@example.com']),
        ('DSN', 'dsn_17.txt', Stop),
        ('DSN', 'dsn_18.txt', ['email@replaced.net']),
        # Microsoft Exchange
        ('Exchange', 'microsoft_01.txt', ['userx@example.COM']),
        ('Exchange', 'microsoft_02.txt', ['userx@example.COM']),
        # SMTP32
        ('SMTP32', 'smtp32_01.txt', ['userx@example.ph']),
        ('SMTP32', 'smtp32_02.txt', ['userx@example.com']),
        ('SMTP32', 'smtp32_03.txt', ['userx@example.com']),
        ('SMTP32', 'smtp32_04.txt', ['after_another@example.net',
                                     'one_bad_address@example.net']),
        ('SMTP32', 'smtp32_05.txt', ['userx@example.com']),
        ('SMTP32', 'smtp32_06.txt', ['Absolute_garbage_addr@example.net']),
        ('SMTP32', 'smtp32_07.txt', ['userx@example.com']),
        # Qmail
        ('Qmail', 'qmail_01.txt', ['userx@example.de']),
        ('Qmail', 'qmail_02.txt', ['userx@example.com']),
        ('Qmail', 'qmail_03.txt', ['userx@example.jp']),
        ('Qmail', 'qmail_04.txt', ['userx@example.au']),
        ('Qmail', 'qmail_05.txt', ['userx@example.com']),
        ('Qmail', 'qmail_06.txt', ['ntl@xxx.com']),
        ('Qmail', 'qmail_07.txt', ['user@example.net']),
        ('Qmail', 'qmail_08.txt', []),
        # LLNL's custom Sendmail
        ('LLNL', 'llnl_01.txt', ['user1@example.gov']),
        # Netscape's server...
        ('Netscape', 'netscape_01.txt', ['aaaaa@corel.com',
                                         'bbbbb@corel.com']),
        # Yahoo's proprietary format
        ('Yahoo', 'yahoo_01.txt', ['userx@example.com']),
        ('Yahoo', 'yahoo_02.txt', ['userx@example.es']),
        ('Yahoo', 'yahoo_03.txt', ['userx@example.com']),
        ('Yahoo', 'yahoo_04.txt', ['userx@example.es',
                                   'usery@example.uk']),
        ('Yahoo', 'yahoo_05.txt', ['userx@example.com',
                                   'usery@example.com']),
        ('Yahoo', 'yahoo_06.txt', ['userx@example.com',
                                   'usery@example.com',
                                   'userz@example.com',
                                   'usera@example.com']),
        ('Yahoo', 'yahoo_07.txt', ['userw@example.com',
                                   'userx@example.com',
                                   'usery@example.com',
                                   'userz@example.com']),
        ('Yahoo', 'yahoo_08.txt', ['usera@example.com',
                                   'userb@example.com',
                                   'userc@example.com',
                                   'userd@example.com',
                                   'usere@example.com',
                                   'userf@example.com']),
        ('Yahoo', 'yahoo_09.txt', ['userx@example.com',
                                   'usery@example.com']),
        ('Yahoo', 'yahoo_10.txt', ['userx@example.com',
                                   'usery@example.com',
                                   'userz@example.com']),
        ('Yahoo', 'yahoo_11.txt', ['bad_user@aol.com']),
        # sina.com appears to use their own weird SINAEMAIL MTA
        ('Sina', 'sina_01.txt', ['userx@sina.com',
                                 'usery@sina.com']),
        ('AOL', 'aol_01.txt', ['screenname@aol.com']),
        # No address can be detected in these...
        # dumbass_01.txt - We love Microsoft. :(
        # Done
        )

    def test_bounce(self):
        for modname, file, addrs in self.DATA:
            module = 'Mailman.Bouncers.' + modname
            __import__(module)
            fp = open(os.path.join('tests', 'bounces', file))
            try:
                msg = email.message_from_file(fp)
            finally:
                fp.close()
            foundaddrs = sys.modules[module].process(msg)
            # Some modules return None instead of [] for failure
            if foundaddrs is None:
                foundaddrs = []
            if foundaddrs is not Stop:
                # MAS: The following strip() is only because of my
                # hybrid test environment.  It is not otherwise needed.
                foundaddrs = [found.strip() for found in foundaddrs]
                addrs.sort()
                foundaddrs.sort()
            self.assertEqual(addrs, foundaddrs)

    def test_SMTP32_failure(self):
        from Mailman.Bouncers import SMTP32
        # This file has no X-Mailer: header
        fp = open(os.path.join('tests', 'bounces', 'postfix_01.txt'))
        try:
            msg = email.message_from_file(fp)
        finally:
            fp.close()
        self.failIf(msg['x-mailer'] is not None)
        self.failIf(SMTP32.process(msg))

    def test_caiwireless(self):
        from Mailman.Bouncers import Caiwireless
        # BAW: this is a mostly bogus test; I lost the samples. :(
        msg = email.message_from_string("""\
Content-Type: multipart/report; boundary=BOUNDARY

--BOUNDARY

--BOUNDARY--

""")
        self.assertEqual(None, Caiwireless.process(msg))

    def test_microsoft(self):
        from Mailman.Bouncers import Microsoft
        # BAW: similarly as above, I lost the samples. :(
        msg = email.message_from_string("""\
Content-Type: multipart/report; boundary=BOUNDARY

--BOUNDARY

--BOUNDARY--

""")
        self.assertEqual(None, Microsoft.process(msg))



def suite():
    suite = unittest.TestSuite()
    suite.addTest(unittest.makeSuite(BounceTest))
    return suite



if __name__ == '__main__':
    unittest.main(defaultTest='suite')
