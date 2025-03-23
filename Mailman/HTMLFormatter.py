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


"""Routines for presentation of list-specific HTML text."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import time
import re
import os
import sys
import errno
import cgi
import html
from typing import Dict, List, Optional, Union, Any

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import MemberAdaptor
from Mailman.htmlformat import *

from Mailman.i18n import _

from Mailman.CSRFcheck import csrf_token


EMPTYSTRING = ''
BR = '<br>'
NL = '\n'
COMMASPACE = ', '

SPACE = ' '
EMSPACE = '&emsp;'
USPACE = '\u2003'  # Unicode em space

# Format a URL into an HTML reference
def maketext(url: str, text: Optional[str] = None) -> str:
    """Format a URL into an HTML reference.

    Args:
        url: The URL to format.
        text: Optional text to use as the link text. If not provided,
              the URL itself is used.

    Returns:
        An HTML formatted link.
    """
    if text is None:
        text = url
    return '<a href="{0}">{1}</a>'.format(html.escape(url), html.escape(text))

def link(url: str, text: Optional[str] = None) -> str:
    """Create an HTML link.

    Args:
        url: The URL to link to.
        text: Optional text to use as the link text. If not provided,
              the URL itself is used.

    Returns:
        An HTML formatted link.
    """
    if text is None:
        text = url
    return '<a href="{0}">{1}</a>'.format(html.escape(url), html.escape(text))

def italic(text: str) -> str:
    """Format text in italic.

    Args:
        text: The text to format.

    Returns:
        The text wrapped in HTML italic tags.
    """
    return '<i>{0}</i>'.format(html.escape(text))

def bold(text: str) -> str:
    """Format text in bold.

    Args:
        text: The text to format.

    Returns:
        The text wrapped in HTML bold tags.
    """
    return '<b>{0}</b>'.format(html.escape(text))

def header(level: int, text: str, **kws: Dict[str, str]) -> str:
    """Create an HTML header.

    Args:
        level: The header level (1-6).
        text: The header text.
        **kws: Additional HTML attributes to add to the header tag.

    Returns:
        An HTML header tag with the specified text and attributes.
    """
    if level < 1 or level > 6:
        raise ValueError('Header level must be between 1 and 6')
    attrs = []
    for key, val in kws.items():
        attrs.append('{0}="{1}"'.format(html.escape(key), html.escape(val)))
    if attrs:
        attrs = ' ' + ' '.join(attrs)
    else:
        attrs = ''
    return '<h{0}{1}>{2}</h{0}>'.format(level, attrs, html.escape(text))

def container(tag: str, text: str, **kws: Dict[str, str]) -> str:
    """Create an HTML container tag.

    Args:
        tag: The HTML tag name.
        text: The text content.
        **kws: Additional HTML attributes to add to the tag.

    Returns:
        An HTML tag with the specified text and attributes.
    """
    attrs = []
    for key, val in kws.items():
        attrs.append('{0}="{1}"'.format(html.escape(key), html.escape(val)))
    if attrs:
        attrs = ' ' + ' '.join(attrs)
    else:
        attrs = ''
    return '<{0}{1}>{2}</{0}>'.format(tag, attrs, html.escape(text))

def div(text: str, **kws: Dict[str, str]) -> str:
    """Create an HTML div container.

    Args:
        text: The text content.
        **kws: Additional HTML attributes to add to the div tag.

    Returns:
        An HTML div tag with the specified text and attributes.
    """
    return container('div', text, **kws)

def span(text: str, **kws: Dict[str, str]) -> str:
    """Create an HTML span container.

    Args:
        text: The text content.
        **kws: Additional HTML attributes to add to the span tag.

    Returns:
        An HTML span tag with the specified text and attributes.
    """
    return container('span', text, **kws)

def pre(text: str, **kws: Dict[str, str]) -> str:
    """Create an HTML pre container.

    Args:
        text: The text content.
        **kws: Additional HTML attributes to add to the pre tag.

    Returns:
        An HTML pre tag with the specified text and attributes.
    """
    return container('pre', text, **kws)

def table(rows: List[List[str]], **kws: Dict[str, str]) -> str:
    """Create an HTML table.

    Args:
        rows: List of rows, where each row is a list of cell contents.
        **kws: Additional HTML attributes to add to the table tag.

    Returns:
        An HTML table with the specified rows and attributes.
    """
    attrs = []
    for key, val in kws.items():
        attrs.append('{0}="{1}"'.format(html.escape(key), html.escape(val)))
    if attrs:
        attrs = ' ' + ' '.join(attrs)
    else:
        attrs = ''
    
    result = ['<table{0}>'.format(attrs)]
    for row in rows:
        result.append('<tr>')
        for cell in row:
            result.append('<td>{0}</td>'.format(html.escape(str(cell))))
        result.append('</tr>')
    result.append('</table>')
    return '\n'.join(result)

def unorderedlist(items: List[str], **kws: Dict[str, str]) -> str:
    """Create an HTML unordered list.

    Args:
        items: List of items to include in the list.
        **kws: Additional HTML attributes to add to the ul tag.

    Returns:
        An HTML unordered list with the specified items and attributes.
    """
    attrs = []
    for key, val in kws.items():
        attrs.append('{0}="{1}"'.format(html.escape(key), html.escape(val)))
    if attrs:
        attrs = ' ' + ' '.join(attrs)
    else:
        attrs = ''
    
    result = ['<ul{0}>'.format(attrs)]
    for item in items:
        result.append('<li>{0}</li>'.format(html.escape(str(item))))
    result.append('</ul>')
    return '\n'.join(result)

def orderedlist(items: List[str], **kws: Dict[str, str]) -> str:
    """Create an HTML ordered list.

    Args:
        items: List of items to include in the list.
        **kws: Additional HTML attributes to add to the ol tag.

    Returns:
        An HTML ordered list with the specified items and attributes.
    """
    attrs = []
    for key, val in kws.items():
        attrs.append('{0}="{1}"'.format(html.escape(key), html.escape(val)))
    if attrs:
        attrs = ' ' + ' '.join(attrs)
    else:
        attrs = ''
    
    result = ['<ol{0}>'.format(attrs)]
    for item in items:
        result.append('<li>{0}</li>'.format(html.escape(str(item))))
    result.append('</ol>')
    return '\n'.join(result)

def form(action: str, method: str = 'POST', encoding: str = None, 
         **kws: Dict[str, str]) -> str:
    """Create an HTML form.

    Args:
        action: The form action URL.
        method: The form method (GET or POST).
        encoding: Optional form encoding type.
        **kws: Additional HTML attributes to add to the form tag.

    Returns:
        An HTML form tag with the specified attributes.
    """
    attrs = []
    for key, val in kws.items():
        attrs.append('{0}="{1}"'.format(html.escape(key), html.escape(val)))
    if encoding:
        attrs.append('enctype="{0}"'.format(html.escape(encoding)))
    attrs = ' '.join(['action="{0}"'.format(html.escape(action)),
                     'method="{0}"'.format(html.escape(method))] + attrs)
    return '<form {0}>'.format(attrs)

def input(type: str, name: str, value: str = '', 
         checked: bool = False, **kws: Dict[str, str]) -> str:
    """Create an HTML input element.

    Args:
        type: The input type.
        name: The input name.
        value: Optional input value.
        checked: Whether the input is checked (for checkboxes/radios).
        **kws: Additional HTML attributes to add to the input tag.

    Returns:
        An HTML input tag with the specified attributes.
    """
    attrs = []
    for key, val in kws.items():
        attrs.append('{0}="{1}"'.format(html.escape(key), html.escape(val)))
    attrs = ' '.join(['type="{0}"'.format(html.escape(type)),
                     'name="{0}"'.format(html.escape(name)),
                     'value="{0}"'.format(html.escape(value))] + attrs)
    if checked:
        attrs += ' checked'
    return '<input {0}>'.format(attrs)

def textarea(name: str, value: str = '', rows: int = 5, cols: int = 40,
            **kws: Dict[str, str]) -> str:
    """Create an HTML textarea element.

    Args:
        name: The textarea name.
        value: Optional textarea value.
        rows: Number of rows.
        cols: Number of columns.
        **kws: Additional HTML attributes to add to the textarea tag.

    Returns:
        An HTML textarea tag with the specified attributes.
    """
    attrs = []
    for key, val in kws.items():
        attrs.append('{0}="{1}"'.format(html.escape(key), html.escape(val)))
    attrs = ' '.join(['name="{0}"'.format(html.escape(name)),
                     'rows="{0}"'.format(rows),
                     'cols="{0}"'.format(cols)] + attrs)
    return '<textarea {0}>{1}</textarea>'.format(attrs, html.escape(value))

def select(name: str, items: List[Union[str, tuple]], 
          selected: Optional[str] = None, **kws: Dict[str, str]) -> str:
    """Create an HTML select element.

    Args:
        name: The select name.
        items: List of items (strings) or tuples of (value, label).
        selected: Optional value to mark as selected.
        **kws: Additional HTML attributes to add to the select tag.

    Returns:
        An HTML select tag with the specified options and attributes.
    """
    attrs = []
    for key, val in kws.items():
        attrs.append('{0}="{1}"'.format(html.escape(key), html.escape(val)))
    attrs = ' '.join(['name="{0}"'.format(html.escape(name))] + attrs)
    
    result = ['<select {0}>'.format(attrs)]
    for item in items:
        if isinstance(item, tuple):
            value, label = item
        else:
            value = label = item
        if value == selected:
            sel = ' selected'
        else:
            sel = ''
        result.append('<option value="{0}"{1}>{2}</option>'.format(
            html.escape(value), sel, html.escape(label)))
    result.append('</select>')
    return '\n'.join(result)

def radio(name: str, value: str, checked: bool = False, 
         **kws: Dict[str, str]) -> str:
    """Create an HTML radio input element.

    Args:
        name: The radio button name.
        value: The radio button value.
        checked: Whether the radio button is checked.
        **kws: Additional HTML attributes to add to the input tag.

    Returns:
        An HTML radio input tag with the specified attributes.
    """
    return input('radio', name, value, checked, **kws)

def checkbox(name: str, value: str, checked: bool = False,
            **kws: Dict[str, str]) -> str:
    """Create an HTML checkbox input element.

    Args:
        name: The checkbox name.
        value: The checkbox value.
        checked: Whether the checkbox is checked.
        **kws: Additional HTML attributes to add to the input tag.

    Returns:
        An HTML checkbox input tag with the specified attributes.
    """
    return input('checkbox', name, value, checked, **kws)

def submit(value: str, **kws: Dict[str, str]) -> str:
    """Create an HTML submit button.

    Args:
        value: The button label.
        **kws: Additional HTML attributes to add to the input tag.

    Returns:
        An HTML submit button with the specified attributes.
    """
    return input('submit', 'submit', value, **kws)

def reset(value: str = 'Reset', **kws: Dict[str, str]) -> str:
    """Create an HTML reset button.

    Args:
        value: The button label.
        **kws: Additional HTML attributes to add to the input tag.

    Returns:
        An HTML reset button with the specified attributes.
    """
    return input('reset', 'reset', value, **kws)

def button(value: str, **kws: Dict[str, str]) -> str:
    """Create an HTML button element.

    Args:
        value: The button label.
        **kws: Additional HTML attributes to add to the button tag.

    Returns:
        An HTML button tag with the specified attributes.
    """
    attrs = []
    for key, val in kws.items():
        attrs.append('{0}="{1}"'.format(html.escape(key), html.escape(val)))
    if attrs:
        attrs = ' ' + ' '.join(attrs)
    else:
        attrs = ''
    return '<button{0}>{1}</button>'.format(attrs, html.escape(value))

def image(src: str, alt: str = '', **kws: Dict[str, str]) -> str:
    """Create an HTML image element.

    Args:
        src: The image source URL.
        alt: Optional alt text.
        **kws: Additional HTML attributes to add to the img tag.

    Returns:
        An HTML img tag with the specified attributes.
    """
    attrs = []
    for key, val in kws.items():
        attrs.append('{0}="{1}"'.format(html.escape(key), html.escape(val)))
    attrs = ' '.join(['src="{0}"'.format(html.escape(src)),
                     'alt="{0}"'.format(html.escape(alt))] + attrs)
    return '<img {0}>'.format(attrs)

class HTMLFormatter(object):
    def GetMailmanFooter(self):
        ownertext = Utils.ObscureEmail(self.GetOwnerEmail(), 1)
        realname = self.real_name
        hostname = self.host_name
        listinfo_link = Link(self.GetScriptURL('listinfo'), realname).Format()
        owner_link = Link('mailto:' + self.GetOwnerEmail(), ownertext).Format()
        innertext = '{0} list run by {1}'.format(listinfo_link, owner_link)
        return Container(
            '<hr>',
            Address(
                Container(
                   innertext,
                    '<br>',
                    Link(self.GetScriptURL('admin'),
                         '{0} administrative interface'.format(realname)),
                    ' (requires authorization)',
                    '<br>',
                    Link(Utils.ScriptURL('listinfo'),
                         'Overview of all {0} mailing lists'.format(hostname)),
                    '<p>', MailmanLogo()))).Format()

    def FormatUsers(self, digest, lang=None, list_hidden=False):
        if lang is None:
            lang = self.preferred_language
        conceal_sub = mm_cfg.ConcealSubscription
        people = []
        if digest:
            members = self.getDigestMemberKeys()
        else:
            members = self.getRegularMemberKeys()
        for m in members:
            if list_hidden or not self.getMemberOption(m, conceal_sub):
                people.append(m)
        num_concealed = len(members) - len(people)
        if num_concealed == 1:
            concealed = '<em>(1 private member not shown)</em>'
        elif num_concealed > 1:
           concealed = '<em>({0} private members not shown)</em>'.format(num_concealed)
        else:
            concealed = ''
        items = []
        people.sort()
        obscure = self.obscure_addresses
        for person in people:
            id = Utils.ObscureEmail(person)
            url = self.GetOptionsURL(person, obscure=obscure)
            person = self.getMemberCPAddress(person)
            if obscure:
                showing = Utils.ObscureEmail(person, for_text=1)
            else:
                showing = person
            realname = Utils.uncanonstr(self.getMemberName(person), lang)
            if realname and mm_cfg.ROSTER_DISPLAY_REALNAME:
                showing += " ({0})".format(Utils.websafe(realname))
            got = Link(url, showing)
            if self.getDeliveryStatus(person) != MemberAdaptor.ENABLED:
                got = Italic('(', got, ')')
            items.append(got)
        return concealed + UnorderedList(*tuple(items)).Format()

    def FormatOptionButton(self, option, value, user):
        if option == mm_cfg.DisableDelivery:
            optval = self.getDeliveryStatus(user) != MemberAdaptor.ENABLED
        else:
            optval = self.getMemberOption(user, option)
        if optval == value:
            checked = ' CHECKED'
        else:
            checked = ''
        name = {mm_cfg.DontReceiveOwnPosts      : 'dontreceive',
                mm_cfg.DisableDelivery          : 'disablemail',
                mm_cfg.DisableMime              : 'mime',
                mm_cfg.AcknowledgePosts         : 'ackposts',
                mm_cfg.Digests                  : 'digest',
                mm_cfg.ConcealSubscription      : 'conceal',
                mm_cfg.SuppressPasswordReminder : 'remind',
                mm_cfg.ReceiveNonmatchingTopics : 'rcvtopic',
                mm_cfg.DontReceiveDuplicates    : 'nodupes',
                }[option]
        return '<input type="radio" name="{0}" value="{1}"{2}>'.format(
            name, value, checked)

    def FormatDigestButton(self):
        if self.digest_is_default:
            checked = ' CHECKED'
        else:
            checked = ''
        return '<input type="radio" name="digest" value="1"{0}>'.format(checked)

    def FormatDisabledNotice(self, user):
        status = self.getDeliveryStatus(user)
        reason = None
        info = self.getBounceInfo(user)
        if status == MemberAdaptor.BYUSER:
            reason = _('; it was disabled by yo)')
        elif status == MemberAdaptor.BYADMIN:
            reason = _('; it was disabled by the list administrator')
        elif status == MemberAdaptor.BYBOUNCE:
            date = time.strftime('}{d-}{b-}{Y',
                                 time.localtime(Utils.midnight(info.date)))
            reason = _('''; it was disabled due to excessive bounces.  The
            last bounce was received on }{(date)s''')
        elif status == MemberAdaptor.UNKNOWN:
            reason = _('; it was disabled for unknown reasons')
        if reason:
            note = FontSize('+1', _(
                'Note: your list delivery is currently disabled}{(reason)s.'
                )).Format()
            link = Link('#disable', _('Mail delivery')).Format()
            mailto = Link('mailto:' + self.GetOwnerEmail(),
                          _('the list administrator')).Format()
            return _('''<p>}{(note)s

            <p>You may have disabled list delivery intentionally,
            or it may have been triggered by bounces from your email
            address.  In either case, to re-enable delivery, change the
            }{(link)s option below.  Contact }{(mailto)s if you have any
            questions or need assistance.''')
        elif info and info.score > 0:
            # Provide information about their current bounce score.  We know
            # their membership is currently enabled.
            score = info.score
            total = self.bounce_score_threshold
            return _('''<p>We have received some recent bounces from your
            address.  Your current <em>bounce score</em> is }{(score)s out of a
            maximum of }{(total)s.  Please double check that your subscribed
            address is correct and that there are no problems with delivery to
            this address.  Your bounce score will be automatically reset if
            the problems are corrected soon.''')
        else:
            return ''

    def FormatUmbrellaNotice(self, user, type):
        addr = self.GetMemberAdminEmail(user)
        if self.umbrella_list:
            return _("(Note - you are subscribing to a list of mailing lists, "
                     "so the }{(type)s notice will be sent to the admin address"
                     " for your membership, }{(addr)s.)<p>")
        else:
            return ""

    def FormatSubscriptionMsg(self):
        msg = ''
        also = ''
        if self.subscribe_policy == 1:
            msg += _('''You will be sent email requesting confirmation, to
            prevent others from gratuitously subscribing you.''')
        elif self.subscribe_policy == 2:
            msg += _("""This is a closed list, which means your subscription
            will be held for approval.  You will be notified of the list
            moderator's decision by email.""")
            also = _('also ')
        elif self.subscribe_policy == 3:
            msg += _("""You will be sent email requesting confirmation, to
            prevent others from gratuitously subscribing you.  Once
            confirmation is received, your request will be held for approval
            by the list moderator.  You will be notified of the moderator's
            decision by email.""")
            also = _("also ")
        if msg:
            msg += ' '
        if self.private_roster == 1:
            msg += _('''This is }{(also)sa private list, which means that the
            list of members is not available to non-members.''')
        elif self.private_roster:
            msg += _('''This is }{(also)sa hidden list, which means that the
            list of members is available only to the list administrator.''')
        else:
            msg += _('''This is }{(also)sa public list, which means that the
            list of members list is available to everyone.''')
            if self.obscure_addresses:
                msg += _(''' (but we obscure the addresses so they are not
                easily recognizable by spammers).''')

        if self.umbrella_list:
            sfx = self.umbrella_member_suffix
            msg += _("""<p>(Note that this is an umbrella list, intended to
            have only other mailing lists as members.  Among other things,
            this means that your confirmation request will be sent to the
            `}{(sfx)s' account for your address.)""")
        return msg

    def FormatUndigestButton(self):
        if self.digest_is_default:
            checked = ''
        else:
            checked = ' CHECKED'
        return '<input type="radio" name="digest" value="0"{0}>'.format(checked)

    def FormatMimeDigestsButton(self):
        if self.mime_is_default_digest:
            checked = ' CHECKED'
        else:
            checked = ''
        return '<input type="radio" name="mime" value="1"{0}>'.format(checked)

    def FormatPlainDigestsButton(self):
        if self.mime_is_default_digest:
            checked = ''
        else:
            checked = ' CHECKED'
        return '<input type="radio" name="plain" value="1"{0}>'.format(checked)

    def FormatEditingOption(self, lang):
        if self.private_roster == 0:
            either = _('<b><i>either</i></b> ')
        else:
            either = ''
        realname = self.real_name

        text = (_('''To unsubscribe from }{(realname)s, get a password reminder,
        or change your subscription options }{(either)senter your subscription
        email address:
        <p><center> ''')
                + TextBox('email', size=30).Format()
                + '  '
                + SubmitButton('UserOptions',
                               _('Unsubscribe or edit options')).Format()
                + Hidden('language', lang).Format()
                + '</center>')
        if self.private_roster == 0:
            text += _('''<p>... <b><i>or</i></b> select your entry from
                      the subscribers list (see above).''')
        text += _(''' If you leave the field blank, you will be prompted for
        your email address''')
        return text

    def RestrictedListMessage(self, which, restriction):
        if not restriction:
            return ''
        elif restriction == 1:
            return _(
                '''(<i>}{(which)s is only available to the list
                members.</i>)''')
        else:
            return _('''(<i>}{(which)s is only available to the list
            administrator.</i>)''')

    def FormatRosterOptionForUser(self, lang):
        return self.RosterOption(lang).Format()

    def RosterOption(self, lang):
        container = Container()
        container.AddItem(Hidden('language', lang))
        if not self.private_roster:
            container.AddItem(_("Click here for the list of ")
                              + self.real_name
                              + _(" subscribers: "))
            container.AddItem(SubmitButton('SubscriberRoster',
                                           _("Visit Subscriber list")))
        else:
            if self.private_roster == 1:
                only = _('members')
                whom = _('Address:')
            else:
                only = _('the list administrator')
                whom = _('Admin address:')
            # Solicit the user and password.
            container.AddItem(
                self.RestrictedListMessage(_('The subscribers list'),
                                           self.private_roster)
                              + _(" <p>Enter your ")
                              + whom[:-1].lower()
                              + _(" and password to visit"
                              "  the subscribers list: <p><center> ")
                              + whom
                              + " ")
            container.AddItem(self.FormatBox('roster-email'))
            container.AddItem(_("Password: ")
                              + self.FormatSecureBox('roster-pw')
                              + "&nbsp;&nbsp;")
            container.AddItem(SubmitButton('SubscriberRoster',
                                           _('Visit Subscriber List')))
            container.AddItem("</center>")
        return container

    def FormatFormStart(self, name, extra='', mlist=None, contexts=None, user=None):
        base_url = self.GetScriptURL(name)
        if extra:
            full_url = "{0}/{1}".format(base_url, extra)
        else:
            full_url = base_url
        if mlist:
            return '<form method="POST" action="{0}">\n<input type="hidden" name="csrf_token" value="{1}">'.format(
                full_url, csrf_token(mlist, contexts, user))
        return '<form method="POST" action="{0}">'.format(full_url)

    def FormatArchiveAnchor(self):
        return '<a href="{0}">{1}</a>'.format(self.GetBaseArchiveURL(), self.real_name)

    def FormatFormEnd(self):
        return '</form>'

    def FormatBox(self, name, size=20, value=''):
        if value:
            safevalue = Utils.websafe(value)
        else:
            safevalue = value
        return '<input type="text" name="{0}" size="{1}" value="{2}">'.format(
            name, size, safevalue)

    def FormatSecureBox(self, name):
        return '<input type="password" name="{0}" size="15">'.format(name)

    def FormatButton(self, name, text='Submit'):
        return '<input type="submit" name="{0}" value="{1}">'.format(name, text)

    def FormatReminder(self, lang):
        if self.send_reminders:
            return _('Once a month, your password will be emailed to you as'
                     ' a reminder.')
        return ''

    def ParseTags(self, template, replacements, lang=None):
        if lang is None:
            charset = 'us-ascii'
        else:
            charset = Utils.GetCharSet(lang)
        text = Utils.maketext(template, raw=1, lang=lang, mlist=self)
        parts = re.split('(</?[Mm][Mm]-[^>]*>)', text)
        i = 1
        while i < len(parts):
            tag = parts[i].lower()
            if replacements in tag:
                repl = replacements[tag]
                if isinstance(repl, type()):
                    repl = repl.encode(charset, 'replace')
                parts[i] = repl
            else:
                parts[i] = ''
            i = i + 2
        return EMPTYSTRING.join(parts)

    # This needs to wait until after the list is inited, so let's build it
    # when it's needed only.
    def GetStandardReplacements(self, lang=None):
        dmember_len = len(self.getDigestMemberKeys())
        member_len = len(self.getRegularMemberKeys())
        # If only one language is enabled for this mailing list, omit the
        # language choice buttons.
        if len(self.GetAvailableLanguages()) == 1:
            listlangs = _(Utils.GetLanguageDescr(self.preferred_language))
        else:
            listlangs = self.GetLangSelectBox(lang).Format()
        if lang:
            cset = Utils.GetCharSet(lang) or 'us-ascii'
        else:
            cset = Utils.GetCharSet(self.preferred_language) or 'us-ascii'
        d = {
            '<mm-mailman-footer>' : self.GetMailmanFooter(),
            '<mm-list-name>' : self.real_name,
            '<mm-email-user>' : self._internal_name,
            '<mm-list-description>' :
                Utils.websafe(self.GetDescription(cset)),
            '<mm-list-info>' : 
                '<!---->' + BR.join(self.info.split(NL)) + '<!---->',
            '<mm-form-end>'  : self.FormatFormEnd(),
            '<mm-archive>'   : self.FormatArchiveAnchor(),
            '</mm-archive>'  : '</a>',
            '<mm-list-subscription-msg>' : self.FormatSubscriptionMsg(),
            '<mm-restricted-list-message>' : \
                self.RestrictedListMessage(_('The current archive'),
                                           self.archive_private),
            '<mm-num-reg-users>' : `member_len`,
            '<mm-num-digesters>' : `dmember_len`,
            '<mm-num-members>' : (`member_len + dmember_len`),
            '<mm-posting-addr>' : '}{s' > {0}'.format(self.GetListEmail()),
            '<mm-request-addr>' : '}{s' > {0}'.format(self.GetRequestEmail()),
            '<mm-owner>' : self.GetOwnerEmail(),
            '<mm-reminder>' : self.FormatReminder(self.preferred_language),
            '<mm-host>' : self.host_name,
            '<mm-list-langs>' : listlangs,
            }
        if mm_cfg.IMAGE_LOGOS:
            d['<mm-favicon>'] = mm_cfg.IMAGE_LOGOS + mm_cfg.SHORTCUT_ICON
        return d

    def GetAllReplacements(self, lang=None, list_hidden=False):
        """
        returns standard replaces plus formatted user lists in
        a dict just like GetStandardReplacements.
        """
        if lang is None:
            lang = self.preferred_language
        d = self.GetStandardReplacements(lang)
        d.update({"<mm-regular-users>": self.FormatUsers(0, lang, list_hidden),
                  "<mm-digest-users>": self.FormatUsers(1, lang, list_hidden)})
        return d

    def GetLangSelectBox(self, lang=None, varname='language'):
        if lang is None:
            lang = self.preferred_language
        # Figure out the available languages
        values = self.GetAvailableLanguages()
        legend = map(_, map(Utils.GetLanguageDescr, values))
        try:
            selected = values.index(lang)
        except ValueError:
            try:
                selected = values.index(self.preferred_language)
            except ValueError:
                selected = mm_cfg.DEFAULT_SERVER_LANGUAGE
        # Return the widget
        return SelectOptions(varname, values, legend, selected)
}