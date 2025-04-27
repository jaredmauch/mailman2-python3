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

"""Produce and process the pending-approval items for a list."""
from __future__ import print_function

from builtins import zip
from builtins import str
import sys
import os
import urllib.parse
import errno
import signal
import email
import email.errors
import time
from urllib.parse import quote_plus, unquote_plus
import re
from email.iterators import body_line_iterator

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import MailList
from Mailman import Errors
from Mailman import Message
from Mailman import i18n
from Mailman.Handlers.Moderate import ModeratedMemberPost
from Mailman.ListAdmin import HELDMSG
from Mailman.ListAdmin import readMessage
from Mailman.Cgi import Auth
from Mailman.htmlformat import *
from Mailman.Logging.Syslog import syslog
from Mailman.CSRFcheck import csrf_check

EMPTYSTRING = ''
NL = '\n'

# Set up i18n.  Until we know which list is being requested, we use the
# server's default.
_ = i18n._
i18n.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)

EXCERPT_HEIGHT = 10
EXCERPT_WIDTH = 76
SSENDER = mm_cfg.SSENDER
SSENDERTIME = mm_cfg.SSENDERTIME
STIME = mm_cfg.STIME
if mm_cfg.DISPLAY_HELD_SUMMARY_SORT_BUTTONS in (SSENDERTIME, STIME):
    ssort = mm_cfg.DISPLAY_HELD_SUMMARY_SORT_BUTTONS
else:
    ssort = SSENDER

AUTH_CONTEXTS = (mm_cfg.AuthListModerator, mm_cfg.AuthListAdmin,
                 mm_cfg.AuthSiteAdmin)


def helds_by_skey(mlist, ssort=SSENDER):
    heldmsgs = mlist.GetHeldMessageIds()
    byskey = {}
    for id in heldmsgs:
        ptime = mlist.GetRecord(id)[0]
        sender = mlist.GetRecord(id)[1]
        if ssort in (SSENDER, SSENDERTIME):
            skey = (0, sender)
        else:
            skey = (ptime, sender)
        byskey.setdefault(skey, []).append((ptime, id))
    # Sort groups by time
    for k, v in list(byskey.items()):
        if len(v) > 1:
            v.sort()
            byskey[k] = v
        if ssort == SSENDERTIME:
            # Rekey with time
            newkey = (v[0][0], k[1])
            del byskey[k]
            byskey[newkey] = v
    return byskey


def hacky_radio_buttons(btnname, labels, values, defaults, spacing=3):
    # We can't use a RadioButtonArray here because horizontal placement can be
    # confusing to the user and vertical placement takes up too much
    # real-estate.  This is a hack!
    space = '&nbsp;' * spacing
    btns = Table(cellspacing='5', cellpadding='0')
    btns.AddRow([space + text + space for text in labels])
    btns.AddRow([Center(RadioButton(btnname, value, default).Format()
                     + '<div class=hidden>' + label + '</div>')
                 for label, value, default in zip(labels, values, defaults)])
    return btns


def main():
    doc = Document()
    doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)

    try:
        if os.environ.get('REQUEST_METHOD') == 'POST':
            content_length = int(os.environ.get('CONTENT_LENGTH', 0))
            if content_length > 0:
                form_data = sys.stdin.read(content_length)
                cgidata = urllib.parse.parse_qs(form_data, keep_blank_values=True)
            else:
                cgidata = {}
        else:
            query_string = os.environ.get('QUERY_STRING', '')
            cgidata = urllib.parse.parse_qs(query_string, keep_blank_values=True)
    except Exception:
        # Someone crafted a POST with a bad Content-Type:.
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script.')))
        # Send this with a 400 status.
        print('Status: 400 Bad Request')
        print(doc.Format())
        return

    parts = Utils.GetPathPieces()
    if not parts:
        handle_no_list()
        return

    listname = parts[0].lower()
    try:
        mlist = MailList.MailList(listname, lock=0)
    except Errors.MMListError as e:
        # Avoid cross-site scripting attacks
        safelistname = Utils.websafe(listname)
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('No such list <em>{safelistname}</em>')))
        # Send this with a 404 status.
        print('Status: 404 Not Found')
        print(doc.Format())
        syslog('error', 'admindb: No such list "%s": %s\n', listname, e)
        return

    # Now that we have a valid mailing list, set the language
    i18n.set_language(mlist.preferred_language)
    doc.set_language(mlist.preferred_language)

    # Must be authenticated to get any farther
    if not mlist.WebAuthenticate(AUTH_CONTEXTS, cgidata.get('adminpw', [''])[0]):
        if 'admlogin' in cgidata:
            # This is a re-authorization attempt
            msg = Bold(FontSize('+1', _('Authorization failed.'))).Format()
            remote = os.environ.get('HTTP_FORWARDED_FOR',
                     os.environ.get('HTTP_X_FORWARDED_FOR',
                     os.environ.get('REMOTE_ADDR',
                                    'unidentified origin')))
            syslog('security',
                   'Authorization failed (admindb): list=%s: remote=%s',
                   listname, remote)
        else:
            msg = ''
        Auth.loginpage(mlist, 'admindb', msg=msg)
        return

    # We need a signal handler to catch the SIGTERM that can come from Apache
    # when the user hits the browser's STOP button.  See the comment in
    # admin.py for details.
    def sigterm_handler(signum, frame, mlist=mlist):
        # Make sure the list gets unlocked...
        mlist.Unlock()
        # ...and ensure we exit, otherwise race conditions could cause us to
        # enter MailList.Save() while we're in the unlocked state, and that
        # could be bad!
        sys.exit(0)

    mlist.Lock()
    try:
        # Install the emergency shutdown signal handler
        signal.signal(signal.SIGTERM, sigterm_handler)

        process_form(mlist, doc, cgidata)
        mlist.Save()
    finally:
        mlist.Unlock()


def handle_no_list(msg=''):
    # Print something useful if no list was given.
    doc = Document()
    doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)

    header = _('Mailman Administrative Database Error')
    doc.SetTitle(header)
    doc.AddItem(Header(2, header))
    doc.AddItem(msg)
    url = Utils.ScriptURL('admin', absolute=1)
    link = Link(url, _('list of available mailing lists.')).Format()
    doc.AddItem(_(f'You must specify a list name.  Here is the {link}'))
    doc.AddItem('<hr>')
    doc.AddItem(MailmanLogo())
    print(doc.Format())


def show_pending_subs(mlist, form):
    # Add the subscription request section
    pendingsubs = mlist.GetSubscriptionIds()
    if not pendingsubs:
        return 0
    form.AddItem('<hr>')
    form.AddItem(Center(Header(2, _('Subscription Requests'))))
    table = Table(border=2)
    table.AddRow([Center(Bold(_('Address/name/time'))),
                  Center(Bold(_('Your decision'))),
                  Center(Bold(_('Reason for refusal')))
                  ])
    # Alphabetical order by email address
    byaddrs = {}
    for id in pendingsubs:
        addr = mlist.GetRecord(id)[1]
        byaddrs.setdefault(addr, []).append(id)
    addrs = list(byaddrs.items())
    addrs.sort()
    num = 0
    for addr, ids in addrs:
        # Eliminate duplicates.
        # The list ws returned sorted ascending.  Keep the last.
        for id in ids[:-1]:
            mlist.HandleRequest(id, mm_cfg.DISCARD)
        id = ids[-1]
        stime, addr, fullname, passwd, digest, lang = mlist.GetRecord(id)
        fullname = Utils.uncanonstr(fullname, mlist.preferred_language)
        displaytime = time.ctime(stime)
        radio = RadioButtonArray(id, (_('Defer'),
                                      _('Approve'),
                                      _('Reject'),
                                      _('Discard')),
                                 values=(mm_cfg.DEFER,
                                         mm_cfg.SUBSCRIBE,
                                         mm_cfg.REJECT,
                                         mm_cfg.DISCARD),
                                 checked=0).Format()
        if addr not in mlist.ban_list:
            radio += ('<br>' + '<label>' +
                     CheckBox(f'ban-%d' % id, 1).Format() +
                     '&nbsp;' + _('Permanently ban from this list') +
                     '</label>')
        # While the address may be a unicode, it must be ascii
        paddr = addr.encode('us-ascii', 'replace')
        table.AddRow(['%s<br><em>%s</em><br>%s' % (paddr,
                                                   Utils.websafe(fullname),
                                                   displaytime),
                      radio,
                      TextBox(f'comment-%d' % id, size=40)
                      ])
        num += 1
    if num > 0:
        form.AddItem(table)
    return num


def show_pending_unsubs(mlist, form):
    # Add the pending unsubscription request section
    lang = mlist.preferred_language
    pendingunsubs = mlist.GetUnsubscriptionIds()
    if not pendingunsubs:
        return 0
    table = Table(border=2)
    table.AddRow([Center(Bold(_('User address/name'))),
                  Center(Bold(_('Your decision'))),
                  Center(Bold(_('Reason for refusal')))
                  ])
    # Alphabetical order by email address
    byaddrs = {}
    for id in pendingunsubs:
        addr = mlist.GetRecord(id)
        byaddrs.setdefault(addr, []).append(id)
    addrs = list(byaddrs.items())
    addrs.sort()
    num = 0
    for addr, ids in addrs:
        # Eliminate duplicates
        # Here the order doesn't matter as the data is just the address.
        for id in ids[1:]:
            mlist.HandleRequest(id, mm_cfg.DISCARD)
        id = ids[0]
        addr = mlist.GetRecord(id)
        try:
            fullname = Utils.uncanonstr(mlist.getMemberName(addr), lang)
        except Errors.NotAMemberError:
            # They must have been unsubscribed elsewhere, so we can just
            # discard this record.
            mlist.HandleRequest(id, mm_cfg.DISCARD)
            continue
        num += 1
        table.AddRow(['%s<br><em>%s</em>' % (addr, Utils.websafe(fullname)),
                      RadioButtonArray(id, (_('Defer'),
                                            _('Approve'),
                                            _('Reject'),
                                            _('Discard')),
                                       values=(mm_cfg.DEFER,
                                               mm_cfg.UNSUBSCRIBE,
                                               mm_cfg.REJECT,
                                               mm_cfg.DISCARD),
                                       checked=0),
                      TextBox(f'comment-%d' % id, size=45)
                      ])
    if num > 0:
        form.AddItem('<hr>')
        form.AddItem(Center(Header(2, _('Unsubscription Requests'))))
        form.AddItem(table)
    return num


def show_helds_overview(mlist, form, ssort=SSENDER):
    # Sort the held messages.
    byskey = helds_by_skey(mlist, ssort)
    if not byskey:
        return 0
    form.AddItem('<hr>')
    form.AddItem(Center(Header(2, _('Held Messages'))))
    # Add the sort sequence choices if wanted
    if mm_cfg.DISPLAY_HELD_SUMMARY_SORT_BUTTONS:
        form.AddItem(Center(_('Show this list grouped/sorted by')))
        form.AddItem(Center(hacky_radio_buttons(
                'summary_sort',
                (_('sender/sender'), _('sender/time'), _('ungrouped/time')),
                (SSENDER, SSENDERTIME, STIME),
                (ssort == SSENDER, ssort == SSENDERTIME, ssort == STIME))))
    # Add the by-sender overview tables
    admindburl = mlist.GetScriptURL('admindb', absolute=1)
    table = Table(border=0)
    form.AddItem(table)
    skeys = list(byskey.keys())
    skeys.sort()
    for skey in skeys:
        sender = skey[1]
        qsender = quote_plus(sender)
        esender = Utils.websafe(sender)
        senderurl = admindburl + '?sender=' + qsender
        # The encompassing sender table
        stable = Table(border=1)
        stable.AddRow([Center(Bold(_('From:')).Format() + esender)])
        stable.AddCellInfo(stable.GetCurrentRowIndex(), 0, colspan=2)
        left = Table(border=0)
        left.AddRow([_('Action to take on all these held messages:')])
        left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
        btns = hacky_radio_buttons(
            'senderaction-' + qsender,
            (_('Defer'), _('Accept'), _('Reject'), _('Discard')),
            (mm_cfg.DEFER, mm_cfg.APPROVE, mm_cfg.REJECT, mm_cfg.DISCARD),
            (1, 0, 0, 0))
        left.AddRow([btns])
        left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
        left.AddRow([
            '<label>' +
            CheckBox('senderpreserve-' + qsender, 1).Format() +
            '&nbsp;' +
            _('Preserve messages for the site administrator') +
            '</label>'
            ])
        left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
        left.AddRow([
            '<label>' +
            CheckBox('senderforward-' + qsender, 1).Format() +
            '&nbsp;' +
            _('Forward messages (individually) to:') +
            '</label>'
            ])
        left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
        left.AddRow([
            TextBox('senderforwardto-' + qsender,
                    value=mlist.GetOwnerEmail())
            ])
        left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
        # If the sender is a member and the message is being held due to a
        # moderation bit, give the admin a chance to clear the member's mod
        # bit.  If this sender is not a member and is not already on one of
        # the sender filters, then give the admin a chance to add this sender
        # to one of the filters.
        if mlist.isMember(sender):
            if mlist.getMemberOption(sender, mm_cfg.Moderate):
                left.AddRow([
                    '<label>' +
                    CheckBox('senderclearmodp-' + qsender, 1).Format() +
                    '&nbsp;' +
                    _("Clear this member's <em>moderate</em> flag") +
                    '</label>'
                    ])
            else:
                left.AddRow(
                    [_('<em>The sender is now a member of this list</em>')])
            left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
        elif sender not in (mlist.accept_these_nonmembers +
                             mlist.hold_these_nonmembers +
                             mlist.reject_these_nonmembers +
                             mlist.discard_these_nonmembers):
            left.AddRow([
                '<label>' +
                CheckBox('senderfilterp-' + qsender, 1).Format() +
                '&nbsp;' +
                _(f'Add <b>{esender}</b> to one of these sender filters:') +
                '</label>'
                ])
            left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
            btns = hacky_radio_buttons(
                'senderfilter-' + qsender,
                (_('Accepts'), _('Holds'), _('Rejects'), _('Discards')),
                (mm_cfg.ACCEPT, mm_cfg.HOLD, mm_cfg.REJECT, mm_cfg.DISCARD),
                (0, 0, 0, 1))
            left.AddRow([btns])
            left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
            if sender not in mlist.ban_list:
                left.AddRow([
                    '<label>' +
                    CheckBox('senderbanp-' + qsender, 1).Format() +
                    '&nbsp;' +
                    _(f"""Ban <b>{esender}</b> from ever subscribing to this
                    mailing list""") + '</label>'])
                left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
        right = Table(border=0)
        right.AddRow([
            _(f"""Click on the message number to view the individual
            message, or you can """) +
            Link(senderurl, _(f'view all messages from {esender}')).Format()
            ])
        right.AddCellInfo(right.GetCurrentRowIndex(), 0, colspan=2)
        right.AddRow(['&nbsp;', '&nbsp;'])
        counter = 1
        for ptime, id in byskey[skey]:
            info = mlist.GetRecord(id)
            ptime, sender, subject, reason, filename, msgdata = info
            # BAW: This is really the size of the message pickle, which should
            # be close, but won't be exact.  Sigh, good enough.
            try:
                size = os.path.getsize(os.path.join(mm_cfg.DATA_DIR, filename))
            except OSError as e:
                if e.errno != errno.ENOENT: raise
                # This message must have gotten lost, i.e. it's already been
                # handled by the time we got here.
                mlist.HandleRequest(id, mm_cfg.DISCARD)
                continue
            dispsubj = Utils.oneline(
                subject, Utils.GetCharSet(mlist.preferred_language))
            if isinstance(dispsubj, bytes):
                dispsubj = dispsubj.decode('latin1', 'replace')
            t = Table(border=0)
            t.AddRow([Link(admindburl + '?msgid=%d' % id, '[%d]' % counter),
                      Bold(_('Subject:')),
                      Utils.websafe(dispsubj)
                      ])
            t.AddRow(['&nbsp;', Bold(_('Size:')), str(size) + _(' bytes')])
            if reason:
                reason = _(reason)
                if isinstance(reason, bytes):
                    reason = reason.decode('latin1', 'replace')
            else:
                reason = _('not available')
            t.AddRow(['&nbsp;', Bold(_('Reason:')), reason])
            # Include the date we received the message, if available
            when = msgdata.get('received_time')
            if when:
                t.AddRow(['&nbsp;', Bold(_('Received:')),
                          time.ctime(when)])
            t.AddRow([InputObj(qsender, 'hidden', str(id), False).Format()])
            counter += 1
            right.AddRow([t])
        stable.AddRow([left, right])
        table.AddRow([stable])
    return 1


def show_sender_requests(mlist, form, sender):
    byskey = helds_by_skey(mlist, SSENDER)
    if not byskey:
        return
    sender_ids = byskey.get((0, sender))
    if sender_ids is None:
        # BAW: should we print an error message?
        return
    sender_ids = [x[1] for x in sender_ids]
    total = len(sender_ids)
    count = 1
    for id in sender_ids:
        info = mlist.GetRecord(id)
        show_post_requests(mlist, id, info, total, count, form)
        count += 1


def show_message_requests(mlist, form, id):
    try:
        id = int(id)
        info = mlist.GetRecord(id)
    except (ValueError, KeyError):
        # BAW: print an error message?
        return
    show_post_requests(mlist, id, info, 1, 1, form)


def show_detailed_requests(mlist, form):
    all = mlist.GetHeldMessageIds()
    total = len(all)
    count = 1
    for id in mlist.GetHeldMessageIds():
        info = mlist.GetRecord(id)
        show_post_requests(mlist, id, info, total, count, form)
        count += 1


def show_post_requests(mlist, id, info, total, count, form):
    # Mailman.ListAdmin.__handlepost no longer tests for pre 2.0beta3
    ptime, sender, subject, reason, filename, msgdata = info
    form.AddItem('<hr>')
    # Header shown on each held posting (including count of total)
    msg = _('Posting Held for Approval')
    if total != 1:
        msg += _(f' (%(count)d of %(total)d)')
    form.AddItem(Center(Header(2, msg)))
    # We need to get the headers and part of the textual body of the message
    # being held.  The best way to do this is to use the email Parser to get
    # an actual object, which will be easier to deal with.  We probably could
    # just do raw reads on the file.
    try:
        msg = readMessage(os.path.join(mm_cfg.DATA_DIR, filename))
    except IOError as e:
        if e.errno != errno.ENOENT:
            raise
        form.AddItem(_(f'<em>Message with id #%(id)d was lost.'))
        form.AddItem('<p>')
        # BAW: kludge to remove id from requests.db.
        try:
            mlist.HandleRequest(id, mm_cfg.DISCARD)
        except Errors.LostHeldMessage:
            pass
        return
    except email.errors.MessageParseError:
        form.AddItem(_(f'<em>Message with id #%(id)d is corrupted.'))
        # BAW: Should we really delete this, or shuttle it off for site admin
        # to look more closely at?
        form.AddItem('<p>')
        # BAW: kludge to remove id from requests.db.
        try:
            mlist.HandleRequest(id, mm_cfg.DISCARD)
        except Errors.LostHeldMessage:
            pass
        return
    # Get the header text and the message body excerpt
    lines = []
    chars = 0
    # A negative value means, include the entire message regardless of size
    limit = mm_cfg.ADMINDB_PAGE_TEXT_LIMIT
    for line in body_line_iterator(msg, decode=True):
        lines.append(line)
        chars += len(line)
        if chars >= limit > 0:
            break
    # We may have gone over the limit on the last line, but keep the full line
    # anyway to avoid losing part of a multibyte character.
    body = EMPTYSTRING.join(lines)
    # Get message charset and try encode in list charset
    # We get it from the first text part.
    # We need to replace invalid characters here or we can throw an uncaught
    # exception in doc.Format().
    for part in msg.walk():
        if part.get_content_maintype() == 'text':
            # Watchout for charset= with no value.
            mcset = part.get_content_charset() or 'us-ascii'
            break
    else:
        mcset = 'us-ascii'
    lcset = Utils.GetCharSet(mlist.preferred_language)
    if mcset != lcset:
        try:
            body = str(body, mcset, 'replace').encode(lcset, 'replace')
        except (LookupError, UnicodeError, ValueError):
            pass
    hdrtxt = NL.join(['%s: %s' % (k, v) for k, v in list(msg.items())])
    hdrtxt = Utils.websafe(hdrtxt)
    # Okay, we've reconstituted the message just fine.  Now for the fun part!
    t = Table(cellspacing=0, cellpadding=0, width='100%')
    t.AddRow([Bold(_('From:')), sender])
    row, col = t.GetCurrentRowIndex(), t.GetCurrentCellIndex()
    t.AddCellInfo(row, col-1, align='right')
    t.AddRow([Bold(_('Subject:')),
              Utils.websafe(Utils.oneline(subject, lcset))])
    t.AddCellInfo(row+1, col-1, align='right')
    t.AddRow([Bold(_('Reason:')), _(reason)])
    t.AddCellInfo(row+2, col-1, align='right')
    when = msgdata.get('received_time')
    if when:
        t.AddRow([Bold(_('Received:')), time.ctime(when)])
        t.AddCellInfo(row+3, col-1, align='right')
    buttons = hacky_radio_buttons(id,
                (_('Defer'), _('Approve'), _('Reject'), _('Discard')),
                (mm_cfg.DEFER, mm_cfg.APPROVE, mm_cfg.REJECT, mm_cfg.DISCARD),
                (1, 0, 0, 0),
                spacing=5)
    t.AddRow([Bold(_('Action:')), buttons])
    t.AddCellInfo(t.GetCurrentRowIndex(), col-1, align='right')
    t.AddRow(['&nbsp;',
              '<label>' +
              CheckBox(f'preserve-%d' % id, 'on', 0).Format() +
              '&nbsp;' + _('Preserve message for site administrator') +
              '</label>'
              ])
    t.AddRow(['&nbsp;',
              '<label>' +
              CheckBox(f'forward-%d' % id, 'on', 0).Format() +
              '&nbsp;' + _('Additionally, forward this message to: ') +
              '</label>' +
              TextBox(f'forward-addr-%d' % id, size=47,
                      value=mlist.GetOwnerEmail()).Format()
              ])
    notice = msgdata.get('rejection_notice', _('[No explanation given]'))
    t.AddRow([
        Bold(_('If you reject this post,<br>please explain (optional):')),
        TextArea('comment-%d' % id, rows=4, cols=EXCERPT_WIDTH,
                 text = Utils.wrap(_(notice), column=80))
        ])
    row, col = t.GetCurrentRowIndex(), t.GetCurrentCellIndex()
    t.AddCellInfo(row, col-1, align='right')
    t.AddRow([Bold(_('Message Headers:')),
              TextArea('headers-%d' % id, hdrtxt,
                       rows=EXCERPT_HEIGHT, cols=EXCERPT_WIDTH, readonly=1)])
    row, col = t.GetCurrentRowIndex(), t.GetCurrentCellIndex()
    t.AddCellInfo(row, col-1, align='right')
    t.AddRow([Bold(_('Message Excerpt:')),
              TextArea('fulltext-%d' % id, Utils.websafe(body),
                       rows=EXCERPT_HEIGHT, cols=EXCERPT_WIDTH, readonly=1)])
    t.AddCellInfo(row+1, col-1, align='right')
    form.AddItem(t)
    form.AddItem('<p>')


def process_form(mlist, doc, cgidata):
    # Get the sender and message id from the query string
    envar = os.environ.get('QUERY_STRING', '')
    qs = urllib.parse.parse_qs(envar)
    sender = qs.get('sender', [''])[0]
    msgid = qs.get('msgid', [''])[0]
    details = qs.get('details', [''])[0]

    # Get the action from the form data
    action = cgidata.get('action', [''])[0]
    if not action:
        # No action specified, show the overview
        show_pending_subs(mlist, doc)
        show_pending_unsubs(mlist, doc)
        show_helds_overview(mlist, doc)
        return

    # Process the action
    if action == 'approve':
        if sender:
            show_sender_requests(mlist, doc, sender)
        elif msgid:
            show_message_requests(mlist, doc, msgid)
        else:
            show_detailed_requests(mlist, doc)
    elif action == 'reject':
        if sender:
            show_sender_requests(mlist, doc, sender)
        elif msgid:
            show_message_requests(mlist, doc, msgid)
        else:
            show_detailed_requests(mlist, doc)
    elif action == 'defer':
        if sender:
            show_sender_requests(mlist, doc, sender)
        elif msgid:
            show_message_requests(mlist, doc, msgid)
        else:
            show_detailed_requests(mlist, doc)
    elif action == 'discard':
        if sender:
            show_sender_requests(mlist, doc, sender)
        elif msgid:
            show_message_requests(mlist, doc, msgid)
        else:
            show_detailed_requests(mlist, doc)
    elif action == 'hold':
        if sender:
            show_sender_requests(mlist, doc, sender)
        elif msgid:
            show_message_requests(mlist, doc, msgid)
        else:
            show_detailed_requests(mlist, doc)
    elif action == 'post':
        if msgid:
            info = mlist.GetRecord(msgid)
            if info:
                total = len(mlist.GetHeldMessageIds())
                count = 1
                show_post_requests(mlist, msgid, info, total, count, doc)
        else:
            show_detailed_requests(mlist, doc)
    else:
        # Unknown action, show the overview
        show_pending_subs(mlist, doc)
        show_pending_unsubs(mlist, doc)
        show_helds_overview(mlist, doc)


def format_body(body, mcset, lcset):
    """Format the message body for display."""
    if isinstance(body, bytes):
        body = body.decode(mcset, 'replace')
    elif not isinstance(body, str):
        body = str(body)
    return body.encode(lcset, 'replace')
