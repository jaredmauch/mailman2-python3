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
from builtins import *
from builtins import object
import sys
import os
import codecs
import logging

from Mailman import mm_cfg
from Mailman.Logging.Utils import _logexc

# Set this to the encoding to be used for your log file output.  If set to
# None, then it uses your system's default encoding.  Otherwise, it must be an
# encoding string appropriate for codecs.open().
LOG_ENCODING = 'iso-8859-1'


class Logger(object):
    def __init__(self, category, nofail=1, immediate=0):
        """nofail says to fallback to sys.__stderr__ if write fails to
        category file - a complaint message is emitted, but no exception is
        raised.  Set nofail=0 if you want to handle the error in your code,
        instead.

        immediate=1 says to create the log file on instantiation.
        Otherwise, the file is created only when there are writes pending.
        """
        self.__filename = os.path.join(mm_cfg.LOG_DIR, category)
        self._fp = None
        self.__nofail = nofail
        self.__encoding = LOG_ENCODING or sys.getdefaultencoding()
        if immediate:
            self.__get_f()

    def __del__(self):
        try:
            self.close()
        except:
            pass

    def __repr__(self):
        return '<%s to %s>' % (self.__class__.__name__, repr(self.__filename))

    def __get_f(self):
        if self._fp:
            return self._fp
        else:
            try:
                ou = os.umask(0o07)
                try:
                    try:
                        f = codecs.open(
                            self.__filename, 'ab', self.__encoding, 'replace')
                    except LookupError:
                        f = open(self.__filename, 'ab')
                    self._fp = f
                finally:
                    os.umask(ou)
            except IOError as e:
                if self.__nofail:
                    _logexc(self, e)
                    f = self._fp = sys.__stderr__
                else:
                    raise
            return f

    def flush(self):
        """Flush the file buffer and sync to disk."""
        f = self.__get_f()
        if hasattr(f, 'flush'):
            f.flush()
            try:
                os.fsync(f.fileno())
            except (OSError, IOError):
                # Some file-like objects may not have a fileno() method
                # or may not support fsync
                pass

    def write(self, msg):
        """Write a message to the log file and ensure it's synced to disk."""
        if msg is str:
            msg = str(msg, self.__encoding, 'replace')
        f = self.__get_f()
        try:
            f.write(msg)
            # Flush and sync after each write to ensure logs are persisted
            self.flush()
        except IOError as msg:
            _logexc(self, msg)

    def writelines(self, lines):
        """Write multiple lines to the log file."""
        for l in lines:
            self.write(l)

    def close(self):
        """Close the log file and ensure all data is synced to disk."""
        try:
            if self._fp is not None:
                self.flush()  # Ensure all data is synced before closing
                self._fp.close()
                self._fp = None
        except:
            pass

    def log(self, msg, level=logging.INFO):
        """Log a message at the specified level."""
        if isinstance(msg, bytes):
            msg = msg.decode(self.__encoding, 'replace')
        elif not isinstance(msg, str):
            msg = str(msg)
        self.logger.log(level, msg)
