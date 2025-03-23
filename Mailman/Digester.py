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


"""Mixin class with list-digest handling methods and settings."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import os
from stat import ST_SIZE
import errno
from typing import Dict, Any, Optional

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
from Mailman.Handlers import ToDigest
from Mailman.i18n import _


class Digester:
    """Mixin class for handling mailing list digests.
    
    This class provides methods and settings for managing digest versions
    of mailing list messages.
    
    Attributes:
        digestable: Whether the list supports digests.
        digest_is_default: Whether digest mode is the default.
        mime_is_default_digest: Whether MIME digests are the default.
        digest_size_threshhold: Size threshold for digest creation.
        digest_send_periodic: Whether to send periodic digests.
        next_post_number: Next post number in sequence.
        digest_header: Header template for digests.
        digest_footer: Footer template for digests.
        digest_volume_frequency: How often to start a new volume.
        one_last_digest: Dictionary of members needing one last digest.
        digest_members: Dictionary of digest members.
        next_digest_number: Next digest number in sequence.
        digest_last_sent_at: Timestamp of last digest sent.
    """

    def __init__(self) -> None:
        """Initialize the digester.
        
        This method should not be called directly. Instead, call InitVars()
        which is used by the mixin architecture.
        """
        self.digestable: bool = False
        self.digest_is_default: bool = False
        self.mime_is_default_digest: bool = False
        self.digest_size_threshhold: int = 0
        self.digest_send_periodic: bool = False
        self.next_post_number: int = 1
        self.digest_header: str = ''
        self.digest_footer: str = ''
        self.digest_volume_frequency: int = 0
        self.one_last_digest: Dict[str, Any] = {}
        self.digest_members: Dict[str, Any] = {}
        self.next_digest_number: int = 1
        self.digest_last_sent_at: float = 0.0

    def InitVars(self) -> None:
        """Initialize the digest configuration variables.
        
        This method is called by the mixin architecture to set up
        default values for the digest configuration.
        """
        # Configurable
        self.digestable = mm_cfg.DEFAULT_DIGESTABLE
        self.digest_is_default = mm_cfg.DEFAULT_DIGEST_IS_DEFAULT
        self.mime_is_default_digest = mm_cfg.DEFAULT_MIME_IS_DEFAULT_DIGEST
        self.digest_size_threshhold = mm_cfg.DEFAULT_DIGEST_SIZE_THRESHHOLD
        self.digest_send_periodic = mm_cfg.DEFAULT_DIGEST_SEND_PERIODIC
        self.next_post_number = 1
        self.digest_header = mm_cfg.DEFAULT_DIGEST_HEADER
        self.digest_footer = mm_cfg.DEFAULT_DIGEST_FOOTER
        self.digest_volume_frequency = mm_cfg.DEFAULT_DIGEST_VOLUME_FREQUENCY
        # Non-configurable.
        self.one_last_digest = {}
        self.digest_members = {}
        self.next_digest_number = 1
        self.digest_last_sent_at = 0

    def send_digest_now(self) -> bool:
        """Send any pending digest messages now.
        
        This method checks for pending digests in the digest.mbox file
        and sends them if any exist. The digest volume and issue number
        are handled by Handler.ToDigest.send_digests().
        
        Returns:
            True if a digest was sent, False otherwise.
        """
        digestmbox = os.path.join(self.fullpath(), 'digest.mbox')
        try:
            try:
                mboxfp = None
                # See if there's a digest pending for this mailing list
                if os.stat(digestmbox)[ST_SIZE] > 0:
                    mboxfp = open(digestmbox)
                    ToDigest.send_digests(self, mboxfp)
                    os.unlink(digestmbox)
            finally:
                if mboxfp:
                    mboxfp.close()
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise
            # List has no outstanding digests
            return False
        return True

    def bump_digest_volume(self) -> None:
        """Increment the digest volume number and reset the digest number.
        
        This method is called when starting a new digest volume.
        """
        self.volume += 1
        self.next_digest_number = 1
