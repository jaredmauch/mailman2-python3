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

"""Central logging class for the Mailman system.

This might eventually be replaced by a syslog based logger, hence the name.
"""

from builtins import object
import quopri

from Mailman.Logging.StampedLogger import StampedLogger


# Global, shared logger instance.  All clients should use this object.
_syslog = None


# Don't instantiate except below.
class _Syslog(object):
    def __init__(self):
        self._logfiles = {}

    def __del__(self):
        self.close()

    def write(self, kind, msg, *args, **kws):
        self.write_ex(kind, msg, args, kws)

    # We need this because SMTPDirect tries to pass in a special dict-like
    # object, which is not a concrete dictionary.  This is not allowed by
    # Python's extended call syntax. :(
    def write_ex(self, kind, msg, args=None, kws=None):
        origmsg = msg
        logf = self._logfiles.get(kind)
        if not logf:
            logf = self._logfiles[kind] = StampedLogger(kind)
        try:
            if args:
                msg %= args
            if kws:
                msg %= kws
        # It's really bad if exceptions in the syslogger cause other crashes
        except Exception as e:
            msg = 'Bad format "%s": %s: %s' % (origmsg, repr(e), e)
        try:
            logf.write(msg + '\n')
        except UnicodeError:
            # Python 2.4 may fail to write 8bit (non-ascii) characters
            # Also, if msg is unicode with non-ascii, quopri.encodestring()
            # will throw UnicodeEncodeError, so avoid that.
            if isinstance(msg, str):
                msg = msg.encode('iso-8859-1', 'replace')
            logf.write(quopri.encodestring(msg) + '\n')

    # For the ultimate in convenience
    __call__ = write

    def close(self):
        for kind, logger in list(self._logfiles.items()):
            logger.close()
        self._logfiles.clear()

    def mailman_log(self, ident, msg):
        """Log a message to mailman's logging system."""
        if isinstance(msg, bytes):
            msg = msg.decode('iso-8859-1', 'replace')
        elif not isinstance(msg, str):
            msg = str(msg)
        self.write(ident, msg)

_syslog = _Syslog()

def mailman_log(ident, msg, *args):
    """Log a message to mailman's logging system."""
    if isinstance(msg, bytes):
        msg = msg.decode('iso-8859-1', 'replace')
    elif not isinstance(msg, str):
        msg = str(msg)
    if args:
        msg = msg % args
    _syslog.mailman_log(ident, msg)

# For backward compatibility
syslog = mailman_log
