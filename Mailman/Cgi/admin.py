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

"""Process and produce the list-administration options forms."""

from __future__ import absolute_import
from __future__ import division

from __future__ import unicode_literals

import sys
import os
import re
import cgi
import urllib.parse
import signal
from types import *

from email.utils import unquote, parseaddr, formataddr

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Message
from Mailman import MailList
from Mailman import Errors
from Mailman import MemberAdaptor
from Mailman import i18n
from Mailman.UserDesc import UserDesc
from Mailman.htmlformat import *
from Mailman.Cgi import Auth
from Mailman.Logging.Syslog import syslog
from Mailman.Utils import sha_new
from Mailman.CSRFcheck import csrf_check

# Set up i18n
_ = i18n._
i18n.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)
def D_(s):
    return s

NL = '\n'
OPTCOLUMNS = 11

AUTH_CONTEXTS = (mm_cfg.AuthListAdmin, mm_cfg.AuthSiteAdmin)


def main():
    # Try to find out which list is being administered
    parts = Utils.GetPathPieces()
    if not parts:
        # None, so just do the admin overview and be done with it
        admin_overview()
        return
    # Get the list object
    listname = parts[0].lower()
    try:
        mlist = MailList.MailList(listname, lock=0)
    except Errors.MMListError as e:
        # Avoid cross-site scripting attacks
        safelistname = Utils.websafe(listname)
        # Send this with a 404 status.
        print('Status: 404 Not Found')
        admin_overview(_('No such list <em>{(safelistname)s</em>'))
        syslog('error', 'admin: No such list "}{s": }{s\n',
               listname, e)
        return
    # Now that we know what list has been requested, all subsequent admin
    # pages are shown in that list's preferred language.
    i18n.set_language(mlist.preferred_language)
    # If the user is not authenticated, we're done.
    cgidata = cgi.FieldStorage(keep_blank_values=1)
    try:
        cgidata.getfirst('csrf_token', '')
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
    safe_params = ['VARHELP', 'adminpw', 'admlogin',
                   'letter', 'chunk', 'findmember',
                   'legend']
    params = cgidata.keys()
    if set(params) - set(safe_params):
        csrf_checked = csrf_check(mlist, cgidata.getfirst('csrf_token'),
                                  'admin')
    else:
        csrf_checked = True
    # if password is present, void cookie to force password authentication.
    if cgidata.getfirst('adminpw'):
        os.environ['HTTP_COOKIE'] = ''
        csrf_checked = True

    if not mlist.WebAuthenticate((mm_cfg.AuthListAdmin,
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
                   'Authorization failed (admin): list=}{s: remote=}{s',
                   listname, remote)
        else:
            msg = ''
        Auth.loginpage(mlist, 'admin', msg=msg)
        return

    # Which subcategory was requested?  Default is `general'
    if len(parts) == 1:
        category = 'general'
        subcat = None
    elif len(parts) == 2:
        category = parts[1]
        subcat = None
    else:
        category = parts[1]
        subcat = parts[2]

    # Is this a log-out request?
    if category == 'logout':
        # site-wide admin should also be able to logout.
        if mlist.AuthContextInfo(mm_cfg.AuthSiteAdmin)[0] == 'site':
            print(mlist.ZapCookie(mm_cfg.AuthSiteAdmin))
        print(mlist.ZapCookie(mm_cfg.AuthListAdmin))
        Auth.loginpage(mlist, 'admin', frontpage=1)
        return

    # Sanity check
    if category not in mlist.GetConfigCategories():
        category = 'general'

    # Is the request for variable details?
    varhelp = None
    qsenviron = os.environ.get('QUERY_STRING')
    parsedqs = None
    if qsenviron:
        parsedqs = cgi.parse_qs(qsenviron)
    if 'VARHELP' in cgidata:
        varhelp = cgidata.getfirst('VARHELP')
    elif parsedqs:
        # POST methods, even if their actions have a query string, don't get
        # put into FieldStorage's keys :-(
        qs = parsedqs.get('VARHELP')
        if qs and isinstance(qs, list):
            varhelp = qs[0]
    if varhelp:
        option_help(mlist, varhelp)
        return

    # The html page document
    doc = Document()
    doc.set_language(mlist.preferred_language)

    # From this point on, the MailList object must be locked.  However, we
    # must release the lock no matter how we exit.  try/finally isn't enough,
    # because of this scenario: user hits the admin page which may take a long
    # time to render; user gets bored and hits the browser's STOP button;
    # browser shuts down socket; server tries to write to broken socket and
    # gets a SIGPIPE.  Under Apache 1.3/mod_cgi, Apache catches this SIGPIPE
    # (I presume it is buffering output from the cgi script), then turns
    # around and SIGTERMs the cgi process.  Apache waits three seconds and
    # then SIGKILLs the cgi process.  We /must/ catch the SIGTERM and do the
    # most reasonable thing we can in as short a time period as possible.  If
    # we get the SIGKILL we're screwed (because it's uncatchable and we'll
    # have no opportunity to clean up after ourselves).
    #
    # This signal handler catches the SIGTERM, unlocks the list, and then
    # exits the process.  The effect of this is that the changes made to the
    # MailList object will be aborted, which seems like the only sensible
    # semantics.
    #
    # BAW: This may not be portable to other web servers or cgi execution
    # models.
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

        if cgidata.keys():
            if csrf_checked:
                # There are options to change
                change_options(mlist, category, subcat, cgidata, doc)
            else:
                doc.addError(
                  _('The form lifetime has expired. (request forgery check)'))
            # Let the list sanity check the changed values
            mlist.CheckValues()
        # Additional sanity checks
        if not mlist.digestable and not mlist.nondigestable:
            doc.addError(
                _('''You have turned off delivery of both digest and
                non-digest messages.  This is an incompatible state of
                affairs.  You must turn on either digest delivery or
                non-digest delivery or your mailing list will basically be
                unusable.'''), tag=_('Warning: '))

        dm = mlist.getDigestMemberKeys()
        if not mlist.digestable and dm:
            doc.addError(
                _('''You have digest members, but digests are turned
                off. Those people will not receive mail.
                Affected member(s) }{(dm)r.'''),
                tag=_('Warning: '))
        rm = mlist.getRegularMemberKeys()
        if not mlist.nondigestable and rm:
            doc.addError(
                _('''You have regular list members but non-digestified mail is
                turned off.  They will receive non-digestified mail until you
                fix this problem. Affected member(s) }{(rm)r.'''),
                tag=_('Warning: '))
        # Glom up the results page and print(i, end=\'\')t out
        show_results(mlist, doc, category, subcat, cgidata)
        print(doc.Format())
        mlist.Save()
    finally:
        # Now be sure to unlock the list.  It's okay if we get a signal here
        # because essentially, the signal handler will do the same thing.  And
        # unlocking is unconditional, so it's not an error if we unlock while
        # we're already unlocked.
        mlist.Unlock()


def admin_overview(msg=''):
    # Show the administrative overview page, with the list of all the lists on
    # this host.  msg is an optional error message to display at the top of
    # the page.
    #
    # This page should be displayed in the server's default language, which
    # should have already been set.
    doc = Document()
    doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)
    doc.AddItem(Header(2, _('Mailing List Administration')))

    if msg:
        doc.AddItem(msg)

    doc.AddItem(Paragraph(_('''This page allows you to administer all the mailing lists on this host.
    Click on a list name to administer that list.''')))

    # Get all the lists
    lists = Utils.list_names()
    if not lists:
        doc.AddItem(Paragraph(_('No mailing lists are currently configured.')))
    else:
        table = Table(border=0, cellspacing=0, cellpadding=2)
        table.AddRow([Bold(_('List Name')), Bold(_('Description'))])
        for listname in sorted(lists):
            try:
                mlist = MailList.MailList(listname, lock=0)
                desc = mlist.description or _('No description available')
                table.AddRow([
                    Link(mlist.GetScriptURL('admin'), listname),
                    desc
                    ])
            except Errors.MMListError as e:
                syslog('error', 'admin overview: No such list "}{s": }{s\n',
                       listname, e)
                continue
        doc.AddItem(table)

    doc.AddItem(mlist.GetMailmanFooter())
    print(doc.Format())


def option_help(mlist, varhelp):
    # The html page document
    doc = Document()
    doc.set_language(mlist.preferred_language)
    doc.AddItem(Header(2, _('Variable Help')))

    doc.AddItem(Paragraph(_('''This page provides detailed information about the
    selected configuration variable.''')))

    # Get the variable's description
    varname, kind, params, dependancies, descr, elaboration = \
        get_item_characteristics(mlist.GetConfigInfo('general', varhelp))
    if not descr:
        doc.AddItem(Paragraph(_('No help available for this variable.')))
    else:
        doc.AddItem(Paragraph(descr))
        if elaboration:
            doc.AddItem(Paragraph(elaboration))

    doc.AddItem(mlist.GetMailmanFooter())
    print(doc.Format())


def show_results(mlist, doc, category, subcat, cgidata):
    # Produce the results page
    doc.AddItem(Header(2, _('Configuration Results')))

    doc.AddItem(Paragraph(_('''Your changes to the }{(category)s configuration
    category have been applied.''')))

    # Show the variables in this category
    doc.AddItem(show_variables(mlist, category, subcat, cgidata, doc))
    doc.AddItem(mlist.GetMailmanFooter())


def show_variables(mlist, category, subcat, cgidata, doc):
    options = mlist.GetConfigInfo(category, subcat)
    if not options:
        doc.AddItem(Paragraph(_('No options available in this category.')))
        return

    # Create the form
    form = Form(mlist.GetScriptURL('admin', absolute=1))
    form.AddItem(mlist.GetMailmanFooter())

    # Add the category and subcategory as hidden fields
    form.AddItem(Hidden('category', category))
    if subcat:
        form.AddItem(Hidden('subcat', subcat))

    # Add the CSRF token
    form.AddItem(Hidden('csrf_token', mlist.GetCSRFToken()))

    # Create the table
    table = Table(border=0, cellspacing=0, cellpadding=2)
    table.AddRow([Bold(_('Option')), Bold(_('Value'))])

    # Add each option
    for item in options:
        add_options_table_item(mlist, category, subcat, table, item)

    # Add the submit button
    table.AddRow([submit_button(), ''])

    # Add the table to the form
    form.AddItem(table)

    return form


def add_options_table_item(mlist, category, subcat, table, item, detailsp=1):
    # Add a row to an options table with the item description and value.
    varname, kind, params, dependancies, descr, elaboration = \
        get_item_characteristics(item)
    if not descr:
        return

    # Get the GUI value
    value = get_item_gui_value(mlist, category, kind, varname, params, item)
    if value is None:
        return

    # Get the description with link to details
    descr = get_item_gui_description(mlist, category, subcat,
                                    varname, descr, elaboration, detailsp)

    # Add the row
    table.AddRow([descr, value],
                 bgcolor=mm_cfg.WEB_ADMINITEM_COLOR)


def get_item_characteristics(record):
    # Break out the components of an item description from its description
    # record:
    #
    # 0 -- option-var name
    # 1 -- type
    # 2 -- entry size
    # 3 -- ?dependancies?
    # 4 -- Brief description
    # 5 -- Optional description elaboration
    varname = record[0]
    kind = record[1]
    params = record[2]
    dependancies = record[3]
    descr = record[4]
    elaboration = record[5]
    return varname, kind, params, dependancies, descr, elaboration


def get_item_gui_value(mlist, category, kind, varname, params, extra):
    """Return a representation of an item's settings."""
    if kind == 'string':
        return StringInput(varname, getattr(mlist, varname, ''))
    elif kind == 'text':
        return TextArea(varname, getattr(mlist, varname, ''))
    elif kind == 'boolean':
        return RadioButton(varname, getattr(mlist, varname, 0))
    elif kind == 'select':
        return Select(varname, params, getattr(mlist, varname, ''))
    elif kind == 'multiselect':
        return MultiSelect(varname, params, getattr(mlist, varname, []))
    elif kind == 'radio':
        return RadioButton(varname, getattr(mlist, varname, ''))
    elif kind == 'checkbox':
        return CheckBox(varname, getattr(mlist, varname, 0))
    elif kind == 'number':
        return NumberInput(varname, getattr(mlist, varname, 0))
    elif kind == 'email':
        return EmailInput(varname, getattr(mlist, varname, ''))
    elif kind == 'url':
        return URLInput(varname, getattr(mlist, varname, ''))
    elif kind == 'password':
        return PasswordInput(varname)
    elif kind == 'file':
        return FileInput(varname)
    else:
        assert 0, 'Bad gui widget type: }{s' }{ kind


def get_item_gui_description(mlist, category, subcat,
                            varname, descr, elaboration, detailsp):
    # Return the item's description, with link to details.
    #
    # Details are not included if this is a VARHELP page, because that /is/
    # the details page!
    if not detailsp:
        return descr
    if elaboration:
        return Link(mlist.GetScriptURL('admin', absolute=1) +
                   '?VARHELP=' + varname, descr)
    return descr


def membership_options(mlist, subcat, cgidata, doc, form):
    # Show the main stuff
    container = Container()
    container.AddItem(Header(2, _('Membership Management')))

    # Add the legend
    qsenviron = os.environ.get('QUERY_STRING')
    if qsenviron:
        qs = cgi.parse_qs(qsenviron).get('legend')
        if qs and isinstance(qs, list):
            qs = qs[0]
        if qs == 'yes':
            container.AddItem(Paragraph(_('''<b>Legend:</b><br>
            <b>Subscribe:</b> Add a new member to the list.<br>
            <b>Unsubscribe:</b> Remove a member from the list.<br>
            <b>Change:</b> Change a member's email address.<br>
            <b>Sync:</b> Synchronize member information with the list.''')))

    # Add the membership management options
    container.AddItem(mass_subscribe(mlist, container))
    container.AddItem(mass_remove(mlist, container))
    container.AddItem(address_change(mlist, container))
    container.AddItem(mass_sync(mlist, container))

    return container


def mass_subscribe(mlist, container):
    # MASS SUBSCRIBE
    table = Table(border=0, cellspacing=0, cellpadding=2)
    table.AddRow([Bold(_('Mass Subscribe'))])
    table.AddRow([StringInput('subscribe', '')])
    table.AddRow([submit_button('subscribe_btn')])
    container.AddItem(Center(table))


def mass_remove(mlist, container):
    # MASS UNSUBSCRIBE
    table = Table(border=0, cellspacing=0, cellpadding=2)
    table.AddRow([Bold(_('Mass Unsubscribe'))])
    table.AddRow([StringInput('unsubscribe', '')])
    table.AddRow([submit_button('unsubscribe_btn')])
    container.AddItem(Center(table))


def address_change(mlist, container):
    # ADDRESS CHANGE
    table = Table(border=0, cellspacing=0, cellpadding=2)
    table.AddRow([Bold(_('Address Change'))])
    table.AddRow([StringInput('oldaddr', '')])
    table.AddRow([StringInput('newaddr', '')])
    table.AddRow([submit_button('change_btn')])
    container.AddItem(Center(table))


def mass_sync(mlist, container):
    # MASS SYNC
    table = Table(border=0, cellspacing=0, cellpadding=2)
    table.AddRow([Bold(_('Mass Sync'))])
    table.AddRow([StringInput('sync', '')])
    table.AddRow([submit_button('sync_btn')])
    container.AddItem(Center(table))


def password_inputs(mlist):
    adminurl = mlist.GetScriptURL('admin', absolute=1)
    table = Table(border=0, cellspacing=0, cellpadding=2)
    table.AddRow([Bold(_('Password'))])
    table.AddRow([PasswordInput('adminpw')])
    table.AddRow([submit_button('adminpw_btn')])
    return table


def submit_button(name='submit'):
    table = Table(border=0, cellspacing=0, cellpadding=2)
    table.AddRow([SubmitButton(name)])
    return table


def change_options(mlist, category, subcat, cgidata, doc):
    global _
    # Set up i18n
    _ = i18n._

    def safeint(formvar, defaultval=None):
        try:
            return int(cgidata.getfirst(formvar, defaultval))
        except (TypeError, ValueError):
            return defaultval

    def i_to_c(mo):
        # Convert a matched string of digits to the corresponding unicode.
        return chr(int(mo.group(1)))

    def clean_input(x):
        # Strip leading/trailing whitespace and convert numeric HTML
        # entities.
        if x is None:
            return x
        x = x.strip()
        x = re.sub(r'&#(\d+);', i_to_c, x)
        return x

    # Handle membership management
    if 'subscribe_btn' in cgidata:
        subscribe = clean_input(cgidata.getfirst('subscribe'))
        if subscribe:
            try:
                mlist.AddMember(subscribe)
                doc.addError(_('Member }{(subscribe)s added.'))
            except Errors.MMAlreadyAMember:
                doc.addError(_('Member }{(subscribe)s is already a member.'))
            except Errors.MMInvalidEmail:
                doc.addError(_('Invalid email address: }{(subscribe)s'))
            except Exception as e:
                doc.addError(_('Error adding member }{(subscribe)s: }{(e)s'))

    if 'unsubscribe_btn' in cgidata:
        unsubscribe = clean_input(cgidata.getfirst('unsubscribe'))
        if unsubscribe:
            try:
                mlist.RemoveMember(unsubscribe)
                doc.addError(_('Member }{(unsubscribe)s removed.'))
            except Errors.MMNotAMember:
                doc.addError(_('Member }{(unsubscribe)s is not a member.'))
            except Errors.MMInvalidEmail:
                doc.addError(_('Invalid email address: }{(unsubscribe)s'))
            except Exception as e:
                doc.addError(_('Error removing member }{(unsubscribe)s: }{(e)s'))

    if 'change_btn' in cgidata:
        oldaddr = clean_input(cgidata.getfirst('oldaddr'))
        newaddr = clean_input(cgidata.getfirst('newaddr'))
        if oldaddr and newaddr:
            try:
                mlist.ChangeMemberAddress(oldaddr, newaddr)
                doc.addError(_('Member }{(oldaddr)s changed to }{(newaddr)s.'))
            except Errors.MMNotAMember:
                doc.addError(_('Member }{(oldaddr)s is not a member.'))
            except Errors.MMInvalidEmail:
                doc.addError(_('Invalid email address: }{(newaddr)s'))
            except Exception as e:
                doc.addError(_('Error changing member }{(oldaddr)s: }{(e)s'))

    if 'sync_btn' in cgidata:
        sync = clean_input(cgidata.getfirst('sync'))
        if sync:
            try:
                mlist.SyncMember(sync)
                doc.addError(_('Member }{(sync)s synchronized.'))
            except Errors.MMNotAMember:
                doc.addError(_('Member }{(sync)s is not a member.'))
            except Errors.MMInvalidEmail:
                doc.addError(_('Invalid email address: }{(sync)s'))
            except Exception as e:
                doc.addError(_('Error synchronizing member }{(sync)s: }{(e)s'))

    # Handle member options
    if 'setmemberopts_btn' in cgidata and 'user' in cgidata:
        user = cgidata['user']
        if isinstance(user, list):
            users = []
            for ui in range(len(user)):
                users.append(clean_input(user[ui]))
            user = users
        else:
            user = clean_input(user)
        if user:
            try:
                mlist.SetMemberOption(user, mm_cfg.Moderate, 1)
                doc.addError(_('Member }{(user)s moderated.'))
            except Errors.MMNotAMember:
                doc.addError(_('Member }{(user)s is not a member.'))
            except Errors.MMInvalidEmail:
                doc.addError(_('Invalid email address: }{(user)s'))
            except Exception as e:
                doc.addError(_('Error moderating member }{(user)s: }{(e)s'))

    # Handle other options
    for item in mlist.GetConfigInfo(category, subcat):
        varname, kind, params, dependancies, descr, elaboration = \
            get_item_characteristics(item)
        if varname in cgidata:
            value = cgidata.getfirst(varname)
            if value is not None:
                if kind == 'boolean':
                    setattr(mlist, varname, value != '0')
                elif kind == 'number':
                    setattr(mlist, varname, safeint(value))
                elif kind == 'multiselect':
                    setattr(mlist, varname, cgidata.getlist(varname))
                else:
                    setattr(mlist, varname, clean_input(value))
}