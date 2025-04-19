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

"""File-based logger, writes to named category files in mm_cfg.LOG_DIR."""

import sys
import os
import codecs
import cgi
import errno

from Mailman import mm_cfg
from Mailman.Logging.Utils import _logexc
from Mailman.MailList import MailList
from Mailman import Errors
from Mailman import Utils
from Mailman.Gui import Auth
from Mailman.HTML import Document, Header, Bold, FontSize
from Mailman.Logging.Syslog import syslog

# Set this to the encoding to be used for your log file output.  If set to
# None, then it uses your system's default encoding.  Otherwise, it must be an
# encoding string appropriate for codecs.open().
LOG_ENCODING = 'iso-8859-1'


def main():
    doc = Document()
    listname = os.environ.get('PATH_INFO', '').lstrip('/')
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
                   'Authorization failed (logger): list=%s: remote=%s',
                   listname, remote)
        else:
            msg = ''
        Auth.loginpage(mlist, 'admin', msg=msg)
        return

    # Create the list directory with proper permissions
    oldmask = os.umask(0o007)
    try:
        os.makedirs(mlist.fullpath(), mode=0o2775)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
    finally:
        os.umask(oldmask)


class Logger:
    def __init__(self, category, nofail=1, immediate=0):
        """nofail says to fallback to sys.__stderr__ if write fails to
        category file - a complaint message is emitted, but no exception is
        raised.  Set nofail=0 if you want to handle the error in your code,
        instead.

        immediate=1 says to create the log file on instantiation.
        Otherwise, the file is created only when there are writes pending.
        """
        self.__filename = os.path.join(mm_cfg.LOG_DIR, category)
        self.__fp = None
        self.__nofail = nofail
        self.__encoding = LOG_ENCODING or sys.getdefaultencoding()
        if immediate:
            self.__get_f()

    def __del__(self):
        self.close()

    def __repr__(self):
        return '<%s to %s>' % (self.__class__.__name__, repr(self.__filename))

    def __get_f(self):
        if self.__fp:
            return self.__fp
        else:
            try:
                ou = os.umask(0o007)
                try:
                    try:
                        f = codecs.open(
                            self.__filename, 'a+', self.__encoding, 'replace',
                            1)
                    except LookupError:
                        f = open(self.__filename, 'a+', 1)
                    self.__fp = f
                finally:
                    os.umask(ou)
            except IOError as e:
                if self.__nofail:
                    _logexc(self, e)
                    f = self.__fp = sys.__stderr__
                else:
                    raise
            return f

    def flush(self):
        f = self.__get_f()
        if hasattr(f, 'flush'):
            f.flush()

    def write(self, message):
        if isinstance(message, str):
            message = message.encode(self.__encoding, 'replace').decode(self.__encoding)
        f = self.__get_f()
        try:
            f.write(message)
        except IOError as e:
            _logexc(self, e)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def close(self):
        if not self.__fp:
            return
        self.__get_f().close()
        self.__fp = None
