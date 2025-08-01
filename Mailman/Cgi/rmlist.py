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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

"""Remove/delete mailing lists through the web."""
from __future__ import print_function

import os
from Mailman.Utils import FieldStorage
import sys
import errno
import shutil

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import MailList
from Mailman import Errors
from Mailman import i18n
from Mailman.htmlformat import *
from Mailman.Logging.Syslog import syslog

# Set up i18n
_ = i18n._
i18n.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)



def main():
    doc = Document()
    doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)

    cgidata = FieldStorage()
    try:
        cgidata.getfirst('password', '')
    except TypeError:
        # Someone crafted a POST with a bad Content-Type:.
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script.')))
        # Send this with a 400 status.
        print('Status: 400 Bad Request')
        print(doc.Format())
        return

    parts = Utils.GetPathPieces()

    if not parts:
        # Bad URL specification
        title = _('Bad URL specification')
        doc.SetTitle(title)
        doc.AddItem(
            Header(3, Bold(FontAttr(title, color='#ff0000', size='+2'))))
        doc.AddItem('<hr>')
        doc.AddItem(MailmanLogo())
        print(doc.Format())
        syslog('error', 'Bad URL specification: %s', parts)
        return
        
    listname = parts[0].lower()
    try:
        mlist = MailList.MailList(listname, lock=0)
    except Errors.MMListError as e:
        # Avoid cross-site scripting attacks
        safelistname = Utils.websafe(listname)
        title = _(f'No such list <em>{safelistname}</em>')
        doc.SetTitle(_(f'No such list {safelistname}'))
        doc.AddItem(
            Header(3,
                   Bold(FontAttr(title, color='#ff0000', size='+2'))))
        doc.AddItem('<hr>')
        doc.AddItem(MailmanLogo())
        # Send this with a 404 status.
        print('Status: 404 Not Found')
        print(doc.Format())
        syslog('error', 'rmlist: No such list "%s": %s\n', listname, e)
        return

    # Now that we have a valid mailing list, set the language
    i18n.set_language(mlist.preferred_language)
    doc.set_language(mlist.preferred_language)

    # Be sure the list owners are not sneaking around!
    if not mm_cfg.OWNERS_CAN_DELETE_THEIR_OWN_LISTS:
        title = _("You're being a sneaky list owner!")
        doc.SetTitle(title)
        doc.AddItem(
            Header(3, Bold(FontAttr(title, color='#ff0000', size='+2'))))
        doc.AddItem(mlist.GetMailmanFooter())
        print(doc.Format())
        syslog('mischief', 'Attempt to sneakily delete a list: %s', listname)
        return

    if 'doit' in cgidata:
        process_request(doc, cgidata, mlist)
        print(doc.Format())
        return

    request_deletion(doc, mlist)
    # Always add the footer and print the document
    doc.AddItem(mlist.GetMailmanFooter())
    print(doc.Format())



def process_request(doc, cgidata, mlist):
    password = cgidata.getfirst('password', '').strip()
    try:
        delarchives = int(cgidata.getfirst('delarchives', '0'))
    except ValueError:
        delarchives = 0

    # Removing a list is limited to the list-creator (a.k.a. list-destroyer),
    # the list-admin, or the site-admin.  Don't use WebAuthenticate here
    # because we want to be sure the actual typed password is valid, not some
    # password sitting in a cookie.
    if mlist.Authenticate((mm_cfg.AuthCreator,
                           mm_cfg.AuthListAdmin,
                           mm_cfg.AuthSiteAdmin),
                          password) == mm_cfg.UnAuthorized:
        remote = os.environ.get('HTTP_FORWARDED_FOR',
                 os.environ.get('HTTP_X_FORWARDED_FOR',
                 os.environ.get('REMOTE_ADDR',
                                'unidentified origin')))
        syslog('security',
               'Authorization failed (rmlist): list=%s: remote=%s',
               mlist.internal_name(), remote)
        request_deletion(
            doc, mlist,
            _('You are not authorized to delete this mailing list'))
        return

    # Do the MTA-specific list deletion tasks
    if mm_cfg.MTA:
        modname = 'Mailman.MTA.' + mm_cfg.MTA
        __import__(modname)
        sys.modules[modname].remove(mlist, cgi=1)
    
    REMOVABLES = ['lists/%s']

    if delarchives:
        REMOVABLES.extend(['archives/private/%s',
                           'archives/private/%s.mbox',
                           'archives/public/%s',
                           'archives/public/%s.mbox',
                           ])

    problems = 0
    listname = mlist.internal_name()
    for dirtmpl in REMOVABLES:
        dir = os.path.join(mm_cfg.VAR_PREFIX, dirtmpl % listname)
        if os.path.islink(dir):
            try:
                os.unlink(dir)
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EPERM): raise
                problems += 1
                syslog('error',
                       'link %s not deleted due to permission problems',
                       dir)
        elif os.path.isdir(dir):
            try:
                shutil.rmtree(dir)
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EPERM): raise
                problems += 1
                syslog('error',
                       'directory %s not deleted due to permission problems',
                       dir)

    title = _('Mailing list deletion results')
    doc.SetTitle(title)
    table = Table(border=0, width='100%')
    table.AddRow([Center(Bold(FontAttr(title, size='+1')))])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0,
                      bgcolor=mm_cfg.WEB_HEADER_COLOR)
    if not problems:
        table.AddRow([_(f'''You have successfully deleted the mailing list
    <b>{listname}</b>.''')])
    else:
        sitelist = Utils.get_site_email(mlist.host_name)
        table.AddRow([_(f'''There were some problems deleting the mailing list
        <b>{listname}</b>.  Contact your site administrator at {sitelist}
        for details.''')])
    doc.AddItem(table)
    doc.AddItem('<hr>')
    doc.AddItem(_('Return to the ') +
                Link(Utils.ScriptURL('listinfo'),
                     _('general list overview')).Format())
    doc.AddItem(_('<br>Return to the ') +
                Link(Utils.ScriptURL('admin'),
                     _('administrative list overview')).Format())
    doc.AddItem(MailmanLogo())



def request_deletion(doc, mlist, errmsg=None):
    realname = mlist.real_name
    title = _(f'Permanently remove mailing list <em>{realname}</em>')
    doc.SetTitle(_(f'Permanently remove mailing list {realname}'))

    table = Table(border=0, width='100%')
    table.AddRow([Center(Bold(FontAttr(title, size='+1')))])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0,
                      bgcolor=mm_cfg.WEB_HEADER_COLOR)

    # Add any error message
    if errmsg:
        table.AddRow([Header(3, Bold(
            FontAttr(_('Error: '), color='#ff0000', size='+2').Format() +
            Italic(errmsg).Format()))])

    table.AddRow([_(f"""This page allows you as the list owner, to permanently
    remove this mailing list from the system.  <strong>This action is not
    undoable</strong> so you should undertake it only if you are absolutely
    sure this mailing list has served its purpose and is no longer necessary.

    <p>Note that no warning will be sent to your list members and after this
    action, any subsequent messages sent to the mailing list, or any of its
    administrative addreses will bounce.

    <p>You also have the option of removing the archives for this mailing list
    at this time.  It is almost always recommended that you do
    <strong>not</strong> remove the archives, since they serve as the
    historical record of your mailing list.

    <p>For your safety, you will be asked to reconfirm the list password.
    """)])
    GREY = mm_cfg.WEB_ADMINITEM_COLOR
    form = Form(mlist.GetScriptURL('rmlist'))
    ftable = Table(border=0, cols='2', width='100%',
                   cellspacing=3, cellpadding=4)
    
    ftable.AddRow([Label(_('List password:')), PasswordBox('password')])
    ftable.AddCellInfo(ftable.GetCurrentRowIndex(), 0, bgcolor=GREY)
    ftable.AddCellInfo(ftable.GetCurrentRowIndex(), 1, bgcolor=GREY)

    ftable.AddRow([Label(_('Also delete archives?')),
                   RadioButtonArray('delarchives', (_('No'), _('Yes')),
                                    checked=0, values=(0, 1))])
    ftable.AddCellInfo(ftable.GetCurrentRowIndex(), 0, bgcolor=GREY)
    ftable.AddCellInfo(ftable.GetCurrentRowIndex(), 1, bgcolor=GREY)

    ftable.AddRow([Center(Link(
        mlist.GetScriptURL('admin'),
        _('<b>Cancel</b> and return to list administration')))])
    ftable.AddCellInfo(ftable.GetCurrentRowIndex(), 0, colspan=2)

    ftable.AddRow([Center(SubmitButton('doit', _('Delete this list')))])
    ftable.AddCellInfo(ftable.GetCurrentRowIndex(), 0, colspan=2)
    form.AddItem(ftable)
    table.AddRow([form])
    doc.AddItem(table)
