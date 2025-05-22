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

def cmp(a, b):
    return (a > b) - (a < b)

import sys
import os
import re
import urllib.parse
import signal
import traceback

from email.utils import unquote, parseaddr, formataddr

from Mailman import mm_cfg
from Mailman import Utils
from Mailman.Message import Message
from Mailman import MailList
from Mailman import Errors
from Mailman import MemberAdaptor
from Mailman import i18n
from Mailman.UserDesc import UserDesc
from Mailman.htmlformat import *
from Mailman.Cgi import Auth
from Mailman.Logging.Syslog import mailman_log
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

def validate_listname(listname):
    """Validate and sanitize a listname to prevent path traversal.
    
    Args:
        listname: The listname to validate
        
    Returns:
        tuple: (is_valid, sanitized_name, error_message)
    """
    if not listname:
        return False, None, _('List name is required')
        
    # Convert to lowercase and strip whitespace
    listname = listname.lower().strip()
    
    # Basic validation
    if not Utils.ValidateListName(listname):
        return False, None, _('Invalid list name')
        
    # Check for path traversal attempts
    if '..' in listname or '/' in listname or '\\' in listname:
        return False, None, _('Invalid list name')
        
    return True, listname, None

def handle_no_list():
    """Handle the case when no list is specified."""
    doc = Document()
    doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)
    doc.SetTitle(_('CGI script error'))
    doc.AddItem(Header(2, _('CGI script error')))
    doc.addError(_('Invalid options to CGI script.'))
    doc.AddItem('<hr>')
    doc.AddItem(MailmanLogo())
    print('Status: 400 Bad Request')
    return doc

def main():
    try:
        # Log page load
        mailman_log('info', 'admin: Page load started')
        mailman_log('debug', 'Entered main()')
        
        # Initialize document early
        doc = Document()
        doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)
        
        # Parse form data first since we need it for authentication
        try:
            mailman_log('debug', 'Parsing form data')
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
            mailman_log('debug', 'cgidata after parse: %s', str(cgidata))
        except Exception as e:
            mailman_log('error', 'admin: Invalid form data: %s\n%s', str(e), traceback.format_exc())
            doc.AddItem(Header(2, _("Error")))
            doc.AddItem(Bold(_('Invalid request')))
            print('Status: 400 Bad Request')
            print(doc.Format())
            return

        # Get the list name
        parts = Utils.GetPathPieces()
        mailman_log('debug', 'Path parts: %s', str(parts))
        if not parts:
            doc = handle_no_list()
            print(doc.Format())
            return

        # Validate listname
        is_valid, listname, error_msg = validate_listname(parts[0])
        if not is_valid:
            doc.SetTitle(_('CGI script error'))
            doc.AddItem(Header(2, _('CGI script error')))
            doc.addError(error_msg)
            doc.AddItem('<hr>')
            doc.AddItem(MailmanLogo())
            print('Status: 400 Bad Request')
            print(doc.Format())
            return

        mailman_log('info', 'admin: Processing list "%s"', listname)
        mailman_log('debug', 'List name: %s', listname)

        try:
            mlist = MailList.MailList(listname, lock=0)
        except Errors.MMListError as e:
            # Avoid cross-site scripting attacks and information disclosure
            safelistname = Utils.websafe(listname)
            doc.SetTitle(_('CGI script error'))
            doc.AddItem(Header(2, _('CGI script error')))
            doc.addError(_('No such list <em>{safelistname}</em>'))
            doc.AddItem('<hr>')
            doc.AddItem(MailmanLogo())
            print('Status: 404 Not Found')
            print(doc.Format())
            mailman_log('error', 'admin: No such list "%s"', listname)
            return
        except Exception as e:
            # Log the full error but don't expose it to the user
            mailman_log('error', 'admin: Unexpected error for list "%s": %s', listname, str(e))
            doc.SetTitle(_('CGI script error'))
            doc.AddItem(Header(2, _('CGI script error')))
            doc.addError(_('An error occurred processing your request'))
            doc.AddItem('<hr>')
            doc.AddItem(MailmanLogo())
            print('Status: 500 Internal Server Error')
            print(doc.Format())
            return

        i18n.set_language(mlist.preferred_language)
        # If the user is not authenticated, we're done.
        try:
            mailman_log('debug', 'Checking authentication')
            # CSRF check
            safe_params = ['VARHELP', 'adminpw', 'admlogin',
                          'letter', 'chunk', 'findmember',
                          'legend']
            params = list(cgidata.keys())
            if set(params) - set(safe_params):
                csrf_checked = csrf_check(mlist, cgidata.get('csrf_token', [''])[0],
                                        'admin')
            else:
                csrf_checked = True
            if cgidata.get('adminpw', [''])[0]:
                os.environ['HTTP_COOKIE'] = ''
                csrf_checked = True
            mailman_log('debug', 'Authentication contexts: %s', str((mm_cfg.AuthListAdmin, mm_cfg.AuthSiteAdmin)))
            mailman_log('debug', 'Password provided: %s', 'Yes' if cgidata.get('adminpw', [''])[0] else 'No')
            mailman_log('debug', 'Cookie present: %s', 'Yes' if os.environ.get('HTTP_COOKIE') else 'No')
            auth_result = mlist.WebAuthenticate((mm_cfg.AuthListAdmin,
                                      mm_cfg.AuthSiteAdmin),
                                     cgidata.get('adminpw', [''])[0])
            mailman_log('debug', 'WebAuthenticate result: %s', str(auth_result))
            if not auth_result:
                mailman_log('debug', 'Authentication failed - checking auth contexts')
                for context in (mm_cfg.AuthListAdmin, mm_cfg.AuthSiteAdmin):
                    mailman_log('debug', 'Checking context %s: %s', 
                               context, str(mlist.AuthContextInfo(context)))
        except Exception as e:
            mailman_log('error', 'admin: Exception during WebAuthenticate: %s\n%s', str(e), traceback.format_exc())
            mailman_log('debug', 'Exception during WebAuthenticate')
            raise
        if not auth_result:
            mailman_log('debug', 'Not authenticated, calling loginpage')
            if 'adminpw' in cgidata:
                msg = Bold(FontSize('+1', _('Authorization failed.'))).Format()
                remote = os.environ.get('HTTP_FORWARDED_FOR',
                         os.environ.get('HTTP_X_FORWARDED_FOR',
                         os.environ.get('REMOTE_ADDR',
                                        'unidentified origin')))
                mailman_log('security',
                          'Authorization failed (admin): list=%s: remote=%s\n%s',
                          listname, remote, traceback.format_exc())
            else:
                msg = ''
            Auth.loginpage(mlist, 'admin', msg=msg)
            mailman_log('debug', 'Called Auth.loginpage')
            return
        mailman_log('debug', 'Authenticated, proceeding to admin page')
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

        # Sanity check - validate category against available categories
        if category not in list(mlist.GetConfigCategories().keys()):
            category = 'general'

        # Is the request for variable details?
        varhelp = None
        qsenviron = os.environ.get('QUERY_STRING')
        parsedqs = None
        if qsenviron:
            parsedqs = urllib.parse.parse_qs(qsenviron)
        if 'VARHELP' in cgidata:
            varhelp = cgidata['VARHELP'][0]
        elif parsedqs:
            # POST methods, even if their actions have a query string, don't get
            # put into FieldStorage's keys :-(
            qs = parsedqs.get('VARHELP')
            if qs and isinstance(qs, list):
                varhelp = qs[0]
        if varhelp:
            option_help(mlist, varhelp)
            return

        doc = Document()
        doc.set_language(mlist.preferred_language)
        form = Form(mlist=mlist, contexts=AUTH_CONTEXTS)
        mailman_log('debug', 'category=%s, subcat=%s', category, subcat)

        # From this point on, the MailList object must be locked
        mlist.Lock()
        try:
            # Install the emergency shutdown signal handler
            def sigterm_handler(signum, frame, mlist=mlist):
                # Make sure the list gets unlocked...
                mlist.Unlock()
                # ...and ensure we exit
                sys.exit(0)
            signal.signal(signal.SIGTERM, sigterm_handler)

            if cgidata:
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
                    _(f'''You have turned off delivery of both digest and
                    non-digest messages.  This is an incompatible state of
                    affairs.  You must turn on either digest delivery or
                    non-digest delivery or your mailing list will basically be
                    unusable.'''), tag=_('Warning: '))

            dm = mlist.getDigestMemberKeys()
            if not mlist.digestable and dm:
                doc.addError(
                    _(f'''You have digest members, but digests are turned
                    off. Those people will not receive mail.
                    Affected member(s) %(dm)r.'''),
                    tag=_('Warning: '))
            rm = mlist.getRegularMemberKeys()
            if not mlist.nondigestable and rm:
                doc.addError(
                    _(f'''You have regular list members but non-digestified mail is
                    turned off.  They will receive non-digestified mail until you
                    fix this problem. Affected member(s) %(rm)r.'''),
                    tag=_('Warning: '))

            # Show the results page
            show_results(mlist, doc, category, subcat, cgidata)
            mailman_log('debug', 'About to print doc.Format()')
            print(doc.Format())
            mlist.Save()
        finally:
            # Now be sure to unlock the list
            mlist.Unlock()
    except Exception as e:
        doc = Document()
        doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('An unexpected error occurred.')))
        doc.AddItem(Preformatted(Utils.websafe(str(e))))
        doc.AddItem(Preformatted(Utils.websafe(traceback.format_exc())))
        print('Status: 500 Internal Server Error')
        print(doc.Format())
        mailman_log('error', 'admin: Unexpected error: %s\n%s', str(e), traceback.format_exc())

def admin_overview(msg=''):
    # Show the administrative overview page, with the list of all the lists on
    # this host.  msg is an optional error message to display at the top of
    # the page.
    #
    # This page should be displayed in the server's default language, which
    # should have already been set.
    hostname = Utils.get_domain()
    if isinstance(hostname, bytes):
        hostname = hostname.decode('latin1', 'replace')
    legend = _('%(hostname)s mailing lists - Admin Links') % {
        'hostname': hostname
    }
    # The html `document'
    doc = Document()
    doc.set_language(mm_cfg.DEFAULT_SERVER_LANGUAGE)
    doc.SetTitle(legend)
    # The table that will hold everything
    table = Table(border=0, width="100%")
    table.AddRow([Center(Header(2, legend))])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2,
                      bgcolor=mm_cfg.WEB_HEADER_COLOR)
    # Skip any mailing list that isn't advertised.
    advertised = []
    listnames = Utils.list_names()
    listnames.sort()

    for name in listnames:
        if isinstance(name, bytes):
            name = name.decode('latin1', 'replace')
        try:
            mlist = MailList.MailList(name, lock=0)
        except Errors.MMUnknownListError:
            # The list could have been deleted by another process.
            continue
        if mlist.advertised:
            real_name = mlist.real_name
            if isinstance(real_name, bytes):
                real_name = real_name.decode('latin1', 'replace')
            description = mlist.GetDescription()
            if isinstance(description, bytes):
                description = description.decode('latin1', 'replace')
            if mm_cfg.VIRTUAL_HOST_OVERVIEW and (
                   mlist.web_page_url.find('/%(hostname)s/' % {'hostname': hostname}) == -1 and
                   mlist.web_page_url.find('/%(hostname)s:' % {'hostname': hostname}) == -1):
                # List is for different identity of this host - skip it.
                continue
            else:
                advertised.append((mlist.GetScriptURL('admin'),
                                   real_name,
                                   Utils.websafe(description)))
        mlist.Unlock()

    # Greeting depends on whether there was an error or not
    if msg:
        greeting = FontAttr(msg, color="ff5060", size="+1")
    else:
        greeting = FontAttr(_('Welcome!'), size='+2')

    welcome = []
    mailmanlink = Link(mm_cfg.MAILMAN_URL, _('Mailman')).Format()
    if not advertised:
        welcome.extend([
            greeting,
            _('<p>There currently are no publicly-advertised %(mailmanlink)s mailing lists on %(hostname)s.') % {
                'mailmanlink': mailmanlink,
                'hostname': hostname
            },
            ])
    else:
        welcome.extend([
            greeting,
            _('<p>Below is the collection of publicly-advertised %(mailmanlink)s mailing lists on %(hostname)s.  Click on a list name to visit the configuration pages for that list.') % {
                'mailmanlink': mailmanlink,
                'hostname': hostname
            },
            ])

    creatorurl = Utils.ScriptURL('create')
    mailman_owner = Utils.get_site_email()
    extra = msg and _('right ') or ''
    welcome.extend([
        _('To visit the administrators configuration page for an unadvertised list, open a URL similar to this one, but with a \'/\' and the %(extra)slist name appended.  If you have the proper authority, you can also <a href="%(creatorurl)s">create a new mailing list</a>.') % {
            'extra': extra,
            'creatorurl': creatorurl
        },
        _('<p>General list information can be found at '),
        Link(Utils.ScriptURL('listinfo'),
             _('the mailing list overview page')),
        '.',
        _('<p>(Send questions and comments to '),
        Link('mailto:%(mailman_owner)s' % {'mailman_owner': mailman_owner}, mailman_owner),
        '.)<p>',
        ])

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

def option_help(mlist, varhelp):
    # The html page document
    doc = Document()
    doc.set_language(mlist.preferred_language)
    # Find out which category and variable help is being requested for.
    item = None
    reflist = varhelp.split('/')
    if len(reflist) >= 2:
        category, subcat = None, None
        if len(reflist) == 2:
            category, varname = reflist
        elif len(reflist) == 3:
            category, subcat, varname = reflist
        options = mlist.GetConfigInfo(category, subcat)
        if options:
            for i in options:
                if i and i[0] == varname:
                    item = i
                    break
    # Print an error message if we couldn't find a valid one
    if not item:
        bad = _('No valid variable name found.')
        doc.addError(bad)
        doc.AddItem(mlist.GetMailmanFooter())
        print(doc.Format())
        return
    # Get the details about the variable
    varname, kind, params, dependancies, description, elaboration = \
             get_item_characteristics(item)
    # Set up the document
    realname = mlist.real_name
    legend = _(f"""{realname} Mailing list Configuration Help
    <br><em>{varname}</em> Option""")

    header = Table(width='100%')
    header.AddRow([Center(Header(3, legend))])
    header.AddCellInfo(header.GetCurrentRowIndex(), 0, colspan=2,
                       bgcolor=mm_cfg.WEB_HEADER_COLOR)
    doc.SetTitle(_("Mailman {varname} List Option Help"))
    doc.AddItem(header)
    doc.AddItem("<b>%s</b> (%s): %s<p>" % (varname, category, description))
    if elaboration:
        doc.AddItem("%s<p>" % elaboration)

    if subcat:
        url = '%s/%s/%s' % (mlist.GetScriptURL('admin'), category, subcat)
    else:
        url = '%s/%s' % (mlist.GetScriptURL('admin'), category)
    form = Form(url, mlist=mlist, contexts=AUTH_CONTEXTS)
    valtab = Table(cellspacing=3, cellpadding=4, width='100%')
    add_options_table_item(mlist, category, subcat, valtab, item, detailsp=0)
    form.AddItem(valtab)
    form.AddItem('<p>')
    form.AddItem(Center(submit_button()))
    doc.AddItem(Center(form))

    doc.AddItem(_(f"""<em><strong>Warning:</strong> changing this option here
    could cause other screens to be out-of-sync.  Be sure to reload any other
    pages that are displaying this option for this mailing list.  You can also
    """))

    adminurl = mlist.GetScriptURL('admin')
    if subcat:
        url = '%s/%s/%s' % (adminurl, category, subcat)
    else:
        url = '%s/%s' % (adminurl, category)
    categoryname = mlist.GetConfigCategories()[category][0]
    doc.AddItem(Link(url, _(f'return to the {categoryname} options page.')))
    doc.AddItem('</em>')
    doc.AddItem(mlist.GetMailmanFooter())
    print(doc.Format())

def add_standard_headers(doc, mlist, title, category, subcat):
    """Add standard headers to admin pages.
    
    Args:
        doc: The Document object
        mlist: The MailList object
        title: The page title
        category: Optional category name
        subcat: Optional subcategory name
    """
    # Set the page title
    doc.SetTitle(title)
    
    # Add the main header
    doc.AddItem(Header(2, title))
    
    # Add navigation breadcrumbs if category/subcat provided
    breadcrumbs = []
    breadcrumbs.append(Link(mlist.GetScriptURL('admin'), _('%(realname)s administrative interface')))
    if category:
        breadcrumbs.append(Link(mlist.GetScriptURL('admin') + '/' + category, _(category)))
    if subcat:
        breadcrumbs.append(Link(mlist.GetScriptURL('admin') + '/' + category + '/' + subcat, _(subcat)))
    # Convert each breadcrumb item to a string before joining
    breadcrumbs = [str(item) for item in breadcrumbs]
    doc.AddItem(Center(' > '.join(breadcrumbs)))
    
    # Add horizontal rule
    doc.AddItem('<hr>')

def show_results(mlist, doc, category, subcat, cgidata):
    # Produce the results page
    adminurl = mlist.GetScriptURL('admin')
    categories = mlist.GetConfigCategories()
    label = _(categories[category][0])
    if isinstance(label, bytes):
        label = label.decode(Utils.GetCharSet(mlist.preferred_language), 'replace')
        
    # Add standard headers
    title = _('%(realname)s Administration (%(label)s)') % {
        'realname': mlist.real_name,
        'label': label
    }
    add_standard_headers(doc, mlist, title, category, subcat)
    
    # Use ParseTags for the main content
    replacements = {
        'realname': mlist.real_name,
        'label': label,
        'adminurl': adminurl,
        'admindburl': mlist.GetScriptURL('admindb'),
        'listinfourl': mlist.GetScriptURL('listinfo'),
        'edithtmlurl': mlist.GetScriptURL('edithtml'),
        'archiveurl': mlist.GetBaseArchiveURL(),
        'rmlisturl': mlist.GetScriptURL('rmlist') if mm_cfg.OWNERS_CAN_DELETE_THEIR_OWN_LISTS and mlist.internal_name() != mm_cfg.MAILMAN_SITE_LIST else None
    }
    
    # Ensure all replacements are properly encoded for the list's language
    for key, value in replacements.items():
        if isinstance(value, bytes):
            replacements[key] = value.decode(Utils.GetCharSet(mlist.preferred_language), 'replace')
    
    output = mlist.ParseTags('admin_results.html', replacements, mlist.preferred_language)
    doc.AddItem(output)
    
    # Now we need to craft the form that will be submitted
    encoding = None
    if category in ('autoreply', 'members'):
        encoding = 'multipart/form-data'
    if subcat:
        form = Form('%(adminurl)s/%(category)s/%(subcat)s' % {
            'adminurl': adminurl,
            'category': category,
            'subcat': subcat
        }, encoding=encoding, mlist=mlist, contexts=AUTH_CONTEXTS)
    else:
        form = Form('%(adminurl)s/%(category)s' % {
            'adminurl': adminurl,
            'category': category
        }, encoding=encoding, mlist=mlist, contexts=AUTH_CONTEXTS)
        
    # Add the form content based on category
    if category == 'members':
        form.AddItem(membership_options(mlist, subcat, cgidata, doc, form))
        form.AddItem(Center(submit_button('setmemberopts_btn')))
    elif category == 'passwords':
        form.AddItem(Center(password_inputs(mlist)))
        form.AddItem(Center(submit_button()))
    else:
        form.AddItem(show_variables(mlist, category, subcat, cgidata, doc))
        form.AddItem(Center(submit_button()))
        
    # Add the form to the document
    doc.AddItem(form)
    doc.AddItem(mlist.GetMailmanFooter())

def show_variables(mlist, category, subcat, cgidata, doc):
    mailman_log('debug', 'show_variables called with category=%s, subcat=%s', category, subcat)
    options = mlist.GetConfigInfo(category, subcat)
    mailman_log('debug', 'Got config info: %s', str(options))

    # The table containing the results
    table = Table(cellspacing=3, cellpadding=4, width='100%')

    # Get and portray the text label for the category.
    categories = mlist.GetConfigCategories()
    mailman_log('debug', 'Got config categories: %s', str(categories))
    label = _(categories[category][0])
    if isinstance(label, bytes):
        label = label.decode(Utils.GetCharSet(mlist.preferred_language), 'replace')
    mailman_log('debug', 'Category label: %s', label)

    table.AddRow([Center(Header(2, label))])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2,
                      bgcolor=mm_cfg.WEB_HEADER_COLOR)

    # The very first item in the config info will be treated as a general
    # description if it is a string
    description = options[0]
    if isinstance(description, bytes):
        description = description.decode(Utils.GetCharSet(mlist.preferred_language), 'replace')
    mailman_log('debug', 'Description: %s', description)
    if isinstance(description, str):
        table.AddRow([description])
        table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
        options = options[1:]

    if not options:
        mailman_log('debug', 'No options to display')
        return table

    # Add the global column headers
    table.AddRow([Center(Bold(_('Description'))),
                  Center(Bold(_('Value')))])
    table.AddCellInfo(max(table.GetCurrentRowIndex(), 0), 0,
                      width='15%')
    table.AddCellInfo(max(table.GetCurrentRowIndex(), 0), 1,
                      width='85%')

    for item in options:
        mailman_log('debug', 'Processing item: %s', str(item))
        if isinstance(item, str):
            # The very first banner option (string in an options list) is
            # treated as a general description, while any others are
            # treated as section headers - centered and italicized...
            if isinstance(item, bytes):
                item = item.decode(Utils.GetCharSet(mlist.preferred_language), 'replace')
            formatted_text = '[%s]' % item
            item = Bold(formatted_text).Format()
            table.AddRow([Center(Italic(item))])
            table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
        else:
            add_options_table_item(mlist, category, subcat, table, item)
    table.AddRow(['<br>'])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
    mailman_log('debug', 'Returning table with %d rows', table.GetCurrentRowIndex() + 1)
    return table

def add_options_table_item(mlist, category, subcat, table, item, detailsp=1):
    mailman_log('debug', 'Adding options table item: %s', str(item))
    # Add a row to an options table with the item description and value.
    varname, kind, params, extra, descr, elaboration = \
             get_item_characteristics(item)
    mailman_log('debug', 'Item characteristics: varname=%s, kind=%s', varname, kind)
    if elaboration is None:
        elaboration = descr
    descr = get_item_gui_description(mlist, category, subcat,
                                     varname, descr, elaboration, detailsp)
    val = get_item_gui_value(mlist, category, kind, varname, params, extra)
    table.AddRow([descr, val])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0,
                      style=f'background-color: {mm_cfg.WEB_ADMINITEM_COLOR}',
                      role='cell')
    table.AddCellInfo(table.GetCurrentRowIndex(), 1,
                      style=f'background-color: {mm_cfg.WEB_ADMINITEM_COLOR}',
                      role='cell')

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
    if len(record) == 5:
        elaboration = None
        varname, kind, params, dependancies, descr = record
    elif len(record) == 6:
        varname, kind, params, dependancies, descr, elaboration = record
    else:
        raise ValueError(f'Badly formed options entry:\n {record}')
    return varname, kind, params, dependancies, descr, elaboration

def get_item_gui_value(mlist, category, kind, varname, params, extra):
    """Return a representation of an item's settings."""
    # Give the category a chance to return the value for the variable
    value = None
    category_data = mlist.GetConfigCategories()[category]
    if isinstance(category_data, tuple):
        gui = category_data[1]
    if hasattr(gui, 'getValue'):
        value = gui.getValue(mlist, kind, varname, params)
    # Filter out None, and volatile attributes
    if value is None and not varname.startswith('_'):
        value = getattr(mlist, varname)
    # Now create the widget for this value
    if kind == mm_cfg.Radio or kind == mm_cfg.Toggle:
        # If we are returning the option for subscribe policy and this site
        # doesn't allow open subscribes, then we have to alter the value of
        # mlist.subscribe_policy as passed to RadioButtonArray in order to
        # compensate for the fact that there is one fewer option.
        # Correspondingly, we alter the value back in the change options
        # function -scott
        #
        # TBD: this is an ugly ugly hack.
        if varname.startswith('_'):
            checked = 0
        else:
            checked = value
        if varname == 'subscribe_policy' and not mm_cfg.ALLOW_OPEN_SUBSCRIBE:
            checked = checked - 1
        # For Radio buttons, we're going to interpret the extra stuff as a
        # horizontal/vertical flag.  For backwards compatibility, the value 0
        # means horizontal, so we use "not extra" to get the parity right.
        return RadioButtonArray(varname, params, checked, not extra)
    elif (kind == mm_cfg.String or kind == mm_cfg.Email or
          kind == mm_cfg.Host or kind == mm_cfg.Number):
        # Ensure value is a string, decoding bytes if necessary
        if isinstance(value, bytes):
            value = value.decode(Utils.GetCharSet(mlist.preferred_language), 'replace')
        return TextBox(varname, value, params)
    elif kind == mm_cfg.Text:
        if params:
            r, c = params
        else:
            r, c = None, None
        # Ensure value is a string, decoding bytes if necessary
        if isinstance(value, bytes):
            value = value.decode(Utils.GetCharSet(mlist.preferred_language), 'replace')
        return TextArea(varname, value or '', r, c)
    elif kind in (mm_cfg.EmailList, mm_cfg.EmailListEx):
        if params:
            r, c = params
        else:
            r, c = None, None
        # Ensure value is a string, decoding bytes if necessary
        if isinstance(value, bytes):
            value = value.decode(Utils.GetCharSet(mlist.preferred_language), 'replace')
        res = NL.join(value)
        return TextArea(varname, res, r, c, wrap='off')
    elif kind == mm_cfg.FileUpload:
        # like a text area, but also with uploading
        if params:
            r, c = params
        else:
            r, c = None, None
        container = Container()
        container.AddItem(_('<em>Enter the text below, or...</em><br>'))
        container.AddItem(TextArea(varname, value or '', r, c))
        container.AddItem(_('<br><em>...specify a file to upload</em><br>'))
        container.AddItem(FileUpload(varname+'_upload', r, c))
        return container
    elif kind == mm_cfg.Select:
        if params:
           values, legend, selected = params
        else:
           values = mlist.available_languages
           legend = [Utils.GetLanguageDescr(lang) for lang in values]
           selected = values.index(mlist.preferred_language)
        return SelectOptions(varname, values, legend, selected)
    elif kind == mm_cfg.Topics:
        # A complex and specialized widget type that allows for setting of a
        # topic name, a mark button, a regexp text box, an "add after mark",
        # and a delete button.  Yeesh!  params are ignored.
        table = Table(border=0)
        # This adds the html for the entry widget
        def makebox(i, name, pattern, desc, empty=False, table=table):
            deltag   = 'topic_delete_%02d' % i
            boxtag   = 'topic_box_%02d' % i
            reboxtag = 'topic_rebox_%02d' % i
            desctag  = 'topic_desc_%02d' % i
            wheretag = 'topic_where_%02d' % i
            addtag   = 'topic_add_%02d' % i
            newtag   = 'topic_new_%02d' % i
            if empty:
                topic_text = _('Topic %(i)d') % {'i': i}
                table.AddRow([Center(Bold(topic_text)),
                            Hidden(newtag)])
            else:
                topic_text = _('Topic %(i)d') % {'i': i}
                table.AddRow([Center(Bold(topic_text)),
                            SubmitButton(deltag, _('Delete'))])
            table.AddRow([Label(_('Topic name:')),
                          TextBox(boxtag, value=name, size=30)])
            table.AddRow([Label(_('Regexp:')),
                          TextArea(reboxtag, text=pattern,
                                   rows=4, cols=30, wrap='off')])
            table.AddRow([Label(_('Description:')),
                          TextArea(desctag, text=desc,
                                   rows=4, cols=30, wrap='soft')])
            if not empty:
                table.AddRow([SubmitButton(addtag, _('Add new item...')),
                              SelectOptions(wheretag, ('before', 'after'),
                                            (_('...before this one.'),
                                             _('...after this one.')),
                                            selected=1),
                              ])
            table.AddRow(['<hr>'])
            table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2, role='cell')
        # Now for each element in the existing data, create a widget
        i = 1
        data = getattr(mlist, varname)
        for name, pattern, desc, empty in data:
            if isinstance(name, bytes):
                name = name.decode(Utils.GetCharSet(mlist.preferred_language), 'replace')
            if isinstance(pattern, bytes):
                pattern = pattern.decode(Utils.GetCharSet(mlist.preferred_language), 'replace')
            if isinstance(desc, bytes):
                desc = desc.decode(Utils.GetCharSet(mlist.preferred_language), 'replace')
            makebox(i, name, pattern, desc, empty)
            i += 1
        # Add one more non-deleteable widget as the first blank entry, but
        # only if there are no real entries.
        if i == 1:
            makebox(i, '', '', '', empty=True)
        return table
    elif kind == mm_cfg.HeaderFilter:
        # A complex and specialized widget type that allows for setting of a
        # spam filter rule including, a mark button, a regexp text box, an
        # "add after mark", up and down buttons, and a delete button.  Yeesh!
        # params are ignored.
        table = Table(border=0)
        # This adds the html for the entry widget
        def makebox(i, pattern, action, empty=False, table=table):
            deltag    = 'hdrfilter_delete_%02d' % i
            reboxtag  = 'hdrfilter_rebox_%02d' % i
            actiontag = 'hdrfilter_action_%02d' % i
            wheretag  = 'hdrfilter_where_%02d' % i
            addtag    = 'hdrfilter_add_%02d' % i
            newtag    = 'hdrfilter_new_%02d' % i
            uptag     = 'hdrfilter_up_%02d' % i
            downtag   = 'hdrfilter_down_%02d' % i
            if empty:
                table.AddRow([Center(Bold(_('Spam Filter Rule %(i)d') % {'i': i})),
                              Hidden(newtag)])
            else:
                table.AddRow([Center(Bold(_('Spam Filter Rule %(i)d') % {'i': i})),
                              SubmitButton(deltag, _('Delete'))])
            table.AddRow([Label(_('Spam Filter Regexp:')),
                          TextArea(reboxtag, text=pattern,
                                   rows=4, cols=30, wrap='off')])
            values = [mm_cfg.DEFER, mm_cfg.HOLD, mm_cfg.REJECT,
                      mm_cfg.DISCARD, mm_cfg.ACCEPT]
            legends = [_('Defer'), _('Hold'), _('Reject'),
                       _('Discard'), _('Accept')]
            table.AddRow([Label(_('Action:')),
                          SelectOptions(actiontag, values, legends,
                                       selected=values.index(action))])
            if not empty:
                table.AddRow([SubmitButton(addtag, _('Add new rule...')),
                              SelectOptions(wheretag, ('before', 'after'),
                                            (_('...before this one.'),
                                             _('...after this one.')),
                                            selected=1),
                              ])
            table.AddRow(['<hr>'])
            table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2, role='cell')
        # Now for each element in the existing data, create a widget
        i = 1
        data = getattr(mlist, varname)
        for pattern, action, empty in data:
            if isinstance(pattern, bytes):
                pattern = pattern.decode(Utils.GetCharSet(mlist.preferred_language), 'replace')
            makebox(i, pattern, action, empty)
            i += 1
        # Add one more non-deleteable widget as the first blank entry, but
        # only if there are no real entries.
        if i == 1:
            makebox(i, '', mm_cfg.DEFER, empty=True)
        return table
    elif kind == mm_cfg.Checkbox:
        return CheckBoxArray(varname, *params)
    else:
        assert 0, 'Bad gui widget type: %s' % kind

def get_item_gui_description(mlist, category, subcat,
                             varname, descr, elaboration, detailsp):
    # Return the item's description, with link to details.
    if detailsp:
        if subcat:
            varhelp = '/?VARHELP=%(category)s/%(subcat)s/%(varname)s' % {
                'category': category,
                'subcat': subcat,
                'varname': varname
            }
        else:
            varhelp = '/?VARHELP=%(category)s/%(varname)s' % {
                'category': category,
                'varname': varname
            }
        if descr == elaboration:
            linktext = _('<br>(Edit <b>%(varname)s</b>)') % {
                'varname': varname
            }
        else:
            linktext = _('<br>(Details for <b>%(varname)s</b>)') % {
                'varname': varname
            }
        link = Link(mlist.GetScriptURL('admin') + varhelp,
                    linktext).Format()
        text = Label('%(descr)s %(link)s' % {
            'descr': descr,
            'link': link
        }).Format()
    else:
        text = Label(descr).Format()
    if varname[0] == '_':
        text += Label(_('<br><em><strong>Note:</strong> setting this value performs an immediate action but does not modify permanent state.</em>')).Format()
    return text

def membership_options(mlist, subcat, cgidata, doc, form):
    # Show the main stuff
    adminurl = mlist.GetScriptURL('admin', absolute=1)
    container = Container()
    header = Table(width="100%")
    
    # Add standard headers based on subcat
    if subcat == 'add':
        title = _('Mass Subscriptions')
    elif subcat == 'remove':
        title = _('Mass Removals')
    elif subcat == 'change':
        title = _('Address Change')
    elif subcat == 'sync':
        title = _('Sync Membership List')
    else:
        title = _('Membership List')
        
    add_standard_headers(doc, mlist, title, 'members', subcat)
    
    # Add a "search for member" button
    table = Table(width='100%')
    link = Link('https://docs.python.org/3/library/re.html'
                '#regular-expression-syntax',
                _('(help)')).Format()
    table.AddRow([Label(_('Find member %(link)s:') % {'link': link}),
                  TextBox('findmember',
                          value=cgidata.get('findmember', [''])[0]),
                  SubmitButton('findmember_btn', _('Search...'))])
    container.AddItem(table)
    container.AddItem('<hr><p>')
    usertable = Table(width="90%", border='2')
    # The email addresses had /better/ be ASCII, but might be encoded in the
    # database as Unicodes.
    all = []
    for _m in mlist.getMembers():
        try:
            # Verify the member still exists
            mlist.getMemberName(_m)
            # Decode the email address as latin-1
            if isinstance(_m, bytes):
                _m = _m.decode('latin-1')
            all.append(_m)
        except Errors.NotAMemberError:
            # Skip addresses that are no longer members
            continue
    all.sort(key=lambda x: x.lower())
    # See if the query has a regular expression
    regexp = cgidata.get('findmember', [''])[0]
    if isinstance(regexp, bytes):
        regexp = regexp.decode('latin1', 'replace')
    regexp = regexp.strip()
    try:
        if isinstance(regexp, bytes):
            regexp = regexp.decode(Utils.GetCharSet(mlist.preferred_language))
    except UnicodeDecodeError:
        # This is probably a non-ascii character and an English language
        # (ascii) list.  Even if we didn't throw the UnicodeDecodeError,
        # the input may have contained mnemonic or numeric HTML entites mixed
        # with other characters.  Trying to grok the real meaning out of that
        # is complex and error prone, so we don't try.
        pass
    if regexp:
        try:
            cre = re.compile(regexp, re.IGNORECASE)
        except re.error:
            doc.addError(_('Bad regular expression: %(regexp)s') % {'regexp': regexp})
        else:
            # BAW: There's got to be a more efficient way of doing this!
            names = []
            valid_members = []
            for addr in all:
                try:
                    name = mlist.getMemberName(addr) or ''
                    if isinstance(name, bytes):
                        name = name.decode('latin-1', 'replace')
                    names.append(name)
                    valid_members.append(addr)
                except Errors.NotAMemberError:
                    # Skip addresses that are no longer members
                    continue
            all = [a for n, a in zip(names, valid_members)
                   if cre.search(n) or cre.search(a)]
    chunkindex = None
    bucket = None
    actionurl = None
    if len(all) < mlist.admin_member_chunksize:
        members = all
    else:
        # Split them up alphabetically, and then split the alphabetical
        # listing by chunks
        buckets = {}
        for addr in all:
            members = buckets.setdefault(addr[0].lower(), [])
            members.append(addr)
        # Now figure out which bucket we want
        bucket = None
        qs = {}
        # POST methods, even if their actions have a query string, don't get
        # put into FieldStorage's keys :-(
        qsenviron = os.environ.get('QUERY_STRING')
        if qsenviron:
            qs = urllib.parse.parse_qs(qsenviron)
            bucket = qs.get('letter', '0')[0].lower()
        keys = list(buckets.keys())
        keys.sort()
        if not bucket or bucket not in buckets:
            bucket = keys[0]
        members = buckets[bucket]
        action = '%(adminurl)s/members?letter=%(bucket)s' % {
            'adminurl': adminurl,
            'bucket': bucket
        }
        if len(members) <= mlist.admin_member_chunksize:
            form.set_action(action)
        else:
            i, r = divmod(len(members), mlist.admin_member_chunksize)
            numchunks = i + (not not r * 1)
            # Now chunk them up
            chunkindex = 0
            if 'chunk' in qs:
                try:
                    chunkindex = int(qs['chunk'][0])
                except ValueError:
                    chunkindex = 0
                if chunkindex < 0 or chunkindex > numchunks:
                    chunkindex = 0
            members = members[chunkindex*mlist.admin_member_chunksize:(chunkindex+1)*mlist.admin_member_chunksize]
            # And set the action URL
            form.set_action('%(action)s&chunk=%(chunkindex)s' % {
                'action': action,
                'chunkindex': chunkindex
            })
    # So now members holds all the addresses we're going to display
    allcnt = len(all)
    if bucket:
        membercnt = len(members)
        count_text = _('%(allcnt)d members total, %(membercnt)d shown') % {
            'allcnt': len(all), 'membercnt': len(members)}
        usertable.AddRow([Center(Italic(count_text))])
    else:
        usertable.AddRow([Center(Italic(_('%(allcnt)d members total') % {
            'allcnt': len(all)
        }))])
    usertable.AddCellInfo(usertable.GetCurrentRowIndex(),
                          usertable.GetCurrentCellIndex(),
                          colspan=OPTCOLUMNS,
                          bgcolor=mm_cfg.WEB_ADMINITEM_COLOR)
    # Add the alphabetical links
    if bucket:
        cells = []
        for letter in keys:
            findfrag = ''
            if regexp:
                findfrag = '&findmember=' + urllib.parse.quote(regexp)
            url = '%(adminurl)s/members?letter=%(letter)s%(findfrag)s' % {
                'adminurl': adminurl,
                'letter': letter,
                'findfrag': findfrag
            }
            if isinstance(url, str):
                url = url.encode(Utils.GetCharSet(mlist.preferred_language),
                                 errors='ignore')
            if letter == bucket:
                # Do this in two steps to get it to work properly with the
                # translatable title.
                formatted_text = '[%s]' % letter.upper()
                text = Bold(formatted_text).Format()
            else:
                formatted_label = '[%s]' % letter.upper()
                text = Link(url, Bold(formatted_label)).Format()
            cells.append(text)
        joiner = '&nbsp;'*2 + '\n'
        usertable.AddRow([Center(joiner.join(cells))])
    usertable.AddCellInfo(usertable.GetCurrentRowIndex(),
                          usertable.GetCurrentCellIndex(),
                          colspan=OPTCOLUMNS,
                          bgcolor=mm_cfg.WEB_ADMINITEM_COLOR)
    usertable.AddRow([Center(h) for h in (_('unsub'),
                                          _('member address<br>member name'),
                                          _('mod'), _('hide'),
                                          _('nomail<br>[reason]'),
                                          _('ack'), _('not metoo'),
                                          _('nodupes'),
                                          _('digest'), _('plain'),
                                          _('language'))])
    rowindex = usertable.GetCurrentRowIndex()
    for i in range(OPTCOLUMNS):
        usertable.AddCellInfo(rowindex, i, bgcolor=mm_cfg.WEB_ADMINITEM_COLOR)
    # Find the longest name in the list
    longest = 0
    if members:
        names = []
        for addr in members:
            try:
                name = mlist.getMemberName(addr) or ''
                if isinstance(name, bytes):
                    name = name.decode('latin-1', 'replace')
                if name:
                    names.append(name)
            except Errors.NotAMemberError:
                # Skip addresses that are no longer members
                continue
        # Make the name field at least as long as the longest email address
        if names:
            longest = max([len(s) for s in names + members])
    # Abbreviations for delivery status details
    ds_abbrevs = {MemberAdaptor.UNKNOWN : _('?'),
                  MemberAdaptor.BYUSER  : _('U'),
                  MemberAdaptor.BYADMIN : _('A'),
                  MemberAdaptor.BYBOUNCE: _('B'),
                  }
    # Now populate the rows
    for addr in members:
        try:
            if isinstance(addr, bytes):
                addr = addr.decode('latin-1')
            qaddr = urllib.parse.quote(addr)
            link = Link(mlist.GetOptionsURL(addr, obscure=1),
                        mlist.getMemberCPAddress(addr))
            fullname = mlist.getMemberName(addr)
            if isinstance(fullname, bytes):
                fullname = fullname.decode('latin1', 'replace')
            fullname = Utils.uncanonstr(fullname, mlist.preferred_language)
            name = TextBox('%(qaddr)s_realname' % {'qaddr': qaddr}, fullname, size=longest).Format()
            cells = [Center(CheckBox('%(qaddr)s_unsub' % {'qaddr': qaddr}, 'off', 0).Format()
                        + '<div class="hidden">' + _('unsub') + '</div>'),
                    link.Format() + '<br>' +
                    name +
                    Hidden('user', qaddr).Format(),
                    ]
        except Errors.NotAMemberError:
            # Skip addresses that are no longer members
            continue

        digest_name = '%(qaddr)s_digest' % {'qaddr': qaddr}
        if addr not in mlist.getRegularMemberKeys():
            cells.append(Center(CheckBox(digest_name, 'off', 0).Format()))
        else:
            cells.append(Center(CheckBox(digest_name, 'on', 1).Format()))

        language_name = '%(qaddr)s_language' % {'qaddr': qaddr}
        languages = mlist.available_languages
        legends = [Utils.GetLanguageDescr(lang) for lang in languages]
        cells.append(Center(SelectOptions(language_name, languages, legends,
                                        selected=mlist.getMemberLanguage(addr)).Format()))

        # Do the `mod' option
        if mlist.getMemberOption(addr, mm_cfg.Moderate):
            value = 'on'
            checked = 1
        else:
            value = 'off'
            checked = 0
        box = CheckBox('%(qaddr)s_mod' % {'qaddr': qaddr}, value, checked)
        cells.append(Center(box.Format()
            + '<div class="hidden">' + _('mod') + '</div>'))
        # Kluge, get these translated.
        (_('hide'), _('nomail'), _('ack'), _('notmetoo'), _('nodupes'))
        for opt in ('hide', 'nomail', 'ack', 'notmetoo', 'nodupes'):
            extra = '<div class="hidden">' + _(opt) + '</div>'
            if opt == 'nomail':
                status = mlist.getDeliveryStatus(addr)
                if status == MemberAdaptor.ENABLED:
                    value = 'off'
                    checked = 0
                else:
                    value = 'on'
                    checked = 1
                    extra = '[%(abbrev)s]' % {'abbrev': ds_abbrevs[status]} + extra
            elif mlist.getMemberOption(addr, mm_cfg.OPTINFO[opt]):
                value = 'on'
                checked = 1
            else:
                value = 'off'
                checked = 0
            box = CheckBox('%(qaddr)s_%(opt)s' % {'qaddr': qaddr, 'opt': opt}, value, checked)
            cells.append(Center(box.Format() + extra))
        usertable.AddRow(cells)
    # Add the usertable and a legend
    legend = UnorderedList()
    legend.AddItem(
        _('<b>unsub</b> -- Click on this to unsubscribe the member.'))
    legend.AddItem(
        _('''<b>mod</b> -- The user's personal moderation flag.  If this is
        set, postings from them will be moderated, otherwise they will be
        approved.'''))
    legend.AddItem(
        _('''<b>hide</b> -- Is the member's address concealed on
        the list of subscribers?'''))
    legend.AddItem(_(
        '''<b>nomail</b> -- Is delivery to the member disabled?  If so, an
        abbreviation will be given describing the reason for the disabled
        delivery:
            <ul><li><b>U</b> -- Delivery was disabled by the user via their
                    personal options page.
                <li><b>A</b> -- Delivery was disabled by the list
                    administrators.
                <li><b>B</b> -- Delivery was disabled by the system due to
                    excessive bouncing from the member's address.
                <li><b>?</b> -- The reason for disabled delivery isn't known.
                    This is the case for all memberships which were disabled
                    in older versions of Mailman.
            </ul>'''))
    legend.AddItem(
        _('''<b>ack</b> -- Does the member get acknowledgements of their
        posts?'''))
    legend.AddItem(
        _('''<b>not metoo</b> -- Does the member want to avoid copies of their
        own postings?'''))
    legend.AddItem(
        _('''<b>nodupes</b> -- Does the member want to avoid duplicates of the
        same message?'''))
    legend.AddItem(
        _('''<b>digest</b> -- Does the member get messages in digests?
        (otherwise, individual messages)'''))
    legend.AddItem(
        _('''<b>plain</b> -- If getting digests, does the member get plain
        text digests?  (otherwise, MIME)'''))
    legend.AddItem(_("<b>language</b> -- Language preferred by the user"))
    addlegend = ''
    parsedqs = 0
    qsenviron = os.environ.get('QUERY_STRING')
    if qsenviron:
        qs = urllib.parse.parse_qs(qsenviron).get('legend')
        if qs and isinstance(qs, list):
            qs = qs[0]
        if qs == 'yes':
            addlegend = 'legend=yes&'
    if addlegend:
        container.AddItem(legend.Format() + '<p>')
        container.AddItem(
            Link(adminurl + '/members/list',
                 _('Click here to hide the legend for this table.')))
    else:
        container.AddItem(
            Link(adminurl + '/members/list?legend=yes',
                 _('Click here to include the legend for this table.')))
    container.AddItem(Center(usertable))

    # There may be additional chunks
    if chunkindex is not None:
        buttons = []
        url = '%(adminurl)s/members?%(addlegend)sletter=%(bucket)s&' % {
            'adminurl': adminurl,
            'addlegend': addlegend,
            'bucket': bucket
        }
        footer = _('''<p><em>To view more members, click on the appropriate
        range listed below:</em>''')
        chunkmembers = buckets[bucket]
        last = len(chunkmembers)
        for i in range(numchunks):
            if i == chunkindex:
                continue
            start = chunkmembers[i*mlist.admin_member_chunksize]
            end = chunkmembers[min((i+1)*mlist.admin_member_chunksize, last)-1]
            thisurl = '%(url)schunk=%(i)d%(findfrag)s' % {
                'url': url,
                'i': i,
                'findfrag': findfrag
            }
            if isinstance(thisurl, str):
                thisurl = thisurl.encode(
                                 Utils.GetCharSet(mlist.preferred_language),
                                 errors='ignore')
            link = Link(thisurl, _('from %(start)s to %(end)s') % {
                'start': start,
                'end': end
            })
            buttons.append(link)
        buttons = UnorderedList(*buttons)
        container.AddItem(footer + buttons.Format() + '<p>')
    return container

def mass_subscribe(mlist, container):
    # MASS SUBSCRIBE
    GREY = mm_cfg.WEB_ADMINITEM_COLOR
    table = Table(width='90%')
    table.AddRow([
        Label(_('Subscribe these users now or invite them?')),
        RadioButtonArray('subscribe_or_invite',
                         (_('Subscribe'), _('Invite')),
                         mm_cfg.DEFAULT_SUBSCRIBE_OR_INVITE,
                         values=(0, 1))
        ])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, bgcolor=GREY)
    table.AddCellInfo(table.GetCurrentRowIndex(), 1, bgcolor=GREY)
    table.AddRow([
        Label(_('Send welcome messages to new subscribers?')),
        RadioButtonArray('send_welcome_msg_to_this_batch',
                         (_('No'), _('Yes')),
                         mlist.send_welcome_msg,
                         values=(0, 1))
        ])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, bgcolor=GREY)
    table.AddCellInfo(table.GetCurrentRowIndex(), 1, bgcolor=GREY)
    table.AddRow([
        Label(_('Send notifications of new subscriptions to the list owner?')),
        RadioButtonArray('send_notifications_to_list_owner',
                         (_('No'), _('Yes')),
                         mlist.admin_notify_mchanges,
                         values=(0, 1))
        ])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, bgcolor=GREY)
    table.AddCellInfo(table.GetCurrentRowIndex(), 1, bgcolor=GREY)
    table.AddRow([Italic(_('Enter one address per line below...'))])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
    table.AddRow([Center(TextArea(name='subscribees',
                                  rows=10, cols='70%', wrap=None))])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
    table.AddRow([Italic(Label(_('...or specify a file to upload:'))),
                  FileUpload('subscribees_upload', cols='50')])
    container.AddItem(Center(table))
    # Invitation text
    table.AddRow(['&nbsp;', '&nbsp;'])
    table.AddRow([Italic(_(f"""Below, enter additional text to be added to the
    top of your invitation or the subscription notification.  Include at least
    one blank line at the end..."""))])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
    table.AddRow([Center(TextArea(name='invitation',
                                  rows=10, cols='70%', wrap=None))])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)

def mass_remove(mlist, container):
    # MASS UNSUBSCRIBE
    GREY = mm_cfg.WEB_ADMINITEM_COLOR
    table = Table(width='90%')
    table.AddRow([
        Label(_('Send unsubscription acknowledgement to the user?')),
        RadioButtonArray('send_unsub_ack_to_this_batch',
                         (_('No'), _('Yes')),
                         0, values=(0, 1))
        ])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, bgcolor=GREY)
    table.AddCellInfo(table.GetCurrentRowIndex(), 1, bgcolor=GREY)
    table.AddRow([
        Label(_('Send notifications to the list owner?')),
        RadioButtonArray('send_unsub_notifications_to_list_owner',
                         (_('No'), _('Yes')),
                         mlist.admin_notify_mchanges,
                         values=(0, 1))
        ])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, bgcolor=GREY)
    table.AddCellInfo(table.GetCurrentRowIndex(), 1, bgcolor=GREY)
    table.AddRow([Italic(_('Enter one address per line below...'))])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
    table.AddRow([Center(TextArea(name='unsubscribees',
                                  rows=10, cols='70%', wrap=None))])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
    table.AddRow([Italic(Label(_('...or specify a file to upload:'))),
                  FileUpload('unsubscribees_upload', cols='50')])
    container.AddItem(Center(table))

def address_change(mlist, container):
    # ADDRESS CHANGE
    GREY = mm_cfg.WEB_ADMINITEM_COLOR
    table = Table(width='90%')
    table.AddRow([Italic(_(f"""To change a list member's address, enter the
    member's current and new addresses below. Use the check boxes to send
    notice of the change to the old and/or new address(es)."""))])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=3)
    table.AddRow([
        Label(_("Member's current address")),
        TextBox(name='change_from'),
        CheckBox('notice_old', 'yes', 0).Format() +
            '&nbsp;' +
            _('Send notice')
        ])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, bgcolor=GREY)
    table.AddCellInfo(table.GetCurrentRowIndex(), 1, bgcolor=GREY)
    table.AddCellInfo(table.GetCurrentRowIndex(), 2, bgcolor=GREY)
    table.AddRow([
        Label(_('Address to change to')),
        TextBox(name='change_to'),
        CheckBox('notice_new', 'yes', 0).Format() +
            '&nbsp;' +
            _('Send notice')
        ])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, bgcolor=GREY)
    table.AddCellInfo(table.GetCurrentRowIndex(), 1, bgcolor=GREY)
    table.AddCellInfo(table.GetCurrentRowIndex(), 2, bgcolor=GREY)
    container.AddItem(Center(table))

def mass_sync(mlist, container):
    # MASS SYNC
    table = Table(width='90%')
    table.AddRow([Italic(_('Enter one address per line below...'))])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
    table.AddRow([Center(TextArea(name='memberlist',
                                  rows=10, cols='70%', wrap=None))])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
    table.AddRow([Italic(Label(_('...or specify a file to upload:'))),
                  FileUpload('memberlist_upload', cols='50')])
    container.AddItem(Center(table))

def password_inputs(mlist):
    adminurl = mlist.GetScriptURL('admin', absolute=1)
    table = Table(cellspacing=3, cellpadding=4)
    table.AddRow([Center(Header(2, _('Change list ownership passwords')))])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2,
                      bgcolor=mm_cfg.WEB_HEADER_COLOR)
    table.AddRow([_(f"""\
The <em>list administrators</em> are the people who have ultimate control over
all parameters of this mailing list.  They are able to change any list
configuration variable available through these administration web pages.

<p>The <em>list moderators</em> have more limited permissions; they are not
able to change any list configuration variable, but they are allowed to tend
to pending administration requests, including approving or rejecting held
subscription requests, and disposing of held postings.  Of course, the
<em>list administrators</em> can also tend to pending requests.

<p>In order to split the list ownership duties into administrators and
moderators, you must set a separate moderator password in the fields below,
and also provide the email addresses of the list moderators in the
<a href="{adminurl}/general">general options section</a>.""")])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
    # Set up the admin password table on the left
    atable = Table(border=0, cellspacing=3, cellpadding=4,
                   bgcolor=mm_cfg.WEB_ADMINPW_COLOR)
    atable.AddRow([Label(_('Enter new administrator password:')),
                   PasswordBox('newpw', size=20)])
    atable.AddRow([Label(_('Confirm administrator password:')),
                   PasswordBox('confirmpw', size=20)])
    # Set up the moderator password table on the right
    mtable = Table(border=0, cellspacing=3, cellpadding=4,
                   bgcolor=mm_cfg.WEB_ADMINPW_COLOR)
    mtable.AddRow([Label(_('Enter new moderator password:')),
                   PasswordBox('newmodpw', size=20)])
    mtable.AddRow([Label(_('Confirm moderator password:')),
                   PasswordBox('confirmmodpw', size=20)])
    # Add these tables to the overall password table
    table.AddRow([atable, mtable])
    table.AddRow([_(f"""\
In addition to the above passwords you may specify a password for
pre-approving posts to the list. Either of the above two passwords can
be used in an Approved: header or first body line pseudo-header to
pre-approve a post that would otherwise be held for moderation. In
addition, the password below, if set, can be used for that purpose and
no other.""")])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
    # Set up the post password table
    ptable = Table(border=0, cellspacing=3, cellpadding=4,
                   bgcolor=mm_cfg.WEB_ADMINPW_COLOR)
    ptable.AddRow([Label(_('Enter new poster password:')),
                   PasswordBox('newpostpw', size=20)])
    ptable.AddRow([Label(_('Confirm poster password:')),
                   PasswordBox('confirmpostpw', size=20)])
    table.AddRow([ptable])
    return table

def submit_button(name='submit'):
    table = Table(border=0, cellspacing=0, cellpadding=2)
    table.AddRow([Bold(SubmitButton(name, _('Submit Your Changes')))])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, align='middle')
    return table

def change_options(mlist, category, subcat, cgidata, doc):
    """Change the list's options."""
    try:
        # Get the configuration categories
        config_categories = mlist.GetConfigCategories()
        
        # Log the configuration categories for debugging
        mailman_log('debug', 'Configuration categories: %s', str(config_categories))
        mailman_log('debug', 'Category type: %s', str(type(config_categories)))
        if isinstance(config_categories, dict):
            mailman_log('debug', 'Category keys: %s', str(list(config_categories.keys())))
            for key, value in config_categories.items():
                mailman_log('debug', 'Category %s type: %s, value: %s', 
                       key, str(type(value)), str(value))
        
        # Validate category exists
        if category not in config_categories:
            mailman_log('error', 'Invalid configuration category: %s', category)
            doc.AddItem(mlist.ParseTags('adminerror.html',
                                      {'error': 'Invalid configuration category'},
                                      mlist.preferred_language))
            return
            
        # Get the category object and validate it
        category_obj = config_categories[category]
        mailman_log('debug', 'Category object for %s: type=%s, value=%s', 
               category, str(type(category_obj)), str(category_obj))
        
        if not hasattr(category_obj, 'items'):
            mailman_log('error', 'Configuration category %s is invalid: %s', 
                   category, str(type(category_obj)))
            doc.AddItem(mlist.ParseTags('adminerror.html',
                                      {'error': 'Invalid configuration category structure'},
                                      mlist.preferred_language))
            return
            
        # Process each item in the category
        for item in category_obj.items:
            try:
                # Get the item's value from the form data
                value = cgidata.get(item.name, None)
                if value is None:
                    continue
                    
                # Set the item's value
                item.set(mlist, value)
                
            except Exception as e:
                mailman_log('error', 'Error setting %s.%s: %s', 
                       category, item.name, str(e))
                doc.AddItem(mlist.ParseTags('adminerror.html',
                                          {'error': 'Error setting %s: %s' % 
                                                   (item.name, str(e))},
                                          mlist.preferred_language))
                return
                
        # Save the changes
        mlist.Save()
        
    except Exception as e:
        mailman_log('error', 'Error in change_options: %s\n%s', 
               str(e), traceback.format_exc())
        doc.AddItem(mlist.ParseTags('adminerror.html',
                                  {'error': 'Internal error: %s' % str(e)},
                                  mlist.preferred_language))
