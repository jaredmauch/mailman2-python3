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

"""Version information for Mailman.

This module defines various version numbers and constants used throughout
the Mailman system. It includes version information for the core system,
data file schemas, and various database schemas.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from typing import Final

# Mailman version
VERSION: Final[str] = '2.1.39'

# Release level constants
ALPHA: Final[int] = 0xa
BETA: Final[int] = 0xb
GAMMA: Final[int] = 0xc
# release candidates
RC: Final[int] = GAMMA
FINAL: Final[int] = 0xf

# Version components
MAJOR_REV: Final[int] = 2
MINOR_REV: Final[int] = 1
MICRO_REV: Final[int] = 39
REL_LEVEL: Final[int] = FINAL
# at most 15 beta releases!
REL_SERIAL: Final[int] = 0

# Combined version as a hex number in the manner of PY_VERSION_HEX
HEX_VERSION: Final[int] = ((MAJOR_REV << 24) | (MINOR_REV << 16) | (MICRO_REV << 8) |
                          (REL_LEVEL << 4)  | (REL_SERIAL << 0))

# Schema version numbers
DATA_FILE_VERSION: Final[int] = 112  # config.pck schema version number
QFILE_SCHEMA_VERSION: Final[int] = 3  # qfile/*.db schema version number
PENDING_FILE_SCHEMA_VERSION: Final[int] = 2  # lists/<listname>/pending.db schema version
REQUESTS_FILE_SCHEMA_VERSION: Final[int] = 1  # lists/<listname>/request.db schema version
