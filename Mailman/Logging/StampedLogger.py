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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

"""Logger that stamps each line with the time and label."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import sys
import time
from typing import Dict, List, Optional, Union, Any, TextIO

from Mailman.Logging.Logger import Logger


class StampedLogger(Logger):
    """A logger that stamps each line with the time and label.
    
    Attributes:
        label: The label to stamp each line with.
        manual_reprime: Whether to manually reprime the logger.
        _last_line: The last line written.
        _last_time: The time of the last write.
    """

    def __init__(self, filename: str, label: str = '', manual_reprime: bool = True,
                 nofail: bool = True) -> None:
        """Initialize the logger.
        
        Args:
            filename: The name of the log file.
            label: The label to stamp each line with (default: '').
            manual_reprime: Whether to manually reprime the logger (default: True).
            nofail: Whether to fail silently (default: True).
        """
        super().__init__(filename, nofail=nofail)
        self.label = label
        self.manual_reprime = manual_reprime
        self._last_line = ''
        self._last_time = 0

    def write(self, line: str) -> None:
        """Write a line to the log file.
        
        Args:
            line: The line to write.
        """
        if not line:
            return
        now = time.time()
        stamp = time.strftime('%b %d %H:%M:%S %Y', time.localtime(now))
        
        # Prepend the stamp to any line that doesn't begin with it
        if not line.startswith(stamp):
            line = '{0} ({1}): {2}'.format(stamp, self.label, line)
        
        # Don't write the same line twice in a row
        if line != self._last_line or now - self._last_time > 3600:
            super().write(line)
            self._last_line = line
            self._last_time = now

    def writelines(self, lines: List[str]) -> None:
        """Write multiple lines to the log file.
        
        Args:
            lines: The lines to write.
        """
        for line in lines:
            self.write(line)

    def __repr__(self) -> str:
        """Return a string representation of the logger.
        
        Returns:
            A string representation of the logger.
        """
        return '<{0} {1} to {2}>'.format(
            self.__class__.__name__, self.label, self.filename)