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

# No lock needed in this script, because we don't change data.

import os
import cgi
import time

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
    parts = Utils.GetPathPieces()
    if not parts:
        listinfo_overview()
        return

    listname = parts[0].lower()
    try:
        mlist = MailList.MailList(listname, lock=0)
    except Errors.MMListError, e:
        # Avoid cross-site scripting attacks
        safelistname = Utils.websafe(listname)
        # Send this with a 404 status.
        print 'Status: 404 Not Found'
        listinfo_overview(_('No such list <em>%(safelistname)s</em>'))
        syslog('error', 'listinfo: No such list "%s": %s', listname, e)
        return

    # See if the user want to see this page in other language
    cgidata = cgi.FieldStorage()
    try:
        language = cgidata.getfirst('language')
    except TypeError:
        # Someone crafted a POST with a bad Content-Type:.
        doc = Document()
        doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('Invalid options to CGI script.')))
        # Send this with a 400 status.
        print 'Status: 400 Bad Request'
        print doc.Format()
        return

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

    legend = _("%(hostname)s Mailing Lists")
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
            _('''<p>There currently are no publicly-advertised
            %(mailmanlink)s mailing lists on %(hostname)s.'''))
    else:
        welcome.append(
            _('''<p>Below is a listing of all the public mailing lists on
            %(hostname)s.  Click on a list name to get more information about
            the list, or to subscribe, unsubscribe, and change the preferences
            on your subscription.'''))

    # set up some local variables
    adj = msg and _('right') or ''
    siteowner = Utils.get_site_email()
    welcome.extend(
        (_(''' To visit the general information page for an unadvertised list,
        open a URL similar to this one, but with a '/' and the %(adj)s
        list name appended.
        <p>List administrators, you can visit '''),
         Link(Utils.ScriptURL('admin'),
              _('the list admin overview page')),
         _(''' to find the management interface for your list.
         <p>If you are having trouble using the lists, please contact '''),
         Link('mailto:' + siteowner, siteowner),
         '.<p>'))

    table.AddRow([apply(Container, welcome)])
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
    print doc.Format()



def list_listinfo(mlist, lang):
    # Generate list specific listinfo
    doc = HeadlessDocument()
    doc.set_language(lang)

    replacements = mlist.GetStandardReplacements(lang)

    if not mlist.digestable or not mlist.nondigestable:
        replacements['<mm-digest-radio-button>'] = ""
        replacements['<mm-undigest-radio-button>'] = ""
        replacements['<mm-digest-question-start>'] = '<!-- '
        replacements['<mm-digest-question-end>'] = ' -->'
    else:
        replacements['<mm-digest-radio-button>'] = mlist.FormatDigestButton()
        replacements['<mm-undigest-radio-button>'] = \
                                                   mlist.FormatUndigestButton()
        replacements['<mm-digest-question-start>'] = ''
        replacements['<mm-digest-question-end>'] = ''
    replacements['<mm-plain-digests-button>'] = \
                                              mlist.FormatPlainDigestsButton()
    replacements['<mm-mime-digests-button>'] = mlist.FormatMimeDigestsButton()
    replacements['<mm-subscribe-box>'] = mlist.FormatBox('email', size=30)
    replacements['<mm-subscribe-button>'] = mlist.FormatButton(
        'email-button', text=_('Subscribe'))
    replacements['<mm-new-password-box>'] = mlist.FormatSecureBox('pw')
    replacements['<mm-confirm-password>'] = mlist.FormatSecureBox('pw-conf')
    replacements['<mm-subscribe-form-start>'] = mlist.FormatFormStart(
        'subscribe')
    if mm_cfg.SUBSCRIBE_FORM_SECRET:
        now = str(int(time.time()))
        remote = os.environ.get('HTTP_FORWARDED_FOR',
                 os.environ.get('HTTP_X_FORWARDED_FOR',
                 os.environ.get('REMOTE_ADDR',
                                'w.x.y.z')))
        # Try to accept a range in case of load balancers, etc.  (LP: #1447445)
        if remote.find('.') >= 0:
            # ipv4 - drop last octet
            remote = remote.rsplit('.', 1)[0]
        else:
            # ipv6 - drop last 16 (could end with :: in which case we just
            #        drop one : resulting in an invalid format, but it's only
            #        for our hash so it doesn't matter.
            remote = remote.rsplit(':', 1)[0]
        # render CAPTCHA, if configured
        if isinstance(mm_cfg.CAPTCHAS, dict) and 'en' in mm_cfg.CAPTCHAS:
            (captcha_question, captcha_box, captcha_idx) = \
                Utils.captcha_display(mlist, lang, mm_cfg.CAPTCHAS)
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
                % (now, captcha_idx,
                          Utils.sha_new(mm_cfg.SUBSCRIBE_FORM_SECRET + ":" +
                          now + ":" +
                          captcha_idx + ":" +
                          mlist.internal_name() + ":" +
                          remote
                          ).hexdigest()
                    )
                )
    # Roster form substitutions
    replacements['<mm-roster-form-start>'] = mlist.FormatFormStart('roster')
    replacements['<mm-roster-option>'] = mlist.FormatRosterOptionForUser(lang)
    # Options form substitutions
    replacements['<mm-options-form-start>'] = mlist.FormatFormStart('options')
    replacements['<mm-editing-options>'] = mlist.FormatEditingOption(lang)
    replacements['<mm-info-button>'] = SubmitButton('UserOptions',
                                                    _('Edit Options')).Format()
    # If only one language is enabled for this mailing list, omit the choice
    # buttons.
    if len(mlist.GetAvailableLanguages()) == 1:
        displang = ''
    else:
        displang = mlist.FormatButton('displang-button',
                                      text = _("View this page in"))
    replacements['<mm-displang-box>'] = displang
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
            % (noscript, lang, mm_cfg.RECAPTCHA_SITE_KEY))
    else:
        replacements['<mm-recaptcha-ui>'] = ''

    # Do the expansion.
    doc.AddItem(mlist.ParseTags('listinfo.html', replacements, lang))
    print doc.Format()



if __name__ == "__main__":
    main()
