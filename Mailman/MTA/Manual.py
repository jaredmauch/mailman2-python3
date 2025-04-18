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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301,
# USA.

"""Creation/deletion hooks for manual /etc/aliases files."""

import sys
import email.Utils
from cStringIO import StringIO
import os

from Mailman import mm_cfg
from Mailman import Message
from Mailman import Utils
from Mailman.Queue.sbcache import get_switchboard
from Mailman.i18n import _, C_
from Mailman.MTA.Utils import makealiases
from Mailman.Web.Document import Document, Header, Bold, FontSize
from Mailman.Web.Auth import loginpage

try:
    import dns.resolver
    from dns.exception import DNSException
    dns_resolver = True
except ImportError:
    dns_resolver = False


# no-ops for interface compliance
def makelock():
    class Dummy:
        def lock(self):
            pass
        def unlock(self, unconditionally=False):
            pass
    return Dummy()


def clear():
    pass


# nolock argument is ignored, but exists for interface compliance
def create(mlist, cgi=False, nolock=False, quiet=False):
    if mlist is None:
        return
    listname = mlist.internal_name()
    fieldsz = len(listname) + len('-unsubscribe')
    if cgi:
        # If a list is being created via the CGI, the best we can do is send
        # an email message to mailman-owner requesting that the proper aliases
        # be installed.
        sfp = StringIO()
        if not quiet:
            print >> sfp, _("""\
The mailing list `%(listname)s' has been created via the through-the-web
interface.  In order to complete the activation of this mailing list, the
proper /etc/aliases (or equivalent) file must be updated.  The program
`newaliases' may also have to be run.

Here are the entries for the /etc/aliases file:
""")
        outfp = sfp
    else:
        if not quiet:
            print C_("""\
To finish creating your mailing list, you must edit your /etc/aliases (or
equivalent) file by adding the following lines, and possibly running the
`newaliases' program:
""")
        print C_("""\
## %(listname)s mailing list""")
        outfp = sys.stdout
    # Common path
    for k, v in makealiases(listname):
        print >> outfp, k + ':', ((fieldsz - len(k)) * ' '), v
    # If we're using the command line interface, we're done.  For ttw, we need
    # to actually send the message to mailman-owner now.
    if not cgi:
        print >> outfp
        return
    # Send the message to the site -owner so someone can do something about
    # this request.
    siteowner = Utils.get_site_email(extra='owner')
    # Should this be sent in the site list's preferred language?
    msg = Message.UserNotification(
        siteowner, siteowner,
        _('Mailing list creation request for list %(listname)s'),
        sfp.getvalue(), mm_cfg.DEFAULT_SERVER_LANGUAGE)
    msg.send(mlist)


def remove(mlist, cgi=False):
    listname = mlist.internal_name()
    fieldsz = len(listname) + len('-unsubscribe')
    if cgi:
        # If a list is being removed via the CGI, the best we can do is send
        # an email message to mailman-owner requesting that the appropriate
        # aliases be deleted.
        sfp = StringIO()
        print >> sfp, _("""\
The mailing list `%(listname)s' has been removed via the through-the-web
interface.  In order to complete the de-activation of this mailing list, the
appropriate /etc/aliases (or equivalent) file must be updated.  The program
`newaliases' may also have to be run.

Here are the entries in the /etc/aliases file that should be removed:
""")
        outfp = sfp
    else:
        print C_("""
To finish removing your mailing list, you must edit your /etc/aliases (or
equivalent) file by removing the following lines, and possibly running the
`newaliases' program:

## %(listname)s mailing list""")
        outfp = sys.stdout
    # Common path
    for k, v in makealiases(listname):
        print >> outfp, k + ':', ((fieldsz - len(k)) * ' '), v
    # If we're using the command line interface, we're done.  For ttw, we need
    # to actually send the message to mailman-owner now.
    if not cgi:
        print >> outfp
        return
    siteowner = Utils.get_site_email(extra='owner')
    # Should this be sent in the site list's preferred language?
    msg = Message.UserNotification(
        siteowner, siteowner,
        _('Mailing list removal request for list %(listname)s'),
        sfp.getvalue(), mm_cfg.DEFAULT_SERVER_LANGUAGE)
    msg['Date'] = email.Utils.formatdate(localtime=1)
    outq = get_switchboard(mm_cfg.OUTQUEUE_DIR)
    outq.enqueue(msg, recips=[siteowner], nodecorate=1)


def main():
    doc = Document()
    try:
        mlist = MailList.MailList(listname, lock=0)
    except Errors.MMListError as e:
        # Avoid cross-site scripting attacks
        safelistname = Utils.websafe(listname)
        doc.AddItem(Header(2, _("Error")))
        doc.AddItem(Bold(_('No such list <em>%(safelistname)s</em>')))
        # Send this with a 404 status
        print('Status: 404 Not Found')
        print(doc.Format())
        return

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
                   'Authorization failed (manual): list=%s: remote=%s',
                   listname, remote)
        else:
            msg = ''
        loginpage(mlist, 'admin', msg=msg)
        return
