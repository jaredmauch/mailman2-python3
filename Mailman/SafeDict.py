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

"""A `safe` dictionary for string interpolation."""

from typing import Any, Dict, Optional, Union
from collections.abc import MutableMapping

COMMASPACE = ', '


class SafeDict(MutableMapping):
    """Dictionary which returns a default value for unknown keys.
    
    This is used in maketext so that editing templates is a bit more robust.
    """
    def __init__(self, data: Optional[Dict[str, Any]] = None) -> None:
        """Initialize with optional dictionary data.
        
        Args:
            data: Initial dictionary data
        """
        self.data = data or {}

    def __getitem__(self, key: str) -> str:
        """Get an item, returning a template placeholder if key not found.
        
        Args:
            key: Dictionary key to look up
            
        Returns:
            The value if found, otherwise a template placeholder
        """
        try:
            return self.data[key]
        except KeyError:
            if isinstance(key, str):
                return f'%({key})s'
            return f'<Missing key: {repr(key)}>'

    def __setitem__(self, key: str, value: Any) -> None:
        """Set a dictionary item.
        
        Args:
            key: The key to set
            value: The value to set
        """
        self.data[key] = value

    def __delitem__(self, key: str) -> None:
        """Delete a dictionary item.
        
        Args:
            key: The key to delete
        """
        del self.data[key]

    def __iter__(self):
        """Iterate over dictionary keys."""
        return iter(self.data)

    def __len__(self) -> int:
        """Get the length of the dictionary."""
        return len(self.data)

    def interpolate(self, template: str) -> str:
        """Interpolate values into a template string.
        
        Args:
            template: The template string
            
        Returns:
            The interpolated string
        """
        return template % self


class MsgSafeDict(SafeDict):
    """SafeDict subclass that can extract values from email message headers."""
    
    def __init__(self, msg: Any, data: Optional[Dict[str, Any]] = None) -> None:
        """Initialize with message and optional data.
        
        Args:
            msg: The email message to extract headers from
            data: Optional initial dictionary data
        """
        self.__msg = msg
        super().__init__(data)

    def __getitem__(self, key: str) -> str:
        """Get an item, checking message headers if not in dictionary.
        
        Args:
            key: The key to look up
            
        Returns:
            The value from the dictionary or message headers
        """
        if key.startswith('msg_'):
            return self.__msg.get(key[4:], 'n/a')
        elif key.startswith('allmsg_'):
            missing = []
            all_values = self.__msg.get_all(key[7:], missing)
            if all_values is missing:
                return 'n/a'
            return COMMASPACE.join(all_values)
        return super().__getitem__(key)

    def copy(self) -> Dict[str, Any]:
        """Create a copy of the dictionary with message headers included.
        
        Returns:
            A new dictionary containing both stored data and message headers
        """
        d = self.data.copy()
        for k in list(self.__msg.keys()):
            vals = self.__msg.get_all(k)
            if len(vals) == 1:
                d[f'msg_{k.lower()}'] = vals[0]
            else:
                d[f'allmsg_{k.lower()}'] = COMMASPACE.join(vals)
        return d
