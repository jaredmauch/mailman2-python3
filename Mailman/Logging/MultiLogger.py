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

"""Logger that writes to multiple loggers."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from typing import Dict, List, Optional, Union, Any, Sequence

import sys
from Mailman.Logging.Utils import _logexc


class MultiLogger:
    """A logger that writes to multiple loggers.
    
    Attributes:
        loggers: The list of loggers to write to.
    """

    def __init__(self, *loggers: Any) -> None:
        """Initialize the logger.
        
        Args:
            *loggers: Variable length argument list of loggers to write to.
        """
        self.loggers = list(loggers)

    def add_logger(self, logger):
        if logger not in self.loggers:
            self.loggers.append(logger)

    def del_logger(self, logger):
        if logger in self.loggers:
            self.loggers.remove(logger)

    def write(self, msg: str) -> None:
        """Write a message to all loggers.
        
        Args:
            msg: The message to write.
        """
        for logger in self.loggers:
            # you want to be sure that a bug in one logger doesn't prevent
            # logging to all the other loggers
            try:
                logger.write(msg)
            except:
                _logexc(logger, msg)

    def writelines(self, lines: Sequence[str]) -> None:
        """Write multiple lines to all loggers.
        
        Args:
            lines: The lines to write.
        """
        for logger in self.loggers:
            logger.writelines(lines)

    def flush(self) -> None:
        """Flush all loggers."""
        for logger in self.loggers:
            if hasattr(logger, 'flush'):
                # you want to be sure that a bug in one logger doesn't prevent
                # logging to all the other loggers
                try:
                    logger.flush()
                except:
                    _logexc(logger)

    def close(self) -> None:
        """Close all loggers."""
        for logger in self.loggers:
            # you want to be sure that a bug in one logger doesn't prevent
            # logging to all the other loggers
            try:
                if logger != sys.__stderr__ and logger != sys.__stdout__:
                    logger.close()
            except:
                _logexc(logger)

    def reprime(self):
        for logger in self.loggers:
            try:
                logger.reprime()
            except AttributeError:
                pass

    def __repr__(self) -> str:
        """Return a string representation of the logger.
        
        Returns:
            A string representation of the logger.
        """
        return '<{0} to {1}>'.format(
            self.__class__.__name__, 
            ', '.join(str(logger) for logger in self.loggers))
