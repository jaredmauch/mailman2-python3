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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301,
# USA.

"""Old style Mailman membership adaptor.

This adaptor gets and sets member information on the MailList object given to
the constructor.  It also equates member keys and lower-cased email addresses,
i.e. KEY is LCE.

This is the adaptor used by default in Mailman 2.1.
"""

import time

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
from Mailman import MemberAdaptor

ISREGULAR = 1
ISDIGEST = 2

# XXX check for bare access to mlist.members, mlist.digest_members,
# mlist.user_options, mlist.passwords, mlist.topics_userinterest

# XXX Fix Errors.MMAlreadyAMember and Errors.NotAMember
# Actually, fix /all/ errors


class OldStyleMemberships(MemberAdaptor.MemberAdaptor):
    def __init__(self, mlist):
        self.__mlist = mlist
        self.archive = mm_cfg.DEFAULT_ARCHIVE  # Initialize archive attribute
        self.digest_send_periodic = mm_cfg.DEFAULT_DIGEST_SEND_PERIODIC  # Initialize digest_send_periodic attribute
        self.archive_private = mm_cfg.DEFAULT_ARCHIVE_PRIVATE  # Initialize archive_private attribute
        self.bounce_you_are_disabled_warnings_interval = mm_cfg.DEFAULT_BOUNCE_YOU_ARE_DISABLED_WARNINGS_INTERVAL  # Initialize bounce warning interval
        self.digest_members = {}  # Initialize digest_members dictionary
        self.digest_is_default = mm_cfg.DEFAULT_DIGEST_IS_DEFAULT  # Initialize digest_is_default attribute
        self.mime_is_default_digest = mm_cfg.DEFAULT_MIME_IS_DEFAULT_DIGEST  # Initialize mime_is_default_digest attribute
        self._pending = {}  # Initialize _pending dictionary for pending operations
        self.autoresponse_graceperiod = 90  # days, default from Autoresponder class

    def GetMailmanHeader(self):
        """Return the standard Mailman header HTML for this list."""
        return self.__mlist.GetMailmanHeader()

    def CheckValues(self):
        """Check that all member values are valid.
        
        This method is called by the admin interface to ensure that all member
        values are valid before displaying them. It should return True if all
        values are valid, False otherwise.
        """
        try:
            # Check that all members have valid email addresses
            for member in self.getMembers():
                if not Utils.ValidateEmail(member):
                    return False
            
            # Check that all members have valid passwords
            for member in self.getMembers():
                if not self.getMemberPassword(member):
                    return False
            
            # Check that all members have valid languages
            for member in self.getMembers():
                lang = self.getMemberLanguage(member)
                if lang not in self.__mlist.available_languages:
                    return False
            
            # Check that all members have valid delivery status
            for member in self.getMembers():
                status = self.getDeliveryStatus(member)
                if status not in (MemberAdaptor.ENABLED, MemberAdaptor.UNKNOWN,
                                MemberAdaptor.BYUSER, MemberAdaptor.BYADMIN,
                                MemberAdaptor.BYBOUNCE):
                    return False
            
            return True
        except Exception as e:
            mailman_log('error', 'Error checking member values: %s', str(e))
            return False

    #
    # Read interface
    #
    def getMembers(self):
        return list(self.__mlist.members.keys()) + list(self.__mlist.digest_members.keys())

    def getRegularMemberKeys(self):
        return list(self.__mlist.members.keys())

    def getDigestMemberKeys(self):
        return list(self.__mlist.digest_members.keys())

    def __get_cp_member(self, member):
        # Handle both string and tuple inputs
        if isinstance(member, tuple):
            _, member = member  # Extract email address from tuple
        lcmember = member.lower()
        missing = []
        val = self.__mlist.members.get(lcmember, missing)
        if val is not missing:
            if type(val) == str:
                return val, ISREGULAR
            else:
                return lcmember, ISREGULAR
        val = self.__mlist.digest_members.get(lcmember, missing)
        if val is not missing:
            if type(val) == str:
                return val, ISDIGEST
            else:
                return lcmember, ISDIGEST
        return None, None

    def isMember(self, member):
        cpaddr, where = self.__get_cp_member(member)
        if cpaddr is not None:
            return 1
        return 0

    def getMemberKey(self, member):
        cpaddr, where = self.__get_cp_member(member)
        if cpaddr is None:
            raise Exception(Errors.NotAMemberError, member)
        return member.lower()

    def getMemberCPAddress(self, member):
        cpaddr, where = self.__get_cp_member(member)
        if cpaddr is None:
            raise Exception(Errors.NotAMemberError, member)
        return cpaddr

    def getMemberCPAddresses(self, members):
        return [self.__get_cp_member(member)[0] for member in members]

    def getMemberPassword(self, member):
        secret = self.__mlist.passwords.get(member.lower())
        if secret is None:
            raise Exception(Errors.NotAMemberError, member)
        return secret

    def authenticateMember(self, member, response):
        secret = self.getMemberPassword(member)
        if secret == response:
            return secret
        return 0

    def __assertIsMember(self, member):
        if not self.isMember(member):
            raise Exception(Errors.NotAMemberError, member)

    def getMemberLanguage(self, member):
        lang = self.__mlist.language.get(
            member.lower(), self.__mlist.preferred_language)
        if lang in self.__mlist.available_languages:
            return lang
        return self.__mlist.preferred_language

    def getMemberOption(self, member, flag):
        self.__assertIsMember(member)
        if flag == mm_cfg.Digests:
            cpaddr, where = self.__get_cp_member(member)
            return where == ISDIGEST
        option = self.__mlist.user_options.get(member.lower(), 0)
        return not not (option & flag)

    def getMemberName(self, member):
        self.__assertIsMember(member)
        name = self.__mlist.usernames.get(member.lower())
        if name is None:
            return ''
        if isinstance(name, bytes):
            try:
                # Try Latin-1 first since that's what we're seeing in the data
                name = name.decode('latin-1', 'replace')
            except UnicodeDecodeError:
                # Fall back to UTF-8 if Latin-1 fails
                name = name.decode('utf-8', 'replace')
        return str(name)

    def getMemberTopics(self, member):
        self.__assertIsMember(member)
        return self.__mlist.topics_userinterest.get(member.lower(), [])

    def getDeliveryStatus(self, member):
        self.__assertIsMember(member)
        return self.__mlist.delivery_status.get(
            member.lower(),
            # Values are tuples, so the default should also be a tuple.  The
            # second item will be ignored.
            (MemberAdaptor.ENABLED, 0))[0]

    def getDeliveryStatusChangeTime(self, member):
        self.__assertIsMember(member)
        return self.__mlist.delivery_status.get(
            member.lower(),
            # Values are tuples, so the default should also be a tuple.  The
            # second item will be ignored.
            (MemberAdaptor.ENABLED, 0))[1]

    def getDeliveryStatusMembers(self, status=(MemberAdaptor.UNKNOWN,
                                               MemberAdaptor.BYUSER,
                                               MemberAdaptor.BYADMIN,
                                               MemberAdaptor.BYBOUNCE)):
        return [member for member in self.getMembers()
                if self.getDeliveryStatus(member) in status]

    def getBouncingMembers(self):
        return [member.lower() for member in list(self.__mlist.bounce_info.keys())]

    def getBounceInfo(self, member):
        self.__assertIsMember(member)
        return self.__mlist.bounce_info.get(member.lower())

    #
    # Write interface
    #
    def addNewMember(self, member, **kws):
        assert self.__mlist.Locked()
        # Make sure this address isn't already a member
        if self.isMember(member):
            raise Exception(Errors.MMAlreadyAMember, member)
        # Parse the keywords
        digest = 0
        password = Utils.MakeRandomPassword()
        language = self.__mlist.preferred_language
        realname = None
        if 'digest' in kws:
            digest = kws['digest']
            del kws['digest']
        if 'password' in kws:
            password = kws['password']
            del kws['password']
        if 'language' in kws:
            language = kws['language']
            del kws['language']
        if 'realname' in kws:
            realname = kws['realname']
            del kws['realname']
        # Assert that no other keywords are present
        if kws:
            raise ValueError(list(kws.keys()))
        # If the localpart has uppercase letters in it, then the value in the
        # members (or digest_members) dict is the case preserved address.
        # Otherwise the value is 0.  Note that the case of the domain part is
        # of course ignored.
        if Utils.LCDomain(member) == member.lower():
            value = 0
        else:
            value = member
        member = member.lower()
        if digest:
            self.__mlist.digest_members[member] = value
        else:
            self.__mlist.members[member] = value
        self.setMemberPassword(member, password)

        self.setMemberLanguage(member, language)
        if realname:
            self.setMemberName(member, realname)
        # Set the member's default set of options
        if self.__mlist.new_member_options:
            self.__mlist.user_options[member] = self.__mlist.new_member_options

    def removeMember(self, member):
        assert self.__mlist.Locked()
        self.__assertIsMember(member)
        # Delete the appropriate entries from the various MailList attributes.
        # Remember that not all of them will have an entry (only those with
        # values different than the default).
        memberkey = member.lower()
        for attr in ('passwords', 'user_options', 'members', 'digest_members',
                     'language',  'topics_userinterest',     'usernames',
                     'bounce_info', 'delivery_status',
                     ):
            dict = getattr(self.__mlist, attr)
            if memberkey in dict:
                del dict[memberkey]

    def changeMemberAddress(self, member, newaddress, nodelete=0):
        assert self.__mlist.Locked()
        # Make sure the old address is a member.  Assertions that the new
        # address is not already a member is done by addNewMember() below.
        self.__assertIsMember(member)
        # Get the old values
        memberkey = member.lower()
        fullname = self.getMemberName(memberkey)
        flags = self.__mlist.user_options.get(memberkey, 0)
        digestsp = self.getMemberOption(memberkey, mm_cfg.Digests)
        password = self.__mlist.passwords.get(memberkey,
                                              Utils.MakeRandomPassword())
        lang = self.getMemberLanguage(memberkey)
        delivery = self.__mlist.delivery_status.get(member.lower(),
                                              (MemberAdaptor.ENABLED,0))
        # First, possibly delete the old member
        if not nodelete:
            self.removeMember(memberkey)
        # Now, add the new member
        self.addNewMember(newaddress, realname=fullname, digest=digestsp,
                          password=password, language=lang)
        # Set the entire options bitfield
        if flags:
            self.__mlist.user_options[newaddress.lower()] = flags
        # If this is a straightforward address change, i.e. nodelete = 0,
        # preserve the delivery status and time if BYUSER or BYADMIN
        if delivery[0] in (MemberAdaptor.BYUSER, MemberAdaptor.BYADMIN)\
          and not nodelete:
            self.__mlist.delivery_status[newaddress.lower()] = delivery

    def setMemberPassword(self, memberkey, password):
        assert self.__mlist.Locked()
        self.__assertIsMember(memberkey)
        self.__mlist.passwords[memberkey.lower()] = password

    def setMemberLanguage(self, memberkey, language):
        assert self.__mlist.Locked()
        self.__assertIsMember(memberkey)
        self.__mlist.language[memberkey.lower()] = language

    def setMemberOption(self, member, flag, value):
        assert self.__mlist.Locked()
        self.__assertIsMember(member)
        memberkey = member.lower()
        # There's one extra gotcha we have to deal with.  If the user is
        # toggling the Digests flag, then we need to move their entry from
        # mlist.members to mlist.digest_members or vice versa.  Blarg.  Do
        # this before the flag setting below in case it fails.
        if flag == mm_cfg.Digests:
            if value:
                # Be sure the list supports digest delivery
                if not self.__mlist.digestable:
                    raise Errors.CantDigestError
                # The user is turning on digest mode
                if memberkey in self.__mlist.digest_members:
                    raise Errors.AlreadyReceivingDigests(member)
                cpuser = self.__mlist.members.get(memberkey)
                if cpuser is None:
                    raise Errors.NotAMemberError(member)
                del self.__mlist.members[memberkey]
                self.__mlist.digest_members[memberkey] = cpuser
                # If we recently turned off digest mode and are now
                # turning it back on, the member may be in one_last_digest.
                # If so, remove it so the member doesn't get a dup of the
                # next digest.
                if memberkey in self.__mlist.one_last_digest:
                    del self.__mlist.one_last_digest[memberkey]
            else:
                # Be sure the list supports regular delivery
                if not self.__mlist.nondigestable:
                    raise Errors.MustDigestError
                # The user is turning off digest mode
                if memberkey in self.__mlist.members:
                    raise Errors.AlreadyReceivingRegularDeliveries(member)
                cpuser = self.__mlist.digest_members.get(memberkey)
                if cpuser is None:
                    raise Errors.NotAMemberError(member)
                del self.__mlist.digest_members[memberkey]
                self.__mlist.members[memberkey] = cpuser
                # When toggling off digest delivery, we want to be sure to set
                # things up so that the user receives one last digest,
                # otherwise they may lose some email
                self.__mlist.one_last_digest[memberkey] = cpuser
            # We don't need to touch user_options because the digest state
            # isn't kept as a bitfield flag.
            return
        # This is a bit kludgey because the semantics are that if the user has
        # no options set (i.e. the value would be 0), then they have no entry
        # in the user_options dict.  We use setdefault() here, and then del
        # the entry below just to make things (questionably) cleaner.
        self.__mlist.user_options.setdefault(memberkey, 0)
        if value:
            self.__mlist.user_options[memberkey] |= flag
        else:
            self.__mlist.user_options[memberkey] &= ~flag
        if not self.__mlist.user_options[memberkey]:
            del self.__mlist.user_options[memberkey]

    def setMemberName(self, member, realname):
        assert self.__mlist.Locked()
        self.__assertIsMember(member)
        if realname is None:
            realname = ''
        if isinstance(realname, bytes):
            try:
                # Try Latin-1 first since that's what we're seeing in the data
                realname = realname.decode('latin-1', 'replace')
            except UnicodeDecodeError:
                # Fall back to UTF-8 if Latin-1 fails
                realname = realname.decode('utf-8', 'replace')
        self.__mlist.usernames[member.lower()] = str(realname)

    def setMemberTopics(self, member, topics):
        assert self.__mlist.Locked()
        self.__assertIsMember(member)
        memberkey = member.lower()
        if topics:
            self.__mlist.topics_userinterest[memberkey] = topics
        # if topics is empty, then delete the entry in this dictionary
        elif memberkey in self.__mlist.topics_userinterest:
            del self.__mlist.topics_userinterest[memberkey]

    def setDeliveryStatus(self, member, status):
        assert status in (MemberAdaptor.ENABLED,  MemberAdaptor.UNKNOWN,
                          MemberAdaptor.BYUSER,   MemberAdaptor.BYADMIN,
                          MemberAdaptor.BYBOUNCE)
        assert self.__mlist.Locked()
        self.__assertIsMember(member)
        member = member.lower()
        if status == MemberAdaptor.ENABLED:
            # Enable by resetting their bounce info.
            self.setBounceInfo(member, None)
        else:
            self.__mlist.delivery_status[member] = (status, time.time())

    def setBounceInfo(self, member, info):
        assert self.__mlist.Locked()
        self.__assertIsMember(member)
        self.__mlist.bounce_info[member.lower()] = info

    def ProcessConfirmation(self, cookie, msg):
        """Process a confirmation request.
        
        Args:
            cookie: The confirmation cookie string
            msg: The message containing the confirmation request
            
        Returns:
            A tuple of (action_type, action_data) where action_type is one of:
            - Pending.SUBSCRIPTION
            - Pending.UNSUBSCRIPTION
            - Pending.HELD_MESSAGE
            And action_data contains the relevant data for that action type.
            
        Raises:
            Errors.MMBadConfirmation: If the confirmation string is invalid
            Errors.MMNeedApproval: If the request needs moderator approval
            Errors.MMAlreadyAMember: If the user is already a member
            Errors.NotAMemberError: If the user is not a member
            Errors.MembershipIsBanned: If the user is banned
            Errors.HostileSubscriptionError: If the subscription is hostile
            Errors.MMBadPasswordError: If the approval password is bad
        """
        from Mailman import Pending
        from Mailman import Utils
        from Mailman import Errors
        
        # Get the pending request
        try:
            action, data = Pending.unpickle(cookie)
        except Exception as e:
            raise Errors.MMBadConfirmation(str(e))
            
        # Check if the request has expired
        if time.time() > data.get('expiration', 0):
            raise Errors.MMBadConfirmation('Confirmation expired')
            
        # Process based on action type
        if action == Pending.SUBSCRIPTION:
            # Check if already a member
            if self.isMember(data['email']):
                raise Errors.MMAlreadyAMember(data['email'])
                
            # Check if banned
            if self.__mlist.isBanned(data['email']):
                raise Errors.MembershipIsBanned(data['email'])
                
            # Add the member
            self.addNewMember(
                data['email'],
                digest=data.get('digest', 0),
                password=data.get('password', Utils.MakeRandomPassword()),
                language=data.get('language', self.__mlist.preferred_language),
                realname=data.get('realname', '')
            )
            
        elif action == Pending.UNSUBSCRIPTION:
            # Check if member
            if not self.isMember(data['email']):
                raise Errors.NotAMemberError(data['email'])
                
            # Remove the member
            self.removeMember(data['email'])
            
        elif action == Pending.HELD_MESSAGE:
            # Process held message
            if data.get('approval_password'):
                if data['approval_password'] != self.__mlist.mod_password:
                    raise Errors.MMBadPasswordError()
                    
            # Forward to moderator if needed
            if data.get('need_approval'):
                self.__mlist.HoldMessage(msg)
                raise Errors.MMNeedApproval()
                
            # Process the message
            if data.get('action') == 'approve':
                self.__mlist.ApproveMessage(msg)
            else:
                self.__mlist.DiscardMessage(msg)
                
        else:
            raise Errors.MMBadConfirmation('Unknown action type')
            
        # Remove the pending request
        Pending.remove(cookie)
        
        return action, data
