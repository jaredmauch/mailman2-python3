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

"""Produce listinfo page, primary web entry-point to mailing lists.
"""
from __future__ import print_function

# No lock needed in this script, because we don't change data.

from builtins import str
import os
import urllib.parse
import time
import sys
import ipaddress

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import MailList
from Mailman import Errors
from Mailman import i18n
from Mailman.htmlformat import *
from Mailman.Logging.Syslog import mailman_log
from Mailman.Utils import validate_ip_address

# Set up i18n
_ = i18n._
i18n.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)


def main():
    parts = Utils.GetPathPieces()
    if not parts:
        listinfo_overview()
        return

    listname = parts[0].lower()
    try:
        mlist = MailList.MailList(listname, lock=0)
    except Errors.MMListError as e:
        # Avoid cross-site scripting attacks
        safelistname = Utils.websafe(listname)
        # Send this with a 404 status.
        print('Status: 404 Not Found')
        listinfo_overview(_(f'No such list <em>{safelistname}</em>'))
        mailman_log('error', 'listinfo: No such list "%s": %s', listname, e)
        return

    # See if the user want to see this page in other language
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
        doc = Document()
        doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script.')))
        # Send this with a 400 status.
        print('Status: 400 Bad Request')
        print(doc.Format())
        return

    language = cgidata.get('language', [None])[0]
    if not Utils.IsLanguage(language):
        language = mlist.preferred_language
    i18n.set_language(language)
    list_listinfo(mlist, language)


def listinfo_overview(msg=''):
    # Present the general listinfo overview
    hostname = Utils.get_domain()
    # Set up the document and assign it the correct language.  The only one we
    # know about at the moment is the server's default.
    doc = Document()
    doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)

    legend = (hostname + "'s Mailing Lists")
    doc.SetTitle(legend)

    table = Table(border=0, width="100%")
    table.AddRow([Center(Header(2, legend))])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2,
                      bgcolor=mm_cfg.WEB_HEADER_COLOR)

    # Skip any mailing lists that isn't advertised.
    advertised = []
    listnames = Utils.list_names()
    listnames.sort()

    for name in listnames:
        try:
            mlist = MailList.MailList(name, lock=0)
        except Errors.MMUnknownListError:
            # The list could have been deleted by another process.
            continue
        if mlist.advertised:
            if mm_cfg.VIRTUAL_HOST_OVERVIEW and (
                   mlist.web_page_url.find('/%s/' % hostname) == -1 and
                   mlist.web_page_url.find('/%s:' % hostname) == -1):
                # List is for different identity of this host - skip it.
                continue
            else:
                advertised.append((mlist.GetScriptURL('listinfo'),
                                   mlist.real_name,
                                   Utils.websafe(mlist.GetDescription())))
    if msg:
        greeting = FontAttr(msg, color="ff5060", size="+1")
    else:
        greeting = FontAttr(_('Welcome!'), size='+2')

    welcome = [greeting]
    mailmanlink = Link(mm_cfg.MAILMAN_URL, _('Mailman')).Format()
    if not advertised:
        welcome.extend(
            _(f'''<p>There currently are no publicly-advertised
            {mailmanlink} mailing lists on {hostname}.'''))
    else:
        welcome.append(
            _(f'''<p>Below is a listing of all the public mailing lists on
            {hostname}.  Click on a list name to get more information about
            the list, or to subscribe, unsubscribe, and change the preferences
            on your subscription.'''))

    # set up some local variables
    adj = msg and _('right') or ''
    siteowner = Utils.get_site_email()
    welcome.extend(
        (_(f''' To visit the general information page for an unadvertised list,
        open a URL similar to this one, but with a '/' and the {adj}
        list name appended.
        <p>List administrators, you can visit '''),
         Link(Utils.ScriptURL('admin'),
              _('the list admin overview page')),
         _(f''' to find the management interface for your list.
         <p>If you are having trouble using the lists, please contact '''),
         Link('mailto:' + siteowner, siteowner),
         '.<p>'))

    table.AddRow([Container(*welcome)])
    table.AddCellInfo(max(table.GetCurrentRowIndex(), 0), 0, colspan=2)

    if advertised:
        table.AddRow(['&nbsp;', '&nbsp;'])
        table.AddRow([Bold(FontAttr(_('List'), size='+2')),
                      Bold(FontAttr(_('Description'), size='+2'))
                      ])
        highlight = 1
        for url, real_name, description in advertised:
            table.AddRow(
                [Link(url, Bold(real_name)),
                      description or Italic(_('[no description available]'))])
            if highlight and mm_cfg.WEB_HIGHLIGHT_COLOR:
                table.AddRowInfo(table.GetCurrentRowIndex(),
                                 bgcolor=mm_cfg.WEB_HIGHLIGHT_COLOR)
            highlight = not highlight

    doc.AddItem(table)
    doc.AddItem('<hr>')
    doc.AddItem(MailmanLogo())
    print(doc.Format())


def list_listinfo(mlist, language):
    # Generate list specific listinfo
    doc = HeadlessDocument()
    doc.set_language(language)

    # First load the template
    template_content, template_path = Utils.findtext('listinfo.html', lang=language, mlist=mlist)
    if template_content is None:
        mailman_log('error', 'Could not load template file: %s', template_path)
        return

    # Then get replacements
    replacements = mlist.GetStandardReplacements(language)

    if not mlist.digestable or not mlist.nondigestable:
        replacements['<mm-digest-radio-button>'] = ""
        replacements['<mm-undigest-radio-button>'] = ""
        replacements['<mm-digest-question-start>'] = '<!-- '
        replacements['<mm-digest-question-end>'] = ' -->'
    else:
        replacements['<mm-digest-radio-button>'] = mlist.FormatDigestButton()
        replacements['<mm-undigest-radio-button>'] = mlist.FormatUndigestButton()
        replacements['<mm-digest-question-start>'] = ''
        replacements['<mm-digest-question-end>'] = ''
    replacements['<mm-plain-digests-button>'] = mlist.FormatPlainDigestsButton()
    replacements['<mm-mime-digests-button>'] = mlist.FormatMimeDigestsButton()
    replacements['<mm-subscribe-box>'] = mlist.FormatBox('email', size=30)
    replacements['<mm-subscribe-button>'] = mlist.FormatButton(
        'email-button', text=_('Subscribe'))
    replacements['<mm-new-password-box>'] = mlist.FormatSecureBox('pw')
    replacements['<mm-confirm-password>'] = mlist.FormatSecureBox('pw-conf')
    replacements['<mm-subscribe-form-start>'] = mlist.FormatFormStart(
        'subscribe')
    if mm_cfg.SUBSCRIBE_FORM_SECRET:
        # Get and validate IP address
        ip = os.environ.get('REMOTE_ADDR', '')
        is_valid, normalized_ip = validate_ip_address(ip)
        if not is_valid:
            ip = ''
        else:
            ip = normalized_ip
        # render CAPTCHA, if configured
        if isinstance(mm_cfg.CAPTCHAS, dict) and 'en' in mm_cfg.CAPTCHAS:
            (captcha_question, captcha_box, captcha_idx) = \
                Utils.captcha_display(mlist, language, mm_cfg.CAPTCHAS)
            pre_question = _(
                    """Please answer the following question to prove that
                    you are not a bot:"""
                )
            replacements['<mm-captcha-ui>'] = (
                """<tr><td BGCOLOR="#dddddd">%s<br>%s</td><td>%s</td></tr>"""
                % (pre_question, captcha_question, captcha_box))
        else:
            # just to have something to include in the hash below
            captcha_idx = ''
        # fill form
        replacements['<mm-subscribe-form-start>'] += (
                '<input type="hidden" name="sub_form_token"'
                ' value="%s:%s:%s">\n'
                % (time.time(), captcha_idx,
                          Utils.sha_new((mm_cfg.SUBSCRIBE_FORM_SECRET + ":" +
                          str(time.time()) + ":" +
                          captcha_idx + ":" +
                          mlist.internal_name() + ":" +
                          ip).encode('utf-8')).hexdigest()
                    )
                )
    # Roster form substitutions
    replacements['<mm-roster-form-start>'] = mlist.FormatFormStart('roster')
    # Options form substitutions
    replacements['<mm-options-form-start>'] = mlist.FormatFormStart('options')
    replacements['<mm-editing-options>'] = mlist.FormatEditingOption(language)
    replacements['<mm-info-button>'] = SubmitButton('UserOptions',
                                                    _('Edit Options')).Format()
    # If only one language is enabled for this mailing list, omit the choice
    # buttons.
    if len(mlist.available_languages) == 1:
        listlangs = _(Utils.GetLanguageDescr(mlist.preferred_language))
    else:
        listlangs = mlist.GetLangSelectBox(language).Format()
    replacements['<mm-displang-box>'] = listlangs
    replacements['<mm-lang-form-start>'] = mlist.FormatFormStart('listinfo')
    replacements['<mm-fullname-box>'] = mlist.FormatBox('fullname', size=30)
    # If reCAPTCHA is enabled, display its user interface
    if mm_cfg.RECAPTCHA_SITE_KEY:
        noscript = _('This form requires JavaScript.')
        replacements['<mm-recaptcha-ui>'] = (
            """<tr><td>&nbsp;</td><td>
            <noscript>%s</noscript>
            <script src="https://www.google.com/recaptcha/api.js?hl=%s">
            </script>
            <div class="g-recaptcha" data-sitekey="%s"></div>
            </td></tr>"""
            % (noscript, language, mm_cfg.RECAPTCHA_SITE_KEY))
    else:
        replacements['<mm-recaptcha-ui>'] = ''

    # Process the template with replacements
    try:
        # Ensure template content is unicode
        if isinstance(template_content, bytes):
            template_content = template_content.decode('utf-8', 'replace')
        
        # Process replacements
        for key, value in replacements.items():
            if isinstance(value, bytes):
                value = value.decode('utf-8', 'replace')
            template_content = template_content.replace(key, str(value))
        
        # Add the processed content to the document
        doc.AddItem(template_content)
        
    except Exception as e:
        mailman_log('error', 'Error processing template: %s', str(e))
        return

    # Print the formatted document
    print(doc.Format())


if __name__ == "__main__":
    main()
