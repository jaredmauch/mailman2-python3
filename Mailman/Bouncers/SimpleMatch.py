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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301,
# USA.

"""Recognizes simple heuristically delimited bounces."""

import re
import email.Iterators



def _c(pattern):
    return re.compile(pattern, re.IGNORECASE)

# Pattern to match any valid email address and not much more.
VALID = _c(r'^[\x21-\x3d\x3f\x41-\x7e]+@[a-z0-9._]+$')

# This is a list of tuples of the form
#
#     (start cre, end cre, address cre)
#
# where `cre' means compiled regular expression, start is the line just before
# the bouncing address block, end is the line just after the bouncing address
# block, and address cre is the regexp that will recognize the addresses.  It
# must have a group called `addr' which will contain exactly and only the
# address that bounced.
PATTERNS = [
    # sdm.de
    (_c(r'here is your list of failed recipients'),
     _c(r'here is your returned mail'),
     _c(r'<(?P<addr>[^>]*)>')),
    # sz-sb.de, corridor.com, nfg.nl
    (_c(r'the following addresses had'),
     _c(r'transcript of session follows'),
     _c(r'^ *(\(expanded from: )?<?(?P<addr>[^\s@]+@[^\s@>]+?)>?\)?\s*$')),
    # robanal.demon.co.uk
    (_c(r'this message was created automatically by mail delivery software'),
     _c(r'original message follows'),
     _c(r'rcpt to:\s*<(?P<addr>[^>]*)>')),
    # s1.com (InterScan E-Mail VirusWall NT ???)
    (_c(r'message from interscan e-mail viruswall nt'),
     _c(r'end of message'),
     _c(r'rcpt to:\s*<(?P<addr>[^>]*)>')),
    # Smail
    (_c(r'failed addresses follow:'),
     _c(r'message text follows:'),
     _c(r'\s*(?P<addr>\S+@\S+)')),
    # newmail.ru
    (_c(r'This is the machine generated message from mail service.'),
     _c(r'--- Below the next line is a copy of the message.'),
     _c(r'<(?P<addr>[^>]*)>')),
    # turbosport.com runs something called `MDaemon 3.5.2' ???
    (_c(r'The following addresses did NOT receive a copy of your message:'),
     _c(r'--- Session Transcript ---'),
     _c(r'[>]\s*(?P<addr>.*)$')),
    # usa.net
    (_c(r'Intended recipient:\s*(?P<addr>.*)$'),
     _c(r'--------RETURNED MAIL FOLLOWS--------'),
     _c(r'Intended recipient:\s*(?P<addr>.*)$')),
    # hotpop.com
    (_c(r'Undeliverable Address:\s*(?P<addr>.*)$'),
     _c(r'Original message attached'),
     _c(r'Undeliverable Address:\s*(?P<addr>.*)$')),
    # Another demon.co.uk format
    (_c(r'This message was created automatically by mail delivery'),
     _c(r'^---- START OF RETURNED MESSAGE ----'),
     _c(r"addressed to '(?P<addr>[^']*)'")),
    # Prodigy.net full mailbox
    (_c(r"User's mailbox is full:"),
     _c(r'Unable to deliver mail.'),
     _c(r"User's mailbox is full:\s*<(?P<addr>[^>]*)>")),
    # Microsoft SMTPSVC
    (_c(r'The email below could not be delivered to the following user:'),
     _c(r'Old message:'),
     _c(r'<(?P<addr>[^>]*)>')),
    # Yahoo on behalf of other domains like sbcglobal.net
    (_c(r'Unable to deliver message to the following address\(es\)\.'),
     _c(r'--- Original message follows\.'),
     _c(r'<(?P<addr>[^>]*)>:')),
    # googlemail.com
    (_c(r'Delivery to the following recipient(s)? failed'),
     _c(r'----- Original message -----'),
     _c(r'^\s*(?P<addr>[^\s@]+@[^\s@]+)\s*$')),
    # kundenserver.de, mxlogic.net
    (_c(r'A message that you( have)? sent could not be delivered'),
     _c(r'^---'),
     _c(r'<(?P<addr>[^>]*)>')),
    # another kundenserver.de
    (_c(r'A message that you( have)? sent could not be delivered'),
     _c(r'^---'),
     _c(r'^(?P<addr>[^\s@]+@[^\s@:]+):')),
    # thehartford.com and amenworld.com
    (_c(r'Del(i|e)very to the following recipient(s)? (failed|was aborted)'),
     # this one may or may not have the original message, but there's nothing
     # unique to stop on, so stop on the first line of at least 3 characters
     # that doesn't start with 'D' (to not stop immediately) and has no '@'.
     _c(r'^[^D][^@]{2,}$'),
     _c(r'^\s*(. )?(?P<addr>[^\s@]+@[^\s@]+)\s*$')),
    # and another thehartfod.com/hartfordlife.com
    (_c(r'^Your message\s*$'),
     _c(r'^because:'),
     _c(r'^\s*(?P<addr>[^\s@]+@[^\s@]+)\s*$')),
    # kviv.be (InterScan NT)
    (_c(r'^Unable to deliver message to'),
     _c(r'\*+\s+End of message\s+\*+'),
     _c(r'<(?P<addr>[^>]*)>')),
    # earthlink.net supported domains
    (_c(r'^Sorry, unable to deliver your message to'),
     _c(r'^A copy of the original message'),
     _c(r'\s*(?P<addr>[^\s@]+@[^\s@]+)\s+')),
    # ademe.fr
    (_c(r'^A message could not be delivered to:'),
     _c(r'^Subject:'),
     _c(r'^\s*(?P<addr>[^\s@]+@[^\s@]+)\s*$')),
    # andrew.ac.jp
    (_c(r'^Invalid final delivery userid:'),
     _c(r'^Original message follows.'),
     _c(r'\s*(?P<addr>[^\s@]+@[^\s@]+)\s*$')),
    # E500_SMTP_Mail_Service@lerctr.org and similar
    (_c(r'---- Failed Recipients ----'),
     _c(r' Mail ----'),
     _c(r'<(?P<addr>[^>]*)>')),
    # cynergycom.net
    (_c(r'A message that you sent could not be delivered'),
     _c(r'^---'),
     _c(r'(?P<addr>[^\s@]+@[^\s@)]+)')),
    # LSMTP for Windows
    (_c(r'^--> Error description:\s*$'),
     _c(r'^Error-End:'),
     _c(r'^Error-for:\s+(?P<addr>[^\s@]+@[^\s@]+)')),
    # Qmail with a tri-language intro beginning in spanish
    (_c(r'Your message could not be delivered'),
     _c(r'^-'),
     _c(r'<(?P<addr>[^>]*)>:')),
    # socgen.com
    (_c(r'Your message could not be delivered to'),
     _c(r'^\s*$'),
     _c(r'(?P<addr>[^\s@]+@[^\s@]+)')),
    # dadoservice.it
    (_c(r'Your message has encountered delivery problems'),
     _c(r'Your message reads'),
     _c(r'addressed to\s*(?P<addr>[^\s@]+@[^\s@)]+)')),
    # gomaps.com
    (_c(r'Did not reach the following recipient'),
     _c(r'^\s*$'),
     _c(r'\s(?P<addr>[^\s@]+@[^\s@]+)')),
    # EYOU MTA SYSTEM
    (_c(r'This is the deliver program at'),
     _c(r'^-'),
     _c(r'^(?P<addr>[^\s@]+@[^\s@<>]+)')),
    # A non-standard qmail at ieo.it
    (_c(r'this is the email server at'),
     _c(r'^-'),
     _c(r'\s(?P<addr>[^\s@]+@[^\s@]+)[\s,]')),
    # pla.net.py (MDaemon.PRO ?)
    (_c(r'- no such user here'),
     _c(r'There is no user'),
     _c(r'^(?P<addr>[^\s@]+@[^\s@]+)\s')),
    # fastdnsservers.com
    (_c(r'The following recipient.*could not be reached'),
     _c(r'bogus stop pattern'),
     _c(r'^(?P<addr>[^\s@]+@[^\s@]+)\s*$')),
    # lttf.com
    (_c(r'Could not deliver message to'),
     _c(r'^\s*--'),
     _c(r'^Failed Recipient:\s*(?P<addr>[^\s@]+@[^\s@]+)\s*$')),
    # uci.edu
    (_c(r'--------Message not delivered'),
     _c(r'--------Error Detail'),
     _c(r'^\s*(?P<addr>[^\s@]+@[^\s@]+)\s*$')),
    # Dovecot LDA Over quota MDN (bogus - should be DSN).
    (_c(r'^Your message'),
     _c(r'^Reporting'),
     _c(
        r'Your message to (?P<addr>[^\s@]+@[^\s@]+) was automatically rejected'
       )),
    # mail.ru
    (_c(r'A message that you sent was rejected'),
     _c(r'This is a copy of your message'),
     _c(r'\s(?P<addr>[^\s@]+@[^\s@]+)')),
    # MailEnable
    (_c(r'Message could not be delivered to some recipients.'),
     _c(r'Message headers follow'),
     _c(r'Recipient: \[SMTP:(?P<addr>[^\s@]+@[^\s@]+)\]')),
    # This one is from Yahoo but dosen't fit the yahoo recognizer format
    (_c(r'wasn\'t able to deliver the following message'),
     _c(r'---Below this line is a copy of the message.'),
     _c(r'To: (?P<addr>[^\s@]+@[^\s@]+)')),
    # From some unknown MTA
    (_c(r'This is a delivery failure notification message'),
     _c(r'The problem appears to be'),
     _c(r'-- (?P<addr>[^\s@]+@[^\s@]+)')),
    # Next one goes here...
    ]



def process(msg, patterns=None):
    if patterns is None:
        patterns = PATTERNS
    # simple state machine
    #     0 = nothing seen yet
    #     1 = intro seen
    addrs = {}
    # MAS: This is a mess. The outer loop used to be over the message
    # so we only looped through the message once.  Looping through the
    # message for each set of patterns is obviously way more work, but
    # if we don't do it, problems arise because scre from the wrong
    # pattern set matches first and then acre doesn't match.  The
    # alternative is to split things into separate modules, but then
    # we process the message multiple times anyway.
    for scre, ecre, acre in patterns:
        state = 0
        for line in email.Iterators.body_line_iterator(msg, decode=True):
            if state == 0:
                if scre.search(line):
                    state = 1
            if state == 1:
                mo = acre.search(line)
                if mo:
                    addr = mo.group('addr')
                    if addr:
                        addrs[addr.strip('<>')] = 1
                elif ecre.search(line):
                    break
        if addrs:
            break
    return [x for x in list(addrs.keys()) if VALID.match(x)]
