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

"""This class mixes in topic feature configuration for mailing lists."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import re
from typing import List, Dict, Tuple, Optional

from Mailman import mm_cfg
from Mailman.i18n import _


class TopicMgr:
    """Mixin class for managing mailing list topics.
    
    This class provides functionality for configuring and managing
    topic-based message filtering in mailing lists.
    
    Attributes:
        topics: List of topic tuples (name, pattern, description, emptyflag)
        topics_enabled: Whether topic filtering is enabled
        topics_bodylines_limit: Number of body lines to check for topics
        topics_userinterest: Dictionary mapping user addresses to their topic interests
    """

    def InitVars(self) -> None:
        """Initialize the topic manager configuration variables.
        
        This method sets up default values for topic configuration
        and user interest tracking.
        """
        # Configurable
        #
        # `topics` is a list of 4-tuples of the following form:
        #
        #     (name, pattern, description, emptyflag)
        #
        # name is a required arbitrary string displayed to the user when they
        # get to select their topics of interest
        #
        # pattern is a required verbose regular expression pattern which is
        # used as IGNORECASE.
        #
        # description is an optional description of what this topic is
        # supposed to match
        #
        # emptyflag is a boolean used internally in the admin interface to
        # signal whether a topic entry is new or not (new ones which do not
        # have a name or pattern are not saved when the submit button is
        # pressed).
        self.topics: List[Tuple[str, str, Optional[str], bool]] = []
        self.topics_enabled: bool = False
        self.topics_bodylines_limit: int = 5
        
        # Non-configurable
        #
        # This is a mapping between user "names" (i.e. addresses) and
        # information about which topics that user is interested in. The
        # values are a list of topic names that the user is interested in,
        # which should match the topic names in self.topics above.
        #
        # If the user has not selected any topics of interest, then the rule
        # is that they will get all messages, and they will not have an entry
        # in this dictionary.
        self.topics_userinterest: Dict[str, List[str]] = {}
