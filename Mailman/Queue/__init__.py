# Copyright (C) 2000-2018 by the Free Software Foundation, Inc.
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

"""Mailman Queue package initialization.

This package contains the queue runners that process various types of messages
in the Mailman system.
"""

import os
import sys

# Add the parent directory to the Python path if it's not already there
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Import the base Runner class first
from Mailman.Queue.Runner import Runner

# Then import the Switchboard
from Mailman.Queue.Switchboard import Switchboard

# Import other runners that don't have dependencies
from Mailman.Queue.OutgoingRunner import OutgoingRunner
from Mailman.Queue.NewsRunner import NewsRunner
from Mailman.Queue.BounceRunner import BounceRunner
from Mailman.Queue.MaildirRunner import MaildirRunner
from Mailman.Queue.RetryRunner import RetryRunner
from Mailman.Queue.CommandRunner import CommandRunner
from Mailman.Queue.ArchRunner import ArchRunner

# Import IncomingRunner and VirginRunner last to avoid circular imports
from Mailman.Queue.IncomingRunner import IncomingRunner
from Mailman.Queue.VirginRunner import VirginRunner

__all__ = [
    'Runner',
    'Switchboard',
    'OutgoingRunner',
    'NewsRunner',
    'BounceRunner',
    'MaildirRunner',
    'RetryRunner',
    'CommandRunner',
    'ArchRunner',
    'IncomingRunner',
    'VirginRunner',
]
