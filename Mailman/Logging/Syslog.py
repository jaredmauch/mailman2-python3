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

"""Logger that writes to syslog."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import sys
import syslog
from typing import Dict, List, Optional, Union, Any

class Syslog:
    """A logger that writes to syslog.
    
    Attributes:
        facility: The syslog facility to use.
        priority: The syslog priority to use.
        prefix: The prefix to add to each message.
    """

    def __init__(self, facility: int = syslog.LOG_MAIL,
                 priority: int = syslog.LOG_INFO,
                 prefix: str = '') -> None:
        """Initialize the logger.
        
        Args:
            facility: The syslog facility to use (default: LOG_MAIL).
            priority: The syslog priority to use (default: LOG_INFO).
            prefix: The prefix to add to each message (default: '').
        """
        self.facility = facility
        self.priority = priority
        self.prefix = prefix
        syslog.openlog(prefix, 0, facility)

    def write(self, msg: str) -> None:
        """Write a message to syslog.
        
        Args:
            msg: The message to write.
        """
        if msg and msg[-1] == '\n':
            msg = msg[:-1]
        syslog.syslog(self.priority, msg)

    def writelines(self, lines: List[str]) -> None:
        """Write multiple lines to syslog.
        
        Args:
            lines: The lines to write.
        """
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        """Flush the logger (no-op for syslog)."""
        pass

    def close(self) -> None:
        """Close the logger."""
        syslog.closelog()

    def __repr__(self) -> str:
        """Return a string representation of the logger.
        
        Returns:
            A string representation of the logger.
        """
        return '<{0} {1} to syslog>'.format(
            self.__class__.__name__, self.prefix)