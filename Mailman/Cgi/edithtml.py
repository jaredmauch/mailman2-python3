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

"""Script which implements admin editing of the list's html templates."""
from __future__ import print_function

import os
import cgi
import errno
import re

from Mailman import Utils
from Mailman import MailList
from Mailman.htmlformat import *
from Mailman.HTMLFormatter import HTMLFormatter
from Mailman import Errors
from Mailman.Cgi import Auth
from Mailman.Logging.Syslog import syslog
from Mailman import i18n
from Mailman.CSRFcheck import csrf_check

_ = i18n._

AUTH_CONTEXTS = (mm_cfg.AuthListAdmin, mm_cfg.AuthSiteAdmin)



def main():
    # Trick out pygettext since we want to mark template_data as translatable,
    # but we don't want to actually translate it here.
    def _(s):
        return s

    template_data = (
        ('listinfo.html',    _('General list information page')),
        ('subscribe.html',   _('Subscribe results page')),
        ('options.html',     _('User specific options page')),
        ('subscribeack.txt', _('Welcome email text file')),
        ('masthead.txt',     _('Digest masthead')),
        ('postheld.txt',     _('User notice of held post')),
        ('approve.txt',      _('User notice of held subscription')),
        ('refuse.txt',       _('Notice of post refused by moderator')),
        ('invite.txt',       _('Invitation to join list')),
        ('verify.txt',       _('Request to confirm subscription')),
        ('unsub.txt',        _('Request to confirm unsubscription')),
        ('nomoretoday.txt',  _('User notice of autoresponse limit')),
        ('postack.txt',      _('User post acknowledgement')),
        ('disabled.txt',     _('Subscription disabled by bounce warning')),
        ('admlogin.html',    _('Admin/moderator login page')),
        ('private.html',     _('Private archive login page')),
        ('userpass.txt',     _('On demand password reminder')),
        )

    _ = i18n._
    doc = Document()

    # Set up the system default language
    i18n.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)
    doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)

    parts = Utils.GetPathPieces()
    if not parts:
        doc.AddItem(Header(2, _("List name is required.")))
        print(doc.Format())
        return

    listname = parts[0].lower()
    try:
        mlist = MailList.MailList(listname, lock=0)
    except Errors.MMListError as e:
        # Avoid cross-site scripting attacks
        safelistname = Utils.websafe(listname)
        doc.AddItem(Header(2, _(f'No such list <em>{safelistname}</em>')))
        # Send this with a 404 status.
        print('Status: 404 Not Found')
        print(doc.Format())
        syslog('error', 'edithtml: No such list "%s": %s', listname, e)
        return

    # Now that we have a valid list, set the language to its default
    i18n.set_language(mlist.preferred_language)
    doc.set_language(mlist.preferred_language)

    # Must be authenticated to get any farther
    cgidata = cgi.FieldStorage()
    try:
        cgidata.getfirst('adminpw', '')
    except TypeError:
        # Someone crafted a POST with a bad Content-Type:.
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script.')))
        # Send this with a 400 status.
        print('Status: 400 Bad Request')
        print(doc.Format())
        return

    # CSRF check
    safe_params = ['VARHELP', 'adminpw', 'admlogin']
    params = list(cgidata.keys())
    if set(params) - set(safe_params):
        csrf_checked = csrf_check(mlist, cgidata.getfirst('csrf_token'),
                                  'admin')
    else:
        csrf_checked = True
    # if password is present, void cookie to force password authentication.
    if cgidata.getfirst('adminpw'):
        os.environ['HTTP_COOKIE'] = ''
        csrf_checked = True

    # Editing the html for a list is limited to the list admin and site admin.
    if not mlist.WebAuthenticate((mm_cfg.AuthListAdmin,
                                  mm_cfg.AuthSiteAdmin),
                                 cgidata.getfirst('adminpw', '')):
        if 'admlogin' in cgidata:
            # This is a re-authorization attempt
            msg = Bold(FontSize('+1', _('Authorization failed.'))).Format()
            remote = os.environ.get('HTTP_FORWARDED_FOR',
                     os.environ.get('HTTP_X_FORWARDED_FOR',
                     os.environ.get('REMOTE_ADDR',
                                    'unidentified origin')))
            syslog('security',
                   'Authorization failed (edithtml): list=%s: remote=%s',
                   listname, remote)
        else:
            msg = ''
        Auth.loginpage(mlist, 'admin', msg=msg)
        return

    # See if the user want to see this page in other language
    language = cgidata.getfirst('language', '')
    if language not in mlist.GetAvailableLanguages():
        language = mlist.preferred_language
    i18n.set_language(language)
    doc.set_language(language)

    realname = mlist.real_name
    if len(parts) > 1:
        template_name = parts[1]
        for (template, info) in template_data:
            if template == template_name:
                template_info = _(info)
                doc.SetTitle(_(
                    f'{realname} -- Edit html for {template_info}'))
                break
        else:
            # Avoid cross-site scripting attacks
            safetemplatename = Utils.websafe(template_name)
            doc.SetTitle(_('Edit HTML : Error'))
            doc.AddItem(Header(2, _(f"{safetemplatename}: Invalid template")))
            doc.AddItem(mlist.GetMailmanFooter())
            print(doc.Format())
            return
    else:
        doc.SetTitle(_(f'{realname} -- HTML Page Editing'))
        doc.AddItem(Header(1, _(f'{realname} -- HTML Page Editing')))
        doc.AddItem(Header(2, _('Select page to edit:')))
        template_list = UnorderedList()
        for (template, info) in template_data:
            l = Link(mlist.GetScriptURL('edithtml') + '/' + template, _(info))
            template_list.AddItem(l)
        doc.AddItem(FontSize("+2", template_list))
        doc.AddItem(mlist.GetMailmanFooter())
        print(doc.Format())
        return

    try:
        if list(cgidata.keys()) and 'langform' not in cgidata:
            if csrf_checked:
                ChangeHTML(mlist, cgidata, template_name, doc, lang=language)
            else:
                doc.addError(
                  _('The form lifetime has expired. (request forgery check)'))
        FormatHTML(mlist, doc, template_name, template_info, lang=language)
    finally:
        doc.AddItem(mlist.GetMailmanFooter())
        print(doc.Format())



def FormatHTML(mlist, doc, template_name, template_info, lang=None):
    if lang not in mlist.GetAvailableLanguages():
        lang = mlist.preferred_language
    lcset = Utils.GetCharSet(lang)
    doc.AddItem(Header(1,'%s:' % mlist.real_name))
    doc.AddItem(Header(1, template_info))
    doc.AddItem('<hr>')

    link = Link(mlist.GetScriptURL('admin'),
                _('View or edit the list configuration information.'))
    backlink = Link(mlist.GetScriptURL('edithtml'),
                    _('Edit the public HTML pages and text files'))

    doc.AddItem(FontSize("+1", link))
    doc.AddItem('<br>')
    doc.AddItem(FontSize("+1", backlink))
    doc.AddItem('<p>')
    doc.AddItem('<hr>')
    if len(mlist.GetAvailableLanguages()) > 1:
        langform = Form(mlist.GetScriptURL('edithtml') + '/' + template_name,
                        mlist=mlist, contexts=AUTH_CONTEXTS)
        langform.AddItem(
                    mlist.FormatButton('editlang-button',
                                       text = _("Edit this template for")))
        langform.AddItem(mlist.GetLangSelectBox(lang))
        langform.AddItem(Hidden('langform', 'True'))
        doc.AddItem(langform)
        doc.AddItem('<hr>')
    form = Form(mlist.GetScriptURL('edithtml') + '/' + template_name,
               mlist=mlist, contexts=AUTH_CONTEXTS)
    text = Utils.maketext(template_name, raw=1, lang=lang, mlist=mlist)
    # MAS: Don't websafe twice.  TextArea does it.
    form.AddItem(TextArea('html_code', text, rows=40, cols=75))
    form.AddItem('<p>' + _('When you are done making changes...'))
    if lang != mlist.preferred_language:
        form.AddItem(Hidden('language', lang))
    form.AddItem(SubmitButton('submit', _('Submit Changes')))
    doc.AddItem(form)



def ChangeHTML(mlist, cgi_info, template_name, doc, lang=None):
    if lang not in mlist.GetAvailableLanguages():
        lang = mlist.preferred_language
    if 'html_code' not in cgi_info:
        doc.AddItem(Header(3,_("Can't have empty html page.")))
        doc.AddItem(Header(3,_("HTML Unchanged.")))
        doc.AddItem('<hr>')
        return
    code = cgi_info['html_code'].value
    if Utils.suspiciousHTML(code):
        doc.AddItem(Header(3,
           _(f"""The page you saved contains suspicious HTML that could
potentially expose your users to cross-site scripting attacks.  This change
has therefore been rejected.  If you still want to make these changes, you
must have shell access to your Mailman server.
             """)))
        doc.AddItem(_('See '))
        doc.AddItem(Link(
'http://wiki.list.org/x/jYA9',
                _('FAQ 4.48.')))
        doc.AddItem(Header(3,_("Page Unchanged.")))
        doc.AddItem('<hr>')
        return
    langdir = os.path.join(mlist.fullpath(), lang)
    # Make sure the directory exists
    omask = os.umask(0)
    try:
        try:
            os.mkdir(langdir, 0o2775)
        except OSError as e:
            if e.errno != errno.EEXIST: raise
    finally:
        os.umask(omask)
    fp = open(os.path.join(langdir, template_name), 'w')
    try:
        fp.write(code)
    finally:
        fp.close()
    doc.AddItem(Header(3, _('HTML successfully updated.')))
    doc.AddItem('<hr>')
