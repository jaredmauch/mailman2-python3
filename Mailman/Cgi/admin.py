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

#from future.builtins import cmp
import sys
import os
import re
import cgi
import urllib.request, urllib.parse, urllib.error
import signal

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
        admin_overview(_(f'No such list <em>{safelistname}</em>'))
        syslog('error', 'admin: No such list "%s": %s\n',
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
                   'Authorization failed (admin): list=%s: remote=%s',
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
    if category not in list(mlist.GetConfigCategories().keys()):
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
        if qs and type(qs) is list:
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

        if list(cgidata.keys()):
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
        # Glom up the results page and print it out
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
    hostname = Utils.get_domain()
    legend = _(f"{hostname} mailing lists - Admin Links")
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
                advertised.append((mlist.GetScriptURL('admin'),
                                   mlist.real_name,
                                   Utils.websafe(mlist.GetDescription())))
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
            _(f'''<p>There currently are no publicly-advertised {mailmanlink}
            mailing lists on {hostname}.'''),
            ])
    else:
        welcome.extend([
            greeting,
            _(f'''<p>Below is the collection of publicly-advertised
            {mailmanlink} mailing lists on {hostname}.  Click on a list
            name to visit the configuration pages for that list.'''),
            ])

    creatorurl = Utils.ScriptURL('create')
    mailman_owner = Utils.get_site_email()
    extra = msg and _('right ') or ''
    welcome.extend([
        _(f'''To visit the administrators configuration page for an
        unadvertised list, open a URL similar to this one, but with a '/' and
        the {extra}list name appended.  If you have the proper authority,
        you can also <a href="{creatorurl}">create a new mailing list</a>.

        <p>General list information can be found at '''),
        Link(Utils.ScriptURL('listinfo'),
             _('the mailing list overview page')),
        '.',
        _('<p>(Send questions and comments to '),
        Link('mailto:%s' % mailman_owner, mailman_owner),
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
        category = subcat = None
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



def show_results(mlist, doc, category, subcat, cgidata):
    # Produce the results page
    adminurl = mlist.GetScriptURL('admin')
    categories = mlist.GetConfigCategories()
    label = _(categories[category][0])

    # Set up the document's headers
    realname = mlist.real_name
    doc.SetTitle(_(f'{realname} Administration ({label})'))
    doc.AddItem(Center(Header(2, _(
        '{realname} mailing list administration<br>{label} Section'))))
    doc.AddItem('<hr>')
    # Now we need to craft the form that will be submitted, which will contain
    # all the variable settings, etc.  This is a bit of a kludge because we
    # know that the autoreply and members categories supports file uploads.
    encoding = None
    if category in ('autoreply', 'members'):
        encoding = 'multipart/form-data'
    if subcat:
        form = Form('%s/%s/%s' % (adminurl, category, subcat),
                    encoding=encoding, mlist=mlist, contexts=AUTH_CONTEXTS)
    else:
        form = Form('%s/%s' % (adminurl, category),
                    encoding=encoding, mlist=mlist, contexts=AUTH_CONTEXTS)
    # This holds the two columns of links
    linktable = Table(valign='top', width='100%')
    linktable.AddRow([Center(Bold(_("Configuration Categories"))),
                      Center(Bold(_("Other Administrative Activities")))])
    # The `other links' are stuff in the right column.
    otherlinks = UnorderedList()
    otherlinks.AddItem(Link(mlist.GetScriptURL('admindb'),
                            _('Tend to pending moderator requests')))
    otherlinks.AddItem(Link(mlist.GetScriptURL('listinfo'),
                            _('Go to the general list information page')))
    otherlinks.AddItem(Link(mlist.GetScriptURL('edithtml'),
                            _('Edit the public HTML pages and text files')))
    otherlinks.AddItem(Link(mlist.GetBaseArchiveURL(),
                            _('Go to list archives')).Format() +
                       '<br>&nbsp;<br>')
    # We do not allow through-the-web deletion of the site list!
    if mm_cfg.OWNERS_CAN_DELETE_THEIR_OWN_LISTS and \
           mlist.internal_name() != mm_cfg.MAILMAN_SITE_LIST:
        otherlinks.AddItem(Link(mlist.GetScriptURL('rmlist'),
                                _('Delete this mailing list')).Format() +
                           _(' (requires confirmation)<br>&nbsp;<br>'))
    otherlinks.AddItem(Link('%s/logout' % adminurl,
                            # BAW: What I really want is a blank line, but
                            # adding an &nbsp; won't do it because of the
                            # bullet added to the list item.
                            '<FONT SIZE="+2"><b>%s</b></FONT>' %
                            _('Logout')))
    # These are links to other categories and live in the left column
    categorylinks_1 = categorylinks = UnorderedList()
    categorylinks_2 = ''
    categorykeys = list(categories.keys())
    half = len(categorykeys) / 2
    counter = 0
    subcat = None
    for k in categorykeys:
        label = _(categories[k][0])
        url = '%s/%s' % (adminurl, k)
        if k == category:
            # Handle subcategories
            subcats = mlist.GetConfigSubCategories(k)
            if subcats:
                subcat = Utils.GetPathPieces()[-1]
                for k, v in subcats:
                    if k == subcat:
                        break
                else:
                    # The first subcategory in the list is the default
                    subcat = subcats[0][0]
                subcat_items = []
                for sub, text in subcats:
                    if sub == subcat:
                        text = Bold('[%s]' % text).Format()
                    subcat_items.append(Link(url + '/' + sub, text))
                categorylinks.AddItem(
                    Bold(label).Format() +
                    UnorderedList(*subcat_items).Format())
            else:
                categorylinks.AddItem(Link(url, Bold('[%s]' % label)))
        else:
            categorylinks.AddItem(Link(url, label))
        counter += 1
        if counter >= half:
            categorylinks_2 = categorylinks = UnorderedList()
            counter = -len(categorykeys)
    # Make the emergency stop switch a rude solo light
    etable = Table()
    # Add all the links to the links table...
    etable.AddRow([categorylinks_1, categorylinks_2])
    etable.AddRowInfo(etable.GetCurrentRowIndex(), valign='top')
    if mlist.emergency:
        label = _('Emergency moderation of all list traffic is enabled')
        etable.AddRow([Center(
            Link('?VARHELP=general/emergency', Bold(label)))])
        color = mm_cfg.WEB_ERROR_COLOR
        etable.AddCellInfo(etable.GetCurrentRowIndex(), 0,
                           colspan=2, bgcolor=color)
    linktable.AddRow([etable, otherlinks])
    # ...and add the links table to the document.
    form.AddItem(linktable)
    form.AddItem('<hr>')
    form.AddItem(
        _(f'''Make your changes in the following section, then submit them
        using the <em>Submit Your Changes</em> button below.''')
        + '<p>')

    # The members and passwords categories are special in that they aren't
    # defined in terms of gui elements.  Create those pages here.
    if category == 'members':
        # Figure out which subcategory we should display
        subcat = Utils.GetPathPieces()[-1]
        if subcat not in ('list', 'add', 'remove', 'change', 'sync'):
            subcat = 'list'
        # Add member category specific tables
        form.AddItem(membership_options(mlist, subcat, cgidata, doc, form))
        form.AddItem(Center(submit_button('setmemberopts_btn')))
        # In "list" subcategory, we can also search for members
        if subcat == 'list':
            form.AddItem('<hr>\n')
            table = Table(width='100%')
            table.AddRow([Center(Header(2, _('Additional Member Tasks')))])
            table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2,
                              bgcolor=mm_cfg.WEB_HEADER_COLOR)
            # Add a blank separator row
            table.AddRow(['&nbsp;', '&nbsp;'])
            # Add a section to set the moderation bit for all members
            table.AddRow([_(f"""<li>Set everyone's moderation bit, including
            those members not currently visible""")])
            table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
            table.AddRow([RadioButtonArray('allmodbit_val',
                                           (_('Off'), _('On')),
                                           mlist.default_member_moderation),
                          SubmitButton('allmodbit_btn', _('Set'))])
            form.AddItem(table)
    elif category == 'passwords':
        form.AddItem(Center(password_inputs(mlist)))
        form.AddItem(Center(submit_button()))
    else:
        form.AddItem(show_variables(mlist, category, subcat, cgidata, doc))
        form.AddItem(Center(submit_button()))
    # And add the form
    doc.AddItem(form)
    doc.AddItem(mlist.GetMailmanFooter())



def show_variables(mlist, category, subcat, cgidata, doc):
    options = mlist.GetConfigInfo(category, subcat)

    # The table containing the results
    table = Table(cellspacing=3, cellpadding=4, width='100%')

    # Get and portray the text label for the category.
    categories = mlist.GetConfigCategories()
    label = _(categories[category][0])

    table.AddRow([Center(Header(2, label))])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2,
                      bgcolor=mm_cfg.WEB_HEADER_COLOR)

    # The very first item in the config info will be treated as a general
    # description if it is a string
    description = options[0]
    if type(description) is str:
        table.AddRow([description])
        table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
        options = options[1:]

    if not options:
        return table

    # Add the global column headers
    table.AddRow([Center(Bold(_('Description'))),
                  Center(Bold(_('Value')))])
    table.AddCellInfo(max(table.GetCurrentRowIndex(), 0), 0,
                      width='15%')
    table.AddCellInfo(max(table.GetCurrentRowIndex(), 0), 1,
                      width='85%')

    for item in options:
        if type(item) == str:
            # The very first banner option (string in an options list) is
            # treated as a general description, while any others are
            # treated as section headers - centered and italicized...
            table.AddRow([Center(Italic(item))])
            table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
        else:
            add_options_table_item(mlist, category, subcat, table, item)
    table.AddRow(['<br>'])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
    return table



def add_options_table_item(mlist, category, subcat, table, item, detailsp=1):
    # Add a row to an options table with the item description and value.
    varname, kind, params, extra, descr, elaboration = \
             get_item_characteristics(item)
    if elaboration is None:
        elaboration = descr
    descr = get_item_gui_description(mlist, category, subcat,
                                     varname, descr, elaboration, detailsp)
    val = get_item_gui_value(mlist, category, kind, varname, params, extra)
    table.AddRow([descr, val])
    table.AddCellInfo(table.GetCurrentRowIndex(), 0,
                      bgcolor=mm_cfg.WEB_ADMINITEM_COLOR)
    table.AddCellInfo(table.GetCurrentRowIndex(), 1,
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
    label, gui = mlist.GetConfigCategories()[category]
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
        return TextBox(varname, value, params)
    elif kind == mm_cfg.Text:
        if params:
            r, c = params
        else:
            r, c = None, None
        return TextArea(varname, value or '', r, c)
    elif kind in (mm_cfg.EmailList, mm_cfg.EmailListEx):
        if params:
            r, c = params
        else:
            r, c = None, None
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
           values = mlist.GetAvailableLanguages()
           legend = list(map(_, list(map(Utils.GetLanguageDescr, values))))
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
                table.AddRow([Center(Bold(_('Topic %(i)d'))),
                              Hidden(newtag)])
            else:
                table.AddRow([Center(Bold(_('Topic %(i)d'))),
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
            table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
        # Now for each element in the existing data, create a widget
        i = 1
        data = getattr(mlist, varname)
        for name, pattern, desc, empty in data:
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
                table.AddRow([Center(Bold(_('Spam Filter Rule %(i)d'))),
                              Hidden(newtag)])
            else:
                table.AddRow([Center(Bold(_('Spam Filter Rule %(i)d'))),
                              SubmitButton(deltag, _('Delete'))])
            table.AddRow([Label(_('Spam Filter Regexp:')),
                          TextArea(reboxtag, text=pattern,
                                   rows=4, cols=30, wrap='off')])
            values = [mm_cfg.DEFER, mm_cfg.HOLD, mm_cfg.REJECT,
                      mm_cfg.DISCARD, mm_cfg.ACCEPT]
            try:
                checked = values.index(action)
            except ValueError:
                checked = 0
            radio = RadioButtonArray(
                actiontag,
                (_('Defer'), _('Hold'), _('Reject'),
                 _('Discard'), _('Accept')),
                values=values,
                checked=checked).Format()
            table.AddRow([Label(_('Action:')), radio])
            if not empty:
                table.AddRow([SubmitButton(addtag, _('Add new item...')),
                              SelectOptions(wheretag, ('before', 'after'),
                                            (_('...before this one.'),
                                             _('...after this one.')),
                                            selected=1),
                              ])
                # BAW: IWBNI we could disable the up and down buttons for the
                # first and last item respectively, but it's not easy to know
                # which is the last item, so let's not worry about that for
                # now.
                table.AddRow([SubmitButton(uptag, _('Move rule up')),
                              SubmitButton(downtag, _('Move rule down'))])
            table.AddRow(['<hr>'])
            table.AddCellInfo(table.GetCurrentRowIndex(), 0, colspan=2)
        # Now for each element in the existing data, create a widget
        i = 1
        data = getattr(mlist, varname)
        for pattern, action, empty in data:
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
    #
    # Details are not included if this is a VARHELP page, because that /is/
    # the details page!
    if detailsp:
        if subcat:
            varhelp = '/?VARHELP=%s/%s/%s' % (category, subcat, varname)
        else:
            varhelp = '/?VARHELP=%s/%s' % (category, varname)
        if descr == elaboration:
            linktext = _(f'<br>(Edit <b>{varname}</b>)')
        else:
            linktext = _(f'<br>(Details for <b>{varname}</b>)')
        link = Link(mlist.GetScriptURL('admin') + varhelp,
                    linktext).Format()
        text = Label('%s %s' % (descr, link)).Format()
    else:
        text = Label(descr).Format()
    if varname[0] == '_':
        text += Label(_(f'''<br><em><strong>Note:</strong>
        setting this value performs an immediate action but does not modify
        permanent state.</em>''')).Format()
    return text



def membership_options(mlist, subcat, cgidata, doc, form):
    # Show the main stuff
    adminurl = mlist.GetScriptURL('admin', absolute=1)
    container = Container()
    header = Table(width="100%")
    # If we're in the list subcategory, show the membership list
    if subcat == 'add':
        header.AddRow([Center(Header(2, _('Mass Subscriptions')))])
        header.AddCellInfo(header.GetCurrentRowIndex(), 0, colspan=2,
                           bgcolor=mm_cfg.WEB_HEADER_COLOR)
        container.AddItem(header)
        mass_subscribe(mlist, container)
        return container
    if subcat == 'remove':
        header.AddRow([Center(Header(2, _('Mass Removals')))])
        header.AddCellInfo(header.GetCurrentRowIndex(), 0, colspan=2,
                           bgcolor=mm_cfg.WEB_HEADER_COLOR)
        container.AddItem(header)
        mass_remove(mlist, container)
        return container
    if subcat == 'change':
        header.AddRow([Center(Header(2, _('Address Change')))])
        header.AddCellInfo(header.GetCurrentRowIndex(), 0, colspan=2,
                           bgcolor=mm_cfg.WEB_HEADER_COLOR)
        container.AddItem(header)
        address_change(mlist, container)
        return container
    if subcat == 'sync':
        header.AddRow([Center(Header(2, _('Sync Membership List')))])
        header.AddCellInfo(header.GetCurrentRowIndex(), 0, colspan=2,
                           bgcolor=mm_cfg.WEB_HEADER_COLOR)
        container.AddItem(header)
        mass_sync(mlist, container)
        return container
    # Otherwise...
    header.AddRow([Center(Header(2, _('Membership List')))])
    header.AddCellInfo(header.GetCurrentRowIndex(), 0, colspan=2,
                       bgcolor=mm_cfg.WEB_HEADER_COLOR)
    container.AddItem(header)
    # Add a "search for member" button
    table = Table(width='100%')
    link = Link('https://docs.python.org/2/library/re.html'
                '#regular-expression-syntax',
                _('(help)')).Format()
    table.AddRow([Label(_(f'Find member {link}:')),
                  TextBox('findmember',
                          value=cgidata.getfirst('findmember', '')),
                  SubmitButton('findmember_btn', _('Search...'))])
    container.AddItem(table)
    container.AddItem('<hr><p>')
    usertable = Table(width="90%", border='2')
    # If there are more members than allowed by chunksize, then we split the
    # membership up alphabetically.  Otherwise just display them all.
    chunksz = mlist.admin_member_chunksize
    # The email addresses had /better/ be ASCII, but might be encoded in the
    # database as Unicodes.
    all = [_m.encode() for _m in mlist.getMembers()]
    all.sort(lambda x, y: cmp(x.lower(), y.lower()))
    # See if the query has a regular expression
    regexp = cgidata.getfirst('findmember', '').strip()
    try:
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
            doc.addError(_('Bad regular expression: ') + regexp)
        else:
            # BAW: There's got to be a more efficient way of doing this!
            names = [mlist.getMemberName(s) or '' for s in all]
            all = [a for n, a in zip(names, all)
                   if cre.search(n) or cre.search(a)]
    chunkindex = None
    bucket = None
    actionurl = None
    if len(all) < chunksz:
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
            qs = cgi.parse_qs(qsenviron)
            bucket = qs.get('letter', '0')[0].lower()
        keys = list(buckets.keys())
        keys.sort()
        if not bucket or bucket not in buckets:
            bucket = keys[0]
        members = buckets[bucket]
        action = adminurl + '/members?letter=%s' % bucket
        if len(members) <= chunksz:
            form.set_action(action)
        else:
            i, r = divmod(len(members), chunksz)
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
            members = members[chunkindex*chunksz:(chunkindex+1)*chunksz]
            # And set the action URL
            form.set_action(action + '&chunk=%s' % chunkindex)
    # So now members holds all the addresses we're going to display
    allcnt = len(all)
    if bucket:
        membercnt = len(members)
        usertable.AddRow([Center(Italic(_(
            '{allcnt} members total, {membercnt} shown')))])
    else:
        usertable.AddRow([Center(Italic(_(f'{allcnt} members total')))])
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
            url = adminurl + '/members?letter=' + letter + findfrag
            if type(url) is str:
                url = url.encode(Utils.GetCharSet(mlist.preferred_language),
                                 errors='ignore')
            if letter == bucket:
                show = Bold('[%s]' % letter.upper()).Format()
            else:
                show = letter.upper()
            cells.append(Link(url, show).Format())
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
        names = [_f for _f in [mlist.getMemberName(s) for s in members] if _f]
        # Make the name field at least as long as the longest email address
        longest = max([len(s) for s in names + members])
    # Abbreviations for delivery status details
    ds_abbrevs = {MemberAdaptor.UNKNOWN : _('?'),
                  MemberAdaptor.BYUSER  : _('U'),
                  MemberAdaptor.BYADMIN : _('A'),
                  MemberAdaptor.BYBOUNCE: _('B'),
                  }
    # Now populate the rows
    for addr in members:
        qaddr = urllib.parse.quote(addr)
        link = Link(mlist.GetOptionsURL(addr, obscure=1),
                    mlist.getMemberCPAddress(addr))
        fullname = Utils.uncanonstr(mlist.getMemberName(addr),
                                    mlist.preferred_language)
        name = TextBox(qaddr + '_realname', fullname, size=longest).Format()
        cells = [Center(CheckBox(qaddr + '_unsub', 'off', 0).Format()
                        + '<div class="hidden">' + _('unsub') + '</div>'),
                 link.Format() + '<br>' +
                 name +
                 Hidden('user', qaddr).Format(),
                 ]
        # Do the `mod' option
        if mlist.getMemberOption(addr, mm_cfg.Moderate):
            value = 'on'
            checked = 1
        else:
            value = 'off'
            checked = 0
        box = CheckBox('%s_mod' % qaddr, value, checked)
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
                    extra = '[%s]' % ds_abbrevs[status] + extra
            elif mlist.getMemberOption(addr, mm_cfg.OPTINFO[opt]):
                value = 'on'
                checked = 1
            else:
                value = 'off'
                checked = 0
            box = CheckBox('%s_%s' % (qaddr, opt), value, checked)
            cells.append(Center(box.Format() + extra))
        # This code is less efficient than the original which did a has_key on
        # the underlying dictionary attribute.  This version is slower and
        # less memory efficient.  It points to a new MemberAdaptor interface
        # method.
        extra = '<div class="hidden">' + _('digest') + '</div>'
        if addr in mlist.getRegularMemberKeys():
            cells.append(Center(CheckBox(qaddr + '_digest', 'off', 0).Format()
                                + extra))
        else:
            cells.append(Center(CheckBox(qaddr + '_digest', 'on', 1).Format()
                                + extra))
        if mlist.getMemberOption(addr, mm_cfg.OPTINFO['plain']):
            value = 'on'
            checked = 1
        else:
            value = 'off'
            checked = 0
        cells.append(Center(CheckBox(
                            '%s_plain' % qaddr, value, checked).Format()
                            + '<div class="hidden">' + _('plain') + '</div>'))
        # User's preferred language
        langpref = mlist.getMemberLanguage(addr)
        langs = mlist.GetAvailableLanguages()
        langdescs = [_(Utils.GetLanguageDescr(lang)) for lang in langs]
        try:
            selected = langs.index(langpref)
        except ValueError:
            selected = 0
        cells.append(Center(SelectOptions(qaddr + '_language', langs,
                                          langdescs, selected)).Format())
        usertable.AddRow(cells)
    # Add the usertable and a legend
    legend = UnorderedList()
    legend.AddItem(
        _('<b>unsub</b> -- Click on this to unsubscribe the member.'))
    legend.AddItem(
        _(f"""<b>mod</b> -- The user's personal moderation flag.  If this is
        set, postings from them will be moderated, otherwise they will be
        approved."""))
    legend.AddItem(
        _(f"""<b>hide</b> -- Is the member's address concealed on
        the list of subscribers?"""))
    legend.AddItem(_(
        """<b>nomail</b> -- Is delivery to the member disabled?  If so, an
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
            </ul>"""))
    legend.AddItem(
        _(f'''<b>ack</b> -- Does the member get acknowledgements of their
        posts?'''))
    legend.AddItem(
        _(f'''<b>not metoo</b> -- Does the member want to avoid copies of their
        own postings?'''))
    legend.AddItem(
        _(f'''<b>nodupes</b> -- Does the member want to avoid duplicates of the
        same message?'''))
    legend.AddItem(
        _(f'''<b>digest</b> -- Does the member get messages in digests?
        (otherwise, individual messages)'''))
    legend.AddItem(
        _(f'''<b>plain</b> -- If getting digests, does the member get plain
        text digests?  (otherwise, MIME)'''))
    legend.AddItem(_("<b>language</b> -- Language preferred by the user"))
    addlegend = ''
    parsedqs = 0
    qsenviron = os.environ.get('QUERY_STRING')
    if qsenviron:
        qs = cgi.parse_qs(qsenviron).get('legend')
        if qs and type(qs) is list:
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
        url = adminurl + '/members?%sletter=%s&' % (addlegend, bucket)
        footer = _(f'''<p><em>To view more members, click on the appropriate
        range listed below:</em>''')
        chunkmembers = buckets[bucket]
        last = len(chunkmembers)
        for i in range(numchunks):
            if i == chunkindex:
                continue
            start = chunkmembers[i*chunksz]
            end = chunkmembers[min((i+1)*chunksz, last)-1]
            thisurl = url + 'chunk=%d' % i + findfrag
            if type(thisurl) is str:
                thisurl = thisurl.encode(
                                 Utils.GetCharSet(mlist.preferred_language),
                                 errors='ignore')
            link = Link(thisurl, _(f'from {start} to {end}'))
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
                         values=(0,1))
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
    global _
    def safeint(formvar, defaultval=None):
        try:
            return int(cgidata.getfirst(formvar))
        except (ValueError, TypeError):
            return defaultval
    confirmed = 0
    # Handle changes to the list moderator password.  Do this before checking
    # the new admin password, since the latter will force a reauthentication.
    new = cgidata.getfirst('newmodpw', '').strip()
    confirm = cgidata.getfirst('confirmmodpw', '').strip()
    if new or confirm:
        if new == confirm:
            mlist.mod_password = sha_new(new).hexdigest()
            # No re-authentication necessary because the moderator's
            # password doesn't get you into these pages.
        else:
            doc.addError(_('Moderator passwords did not match'))
    # Handle changes to the list poster password.  Do this before checking
    # the new admin password, since the latter will force a reauthentication.
    new = cgidata.getfirst('newpostpw', '').strip()
    confirm = cgidata.getfirst('confirmpostpw', '').strip()
    if new or confirm:
        if new == confirm:
            mlist.post_password = sha_new(new).hexdigest()
            # No re-authentication necessary because the poster's
            # password doesn't get you into these pages.
        else:
            doc.addError(_('Poster passwords did not match'))
    # Handle changes to the list administrator password
    new = cgidata.getfirst('newpw', '').strip()
    confirm = cgidata.getfirst('confirmpw', '').strip()
    if new or confirm:
        if new == confirm:
            mlist.password = sha_new(new).hexdigest()
            # Set new cookie
            print(mlist.MakeCookie(mm_cfg.AuthListAdmin))
        else:
            doc.addError(_('Administrator passwords did not match'))
    # Give the individual gui item a chance to process the form data
    categories = mlist.GetConfigCategories()
    label, gui = categories[category]
    # BAW: We handle the membership page special... for now.
    if category != 'members':
        gui.handleForm(mlist, category, subcat, cgidata, doc)
    # mass subscription, removal processing for members category
    subscribers = ''
    subscribers += cgidata.getfirst('subscribees', '')
    subscribers += cgidata.getfirst('subscribees_upload', '')
    if subscribers:
        entries = [_f for _f in [n.strip() for n in subscribers.splitlines()] if _f]
        send_welcome_msg = safeint('send_welcome_msg_to_this_batch',
                                   mlist.send_welcome_msg)
        send_admin_notif = safeint('send_notifications_to_list_owner',
                                   mlist.admin_notify_mchanges)
        # Default is to subscribe
        subscribe_or_invite = safeint('subscribe_or_invite', 0)
        invitation = cgidata.getfirst('invitation', '')
        digest = mlist.digest_is_default
        if not mlist.digestable:
            digest = 0
        if not mlist.nondigestable:
            digest = 1
        subscribe_errors = []
        subscribe_success = []
        # Now cruise through all the subscribees and do the deed.  BAW: we
        # should limit the number of "Successfully subscribed" status messages
        # we display.  Try uploading a file with 10k names -- it takes a while
        # to render the status page.
        for entry in entries:
            safeentry = Utils.websafe(entry)
            fullname, address = parseaddr(entry)
            # Canonicalize the full name
            fullname = Utils.canonstr(fullname, mlist.preferred_language)
            userdesc = UserDesc(address, fullname,
                                Utils.MakeRandomPassword(),
                                digest, mlist.preferred_language)
            try:
                if subscribe_or_invite:
                    if mlist.isMember(address):
                        raise Errors.MMAlreadyAMember
                    else:
                        mlist.InviteNewMember(userdesc, invitation)
                else:
                    _ = D_
                    whence = _('admin mass sub')
                    _ = i18n._
                    mlist.ApprovedAddMember(userdesc, send_welcome_msg,
                                            send_admin_notif, invitation,
                                            whence=whence)
            except Errors.MMAlreadyAMember:
                subscribe_errors.append((safeentry, _('Already a member')))
            except Errors.MMBadEmailError:
                if userdesc.address == '':
                    subscribe_errors.append((_('&lt;blank line&gt;'),
                                             _('Bad/Invalid email address')))
                else:
                    subscribe_errors.append((safeentry,
                                             _('Bad/Invalid email address')))
            except Errors.MMHostileAddress:
                subscribe_errors.append(
                    (safeentry, _('Hostile address (illegal characters)')))
            except Errors.MembershipIsBanned as pattern:
                subscribe_errors.append(
                    (safeentry, _(f'Banned address (matched {pattern})')))
            else:
                member = Utils.uncanonstr(formataddr((fullname, address)))
                subscribe_success.append(Utils.websafe(member))
        if subscribe_success:
            if subscribe_or_invite:
                doc.AddItem(Header(5, _('Successfully invited:')))
            else:
                doc.AddItem(Header(5, _('Successfully subscribed:')))
            doc.AddItem(UnorderedList(*subscribe_success))
            doc.AddItem('<p>')
        if subscribe_errors:
            if subscribe_or_invite:
                doc.AddItem(Header(5, _('Error inviting:')))
            else:
                doc.AddItem(Header(5, _('Error subscribing:')))
            items = ['%s -- %s' % (x0, x1) for x0, x1 in subscribe_errors]
            doc.AddItem(UnorderedList(*items))
            doc.AddItem('<p>')
    # Unsubscriptions
    removals = ''
    if 'unsubscribees' in cgidata:
        removals += cgidata['unsubscribees'].value
    if 'unsubscribees_upload' in cgidata and \
           cgidata['unsubscribees_upload'].value:
        removals += cgidata['unsubscribees_upload'].value
    if removals:
        names = [_f for _f in [n.strip() for n in removals.splitlines()] if _f]
        send_unsub_notifications = safeint(
            'send_unsub_notifications_to_list_owner',
            mlist.admin_notify_mchanges)
        userack = safeint(
            'send_unsub_ack_to_this_batch',
            mlist.send_goodbye_msg)
        unsubscribe_errors = []
        unsubscribe_success = []
        for addr in names:
            try:
                _ = D_
                whence = _('admin mass unsub')
                _ = i18n._
                mlist.ApprovedDeleteMember(
                    addr, whence=whence,
                    admin_notif=send_unsub_notifications,
                    userack=userack)
                unsubscribe_success.append(Utils.websafe(addr))
            except Errors.NotAMemberError:
                unsubscribe_errors.append(Utils.websafe(addr))
        if unsubscribe_success:
            doc.AddItem(Header(5, _('Successfully Unsubscribed:')))
            doc.AddItem(UnorderedList(*unsubscribe_success))
            doc.AddItem('<p>')
        if unsubscribe_errors:
            doc.AddItem(Header(3, Bold(FontAttr(
                _('Cannot unsubscribe non-members:'),
                color='#ff0000', size='+2')).Format()))
            doc.AddItem(UnorderedList(*unsubscribe_errors))
            doc.AddItem('<p>')
    # Address Changes
    if 'change_from' in cgidata:
        change_from = cgidata.getfirst('change_from', '')
        change_to = cgidata.getfirst('change_to', '')
        schange_from = Utils.websafe(change_from)
        schange_to = Utils.websafe(change_to)
        success = False
        msg = None
        if not (change_from and change_to):
            msg = _('You must provide both current and new addresses.')
        elif change_from == change_to:
            msg = _('Current and new addresses must be different.')
        elif mlist.isMember(change_to):
            # ApprovedChangeMemberAddress will just delete the old address
            # and we don't want that here.
            msg = _(f'{schange_to} is already a list member.')
        else:
            try:
                Utils.ValidateEmail(change_to)
            except (Errors.MMBadEmailError, Errors.MMHostileAddress):
                msg = _(f'{schange_to} is not a valid email address.')
        if msg:
            doc.AddItem(Header(3, msg))
            doc.AddItem('<p>')
            return
        try:
            mlist.ApprovedChangeMemberAddress(change_from, change_to, False)
        except Errors.NotAMemberError:
            msg = _(f'{schange_from} is not a member')
        except Errors.MMAlreadyAMember:
            msg = _(f'{schange_to} is already a member')
        except Errors.MembershipIsBanned as pat:
            spat = Utils.websafe(str(pat))
            msg = _(f'{schange_to} matches banned pattern {spat}')
        else:
            msg = _(f'Address {schange_from} changed to {schange_to}')
            success = True
        doc.AddItem(Header(3, msg))
        lang = mlist.getMemberLanguage(change_to)
        otrans = i18n.get_translation()
        i18n.set_language(lang)
        list_name = mlist.getListAddress()
        text = Utils.wrap(_(f"""The member address {change_from} on the
{list_name} list has been changed to {change_to}.
"""))
        subject = _(f'{list_name} address change notice.')
        i18n.set_translation(otrans)
        if success and cgidata.getfirst('notice_old', '') == 'yes':
            # Send notice to old address.
            msg = Message.UserNotification(change_from,
                mlist.GetOwnerEmail(),
                text=text,
                subject=subject,
                lang=lang
                )
            msg.send(mlist)
            doc.AddItem(Header(3, _(f'Notification sent to {schange_from}.')))
        if success and cgidata.getfirst('notice_new', '') == 'yes':
            # Send notice to new address.
            msg = Message.UserNotification(change_to,
                mlist.GetOwnerEmail(),
                text=text,
                subject=subject,
                lang=lang
                )
            msg.send(mlist)
            doc.AddItem(Header(3, _(f'Notification sent to {schange_to}.')))
        doc.AddItem('<p>')

    # sync operation
    memberlist = ''
    memberlist += cgidata.getvalue('memberlist', '')
    memberlist += cgidata.getvalue('memberlist_upload', '')
    if memberlist:
        # Browsers will convert special characters in the text box to HTML
        # entities. We need to fix those.
        def i_to_c(mo):
            # Convert a matched string of digits to the corresponding unicode.
            return chr(int(mo.group(1)))
        def clean_input(x):
            # Strip leading/trailing whitespace and convert numeric HTML
            # entities.
            return re.sub(r'&#(\d+);', i_to_c, x.strip())
        entries = [_f for _f in [clean_input(n) for n in memberlist.splitlines()] if _f]
        lc_addresses = [parseaddr(x)[1].lower() for x in entries
                        if parseaddr(x)[1]]
        subscribe_errors = []
        subscribe_success = []
        # First we add all the addresses that should be added to the list.
        for entry in entries:
            safeentry = Utils.websafe(entry)
            fullname, address = parseaddr(entry)
            if mlist.isMember(address):
                continue
            # Canonicalize the full name.
            fullname = Utils.canonstr(fullname, mlist.preferred_language)
            userdesc = UserDesc(address, fullname,
                                Utils.MakeRandomPassword(),
                                0, mlist.preferred_language)
            try:
                # Add a member if not yet member.
                    mlist.ApprovedAddMember(userdesc, 0, 0, 0,
                                            whence='admin sync members')
            except Errors.MMBadEmailError:
                if userdesc.address == '':
                    subscribe_errors.append((_('&lt;blank line&gt;'),
                                             _('Bad/Invalid email address')))
                else:
                    subscribe_errors.append((safeentry,
                                             _('Bad/Invalid email address')))
            except Errors.MMHostileAddress:
                subscribe_errors.append(
                    (safeentry, _('Hostile address (illegal characters)')))
            except Errors.MembershipIsBanned as pattern:
                subscribe_errors.append(
                    (safeentry, _(f'Banned address (matched {pattern})')))
            else:
                member = Utils.uncanonstr(formataddr((fullname, address)))
                subscribe_success.append(Utils.websafe(member))

        # Then we remove the addresses not in our list.
        unsubscribe_errors = []
        unsubscribe_success = []

        for entry in mlist.getMembers():
            # If an entry is not found in the uploaded "entries" list, then
            # remove the member.
            if not(entry in lc_addresses):
                try:
                    mlist.ApprovedDeleteMember(entry, 0, 0)
                except Errors.NotAMemberError:
                    # This can happen if the address is illegal (i.e. can't be
                    # parsed by email.utils.parseaddr()) but for legacy
                    # reasons is in the database.  Use a lower level remove to
                    # get rid of this member's entry
                    mlist.removeMember(entry)
                else:
                    unsubscribe_success.append(Utils.websafe(entry))

        if subscribe_success:
            doc.AddItem(Header(5, _('Successfully subscribed:')))
            doc.AddItem(UnorderedList(*subscribe_success))
            doc.AddItem('<p>')
        if subscribe_errors:
            doc.AddItem(Header(5, _('Error subscribing:')))
            items = ['%s -- %s' % (x0, x1) for x0, x1 in subscribe_errors]
            doc.AddItem(UnorderedList(*items))
            doc.AddItem('<p>')
        if unsubscribe_success:
            doc.AddItem(Header(5, _('Successfully Unsubscribed:')))
            doc.AddItem(UnorderedList(*unsubscribe_success))
            doc.AddItem('<p>')

    # See if this was a moderation bit operation
    if 'allmodbit_btn' in cgidata:
        val = safeint('allmodbit_val')
        if val not in (0, 1):
            doc.addError(_('Bad moderation flag value'))
        else:
            for member in mlist.getMembers():
                mlist.setMemberOption(member, mm_cfg.Moderate, val)
    # do the user options for members category
    if 'setmemberopts_btn' in cgidata and 'user' in cgidata:
        user = cgidata['user']
        if type(user) is ListType:
            users = []
            for ui in range(len(user)):
                users.append(urllib.parse.unquote(user[ui].value))
        else:
            users = [urllib.parse.unquote(user.value)]
        errors = []
        removes = []
        for user in users:
            quser = urllib.parse.quote(user)
            if '%s_unsub' % quser in cgidata:
                try:
                    _ = D_
                    whence=_('member mgt page')
                    _ = i18n._
                    mlist.ApprovedDeleteMember(user, whence=whence)
                    removes.append(user)
                except Errors.NotAMemberError:
                    errors.append((user, _('Not subscribed')))
                continue
            if not mlist.isMember(user):
                doc.addError(_(f'Ignoring changes to deleted member: {user}'),
                             tag=_('Warning: '))
                continue
            value = '%s_digest' % quser in cgidata
            try:
                mlist.setMemberOption(user, mm_cfg.Digests, value)
            except (Errors.AlreadyReceivingDigests,
                    Errors.AlreadyReceivingRegularDeliveries,
                    Errors.CantDigestError,
                    Errors.MustDigestError):
                # BAW: Hmm...
                pass

            newname = cgidata.getfirst(quser+'_realname', '')
            newname = Utils.canonstr(newname, mlist.preferred_language)
            mlist.setMemberName(user, newname)

            newlang = cgidata.getfirst(quser+'_language')
            oldlang = mlist.getMemberLanguage(user)
            if Utils.IsLanguage(newlang) and newlang != oldlang:
                mlist.setMemberLanguage(user, newlang)

            moderate = not not cgidata.getfirst(quser+'_mod')
            mlist.setMemberOption(user, mm_cfg.Moderate, moderate)

            # Set the `nomail' flag, but only if the user isn't already
            # disabled (otherwise we might change BYUSER into BYADMIN).
            if '%s_nomail' % quser in cgidata:
                if mlist.getDeliveryStatus(user) == MemberAdaptor.ENABLED:
                    mlist.setDeliveryStatus(user, MemberAdaptor.BYADMIN)
            else:
                mlist.setDeliveryStatus(user, MemberAdaptor.ENABLED)
            for opt in ('hide', 'ack', 'notmetoo', 'nodupes', 'plain'):
                opt_code = mm_cfg.OPTINFO[opt]
                if '%s_%s' % (quser, opt) in cgidata:
                    mlist.setMemberOption(user, opt_code, 1)
                else:
                    mlist.setMemberOption(user, opt_code, 0)
        # Give some feedback on who's been removed
        if removes:
            doc.AddItem(Header(5, _('Successfully Removed:')))
            doc.AddItem(UnorderedList(*removes))
            doc.AddItem('<p>')
        if errors:
            doc.AddItem(Header(5, _("Error Unsubscribing:")))
            items = ['%s -- %s' % (x[0], x[1]) for x in errors]
            doc.AddItem(UnorderedList(*tuple((items))))
            doc.AddItem("<p>")
