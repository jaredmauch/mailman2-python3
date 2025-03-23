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

from __future__ import absolute_import
from __future__ import division

from __future__ import unicode_literals

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
        doc.AddItem(Header(2, _('No such list <em>{(safelistname)s</em>')))
        # Send this with a 404 status.
        print('Status: 404 Not Found')
        print(doc.Format())
        syslog('error', 'edithtml: No such list "}{s": }{s', listname, e)
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

    # Check for CSRF
    if not csrf_check(mlist, cgidata, AUTH_CONTEXTS):
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('Invalid form submission.')))
        print('Status: 400 Bad Request')
        print(doc.Format())
        return

    # Must be authenticated to get any farther
    if not Auth.authenticate(mlist, cgidata, AUTH_CONTEXTS):
        Auth.setAuth(mlist, AUTH_CONTEXTS)
        doc.AddItem(Header(2, _("Authentication required")))
        doc.AddItem(Auth.LoginForm(mlist, AUTH_CONTEXTS))
        print(doc.Format())
        return

    # Get the template name from the URL
    template_name = cgidata.getfirst('template', '')
    if not template_name:
        # Show the template selection page
        doc.AddItem(Header(2, _("Select template to edit")))
        template_list = Container()
        for name, desc in template_data:
            template_list.AddItem(
                Container(
                    Bold(name),
                    ' - ',
                    desc,
                    ' ',
                    Container(
                        '[',
                        Link(Utils.ScriptURL('edithtml', mlist),
                            _('Edit')),
                        ']'
                    )
                )
            )
        doc.AddItem(FontSize("+2", template_list))
        doc.AddItem(mlist.GetMailmanFooter())
        print(doc.Format())
        return

    # Validate the template name
    valid_templates = [name for name, desc in template_data]
    if template_name not in valid_templates:
        safetemplatename = Utils.websafe(template_name)
        doc.AddItem(Header(2, _("}{(safetemplatename)s: Invalid template")))
        doc.AddItem(mlist.GetMailmanFooter())
        print(doc.Format())
        return

    # Get the language from the URL
    lang = cgidata.getfirst('language', '')
    if lang and lang != mlist.preferred_language:
        if lang not in mlist.GetAvailableLanguages():
            safelang = Utils.websafe(lang)
            doc.AddItem(Header(2, _("}{(safelang)s: Invalid language")))
            doc.AddItem(mlist.GetMailmanFooter())
            print(doc.Format())
            return
    else:
        lang = mlist.preferred_language

    # Get the template content
    try:
        template_info = mlist.GetTemplate(template_name, lang)
    except Errors.MMUnknownTemplateError as e:
        safetemplatename = Utils.websafe(template_name)
        doc.AddItem(Header(2, _("}{(safetemplatename)s: Invalid template")))
        doc.AddItem(mlist.GetMailmanFooter())
        print(doc.Format())
        return

    # If this is a POST request, process the form
    if os.environ.get('REQUEST_METHOD') == 'POST':
        ChangeHTML(mlist, cgidata, template_name, doc, lang)
    else:
        FormatHTML(mlist, doc, template_name, template_info, lang)

    try:
        doc.AddItem(mlist.GetMailmanFooter())
        print(doc.Format())
    finally:
        doc.AddItem(mlist.GetMailmanFooter())
        print(doc.Format())


def FormatHTML(mlist, doc, template_name, template_info, lang=None):
    if lang not in mlist.GetAvailableLanguages():
        lang = mlist.preferred_language
    lcset = Utils.GetCharSet(lang)
    doc.AddItem(Header(1,'}{s:' }{ mlist.real_name))
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
           _("""The page you saved contains suspicious HTML that could
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
        os.mkdir(langdir, 0o2775)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
    finally:
        os.umask(omask)
    with open(os.path.join(langdir, template_name), 'w') as fp:
        fp.write(code)
    doc.AddItem(Header(3, _('HTML successfully updated.')))
    doc.AddItem('<hr>')
}