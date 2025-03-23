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

"""Base class for loggers."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import sys
import time
import traceback
from typing import Dict, List, Optional, Union, Any, TextIO, Tuple

from Mailman import mm_cfg
from Mailman.Logging.Utils import _logexc, _logfile_open

# Set this to the encoding to be used for your log file output.  If set to
# None, then it uses your system's default encoding.  Otherwise, it must be an
# encoding string appropriate for codecs.open().
LOG_ENCODING = 'iso-8859-1'


class Logger:
    """Base class for loggers.
    
    Attributes:
        filename: The name of the log file.
        mode: The mode to open the file in.
        encoding: The encoding to use for the file.
        nofail: Whether to fail silently.
        _fileobj: The file object for writing logs.
    """

    def __init__(self, filename: str, mode: str = 'a', encoding: str = 'utf-8',
                 nofail: bool = True) -> None:
        """Initialize the logger.
        
        Args:
            filename: The name of the log file.
            mode: The mode to open the file in (default: 'a').
            encoding: The encoding to use (default: 'utf-8').
            nofail: Whether to fail silently (default: True).
        """
        self.filename = filename
        self.mode = mode
        self.encoding = encoding
        self.nofail = nofail
        self._fileobj = None
        self._open()

    def _open(self) -> None:
        """Open the log file."""
        try:
            self._fileobj = _logfile_open(self.filename, self.mode, self.encoding)
        except IOError:
            if not self.nofail:
                raise
            self._fileobj = None

    def _close(self) -> None:
        """Close the log file."""
        if self._fileobj:
            try:
                self._fileobj.close()
            except IOError:
                if not self.nofail:
                    raise
            self._fileobj = None

    def write(self, msg: str) -> None:
        """Write a message to the log file.
        
        Args:
            msg: The message to write.
        """
        if self._fileobj:
            try:
                self._fileobj.write(msg)
                self._fileobj.flush()
            except IOError:
                if not self.nofail:
                    raise
                # Try to reopen the file
                self._close()
                self._open()
                if self._fileobj:
                    try:
                        self._fileobj.write(msg)
                        self._fileobj.flush()
                    except IOError:
                        if not self.nofail:
                            raise

    def writelines(self, lines: List[str]) -> None:
        """Write multiple lines to the log file.
        
        Args:
            lines: The lines to write.
        """
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        """Flush the log file."""
        if self._fileobj:
            try:
                self._fileobj.flush()
            except IOError:
                if not self.nofail:
                    raise

    def close(self) -> None:
        """Close the log file."""
        self._close()

    def __repr__(self) -> str:
        """Return a string representation of the logger.
        
        Returns:
            A string representation of the logger.
        """
        return '<{0} to {1}>'.format(self.__class__.__name__, self.filename)

    def __del__(self):
        self.close()