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

"""Mailman message handlers.

This package contains the message handlers for Mailman's pipeline architecture.
Each handler module must define a process() function which takes three arguments:
    mlist  - The MailList instance
    msg    - The Message instance
    msgdata - A dictionary of message metadata
"""

from __future__ import absolute_import, print_function, unicode_literals

# Define lazy imports to avoid circular dependencies
def get_handler(name):
    """Get a handler module by name."""
    return __import__('Mailman.Handlers.' + name, fromlist=['Mailman.Handlers'])

# Define handler names for reference
HANDLER_NAMES = [
    'SpamDetect', 'Approve', 'Replybot', 'Moderate', 'Hold', 'MimeDel', 'Scrubber',
    'Emergency', 'Tagger', 'CalcRecips', 'AvoidDuplicates', 'Cleanse', 'CleanseDKIM',
    'CookHeaders', 'ToDigest', 'ToArchive', 'ToUsenet', 'AfterDelivery', 'Acknowledge',
    'WrapMessage', 'ToOutgoing', 'OwnerRecips'
]

# Export handler names
__all__ = HANDLER_NAMES + ['get_handler']
