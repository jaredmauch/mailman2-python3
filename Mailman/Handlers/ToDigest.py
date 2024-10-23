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

"""Add the message to the list's current digest and possibly send it."""
from __future__ import print_function

# Messages are accumulated to a Unix mailbox compatible file containing all
# the messages destined for the digest.  This file must be parsable by the
# mailbox.UnixMailbox class (i.e. it must be ^From_ quoted).
#
# When the file reaches the size threshold, it is moved to the qfiles/digest
# directory and the DigestRunner will craft the MIME, rfc1153, and
# (eventually) URL-subject linked digests from the mbox.

from builtins import str
import os
import re
import copy
import time
import traceback
from io import StringIO

from email.parser import Parser
from email.generator import Generator
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.message import MIMEMessage
from email.utils import getaddresses, formatdate
from email.header import decode_header, make_header, Header
from email.charset import Charset

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Message
from Mailman import i18n
from Mailman import Errors
from Mailman.Mailbox import Mailbox
from Mailman.MemberAdaptor import ENABLED
from Mailman.Handlers.Decorate import decorate
from Mailman.Queue.sbcache import get_switchboard
from Mailman.Mailbox import Mailbox
from Mailman.Handlers.Scrubber import process as scrubber
from Mailman.Logging.Syslog import syslog

_ = i18n._

UEMPTYSTRING = u''
EMPTYSTRING = ''


def to_cset_out(text, lcset):
    # Convert text from unicode or lcset to output cset.
    ocset = Charset(lcset).get_output_charset() or lcset
    if isinstance(text, str):
        return text.encode(ocset, 'replace')
    else:
        return text.decode(lcset, 'replace').encode(ocset, 'replace')



def process(mlist, msg, msgdata):
    # Short circuit non-digestable lists.
    if not mlist.digestable or msgdata.get('isdigest'):
        return
    mboxfile = os.path.join(mlist.fullpath(), 'digest.mbox')
    omask = os.umask(0o007)
    try:
        mboxfp = open(mboxfile, 'a+')
    finally:
        os.umask(omask)
    mbox = Mailbox(mboxfp)
    mbox.AppendMessage(msg)
    # Calculate the current size of the accumulation file.  This will not tell
    # us exactly how big the MIME, rfc1153, or any other generated digest
    # message will be, but it's the most easily available metric to decide
    # whether the size threshold has been reached.
    mboxfp.flush()
    size = os.path.getsize(mboxfile)
    if (mlist.digest_size_threshhold > 0 and
        size / 1024.0 >= mlist.digest_size_threshhold):
        # This is a bit of a kludge to get the mbox file moved to the digest
        # queue directory.
        try:
            # Enclose in try/except here because a error in send_digest() can
            # silently stop regular delivery.  Unsuccessful digest delivery
            # should be tried again by cron and the site administrator will be
            # notified of any error explicitly by the cron error message.
            mboxfp.seek(0)
            send_digests(mlist, mboxfp)
            os.unlink(mboxfile)
        except Exception as errmsg:
            # Bare except is generally prohibited in Mailman, but we can't
            # forecast what exceptions can occur here.
            syslog('error', 'send_digests() failed: %s', errmsg)
            s = StringIO()
            traceback.print_exc(file=s)
            syslog('error', s.getvalue())
    mboxfp.close()



def send_digests(mlist, mboxfp):
    # Set the digest volume and time
    if mlist.digest_last_sent_at:
        bump = False
        # See if we should bump the digest volume number
        timetup = time.localtime(mlist.digest_last_sent_at)
        now = time.localtime(time.time())
        freq = mlist.digest_volume_frequency
        if freq == 0 and timetup[0] < now[0]:
            # Yearly
            bump = True
        elif freq == 1 and timetup[1] != now[1]:
            # Monthly, but we take a cheap way to calculate this.  We assume
            # that the clock isn't going to be reset backwards.
            bump = True
        elif freq == 2 and (timetup[1] % 4 != now[1] % 4):
            # Quarterly, same caveat
            bump = True
        elif freq == 3:
            # Once again, take a cheap way of calculating this
            weeknum_last = int(time.strftime('%W', timetup))
            weeknum_now = int(time.strftime('%W', now))
            if weeknum_now > weeknum_last or timetup[0] > now[0]:
                bump = True
        elif freq == 4 and timetup[7] != now[7]:
            # Daily
            bump = True
        if bump:
            mlist.bump_digest_volume()
    mlist.digest_last_sent_at = time.time()
    # Wrapper around actually digest crafter to set up the language context
    # properly.  All digests are translated to the list's preferred language.
    otranslation = i18n.get_translation()
    i18n.set_language(mlist.preferred_language)
    try:
        send_i18n_digests(mlist, mboxfp)
    finally:
        i18n.set_translation(otranslation)



def send_i18n_digests(mlist, mboxfp):
    mbox = Mailbox(mboxfp)
    # Prepare common information (first lang/charset)
    lang = mlist.preferred_language
    lcset = Utils.GetCharSet(lang)
    lcset_out = Charset(lcset).output_charset or lcset
    # Common Information (contd)
    realname = mlist.real_name
    volume = mlist.volume
    issue = mlist.next_digest_number
    digestid = _('%(realname)s Digest, Vol %(volume)d, Issue %(issue)d')
    digestsubj = Header(digestid, lcset, header_name='Subject')
    # Set things up for the MIME digest.  Only headers not added by
    # CookHeaders need be added here.
    # Date/Message-ID should be added here also.
    mimemsg = Message.Message()
    mimemsg['Content-Type'] = 'multipart/mixed'
    mimemsg['MIME-Version'] = '1.0'
    mimemsg['From'] = mlist.GetRequestEmail()
    mimemsg['Subject'] = digestsubj
    mimemsg['To'] = mlist.GetListEmail()
    mimemsg['Reply-To'] = mlist.GetListEmail()
    mimemsg['Date'] = formatdate(localtime=1)
    mimemsg['Message-ID'] = Utils.unique_message_id(mlist)
    # Set things up for the rfc1153 digest
    plainmsg = StringIO()
    rfc1153msg = Message.Message()
    rfc1153msg['From'] = mlist.GetRequestEmail()
    rfc1153msg['Subject'] = digestsubj
    rfc1153msg['To'] = mlist.GetListEmail()
    rfc1153msg['Reply-To'] = mlist.GetListEmail()
    rfc1153msg['Date'] = formatdate(localtime=1)
    rfc1153msg['Message-ID'] = Utils.unique_message_id(mlist)
    separator70 = '-' * 70
    separator30 = '-' * 30
    # In the rfc1153 digest, the masthead contains the digest boilerplate plus
    # any digest header.  In the MIME digests, the masthead and digest header
    # are separate MIME subobjects.  In either case, it's the first thing in
    # the digest, and we can calculate it now, so go ahead and add it now.
    mastheadtxt = Utils.maketext(
        'masthead.txt',
        {'real_name' :        mlist.real_name,
         'got_list_email':    mlist.GetListEmail(),
         'got_listinfo_url':  mlist.GetScriptURL('listinfo', absolute=1),
         'got_request_email': mlist.GetRequestEmail(),
         'got_owner_email':   mlist.GetOwnerEmail(),
         }, mlist=mlist)
    # MIME
    masthead = MIMEText(mastheadtxt, _charset=lcset)
    masthead['Content-Description'] = digestid
    mimemsg.attach(masthead)
    # RFC 1153
    print(mastheadtxt, file=plainmsg)
    print(file=plainmsg)
    # Now add the optional digest header but only if more than whitespace.
    if re.sub(r'\s', '', mlist.digest_header):
        headertxt = decorate(mlist, mlist.digest_header, _('digest header'))
        # MIME
        header = MIMEText(headertxt, _charset=lcset)
        header['Content-Description'] = _('Digest Header')
        mimemsg.attach(header)
        # RFC 1153
        print(headertxt, file=plainmsg)
        print(file=plainmsg)
    # Now we have to cruise through all the messages accumulated in the
    # mailbox file.  We can't add these messages to the plainmsg and mimemsg
    # yet, because we first have to calculate the table of contents
    # (i.e. grok out all the Subjects).  Store the messages in a list until
    # we're ready for them.
    #
    # Meanwhile prepare things for the table of contents
    toc = StringIO()
    print(_("Today's Topics:\n"), file=toc)
    # Now cruise through all the messages in the mailbox of digest messages,
    # building the MIME payload and core of the RFC 1153 digest.  We'll also
    # accumulate Subject: headers and authors for the table-of-contents.
    messages = []
    msgcount = 0
    msg = next(mbox)
    while msg is not None:
        if msg == '':
            # It was an unparseable message
            msg = next(mbox)
            continue
        msgcount += 1
        messages.append(msg)
        # Get the Subject header
        msgsubj = msg.get('subject', _('(no subject)'))
        subject = Utils.oneline(msgsubj, lcset)
        # Don't include the redundant subject prefix in the toc
        mo = re.match('(re:? *)?(%s)' % re.escape(mlist.subject_prefix),
                      subject, re.IGNORECASE)
        if mo:
            subject = subject[:mo.start(2)] + subject[mo.end(2):]
        username = ''
        addresses = getaddresses([Utils.oneline(msg.get('from', ''), lcset)])
        # Take only the first author we find
        if isinstance(addresses, ListType) and addresses:
            username = addresses[0][0]
            if not username:
                username = addresses[0][1]
        if username:
            username = ' (%s)' % username
        # Put count and Wrap the toc subject line
        wrapped = Utils.wrap('%2d. %s' % (msgcount, subject), 65)
        slines = wrapped.split('\n')
        # See if the user's name can fit on the last line
        if len(slines[-1]) + len(username) > 70:
            slines.append(username)
        else:
            slines[-1] += username
        # Add this subject to the accumulating topics
        first = True
        for line in slines:
            if first:
                print(' ', line, file=toc)
                first = False
            else:
                print('     ', line.lstrip(), file=toc)
        # We do not want all the headers of the original message to leak
        # through in the digest messages.  For this phase, we'll leave the
        # same set of headers in both digests, i.e. those required in RFC 1153
        # plus a couple of other useful ones.  We also need to reorder the
        # headers according to RFC 1153.  Later, we'll strip out headers for
        # for the specific MIME or plain digests.
        keeper = {}
        all_keepers = {}
        for header in (mm_cfg.MIME_DIGEST_KEEP_HEADERS +
                       mm_cfg.PLAIN_DIGEST_KEEP_HEADERS):
            all_keepers[header] = True
        all_keepers = list(all_keepers.keys())
        for keep in all_keepers:
            keeper[keep] = msg.get_all(keep, [])
        # Now remove all unkempt headers :)
        for header in list(msg.keys()):
            del msg[header]
        # And add back the kept header in the RFC 1153 designated order
        for keep in all_keepers:
            for field in keeper[keep]:
                msg[keep] = field
        # And a bit of extra stuff
        msg['Message'] = repr(msgcount)
        # Get the next message in the digest mailbox
        msg = next(mbox)
    # Now we're finished with all the messages in the digest.  First do some
    # sanity checking and then on to adding the toc.
    if msgcount == 0:
        # Why did we even get here?
        return
    toctext = to_cset_out(toc.getvalue(), lcset)
    # MIME
    tocpart = MIMEText(toctext, _charset=lcset)
    tocpart['Content-Description']= _("Today's Topics (%(msgcount)d messages)")
    mimemsg.attach(tocpart)
    # RFC 1153
    print(toctext, file=plainmsg)
    print(file=plainmsg)
    # For RFC 1153 digests, we now need the standard separator
    print(separator70, file=plainmsg)
    print(file=plainmsg)
    # Now go through and add each message
    mimedigest = MIMEBase('multipart', 'digest')
    mimemsg.attach(mimedigest)
    first = True
    for msg in messages:
        # MIME.  Make a copy of the message object since the rfc1153
        # processing scrubs out attachments.
        mimedigest.attach(MIMEMessage(copy.deepcopy(msg)))
        # rfc1153
        if first:
            first = False
        else:
            print(separator30, file=plainmsg)
            print(file=plainmsg)
        # Use Mailman.Handlers.Scrubber.process() to get plain text
        try:
            msg = scrubber(mlist, msg)
        except Errors.DiscardMessage:
            print(_('[Message discarded by content filter]'), file=plainmsg)
            continue
        # Honor the default setting
        for h in mm_cfg.PLAIN_DIGEST_KEEP_HEADERS:
            if msg[h]:
                uh = Utils.wrap('%s: %s' % (h, Utils.oneline(msg[h], lcset)))
                uh = '\n\t'.join(uh.split('\n'))
                print(uh, file=plainmsg)
        print(file=plainmsg)
        # If decoded payload is empty, this may be multipart message.
        # -- just stringfy it.
        payload = msg.get_payload(decode=True) \
                  or msg.as_string().split('\n\n',1)[1]
        mcset = msg.get_content_charset('')
        if mcset and mcset != lcset and mcset != lcset_out:
            try:
                payload = str(payload, mcset, 'replace'
                          ).encode(lcset, 'replace')
            except (UnicodeError, LookupError):
                # TK: Message has something unknown charset.
                #     _out means charset in 'outer world'.
                payload = str(payload, lcset_out, 'replace'
                          ).encode(lcset, 'replace')
        print(payload, file=plainmsg)
        if not payload.endswith('\n'):
            print(file=plainmsg)
    # Now add the footer but only if more than whitespace.
    if re.sub(r'\s', '', mlist.digest_footer):
        footertxt = decorate(mlist, mlist.digest_footer, _('digest footer'))
        # MIME
        footer = MIMEText(footertxt, _charset=lcset)
        footer['Content-Description'] = _('Digest Footer')
        mimemsg.attach(footer)
        # RFC 1153
        # MAS: There is no real place for the digest_footer in an RFC 1153
        # compliant digest, so add it as an additional message with
        # Subject: Digest Footer
        print(separator30, file=plainmsg)
        print(file=plainmsg)
        print('Subject: ' + _('Digest Footer'), file=plainmsg)
        print(file=plainmsg)
        print(footertxt, file=plainmsg)
        print(file=plainmsg)
        print(separator30, file=plainmsg)
        print(file=plainmsg)
    # Do the last bit of stuff for each digest type
    signoff = _('End of ') + digestid
    # MIME
    # BAW: This stuff is outside the normal MIME goo, and it's what the old
    # MIME digester did.  No one seemed to complain, probably because you
    # won't see it in an MUA that can't display the raw message.  We've never
    # got complaints before, but if we do, just wax this.  It's primarily
    # included for (marginally useful) backwards compatibility.
    mimemsg.postamble = signoff
    # rfc1153
    print(signoff, file=plainmsg)
    print('*' * len(signoff), file=plainmsg)
    # Do our final bit of housekeeping, and then send each message to the
    # outgoing queue for delivery.
    mlist.next_digest_number += 1
    virginq = get_switchboard(mm_cfg.VIRGINQUEUE_DIR)
    # Calculate the recipients lists
    plainrecips = []
    mimerecips = []
    drecips = mlist.getDigestMemberKeys() + list(mlist.one_last_digest.keys())
    for user in mlist.getMemberCPAddresses(drecips):
        # user might be None if someone who toggled off digest delivery
        # subsequently unsubscribed from the mailing list.  Also, filter out
        # folks who have disabled delivery.
        if user is None or mlist.getDeliveryStatus(user) != ENABLED:
            continue
        # Otherwise, decide whether they get MIME or RFC 1153 digests
        if mlist.getMemberOption(user, mm_cfg.DisableMime):
            plainrecips.append(user)
        else:
            mimerecips.append(user)
    # Zap this since we're now delivering the last digest to these folks.
    mlist.one_last_digest.clear()
    # MIME
    virginq.enqueue(mimemsg,
                    recips=mimerecips,
                    listname=mlist.internal_name(),
                    isdigest=True)
    # RFC 1153
    rfc1153msg.set_payload(to_cset_out(plainmsg.getvalue(), lcset), lcset)
    virginq.enqueue(rfc1153msg,
                    recips=plainrecips,
                    listname=mlist.internal_name(),
                    isdigest=True)
