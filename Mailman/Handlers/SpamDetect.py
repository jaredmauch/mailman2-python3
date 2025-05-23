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

from __future__ import absolute_import, print_function, unicode_literals

import re
from unicodedata import normalize
from email.errors import HeaderParseError
from email.header import decode_header
from email.utils import parseaddr

from Mailman import mm_cfg
from Mailman import Errors
from Mailman import i18n
from Mailman import Utils
from Mailman.Handlers.Hold import hold_for_approval
from Mailman.Logging.Syslog import syslog
from Mailman.Message import Message

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


def getDecodedHeaders(msg, lcset):
    """Return a Unicode string containing all headers of msg, unfolded and RFC 2047
    decoded.  If a header cannot be decoded, it is replaced with a string of
    question marks.
    """
    headers = []
    for name in msg.keys():
        # Get all values for this header (could be multiple)
        for value in msg.get_all(name, []):
            try:
                # Format as "Header: Value"
                header_line = '%s: %s' % (name, value)
                # Ensure we have a string
                if isinstance(header_line, bytes):
                    header_line = header_line.decode('utf-8', 'replace')
                headers.append(header_line)
            except (UnicodeError, AttributeError):
                # If we can't decode it, replace with question marks
                headers.append('?' * len(str(value)))
    return '\n'.join(headers)


def process(mlist, msg, msgdata):
    # Check for Google Groups messages first
    google_groups_headers = [
        'X-Google-Groups-Id',
        'X-Google-Groups-Info',
        'X-Google-Groups-Url',
        'X-Google-Groups-Name',
        'X-Google-Groups-Email'
    ]
    
    for header in google_groups_headers:
        if msg.get(header):
            syslog('vette', 'Google Groups message detected via header %s, discarding', header)
            # Send bounce to the message's errors-to address
            try:
                bounce_msg = Message()
                bounce_msg['From'] = mlist.GetBounceEmail()
                # Use the message's errors-to header if present, otherwise use the From address
                bounce_to = msg.get('errors-to') or msg.get('from', 'unknown')
                bounce_msg['To'] = bounce_to
                bounce_msg['Subject'] = 'Message rejected: Google Groups not allowed'
                bounce_msg['Message-ID'] = Utils.unique_message_id(mlist)
                bounce_msg['Date'] = Utils.formatdate(localtime=True)
                bounce_msg['X-Mailman-From'] = msg.get('from', 'unknown')
                bounce_msg['X-Mailman-To'] = msg.get('to', 'unknown')
                bounce_msg['X-Mailman-List'] = mlist.internal_name()
                bounce_msg['X-Mailman-Reason'] = 'Google Groups messages are not allowed'
                
                # Include original message headers
                bounce_text = 'Original message headers:\n'
                for name, value in msg.items():
                    bounce_text += f'{name}: {value}\n'
                bounce_msg.set_payload(bounce_text)
                
                # Send the bounce
                mlist.BounceMessage(bounce_msg, msgdata)
                syslog('vette', 'Sent bounce to %s for rejected Google Groups message', bounce_to)
            except Exception as e:
                syslog('error', 'Failed to send bounce for Google Groups message: %s', str(e))
            
            # Discard the original message
            raise Errors.DiscardMessage

    # Before anything else, check DMARC if necessary.  We do this as early
    # as possible so reject/discard actions trump other holds/approvals and
    # wrap/munge actions get flagged even for approved messages.
    # But not for owner mail which should not be subject to DMARC reject or
    # discard actions.
    if not msgdata.get('toowner'):
        msgdata['from_is_list'] = 0
        dn, addr = parseaddr(msg.get('from', ''))
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
                    raise Errors.RejectMessage(text)
                elif mlist.dmarc_moderation_action == 4:
                    raise Errors.DiscardMessage

        # Get member address if any.
        for sender_tuple in msg.get_senders():
            # Extract email address from the (realname, address) tuple
            _, sender = sender_tuple
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
            if isinstance(value, bytes):
                value = value.decode('utf-8', 'replace')
            mo = cre.search(value)
            if mo:
                # we've detected spam, so throw the message away
                raise SpamDetected
    # Now do header_filter_rules
    # TK: Collect headers in sub-parts because attachment filename
    # extension may be a clue to possible virus/spam.
    headers = ''
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
