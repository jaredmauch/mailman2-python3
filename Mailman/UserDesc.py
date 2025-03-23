# Copyright (C) 2001-2018 by the Free Software Foundation, Inc.
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

"""User description class/structure, for ApprovedAddMember and friends.

This module provides a UserDesc class that encapsulates user information
for mailing list operations, such as adding new members or updating
existing ones.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from typing import Optional, Any


class UserDesc:
    """A class to hold user description information.
    
    This class is used to store and manipulate user information for mailing
    list operations. It provides methods to combine user descriptions and
    format them for display.
    
    Attributes:
        address: The user's email address
        fullname: The user's full name
        password: The user's password
        digest: Whether the user receives digest format
        language: The user's preferred language
    """

    def __init__(self, address: Optional[str] = None,
                 fullname: Optional[str] = None,
                 password: Optional[str] = None,
                 digest: Optional[bool] = None,
                 lang: Optional[str] = None) -> None:
        """Initialize a UserDesc instance.
        
        Args:
            address: The user's email address
            fullname: The user's full name
            password: The user's password
            digest: Whether the user receives digest format
            lang: The user's preferred language
        """
        if address is not None:
            self.address = address
        if fullname is not None:
            self.fullname = fullname
        if password is not None:
            self.password = password
        if digest is not None:
            self.digest = digest
        if lang is not None:
            self.language = lang

    def __iadd__(self, other: 'UserDesc') -> 'UserDesc':
        """Add another UserDesc's attributes to this one.
        
        This method updates this UserDesc with any non-None attributes
        from the other UserDesc.
        
        Args:
            other: Another UserDesc instance to combine with
            
        Returns:
            This UserDesc instance
        """
        if getattr(other, 'address', None) is not None:
            self.address = other.address
        if getattr(other, 'fullname', None) is not None:
            self.fullname = other.fullname
        if getattr(other, 'password', None) is not None:
            self.password = other.password
        if getattr(other, 'digest', None) is not None:
            self.digest = other.digest
        if getattr(other, 'language', None) is not None:
            self.language = other.language
        return self

    def __repr__(self) -> str:
        """Return a string representation of this UserDesc.
        
        Returns:
            A formatted string containing all user information
        """
        address = getattr(self, 'address', 'n/a')
        fullname = getattr(self, 'fullname', 'n/a')
        password = getattr(self, 'password', 'n/a')
        digest = getattr(self, 'digest', 'n/a')
        if digest == 0:
            digest = 'no'
        elif digest == 1:
            digest = 'yes'
        language = getattr(self, 'language', 'n/a')
        
        # In Python 3, we don't need to encode strings as they are already Unicode
        return '<UserDesc {0} ({1}) [{2}] [digest? {3}] [{4}]>'.format(
            address, fullname, password, digest, language)