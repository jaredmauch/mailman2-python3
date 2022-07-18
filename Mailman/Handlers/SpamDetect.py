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

"""Do more detailed spam detection.

This module hard codes site wide spam detection.  By hacking the
KNOWN_SPAMMERS variable, you can set up more regular expression matches
against message headers.  If spam is detected the message is discarded
immediately.

TBD: This needs to be made more configurable and robust.
"""

import re

from unicodedata import normalize
from email.Errors import HeaderParseError
from email.Header import decode_header
from email.Utils import parseaddr

from Mailman import mm_cfg
from Mailman import Errors
from Mailman import i18n
from Mailman import Utils
from Mailman.Handlers.Hold import hold_for_approval
from Mailman.Logging.Syslog import syslog

try:
    True, False
except NameError:
    True = 1
    False = 0

# First, play footsie with _ so that the following are marked as translated,
# but aren't actually translated until we need the text later on.
def _(s):
    return s



class SpamDetected(Errors.DiscardMessage):
    """The message contains known spam"""

class HeaderMatchHold(Errors.HoldMessage):
    def __init__(self, pattern):
        self.__pattern = pattern

    def reason_notice(self):
        pattern = self.__pattern
        return _('Header matched regexp: %(pattern)s')


# And reset the translator
_ = i18n._



def getDecodedHeaders(msg, cset='utf-8'):
    """Returns a unicode containing all the headers of msg, unfolded and
    RFC 2047 decoded, normalized and separated by new lines.
    """

    headers = u''
    for h, v in msg.items():
        uvalue = u''
        try:
            v = decode_header(re.sub('\n\s', ' ', v))
        except HeaderParseError:
            v = [(v, 'us-ascii')]
        for frag, cs in v:
            if not cs:
                cs = 'us-ascii'
            try:
                uvalue += unicode(frag, cs, 'replace')
            except LookupError:
                # The encoding charset is unknown.  At this point, frag
                # has been QP or base64 decoded into a byte string whose
                # charset we don't know how to handle.  We will try to
                # unicode it as iso-8859-1 which may result in a garbled
                # mess, but we have to do something.
                uvalue += unicode(frag, 'iso-8859-1', 'replace')
        uhdr = h.decode('us-ascii', 'replace')
        headers += u'%s: %s\n' % (h, normalize(mm_cfg.NORMALIZE_FORM, uvalue))
    return headers



def process(mlist, msg, msgdata):
    # Before anything else, check DMARC if necessary.  We do this as early
    # as possible so reject/discard actions trump other holds/approvals and
    # wrap/munge actions get flagged even for approved messages.
    # But not for owner mail which should not be subject to DMARC reject or
    # discard actions.
    if not msgdata.get('toowner'):
        msgdata['from_is_list'] = 0
        dn, addr = parseaddr(msg.get('from'))
        if addr and mlist.dmarc_moderation_action > 0:
            if (mlist.GetPattern(addr, mlist.dmarc_moderation_addresses) or
                Utils.IsDMARCProhibited(mlist, addr)):
                # Note that for dmarc_moderation_action, 0 = Accept, 
                #    1 = Munge, 2 = Wrap, 3 = Reject, 4 = Discard
                if mlist.dmarc_moderation_action == 1:
                    msgdata['from_is_list'] = 1
                elif mlist.dmarc_moderation_action == 2:
                    msgdata['from_is_list'] = 2
                elif mlist.dmarc_moderation_action == 3:
                    # Reject
                    text = mlist.dmarc_moderation_notice
                    if text:
                        text = Utils.wrap(text)
                    else:
                        listowner = mlist.GetOwnerEmail()
                        text = Utils.wrap(_(
"""You are not allowed to post to this mailing list From: a domain which
publishes a DMARC policy of reject or quarantine, and your message has been
automatically rejected.  If you think that your messages are being rejected in
error, contact the mailing list owner at %(listowner)s."""))
                    raise Errors.RejectMessage, text
                elif mlist.dmarc_moderation_action == 4:
                    raise Errors.DiscardMessage

        # Get member address if any.
        for sender in msg.get_senders():
            if mlist.isMember(sender):
                break
        else:
            sender = msg.get_sender()
        if (mlist.member_verbosity_threshold > 0 and
            Utils.IsVerboseMember(mlist, sender)
           ):
             mlist.setMemberOption(sender, mm_cfg.Moderate, 1)
             syslog('vette',
                    '%s: Automatically Moderated %s for verbose postings.',
                     mlist.real_name, sender) 

    if msgdata.get('approved'):
        return
    # First do site hard coded header spam checks
    for header, regex in mm_cfg.KNOWN_SPAMMERS:
        cre = re.compile(regex, re.IGNORECASE)
        for value in msg.get_all(header, []):
            mo = cre.search(value)
            if mo:
                # we've detected spam, so throw the message away
                raise SpamDetected
    # Now do header_filter_rules
    # TK: Collect headers in sub-parts because attachment filename
    # extension may be a clue to possible virus/spam.
    headers = u''
    # Get the character set of the lists preferred language for headers
    lcset = Utils.GetCharSet(mlist.preferred_language)
    for p in msg.walk():
        headers += getDecodedHeaders(p, lcset)
    for patterns, action, empty in mlist.header_filter_rules:
        if action == mm_cfg.DEFER:
            continue
        for pattern in patterns.splitlines():
            if pattern.startswith('#'):
                continue
            # ignore 'empty' patterns
            if not pattern.strip():
                continue
            pattern = Utils.xml_to_unicode(pattern, lcset)
            pattern = normalize(mm_cfg.NORMALIZE_FORM, pattern)
            try:
                mo = re.search(pattern,
                               headers,
                               re.IGNORECASE|re.MULTILINE|re.UNICODE)
            except (re.error, TypeError):
                syslog('error',
                       'ignoring header_filter_rules invalid pattern: %s',
                       pattern)
            if mo:
                if action == mm_cfg.DISCARD:
                    raise Errors.DiscardMessage
                if action == mm_cfg.REJECT:
                    if msgdata.get('toowner'):
                        # Don't send rejection notice if addressed to '-owner'
                        # because it may trigger a loop of notices if the
                        # sender address is forged.  We just discard it here.
                        raise Errors.DiscardMessage
                    raise Errors.RejectMessage(
                        _('Message rejected by filter rule match'))
                if action == mm_cfg.HOLD:
                    if msgdata.get('toowner'):
                        # Don't hold '-owner' addressed message.  We just
                        # pass it here but list-owner can set this to be
                        # discarded on the GUI if he wants.
                        return
                    hold_for_approval(
                        mlist, msg, msgdata, HeaderMatchHold(pattern))
                if action == mm_cfg.ACCEPT:
                    return
