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


"""Shared mailman errors and messages."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from typing import Optional, List, Any


# exceptions for problems related to opening a list
class MMListError(Exception):
    """Base class for list-related errors."""
    pass


class MMUnknownListError(MMListError):
    """Raised when a list cannot be found."""
    pass


class MMCorruptListDatabaseError(MMListError):
    """Raised when the list database is corrupted."""
    pass


class MMListNotReadyError(MMListError):
    """Raised when a list is not ready for operations."""
    pass


class MMListAlreadyExistsError(MMListError):
    """Raised when attempting to create a list that already exists."""
    pass


class BadListNameError(MMListError):
    """Raised when a list name is invalid."""
    pass


# Membership exceptions
class MMMemberError(Exception):
    """Base class for membership-related errors."""
    pass


class MMBadUserError(MMMemberError):
    """Raised when a user is invalid."""
    pass


class MMAlreadyAMember(MMMemberError):
    """Raised when a user is already a member."""
    pass


class MMAlreadyPending(MMMemberError):
    """Raised when a user already has a pending request."""
    pass


# "New" style membership exceptions (new w/ MM2.1)
class MemberError(Exception):
    """Base class for new-style membership errors."""
    pass


class NotAMemberError(MemberError):
    """Raised when a user is not a member."""
    pass


class AlreadyReceivingDigests(MemberError):
    """Raised when a user is already receiving digests."""
    pass


class AlreadyReceivingRegularDeliveries(MemberError):
    """Raised when a user is already receiving regular deliveries."""
    pass


class CantDigestError(MemberError):
    """Raised when a user cannot receive digests."""
    pass


class MustDigestError(MemberError):
    """Raised when a user must receive digests."""
    pass


class MembershipIsBanned(MemberError):
    """Raised when a user's membership is banned."""
    pass


# Exception hierarchy for various authentication failures
class MMAuthenticationError(Exception):
    """Base class for authentication errors."""
    pass


class MMBadPasswordError(MMAuthenticationError):
    """Raised when a password is incorrect."""
    pass


class MMPasswordsMustMatch(MMAuthenticationError):
    """Raised when passwords do not match."""
    pass


class MMCookieError(MMAuthenticationError):
    """Base class for cookie-related errors."""
    pass


class MMExpiredCookieError(MMCookieError):
    """Raised when a cookie has expired."""
    pass


class MMInvalidCookieError(MMCookieError):
    """Raised when a cookie is invalid."""
    pass


class MMMustDigestError(Exception):
    """Raised when a user must receive digests."""
    pass


class MMCantDigestError(Exception):
    """Raised when a user cannot receive digests."""
    pass


class MMNeedApproval:
    """Raised when an action needs approval."""
    def __init__(self, message: Optional[str] = None) -> None:
        """Initialize the approval request.
        
        Args:
            message: Optional message explaining why approval is needed.
        """
        self.message = message

    def __str__(self) -> str:
        """Return the message or empty string if no message."""
        return self.message or ''


class MMSubscribeNeedsConfirmation(Exception):
    """Raised when a subscription needs confirmation."""
    pass


class MMBadConfirmation:
    """Raised when a confirmation is invalid."""
    def __init__(self, message: Optional[str] = None) -> None:
        """Initialize the bad confirmation.
        
        Args:
            message: Optional message explaining why the confirmation is invalid.
        """
        self.message = message

    def __str__(self) -> str:
        """Return the message or empty string if no message."""
        return self.message or ''


class MMAlreadyDigested(Exception):
    """Raised when a message is already digested."""
    pass


class MMAlreadyUndigested(Exception):
    """Raised when a message is already undigested."""
    pass


# Error messages
MODERATED_LIST_MSG = "Moderated list"
IMPLICIT_DEST_MSG = "Implicit destination"
SUSPICIOUS_HEADER_MSG = "Suspicious header"
FORBIDDEN_SENDER_MSG = "Forbidden sender"


# New style class based exceptions
class MailmanError(Exception):
    """Base class for all Mailman exceptions."""
    pass


class MMLoopingPost(MailmanError):
    """Raised when a post has already been processed by this list."""
    pass


# Exception hierarchy for bad email address errors
class EmailAddressError(MailmanError):
    """Base class for email address validation errors."""
    pass


class MMBadEmailError(EmailAddressError):
    """Raised when an email address is invalid."""
    pass


class MMHostileAddress(EmailAddressError):
    """Raised when an email address contains potentially hostile characters."""
    pass


# Exceptions for admin request database
class LostHeldMessage(MailmanError):
    """Raised when a held message was lost."""
    pass


def _(s: str) -> str:
    """Translation function.
    
    Args:
        s: String to translate.
        
    Returns:
        The translated string.
    """
    return s


# Exceptions for the Handler subsystem
class HandlerError(MailmanError):
    """Base class for all handler errors."""
    pass


class HoldMessage(HandlerError):
    """Base class for all message-being-held short circuits."""

    # funky spelling is necessary to break import loops
    reason = _('For some unknown reason')

    def reason_notice(self) -> str:
        """Return the reason for holding the message.
        
        Returns:
            The reason message.
        """
        return self.reason

    # funky spelling is necessary to break import loops
    rejection = _('Your message was rejected')

    def rejection_notice(self, mlist: Any) -> str:
        """Return the rejection notice.
        
        Args:
            mlist: The mailing list object.
            
        Returns:
            The rejection message.
        """
        return self.rejection


class DiscardMessage(HandlerError):
    """The message can be discarded with no further action."""
    pass


class SomeRecipientsFailed(HandlerError):
    """Delivery to some or all recipients failed."""
    def __init__(self, tempfailures: List[str], permfailures: List[str]) -> None:
        """Initialize the error.
        
        Args:
            tempfailures: List of temporary failures.
            permfailures: List of permanent failures.
        """
        super().__init__()
        self.tempfailures = tempfailures
        self.permfailures = permfailures


# multiple inheritance for backwards compatibility
class LoopError(DiscardMessage, MMLoopingPost):
    """Raised when we've seen this message before."""
    pass


class RejectMessage(HandlerError):
    """The message will be bounced back to the sender."""
    def __init__(self, notice: Optional[str] = None) -> None:
        """Initialize the rejection.
        
        Args:
            notice: Optional notice to include in the rejection.
        """
        if notice is None:
            notice = _('Your message was rejected')
        if notice.endswith('\n\n'):
            pass
        elif notice.endswith('\n'):
            notice += '\n'
        else:
            notice += '\n\n'
        self.__notice = notice

    def notice(self) -> str:
        """Return the rejection notice.
        
        Returns:
            The rejection message.
        """
        return self.__notice


class HostileSubscriptionError(MailmanError):
    """Raised when a cross-subscription attempt was made."""
    # This exception gets raised when an invitee attempts to use the
    # invitation to cross-subscribe to some other mailing list.
