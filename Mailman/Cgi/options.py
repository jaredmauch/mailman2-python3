# Copyright (C) 1998-2020 by the Free Software Foundation, Inc.
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

"""CGI script to handle user options."""

import os
import sys
import cgi
import time
import urllib.parse
import email.utils
import email.header

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import MailList
from Mailman import Errors
from Mailman import i18n
from Mailman.htmlformat import *
from Mailman.Cgi import Auth
from Mailman.Logging.Syslog import syslog

_ = i18n._

def main():
    # First, do some sanity checking on the form submission
    try:
        cgidata = cgi.FieldStorage()
    except Exception:
        # Something serious went wrong.  We could be under attack.
        syslog('error', 'CGI form submission error')
        print('Content-Type: text/plain\n')
        print(_('An error has occurred'))
        return

    # Get the list name and user email address
    try:
        listname = cgidata.getvalue('listname', '')
        emailaddr = cgidata.getvalue('emailaddr', '')
        if not listname or not emailaddr:
            print('Content-Type: text/plain\n')
            print(_('Missing listname or emailaddr'))
            return
    except Exception:
        print('Content-Type: text/plain\n')
        print(_('Invalid form submission'))
        return

    # Get the list object
    try:
        mlist = MailList.MailList(listname, lock=0)
    except Errors.MMListError as e:
        print('Content-Type: text/plain\n')
        print(_('No such list %(listname)s'))
        return

    # Authenticate the user
    try:
        cgidata = Auth.authenticate(mlist, cgidata)
    except Auth.NotLoggedIn as e:
        Auth.loginpage(mlist, cgidata, e)
        return

    # Get the user's options
    try:
        user_options = mlist.getMemberOptions(emailaddr)
    except Errors.NotAMemberError:
        print('Content-Type: text/plain\n')
        print(_('You are not a member of this list'))
        return

    # Process the form submission
    if cgidata.getvalue('submit'):
        try:
            # Update the user's options
            for option in user_options:
                value = cgidata.getvalue(option, '')
                if value:
                    user_options[option] = value
            mlist.setMemberOptions(emailaddr, user_options)
            print('Content-Type: text/plain\n')
            print(_('Your options have been updated'))
            return
        except Exception as e:
            print('Content-Type: text/plain\n')
            print(_('An error occurred while updating your options'))
            syslog('error', 'Error updating options: %s', e)
            return

    # Display the options form
    print('Content-Type: text/html\n')
    print('<html><head><title>%s</title></head><body>' % 
          _('Mailing List Options'))
    print('<h1>%s</h1>' % _('Mailing List Options'))
    print('<form method="post" action="options.py">')
    print('<input type="hidden" name="listname" value="%s">' % listname)
    print('<input type="hidden" name="emailaddr" value="%s">' % emailaddr)
    print('<table>')
    for option in user_options:
        print('<tr><td>%s</td><td><input type="text" name="%s" value="%s"></td></tr>' %
              (option, option, user_options[option]))
    print('</table>')
    print('<input type="submit" name="submit" value="%s">' % _('Update Options'))
    print('</form>')
    print('</body></html>')

if __name__ == '__main__':
    main()
