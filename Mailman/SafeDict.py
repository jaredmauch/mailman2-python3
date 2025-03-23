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

"""A `safe' dictionary for string interpolation.

This module provides dictionary classes that handle string interpolation safely,
returning default values for unknown keys rather than raising KeyError exceptions.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from typing import List, Tuple, Dict, Set, Optional, Any, Union
from collections import UserDict

COMMASPACE = ', '


class SafeDict(UserDict):
    """Dictionary which returns a default value for unknown keys.

    This is used in maketext so that editing templates is a bit more robust.
    When a key is not found, it returns a string representation of the key
    wrapped in curly braces, making it safe for string interpolation.

    Attributes:
        data: The underlying dictionary data
    """

    def __getitem__(self, key: Any) -> str:
        """Get an item from the dictionary, returning a safe default if not found.
        
        Args:
            key: The key to look up
            
        Returns:
            The value if found, or a string representation of the key if not found
        """
        try:
            return self.data[key]
        except KeyError:
            if isinstance(key, str):
                return '{(' + key + ')s}'
            else:
                return '<Missing key: ' + str(key) + '>'

    def interpolate(self, template: str) -> str:
        """Interpolate a template string using this dictionary.
        
        Args:
            template: The template string to interpolate
            
        Returns:
            The interpolated string
        """
        return template % self


class MsgSafeDict(SafeDict):
    """A SafeDict subclass that handles email message attributes.
    
    This class provides special handling for email message attributes,
    allowing access to message headers and other attributes through
    special key prefixes.
    
    Attributes:
        __msg: The email message object
        data: The underlying dictionary data
    """

    def __init__(self, msg: Any, dict: Optional[Dict[str, Any]] = None) -> None:
        """Initialize the MsgSafeDict.
        
        Args:
            msg: The email message object
            dict: Optional initial dictionary data
        """
        self.__msg = msg
        super().__init__(dict)

    def __getitem__(self, key: str) -> str:
        """Get an item from the dictionary or message.
        
        Special handling for message attributes:
        - Keys starting with 'msg_' access message headers
        - Keys starting with 'allmsg_' access all values for a header
        
        Args:
            key: The key to look up
            
        Returns:
            The value if found, or a safe default if not found
        """
        if key.startswith('msg_'):
            return self.__msg.get(key[4:], 'n/a')
        elif key.startswith('allmsg_'):
            missing = []
            all = self.__msg.get_all(key[7:], missing)
            if all is missing:
                return 'n/a'
            return COMMASPACE.join(all)
        else:
            return super().__getitem__(key)

    def copy(self) -> Dict[str, Any]:
        """Create a copy of the dictionary with message attributes.
        
        Returns:
            A new dictionary containing both regular and message attributes
        """
        d = self.data.copy()
        for k in self.__msg.keys():
            vals = self.__msg.get_all(k)
            if len(vals) == 1:
                d['msg_' + k.lower()] = vals[0]
            else:
                d['allmsg_' + k.lower()] = COMMASPACE.join(vals)
        return d
}