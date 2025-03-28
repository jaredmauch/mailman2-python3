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

"""Mailman logging classes."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from typing import Dict, List, Optional, Union, Any

from Mailman.Logging.Logger import Logger
from Mailman.Logging.MultiLogger import MultiLogger
from Mailman.Logging.StampedLogger import StampedLogger
from Mailman.Logging.Syslog import Syslog
from Mailman.Logging.Utils import LogStdErr, LogStdOut

# For backwards compatibility
from Mailman.Logging.Utils import *
