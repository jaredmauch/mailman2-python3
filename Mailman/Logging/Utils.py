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

"""Logging utilities."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import sys
import traceback
from typing import Dict, List, Optional, Union, Any, TextIO


def _logexc(logger=None, msg=''):
    sys.__stderr__.write('Logging error: {}\n'.format(logger))
    traceback.print_exc(file=sys.__stderr__)
    sys.__stderr__.write('Original log message:\n{}\n'.format(msg))


def LogStdErr(*args: Any, **kws: Any) -> None:
    """Write a message to standard error.
    
    Args:
        *args: Variable length argument list to write.
        **kws: Arbitrary keyword arguments.
    """
    sys.stderr.write(' '.join(str(arg) for arg in args))
    sys.stderr.write('\n')
    sys.stderr.flush()


def LogStdOut(*args: Any, **kws: Any) -> None:
    """Write a message to standard output.
    
    Args:
        *args: Variable length argument list to write.
        **kws: Arbitrary keyword arguments.
    """
    sys.stdout.write(' '.join(str(arg) for arg in args))
    sys.stdout.write('\n')
    sys.stdout.flush()


def _logfile_open(filename: str, mode: str = 'a', encoding: str = 'utf-8') -> TextIO:
    """Open a log file with proper encoding.
    
    Args:
        filename: The name of the file to open.
        mode: The mode to open the file in (default: 'a').
        encoding: The encoding to use (default: 'utf-8').
    
    Returns:
        A file object opened with the specified encoding.
    """
    return open(filename, mode=mode, encoding=encoding)


# For backwards compatibility
def maketext(*args: Any, **kws: Any) -> str:
    """Convert arguments to text for logging.
    
    Args:
        *args: Variable length argument list to convert.
        **kws: Arbitrary keyword arguments.
    
    Returns:
        A string representation of the arguments.
    """
    return ' '.join(str(arg) for arg in args)


def LogStdErr(category: str, label: str, manual_reprime: bool = True, 
              tee_to_real_stderr: bool = True) -> None:
    """Log an error message to stderr.
    
    Args:
        category: The category of the error.
        label: The error label.
        manual_reprime: Whether to manually reprime the error.
        tee_to_real_stderr: Whether to also write to the real stderr.
    """
    msg = '{0}: {1}'.format(category, label)
    sys.stderr.write(msg + '\n')
    if tee_to_real_stderr:
        sys.__stderr__.write('Original log message:\n{}\n'.format(msg))
    sys.stderr.flush()
    if manual_reprime:
        sys.stderr = sys.__stderr__