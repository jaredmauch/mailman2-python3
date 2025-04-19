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

from typing import Dict, Optional, Any, Union
import quopri
from Mailman.Logging.StampedLogger import StampedLogger

# Global, shared logger instance.  All clients should use this object.
syslog: Optional['_Syslog'] = None

class _Syslog:
    """A central logging system for Mailman.
    
    This class manages multiple log files for different types of messages.
    Each log file is handled by a StampedLogger instance.
    """
    
    def __init__(self) -> None:
        """Initialize the syslog with an empty dictionary of logfiles."""
        self._logfiles: Dict[str, StampedLogger] = {}

    def __del__(self) -> None:
        """Clean up by closing all log files."""
        self.close()

    def write(self, kind: str, msg: str, *args: Any, **kws: Any) -> None:
        """Write a message to the specified log type.
        
        Args:
            kind: The type of log message (determines which log file to use)
            msg: The message format string
            *args: Positional arguments for message formatting
            **kws: Keyword arguments for message formatting
        """
        self.write_ex(kind, msg, args, kws)

    def write_ex(self, kind: str, msg: str, 
                args: Optional[tuple] = None, 
                kws: Optional[dict] = None) -> None:
        """Extended write method that takes explicit args and kwargs.
        
        Args:
            kind: The type of log message
            msg: The message format string
            args: Tuple of positional arguments for message formatting
            kws: Dictionary of keyword arguments for message formatting
        """
        logf = self._logfiles.get(kind)
        if not logf:
            logf = self._logfiles[kind] = StampedLogger(kind)
        
        try:
            if args:
                msg = msg % args
            if kws:
                msg = msg % kws
        except Exception as e:
            msg = f'Bad format "{msg}": {type(e).__name__}: {str(e)}'
        
        try:
            logf.write(f'{msg}\n')
        except UnicodeError:
            # Handle non-ASCII characters by encoding to quoted-printable
            if isinstance(msg, str):
                msg = msg.encode('utf-8', errors='replace')
            encoded_msg = quopri.encodestring(msg).decode('ascii')
            logf.write(f'{encoded_msg}\n')

    # For ultimate convenience, allow direct calling
    __call__ = write

    def close(self) -> None:
        """Close all open log files and clear the logfiles dictionary."""
        for logger in self._logfiles.values():
            logger.close()
        self._logfiles.clear()

# Create the singleton instance
syslog = _Syslog()
