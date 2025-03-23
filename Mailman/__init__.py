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

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import sys
import os

# Add the parent directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import version info
from Mailman.Version import VERSION, RELEASE_DATE

# Import core modules
from Mailman import Utils
from Mailman import Errors
from Mailman import MailList
from Mailman import Message
from Mailman import Pending
from Mailman import LockFile
from Mailman import Post
from Mailman import i18n

# Import default configuration
from Mailman import Defaults

# Import site configuration if it exists
try:
    from Mailman import mm_cfg
except ImportError:
    print('Warning: mm_cfg.py not found. Using default configuration.', file=sys.stderr)
    from Mailman import Defaults as mm_cfg

# Import version information
from Mailman.Version import VERSION, RELEASE_DATE

# Import core modules
from Mailman import Utils
from Mailman import Errors
from Mailman import MailList
from Mailman import Message
from Mailman import Pending
from Mailman import LockFile
from Mailman import Post
from Mailman import i18n

# Import default configuration
from Mailman import Defaults

# Import site configuration if it exists
try:
    from Mailman import mm_cfg
except ImportError:
    print('Warning: mm_cfg.py not found. Using default configuration.', file=sys.stderr)
    from Mailman import Defaults as mm_cfg
