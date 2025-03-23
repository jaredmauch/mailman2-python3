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

"""Mixin class for configuring Usenet gateway.

All the actual functionality is in Handlers/ToUsenet.py for the mail->news
gateway and cron/gate_news for the news->mail gateway.

"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from typing import Optional

from Mailman import mm_cfg
from Mailman.i18n import _


class GatewayManager:
    """Mixin class for managing Usenet gateway configuration.
    
    This class provides configuration variables and methods for managing
    the interface between a mailing list and a Usenet newsgroup.
    
    Attributes:
        nntp_host: The hostname of the NNTP server.
        linked_newsgroup: The name of the linked Usenet newsgroup.
        gateway_to_news: Whether to gateway messages to the newsgroup.
        gateway_to_mail: Whether to gateway messages to the mailing list.
        news_prefix_subject_too: Whether to prefix subjects in news posts.
        news_moderation: Whether the newsgroup is moderated.
    """

    def __init__(self) -> None:
        """Initialize the gateway manager.
        
        This method should not be called directly. Instead, call InitVars()
        which is used by the mixin architecture.
        """
        self.nntp_host: str = ''
        self.linked_newsgroup: str = ''
        self.gateway_to_news: bool = False
        self.gateway_to_mail: bool = False
        self.news_prefix_subject_too: bool = True
        self.news_moderation: bool = False

    def InitVars(self) -> None:
        """Initialize the gateway configuration variables.
        
        This method is called by the mixin architecture to set up
        default values for the gateway configuration.
        """
        # Configurable
        self.nntp_host = mm_cfg.DEFAULT_NNTP_HOST
        self.linked_newsgroup = ''
        self.gateway_to_news = False
        self.gateway_to_mail = False
        self.news_prefix_subject_too = True
        # In patch #401270, this was called newsgroup_is_moderated, but the
        # semantics weren't quite the same.
        self.news_moderation = False
