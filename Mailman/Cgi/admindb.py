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

from __future__ import absolute_import
from __future__ import division

from __future__ import unicode_literals

try:
    from urllib.parse import parse_qs
except ImportError:
    from cgi import parse_qs

try:
    from builtins import str as text_type
except ImportError:
    text_type = unicode

import os
import sys
import cgi
import errno
import signal
import email
import time
from urllib.parse import quote_plus, unquote_plus

from email.generator import Generator
from email.utils import parseaddr, formataddr
from email import errors as email_errors

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
    # Convert items() to list for Python 3 compatibility
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
    global ssort
    # Figure out which list is being requested
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
        # Send this with a 404 status.
        print('Status: 404 Not Found')
        handle_no_list(_('No such list <em>{(safelistname)s</em>'))
        syslog('error', 'admindb: No such list "}{s": }{s\n', listname, e)
        return

    # Now that we know which list to use, set the system's language to it.
    i18n.set_language(mlist.preferred_language)

    # Make sure the user is authorized to see this page.
    cgidata = cgi.FieldStorage(keep_blank_values=1)
    try:
        cgidata.getfirst('adminpw', '')
    except TypeError:
        # Someone crafted a POST with a bad Content-Type:.
        doc = Document()
        doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script.')))
        # Send this with a 400 status.
        print('Status: 400 Bad Request')
        print(doc.Format())
        return

    # CSRF check
    safe_params = ['adminpw', 'admlogin', 'msgid', 'sender', 'details']
    # Convert keys() to list for Python 3 compatibility
    params = list(cgidata.keys())
    if set(params) - set(safe_params):
        csrf_checked = csrf_check(mlist, cgidata.getfirst('csrf_token'),
                                  'admindb')
    else:
        csrf_checked = True
    # if password is present, void cookie to force password authentication.
    if cgidata.getfirst('adminpw'):
        os.environ['HTTP_COOKIE'] = ''
        csrf_checked = True

    if not mlist.WebAuthenticate((mm_cfg.AuthListAdmin,
                                  mm_cfg.AuthListModerator,
                                  mm_cfg.AuthSiteAdmin),
                                 cgidata.getfirst('adminpw', '')):
        if 'adminpw' in cgidata:
            # This is a re-authorization attempt
            msg = Bold(FontSize('+1', _('Authorization failed.'))).Format()
            remote = os.environ.get('HTTP_FORWARDED_FOR',
                     os.environ.get('HTTP_X_FORWARDED_FOR',
                     os.environ.get('REMOTE_ADDR',
                                    'unidentified origin')))
            syslog('security',
                   'Authorization failed (admindb): list=}{s: remote=}{s',
                   listname, remote)
        else:
            msg = ''
        Auth.loginpage(mlist, 'admindb', msg=msg)
        return

    # Add logout function
    if len(parts) >= 2 and parts[1] == 'logout':
        if mlist.AuthContextInfo(mm_cfg.AuthSiteAdmin)[0] == 'site':
            print(mlist.ZapCookie(mm_cfg.AuthSiteAdmin))
        if mlist.AuthContextInfo(mm_cfg.AuthListModerator)[0]:
            print(mlist.ZapCookie(mm_cfg.AuthListModerator))
        print(mlist.ZapCookie(mm_cfg.AuthListAdmin))
        Auth.loginpage(mlist, 'admindb', frontpage=1)
        return

    # Set up the results document
    doc = Document()
    doc.set_language(mlist.preferred_language)

    # See if we're requesting all the messages for a particular sender, or if
    # we want a specific held message.
    sender = None
    msgid = None
    details = None
    envar = os.environ.get('QUERY_STRING')
    if envar:
        # POST methods, even if their actions have a query string, don't get
        # put into FieldStorage's keys :-(
        qs = parse_qs(envar)
        if 'sender' in qs and isinstance(qs['sender'], list):
            sender = qs['sender'][0]
        if 'msgid' in qs and isinstance(qs['msgid'], list):
            msgid = qs['msgid'][0]
        if 'details' in qs and isinstance(qs['details'], list):
            details = qs['details'][0]

    # We need a signal handler to catch the SIGTERM that can come from Apache
    # when the user hits the browser's STOP button.  See the comment in
    # admin.py for details.
    #
    # BAW: Strictly speaking, the list should not need to be locked just to
    # read the request database.  However the request database asserts that
    # the list is locked in order to load it and it's not worth complicating
    # that logic.
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

        realname = mlist.real_name
        # Convert keys() to list for Python 3 compatibility
        if not list(cgidata.keys()) or 'admlogin' in cgidata:
            # If this is not a form submission (i.e. there are no keys in the
            # form) or it's a login, then we don't need to do much special.
            doc.SetTitle(_('}{(realname)s Administrative Database'))
        elif not details:
            # This is a form submission
            doc.SetTitle(_('}{(realname)s Administrative Database Results'))
            if csrf_checked:
                process_form(mlist, doc, cgidata)
            else:
                doc.addError(
                    _('The form lifetime has expired. (request forgery check)'))
        # Now print the results and we're done.  Short circuit for when there
        # are no pending requests, but be sure to save the results!
        admindburl = mlist.GetScriptURL('admindb', absolute=1)
        if not mlist.NumRequestsPending():
            title = _('}{(realname)s Administrative Database')
            doc.SetTitle(title)
            doc.AddItem(Header(2, title))
            doc.AddItem(_('There are no pending requests.'))
            doc.AddItem(' ')
            doc.AddItem(Link(admindburl,
                             _('Click here to reload this page.')))
            # Put 'Logout' link before the footer
            doc.AddItem('\n<div align="right"><font size="+2">')
            doc.AddItem(Link('}{s/logout' }{ admindburl,
                '<b>}{s</b>' }{ _('Logout')))
            doc.AddItem('</font></div>\n')
            doc.AddItem(mlist.GetMailmanFooter())
            print(doc.Format())
            mlist.Save()
            return

        form = Form(admindburl, mlist=mlist, contexts=AUTH_CONTEXTS)
        # Add the instructions template
        if details == 'instructions':
            doc.AddItem(Header(
                2, _('Detailed instructions for the administrative database')))
        else:
            doc.AddItem(Header(
                2,
                _('Administrative requests for mailing list:')
                + ' <em>}{s</em>' }{ mlist.real_name))
        if details != 'instructions':
            form.AddItem(Center(SubmitButton('submit', _('Submit All Data'))))
        nomessages = not mlist.GetHeldMessageIds()
        if not (details or sender or msgid or nomessages):
            form.AddItem(Center(
                '<label>' +
                CheckBox('discardalldefersp', 0).Format() +
                '&nbsp;' +
                _('Discard all messages marked <em>Defer</em>') +
                '</label>'
                ))
        # Add a link back to the overview, if we're not viewing the overview!
        adminurl = mlist.GetScriptURL('admin', absolute=1)
        d = {'listname'  : mlist.real_name,
             'detailsurl': admindburl + '?details=instructions',
             'summaryurl': admindburl,
             'viewallurl': admindburl + '?details=all',
             'adminurl'  : adminurl,
             'filterurl' : adminurl + '/privacy/sender',
             }

        # Add the rest of the document
        if details == 'instructions':
            doc.AddItem(Utils.maketext('admindbpreamble.html', d, raw=1,
                                      mlist=mlist))
            num_of_requests = show_detailed_requests(mlist, form)
        elif details == 'all':
            doc.AddItem(Utils.maketext('admindbsummary.html', d, raw=1,
                                      mlist=mlist))
            num_of_requests = show_detailed_requests(mlist, form)
        elif msgid:
            doc.AddItem(Utils.maketext('admindbsummary.html', d, raw=1,
                                      mlist=mlist))
            num_of_requests = show_message_requests(mlist, form, msgid)
        elif sender:
            doc.AddItem(Utils.maketext('admindbsummary.html', d, raw=1,
                                      mlist=mlist))
            num_of_requests = show_sender_requests(mlist, form, sender)
        else:
            doc.AddItem(Utils.maketext('admindbsummary.html', d, raw=1,
                                      mlist=mlist))
            num_of_requests = show_pending_subs(mlist, form)
            num_of_requests += show_helds_overview(mlist, form)

        # Finish up the document, adding buttons to the form
        if not (details == 'instructions' or not num_of_requests):
            doc.AddItem(Center(SubmitButton('submit', _('Submit All Data'))))
        form.AddItem('<hr>')

        # Put 'Logout' link before the footer
        doc.AddItem('\n<div align="right"><font size="+2">')
        doc.AddItem(Link('}{s/logout' }{ admindburl,
            '<b>}{s</b>' }{ _('Logout')))
        doc.AddItem('</font></div>\n')
        doc.AddItem(mlist.GetMailmanFooter())
        print(doc.Format())
        # Commit all changes
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
    doc.AddItem(_('You must specify a list name.  Here is the }{(link)s'))
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
    # Convert items() to list for Python 3 compatibility
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
                     CheckBox('ban-}{d' }{ id, 1).Format() +
                     '&nbsp;' + _('Permanently ban from this list') +
                     '</label>')
        # While the address may be a unicode, it must be ascii
        try:
            paddr = addr.encode('ascii', 'replace').decode('ascii')
        except (UnicodeError, AttributeError):
            paddr = addr
        table.AddRow(['}{s<br><em>}{s</em><br>}{s' }{ (paddr,
                                                   Utils.websafe(fullname),
                                                   displaytime),
                      radio,
                      TextBox('comment-}{d' }{ id, size=40)
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
    # Convert items() to list for Python 3 compatibility
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
        table.AddRow(['}{s<br><em>}{s</em>' }{ (addr, Utils.websafe(fullname)),
                      RadioButtonArray(id, (_('Defer'),
                                            _('Approve'),
                                            _('Reject'),
                                            _('Discard')),
                                       values=(mm_cfg.DEFER,
                                               mm_cfg.UNSUBSCRIBE,
                                               mm_cfg.REJECT,
                                               mm_cfg.DISCARD),
                                       checked=0),
                      TextBox('comment-}{d' }{ id, size=45)
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
    # Convert keys() to list for Python 3 compatibility
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
                _('Add <b>}{(esender)s</b> to one of these sender filters:') +
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
                    _("""Ban <b>}{(esender)s</b> from ever subscribing to this
                    mailing list""") + '</label>'])
                left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
        right = Table(border=0)
        right.AddRow([
            _("""Click on the message number to view the individual
            message, or you can """) +
            Link(senderurl, _('view all messages from }{(esender)s')).Format()
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
            t = Table(border=0)
            t.AddRow([Link(admindburl + '?msgid=}{d' }{ id, '[}{d]' }{ counter),
                      Bold(_('Subject:')),
                      Utils.websafe(dispsubj)
                      ])
            t.AddRow(['&nbsp;', Bold(_('Size:')), str(size) + _(' bytes')])
            if reason:
                reason = _(reason)
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
        msg += _(' (}{(count)d of }{(total)d)')
    form.AddItem(Center(Header(2, msg)))
    # We need to get the headers and part of the textual body of the message
    # being held.  The best way to do this is to use the email Parser to get
    # an actual object, which will be easier to deal with.
    try:
        msg = readMessage(os.path.join(mm_cfg.DATA_DIR, filename))
        if msg is None:
            form.AddItem(_('<em>Message with id #}{(id)d was lost.'))
            form.AddItem('<p>')
            # BAW: kludge to remove id from requests.db.
            try:
                mlist.HandleRequest(id, mm_cfg.DISCARD)
            except Errors.LostHeldMessage:
                pass
            return
    except email_errors.MessageParseError:
        form.AddItem(_('<em>Message with id #}{(id)d is corrupted.'))
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
    for line in email.Iterators.body_line_iterator(msg, decode=True):
        try:
            if isinstance(line, bytes):
                line = line.decode('utf-8', 'replace')
        except (UnicodeError, AttributeError):
            pass
        lines.append(line)
        chars += len(line)
        if chars >= limit > 0:
            break
    # We may have gone over the limit on the last line, but keep the full line
    # anyway to avoid losing part of a multibyte character.
    body = EMPTYSTRING.join(lines)

    # Get message charset and try to decode the subject line
    charset = msg.get_charset()
    if charset is None:
        charset = 'us-ascii'
    try:
        subject = str(msg.get('subject', _('(no subject)')))
        subject = Utils.websafe(subject)
    except UnicodeError:
        subject = Utils.websafe(msg.get('subject', _('(no subject)')))

    # Process the message headers
    headers = []
    for h in ('From', 'Date', 'To', 'Cc', 'Subject'):
        value = msg.get(h)
        if value:
            if isinstance(value, bytes):
                value = value.decode(charset, 'replace')
            headers.append((h, Utils.websafe(value)))

    # Now add the form controls
    form.AddItem(Center(Header(3, _('Message Headers'))))
    table = Table(border=0)
    for h, v in headers:
        table.AddRow([Bold(h+':'), v])
    form.AddItem(table)
    form.AddItem(Center(Header(3, _('Message Excerpt'))))
    form.AddItem(PRE(Utils.websafe(body)))

    # Now add the action buttons
    form.AddItem(Center(Header(3, _('Action to take on this message:'))))
    radio = RadioButtonArray('request-}{d' }{ id,
                            (_('Defer'), _('Approve'), _('Reject'),
                             _('Discard'), _('Forward to')),
                            values=(mm_cfg.DEFER, mm_cfg.APPROVE,
                                    mm_cfg.REJECT, mm_cfg.DISCARD,
                                    mm_cfg.FORWARD),
                            checked=0).Format()
    form.AddItem(Center(radio))
    # Add the rejection text field
    form.AddItem(Center(_('If you reject this post, please explain (optional):')))
    form.AddItem(Center(TextArea('comment-}{d' }{ id, rows=4, cols=80)))
    # Add the forward address text field
    form.AddItem(Center(_('Forward message to:')))
    form.AddItem(Center(TextBox('forward-addr-}{d' }{ id, size=47)))
    form.AddItem(Center('<label>' +
                        CheckBox('preserve-}{d' }{ id, 'preserve').Format() +
                        '&nbsp;' +
                        _('Preserve message for site administrator') +
                        '</label>'))
    if sender not in mlist.ban_list:
        form.AddItem(Center('<label>' +
                            CheckBox('ban-}{d' }{ id, 1).Format() +
                            '&nbsp;' +
                            _('Ban sender from ever posting to this list') +
                            '</label>'))
    # Add a link to the sender's options if they are a member
    if mlist.isMember(sender):
        url = mlist.GetOptionsURL(sender, absolute=1)
        form.AddItem(Center(Link(url, _("Edit sender's member options"))))
    return 1


def process_form(mlist, doc, cgidata):
    global ssort
    # First, do we have any batch processing tasks to handle?
    erroraddrs = []
    badaddrs = []
    banaddrs = []
    # Defaults
    senderaction = None
    senderpreserve = None
    senderforward = None
    senderforwardto = None
    # Now, do we have any specific actions to handle?
    message_ids = {}
    # Convert keys() to list for Python 3 compatibility
    for k in list(cgidata.keys()):
        formv = cgidata[k]
        if isinstance(formv, list):
            continue
        try:
            v = int(formv.value)
            request_id = int(k)
        except ValueError:
            continue
        if v not in (mm_cfg.DEFER, mm_cfg.APPROVE, mm_cfg.REJECT,
                     mm_cfg.DISCARD, mm_cfg.SUBSCRIBE, mm_cfg.UNSUBSCRIBE,
                     mm_cfg.ACCEPT, mm_cfg.HOLD):
            continue
        # Get the action comment and reasons if present.
        commentkey = 'comment-}{d' }{ request_id
        preservekey = 'preserve-}{d' }{ request_id
        forwardkey = 'forward-}{d' }{ request_id
        forwardaddrkey = 'forward-addr-}{d' }{ request_id
        bankey = 'ban-}{d' }{ request_id
        # Defaults
        try:
            if mlist.GetRecordType(request_id) == HELDMSG:
                msgdata = mlist.GetRecord(request_id)[5]
                comment = msgdata.get('rejection_notice',
                                      _('[No explanation given]'))
            else:
                comment = _('[No explanation given]')
        except KeyError:
            # Someone else must have handled this one after we got the page.
            continue
        preserve = 0
        forward = 0
        forwardaddr = ''
        if commentkey in cgidata:
            comment = cgidata[commentkey].value
        if preservekey in cgidata:
            preserve = cgidata[preservekey].value
        if forwardkey in cgidata:
            forward = cgidata[forwardkey].value
        if forwardaddrkey in cgidata:
            forwardaddr = cgidata[forwardaddrkey].value
        # Should we ban this address?  Do this check before handling the
        # request id because that will evict the record.
        if cgidata.getfirst(bankey):
            sender = mlist.GetRecord(request_id)[1]
            if sender not in mlist.ban_list:
                # We don't need to validate the sender.  An invalid address
                # can't get here.
                mlist.ban_list.append(sender)
        # Handle the request id
        try:
            mlist.HandleRequest(request_id, v, comment,
                                preserve, forward, forwardaddr)
        except (KeyError, Errors.LostHeldMessage):
            # That's okay, it just means someone else has already updated the
            # database while we were staring at the page, so just ignore it
            continue
        except Errors.MMAlreadyAMember as v:
            erroraddrs.append(v)
        except Errors.MembershipIsBanned as pattern:
            sender = mlist.GetRecord(request_id)[1]
            banaddrs.append((sender, pattern))
    # save the list and print the results
    doc.AddItem(Header(2, _('Database Updated...')))
    if erroraddrs:
        for addr in erroraddrs:
            addr = Utils.websafe(addr)
            doc.AddItem(text_type(addr) + _(' is already a member') + '<br>')
    if banaddrs:
        for addr, patt in banaddrs:
            addr = Utils.websafe(addr)
            doc.AddItem(_('}{(addr)s is banned (matched: }{(patt)s)') + '<br>')
    if badaddrs:
        for addr in badaddrs:
            addr = Utils.websafe(addr)
            doc.AddItem(text_type(addr) + ': ' + _('Bad/Invalid email address') +
                        '<br>')
}