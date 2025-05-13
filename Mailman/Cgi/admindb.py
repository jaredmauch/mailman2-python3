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
from Mailman.Message import Message
from Mailman import i18n
from Mailman.Handlers.Moderate import ModeratedMemberPost
from Mailman.ListAdmin import HELDMSG, ListAdmin, PermissionError
from Mailman.ListAdmin import readMessage
from Mailman.Cgi import Auth
from Mailman.htmlformat import *
from Mailman.Logging.Syslog import syslog, mailman_log
from Mailman.CSRFcheck import csrf_check
import traceback

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


def output_error_page(status, title, message, details=None):
    doc = Document()
    doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)
    doc.AddItem(Header(2, _(title)))
    doc.AddItem(Bold(_(message)))
    if details:
        doc.AddItem(Preformatted(Utils.websafe(str(details))))
    doc.AddItem(_('Please contact the site administrator.'))
    return doc


def output_success_page(doc):
    print(doc.Format())
    return


def main():
    try:
        # Log page load with process identity
        mailman_log('info', 'admindb: Page load started')
        mailman_log('info', 'Process identity - EUID: %d, EGID: %d, RUID: %d, RGID: %d',
                   os.geteuid(), os.getegid(), os.getuid(), os.getgid())
        
        # Initialize document early
        doc = Document()
        doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)
        
        # Parse form data first since we need it for authentication
        try:
            if os.environ.get('REQUEST_METHOD') == 'POST':
                content_length = int(os.environ.get('CONTENT_LENGTH', 0))
                if content_length > 0:
                    form_data = sys.stdin.buffer.read(content_length).decode('utf-8')
                    cgidata = urllib.parse.parse_qs(form_data, keep_blank_values=True)
                else:
                    cgidata = {}
            else:
                query_string = os.environ.get('QUERY_STRING', '')
                cgidata = urllib.parse.parse_qs(query_string, keep_blank_values=True)
        except Exception as e:
            mailman_log('error', 'admindb: Invalid form data: %s\n%s', str(e), traceback.format_exc())
            try:
                doc = output_error_page('400 Bad Request', 'Error', 'Invalid options to CGI script.')
                return output_success_page(doc)
            except Exception as output_error:
                mailman_log('error', 'admindb: Failed to output error page: %s\n%s', 
                           str(output_error), traceback.format_exc())
                raise

        # Get the list name
        parts = Utils.GetPathPieces()
        if not parts:
            try:
                doc = handle_no_list()
                return output_success_page(doc)
            except Exception as e:
                mailman_log('error', 'admindb: Failed to handle no list case: %s\n%s', 
                           str(e), traceback.format_exc())
                raise

        listname = parts[0].lower()
        mailman_log('info', 'admindb: Processing list "%s"', listname)

        # Check if list directory exists before trying to load
        listdir = os.path.join(mm_cfg.LIST_DATA_DIR, listname)
        if not os.path.exists(listdir):
            mailman_log('error', 'admindb: List directory does not exist: %s', listdir)
            try:
                doc = output_error_page('404 Not Found', 'Error', 
                                       'No such list <em>%s</em>' % Utils.websafe(listname),
                                       'The list directory does not exist.')
                return output_success_page(doc)
            except Exception as e:
                mailman_log('error', 'admindb: Failed to output list not found error: %s\n%s', 
                           str(e), traceback.format_exc())
                raise

        try:
            mlist = MailList.MailList(listname, lock=0)
        except Errors.MMListError as e:
            mailman_log('error', 'admindb: No such list "%s": %s\n%s', 
                       listname, e, traceback.format_exc())
            try:
                doc = output_error_page('404 Not Found', 'Error',
                                       'No such list <em>%s</em>' % Utils.websafe(listname),
                                       'The list configuration could not be loaded.')
                return output_success_page(doc)
            except Exception as output_error:
                mailman_log('error', 'admindb: Failed to output list error page: %s\n%s', 
                           str(output_error), traceback.format_exc())
                raise
        except PermissionError as e:
            mailman_log('error', 'admindb: Permission error accessing list "%s": %s\n%s', 
                       listname, e, traceback.format_exc())
            try:
                doc = output_error_page('500 Internal Server Error', 'Error',
                                       'Permission error accessing list <em>%s</em>' % Utils.websafe(listname),
                                       str(e))
                return output_success_page(doc)
            except Exception as output_error:
                mailman_log('error', 'admindb: Failed to output permission error page: %s\n%s', 
                           str(output_error), traceback.format_exc())
                raise
        except Exception as e:
            mailman_log('error', 'admindb: Unexpected error loading list "%s": %s\n%s',
                       listname, str(e), traceback.format_exc())
            try:
                doc = output_error_page('500 Internal Server Error', 'Error',
                                       'Error accessing list <em>%s</em>' % Utils.websafe(listname),
                                       str(e))
                return output_success_page(doc)
            except Exception as output_error:
                mailman_log('error', 'admindb: Failed to output unexpected error page: %s\n%s', 
                           str(output_error), traceback.format_exc())
                raise

        # Now that we know what list has been requested, all subsequent admin
        # pages are shown in that list's preferred language.
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
                mailman_log('security',
                           'Authorization failed (admindb): list=%s: remote=%s\n%s',
                           listname, remote, traceback.format_exc())
            else:
                msg = ''
            try:
                Auth.loginpage(mlist, 'admindb', msg=msg)
            except Exception as e:
                mailman_log('error', 'admindb: Failed to display login page: %s\n%s', 
                           str(e), traceback.format_exc())
                raise
            return

        # We need a signal handler to catch the SIGTERM that can come from Apache
        # when the user hits the browser's STOP button.  See the comment in
        # admin.py for details.
        def sigterm_handler(signum, frame, mlist=mlist):
            try:
                # Make sure the list gets unlocked...
                mlist.Unlock()
                # Log the termination
                mailman_log('info', 'admindb: SIGTERM received, unlocking list and exiting')
            except Exception as e:
                mailman_log('error', 'admindb: Error in SIGTERM handler: %s\n%s', 
                           str(e), traceback.format_exc())
            finally:
                # ...and ensure we exit, otherwise race conditions could cause us to
                # enter MailList.Save() while we're in the unlocked state, and that
                # could be bad!
                sys.exit(0)

        mlist.Lock()
        try:
            # Install the emergency shutdown signal handler
            signal.signal(signal.SIGTERM, sigterm_handler)

            try:
                process_form(mlist, doc, cgidata)
                mlist.Save()
                # Output the success page with proper headers
                return output_success_page(doc)
            except PermissionError as e:
                mailman_log('error', 'admindb: Permission error processing form: %s\n%s',
                           str(e), traceback.format_exc())
                doc = output_error_page('500 Internal Server Error', 'Error',
                                       'Permission error while processing request',
                                       str(e))
                return output_success_page(doc)
            except Exception as e:
                mailman_log('error', 'admindb: Error processing form: %s\n%s',
                           str(e), traceback.format_exc())
                doc = output_error_page('500 Internal Server Error', 'Error',
                                       'Error processing request',
                                       str(e))
                return output_success_page(doc)
        finally:
            mlist.Unlock()
    except Exception as e:
        mailman_log('error', 'admindb: Unhandled exception in main(): %s\n%s', 
                   str(e), traceback.format_exc())
        raise


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
    
    # Return the document instead of outputting headers
    return doc


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
    except ValueError as e:
        mailman_log('error', 'admindb: Invalid message ID "%s": %s\n%s', 
                   id, str(e), traceback.format_exc())
        form.AddItem(Header(2, _("Error")))
        form.AddItem(Bold(_('Invalid message ID.')))
        return
    except KeyError as e:
        mailman_log('error', 'admindb: Message ID %d not found: %s\n%s', 
                   id, str(e), traceback.format_exc())
        form.AddItem(Header(2, _("Error")))
        form.AddItem(Bold(_('Message not found.')))
        return
    except Exception as e:
        mailman_log('error', 'admindb: Error getting message %d: %s\n%s', 
                   id, str(e), traceback.format_exc())
        form.AddItem(Header(2, _("Error")))
        form.AddItem(Bold(_('Error retrieving message.')))
        return

    try:
        show_post_requests(mlist, id, info, 1, 1, form)
    except Exception as e:
        mailman_log('error', 'admindb: Error showing message %d: %s\n%s', 
                   id, str(e), traceback.format_exc())
        form.AddItem(Header(2, _("Error")))
        form.AddItem(Bold(_('Error displaying message.')))
        return


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
    try:
        # Get the sender and message id from the query string
        envar = os.environ.get('QUERY_STRING', '')
        qs = urllib.parse.parse_qs(envar)
        sender = qs.get('sender', [''])[0]
        msgid = qs.get('msgid', [''])[0]
        details = qs.get('details', [''])[0]

        # Set the page title
        title = _(f'{mlist.real_name} Administrative Database')
        doc.SetTitle(title)
        doc.AddItem(Header(2, title))

        # Check if there are any pending requests
        if not mlist.NumRequestsPending():
            doc.AddItem(_('There are no pending requests.'))
            doc.AddItem(' ')
            admindburl = mlist.GetScriptURL('admindb', absolute=1)
            doc.AddItem(Link(admindburl, _('Click here to reload this page.')))
            # Add the footer
            doc.AddItem(mlist.GetMailmanFooter())
            return

        # Create a form for the overview
        form = Form(mlist.GetScriptURL('admindb', absolute=1), mlist=mlist, contexts=AUTH_CONTEXTS)
        form.AddItem(Center(SubmitButton('submit', _('Submit All Data'))))

        # Get the action from the form data
        action = cgidata.get('action', [''])[0]
        if not action:
            # No action specified, show the overview
            show_pending_subs(mlist, form)
            show_pending_unsubs(mlist, form)
            show_helds_overview(mlist, form)
            doc.AddItem(form)
            # Add the footer
            doc.AddItem(mlist.GetMailmanFooter())
            return

        # Process the form submission
        if action == 'submit':
            # Process the form data
            process_submissions(mlist, cgidata)
            # Show success message
            doc.AddItem(_('Your changes have been made.'))
            doc.AddItem(' ')
            admindburl = mlist.GetScriptURL('admindb', absolute=1)
            doc.AddItem(Link(admindburl, _('Click here to return to the pending requests page.')))
            # Add the footer
            doc.AddItem(mlist.GetMailmanFooter())
            return

        # If we get here, something went wrong
        doc.AddItem(_('Invalid action specified.'))
        doc.AddItem(' ')
        admindburl = mlist.GetScriptURL('admindb', absolute=1)
        doc.AddItem(Link(admindburl, _('Click here to return to the pending requests page.')))
        # Add the footer
        doc.AddItem(mlist.GetMailmanFooter())

    except Exception as e:
        mailman_log('error', 'admindb: Error in process_form: %s\n%s', 
                   str(e), traceback.format_exc())
        raise


def format_body(body, mcset, lcset):
    """Format the message body for display."""
    if isinstance(body, bytes):
        body = body.decode(mcset, 'replace')
    elif not isinstance(body, str):
        body = str(body)
    return body.encode(lcset, 'replace')
