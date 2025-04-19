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

"""CGI script to handle list subscriptions."""

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

    # Process the subscription request
    if cgidata.getvalue('submit'):
        try:
            # Add the user to the list
            mlist.ApprovedAddMember(emailaddr)
            print('Content-Type: text/plain\n')
            print(_('You have been subscribed to the list'))
            return
        except Errors.MMListError as e:
            print('Content-Type: text/plain\n')
            print(_('An error occurred while subscribing you to the list'))
            syslog('error', 'Error subscribing: %s', e)
            return

    # Display the subscription form
    print('Content-Type: text/html\n')
    print('<html><head><title>%s</title></head><body>' % 
          _('Subscribe to Mailing List'))
    print('<h1>%s</h1>' % _('Subscribe to Mailing List'))
    print('<form method="post" action="subscribe.py">')
    print('<input type="hidden" name="listname" value="%s">' % listname)
    print('<input type="hidden" name="emailaddr" value="%s">' % emailaddr)
    print('<p>%s</p>' % _('Click the button below to subscribe to the list:'))
    print('<input type="submit" name="submit" value="%s">' % _('Subscribe'))
    print('</form>')
    print('</body></html>')

if __name__ == '__main__':
    main()
