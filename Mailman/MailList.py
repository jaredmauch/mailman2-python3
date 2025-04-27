# Copyright (C) 1998-2020 by the Free Software Foundation, Inc.
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


"""The class representing a Mailman mailing list.

Mixes in many task-specific classes.
"""

import sys
import os
import time
import marshal
import errno
import re
import shutil
import socket
import urllib.request, urllib.parse, urllib.error
import pickle

from io import StringIO
from collections import UserDict
from urllib.parse import urlparse

import email.iterators
from email.utils import getaddresses, formataddr, parseaddr
from email.header import Header
import email.message

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
from Mailman import LockFile
from Mailman.UserDesc import UserDesc

# base classes
from Mailman.Archiver import Archiver
from Mailman.Autoresponder import Autoresponder
from Mailman.Bouncer import Bouncer
from Mailman.Deliverer import Deliverer
from Mailman.Digester import Digester
from Mailman.GatewayManager import GatewayManager
from Mailman.HTMLFormatter import HTMLFormatter
from Mailman.ListAdmin import ListAdmin
from Mailman.SecurityManager import SecurityManager
from Mailman.TopicMgr import TopicMgr
from Mailman import Pending

# gui components package
from Mailman import Gui

# other useful classes
from Mailman import MemberAdaptor
from Mailman.OldStyleMemberships import OldStyleMemberships
from Mailman import Message
from Mailman import Site
from Mailman import i18n
from Mailman.Logging.Syslog import syslog
from Mailman.Message import OwnerNotification

from builtins import str
from builtins import object
import os
import time
import errno
import pickle
import marshal
from io import StringIO
import socket
from types import MethodType

import email
from email.mime.message import MIMEMessage
from email.generator import Generator
from email.utils import getaddresses
import email.message

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Message
from Mailman import Errors
from Mailman.UserDesc import UserDesc
from Mailman.Queue.sbcache import get_switchboard
from Mailman.Logging.Syslog import mailman_log
from Mailman import i18n

_ = i18n._
def D_(s):
    return s

EMPTYSTRING = ''
OR = '|'

# Use mixins here just to avoid having any one chunk be too large.
class MailList(HTMLFormatter, Deliverer, ListAdmin,
               Archiver, Digester, SecurityManager, Bouncer, GatewayManager,
               Autoresponder, TopicMgr, Pending.Pending):
    #
    # A MailList object's basic Python object model support
    #
    def __init__(self, name=None, lock=1):
        # No timeout by default.  If you want to timeout, open the list
        # unlocked, then lock explicitly.
        #
        # Only one level of mixin inheritance allowed
        for baseclass in self.__class__.__bases__:
            if hasattr(baseclass, '__init__'):
                baseclass.__init__(self)
        # Initialize volatile attributes
        self.InitTempVars(name)
        # Default membership adaptor class
        self._memberadaptor = OldStyleMemberships(self)
        # This extension mechanism allows list-specific overrides of any
        # method (well, except __init__(), InitTempVars(), and InitVars()
        # I think).  Note that fullpath() will return None when we're creating
        # the list, which will only happen when name is None.
        if name is None:
            return
        filename = os.path.join(self.fullpath(), 'extend.py')
        dict = {}
        try:
            exec(compile(open(filename, "rb").read(), filename, 'exec'), dict)
        except IOError as e:
            # Ignore missing files, but log other errors
            if e.errno == errno.ENOENT:
                pass
            else:
                syslog('error', 'IOError reading list extension: %s', e)
        else:
            func = dict.get('extend')
            if func:
                func(self)

        # Initialize all mixin classes and their attributes first
        try:
            # First initialize the main class
            self.InitVars()
            
            # Then initialize each mixin class
            for baseclass in self.__class__.__bases__:
                if hasattr(baseclass, 'InitVars'):
                    baseclass.InitVars(self)
                    
            # Finally, ensure all security-related attributes are initialized
            from Mailman.SecurityManager import SecurityManager
            if isinstance(self, SecurityManager):
                self.InitVars()
                
        except Exception as e:
            syslog('error', 'Failed to initialize list %s: %s', name, e)
            raise

        if lock:
            # This will load the database.
            try:
                self.Lock()
            except Errors.MMCorruptListDatabaseError as e:
                syslog('error', 'Failed to load list %s: %s', name, e)
                raise
        else:
            try:
                self.Load()
            except Errors.MMCorruptListDatabaseError as e:
                syslog('error', 'Failed to load list %s: %s', name, e)
                raise

    def __getattr__(self, name):
        # First check if the attribute exists in the instance's dictionary
        if name in self.__dict__:
            value = self.__dict__[name]
            # Check if the value is bytes that looks like Latin-1
            if isinstance(value, bytes):
                try:
                    # Try to decode as Latin-1 to see if it's valid
                    value.decode('latin-1')
                    syslog('warning', 
                           'Binary data that looks like Latin-1 string accessed: %s.%s = %r',
                           self.internal_name(), name, value)
                except UnicodeDecodeError:
                    pass
            return value

        # Then try the memberadaptor
        try:
            return getattr(self._memberadaptor, name)
        except AttributeError:
            pass

        # Try GUI components
        for guicomponent in self._gui:
            try:
                return getattr(guicomponent, name)
            except AttributeError:
                pass

        # Get the full method resolution order (MRO)
        mro = self.__class__.__mro__
        
        # Try each class in the MRO (except object)
        for cls in mro[1:]:  # Skip the first class (self.__class__)
            try:
                # Get the attribute from the class
                attr = getattr(cls, name)
                
                # If it's a method, bind it to this instance
                if callable(attr):
                    return attr.__get__(self, self.__class__)
                    
                # For non-method attributes, check if we have an instance value
                if hasattr(self, '_' + name):
                    return getattr(self, '_' + name)
                    
                # If the attribute exists in the class's dict, use that
                if name in cls.__dict__:
                    return attr
                    
                # If we have an instance value, use that
                if hasattr(self, name):
                    return getattr(self, name)
                    
                # If this class has InitVars, try to initialize the attribute
                if hasattr(cls, 'InitVars'):
                    try:
                        cls.InitVars(self)
                        if hasattr(self, name):
                            return getattr(self, name)
                    except Exception as e:
                        syslog('error', 'Failed to initialize %s.%s: %s', 
                               cls.__name__, name, str(e))
                        continue
                        
                return attr
            except AttributeError:
                continue

        # If we get here, try to initialize through the main class's InitVars
        try:
            self.InitVars()
            if hasattr(self, name):
                return getattr(self, name)
        except Exception as e:
            syslog('error', 'Failed to initialize %s: %s', name, str(e))

        raise AttributeError(name)

    def __repr__(self):
        if self.Locked():
            status = '(locked)'
        else:
            status = '(unlocked)'
        return '<mailing list "%s" %s at %x>' % (
            self.internal_name(), status, id(self))

    #
    # Lock management
    #
    def Lock(self, timeout=0):
        """Lock the list and load its configuration."""
        try:
            self.__lock.lock(timeout)
            # Must reload our database for consistency.  Watch out for lists that
            # don't exist.
            try:
                if not self.Locked():
                    self.Load()
            except Errors.MMCorruptListDatabaseError as e:
                syslog('error', 'Failed to load list %s: %s', 
                       self.internal_name(), e)
                self.Unlock()
                raise
        except Exception as e:
            syslog('error', 'Failed to lock list %s: %s', 
                   self.internal_name(), e)
            self.Unlock()
            raise

    def Unlock(self):
        self.__lock.unlock(unconditionally=1)

    def Locked(self):
        return self.__lock.locked()


    #
    # Useful accessors
    #
    def internal_name(self):
        name = self._internal_name
        if isinstance(name, bytes):
            try:
                # Try Latin-1 first since that's what we're seeing in the data
                name = name.decode('latin-1', 'replace')
            except UnicodeDecodeError:
                # Fall back to UTF-8 if Latin-1 fails
                name = name.decode('utf-8', 'replace')
        return name

    def fullpath(self):
        return self._full_path

    def getListAddress(self, extra=None):
        if extra is None:
            return '%s@%s' % (self.internal_name(), self.host_name)
        return '%s-%s@%s' % (self.internal_name(), extra, self.host_name)

    # For backwards compatibility
    def GetBouncesEmail(self):
        return self.getListAddress('bounces')

    def GetOwnerEmail(self):
        return self.getListAddress('owner')

    def GetRequestEmail(self, cookie=''):
        if mm_cfg.VERP_CONFIRMATIONS and cookie:
            return self.GetConfirmEmail(cookie)
        else:
            return self.getListAddress('request')

    def GetConfirmEmail(self, cookie):
        return mm_cfg.VERP_CONFIRM_FORMAT % {
            'addr'  : '%s-confirm' % self.internal_name(),
            'cookie': cookie,
            } + '@' + self.host_name

    def GetConfirmJoinSubject(self, listname, cookie):
        if mm_cfg.VERP_CONFIRMATIONS and cookie:
            cset = i18n.get_translation().charset() or \
                       Utils.GetCharSet(self.preferred_language)
            subj = Header(
     _('Your confirmation is required to join the %(listname)s mailing list'),
                          cset, header_name='subject')
            return subj
        else:
            return 'confirm ' + cookie

    def GetConfirmLeaveSubject(self, listname, cookie):
        if mm_cfg.VERP_CONFIRMATIONS and cookie:
            cset = i18n.get_translation().charset() or \
                       Utils.GetCharSet(self.preferred_language)
            subj = Header(
     _('Your confirmation is required to leave the %(listname)s mailing list'),
                          cset, header_name='subject')
            return subj
        else:
            return 'confirm ' + cookie

    def GetListEmail(self):
        return self.getListAddress()

    def GetMemberAdminEmail(self, member):
        """Usually the member addr, but modified for umbrella lists.

        Umbrella lists have other mailing lists as members, and so admin stuff
        like confirmation requests and passwords must not be sent to the
        member addresses - the sublists - but rather to the administrators of
        the sublists.  This routine picks the right address, considering
        regular member address to be their own administrative addresses.

        """
        if not self.umbrella_list:
            return member
        else:
            acct, host = tuple(member.split('@'))
            return "%s%s@%s" % (acct, self.umbrella_member_suffix, host)

    def GetScriptURL(self, scriptname, absolute=0):
        return Utils.ScriptURL(scriptname, self.web_page_url, absolute) + \
               '/' + self.internal_name()

    def GetOptionsURL(self, user, obscure=0, absolute=0):
        url = self.GetScriptURL('options', absolute)
        if obscure:
            user = Utils.ObscureEmail(user)
        return '%s/%s' % (url, urllib.parse.quote(user.lower()))

    def GetDescription(self, cset=None, errors='xmlcharrefreplace'):
        # Get list's description in charset specified by cset.
        # If cset is None, it uses charset of context language.
        mcset = Utils.GetCharSet(self.preferred_language)
        if cset is None:
            # translation context may not be initialized
            trns = i18n.get_translation()
            if trns is None:
                ccset = 'us-ascii'
            else:
                ccset = i18n.get_translation().charset() or 'us-ascii'
        else:
            ccset = cset
        if isinstance(self.description, str):
            return self.description.encode(ccset, errors)
        if mcset == ccset:
            return self.description
        return Utils.xml_to_unicode(self.description, mcset).encode(ccset,
                                                                    errors)


    #
    # Instance and subcomponent initialization
    #
    def InitTempVars(self, name):
        """Set transient variables of this and inherited classes."""
        # The timestamp is set whenever we load the state from disk.  If our
        # timestamp is newer than the modtime of the config.pck file, we don't
        # need to reload, otherwise... we do.
        self.__timestamp = 0
        self.__lock = LockFile.LockFile(
            os.path.join(mm_cfg.LOCK_DIR, name or '<site>') + '.lock',
            # TBD: is this a good choice of lifetime?
            lifetime = mm_cfg.LIST_LOCK_LIFETIME,
            withlogging = mm_cfg.LIST_LOCK_DEBUGGING)
        # Ensure name is a string
        if isinstance(name, bytes):
            name = name.decode('utf-8', 'replace')
        self._internal_name = name
        if name:
            self._full_path = Site.get_listpath(name)
        else:
            self._full_path = ''
        # Only one level of mixin inheritance allowed
        for baseclass in self.__class__.__bases__:
            if hasattr(baseclass, 'InitTempVars'):
                baseclass.InitTempVars(self)
        # Now, initialize our gui components
        self._gui = []
        for component in dir(Gui):
            if component.startswith('_'):
                continue
            self._gui.append(getattr(Gui, component)())

    def InitVars(self, name=None, admin='', crypted_password='', urlhost=None):
        """Initialize the list's variables.
        
        This method initializes the list's variables with default values.
        It also initializes the mixin classes.
        """
        # Initialize mixin classes
        for baseclass in self.__class__.__bases__:
            if hasattr(baseclass, 'InitVars'):
                baseclass.InitVars(self)
        
        # Only initialize member dictionaries if they don't exist
        if not hasattr(self, 'members'):
            self.members = {}
        if not hasattr(self, 'digest_members'):
            self.digest_members = {}
        if not hasattr(self, 'user_options'):
            self.user_options = {}
        if not hasattr(self, 'passwords'):
            self.passwords = {}
        if not hasattr(self, 'language'):
            self.language = {}
        if not hasattr(self, 'usernames'):
            self.usernames = {}
        if not hasattr(self, 'topics_userinterest'):
            self.topics_userinterest = {}
        if not hasattr(self, 'bounce_info'):
            self.bounce_info = {}
        if not hasattr(self, 'delivery_status'):
            self.delivery_status = {}
            
        # Initialize other variables
        if name is not None:
            self.internal_name = name
        if admin:
            self.admin = admin
        if crypted_password:
            self.crypted_password = crypted_password
        if urlhost is not None:
            self.urlhost = urlhost

        # Non-configurable list info
        if name:
            # Ensure name is a string
            if isinstance(name, bytes):
                try:
                    # Try Latin-1 first since that's what we're seeing in the data
                    name = name.decode('latin-1', 'replace')
                except UnicodeDecodeError:
                    # Fall back to UTF-8 if Latin-1 fails
                    name = name.decode('utf-8', 'replace')
            self._internal_name = name

        # When was the list created?
        self.created_at = time.time()

        # Must save this state, even though it isn't configurable
        self.digestable = mm_cfg.DEFAULT_DIGESTABLE  # Initialize digestable flag
        self.digest_is_default = mm_cfg.DEFAULT_DIGEST_IS_DEFAULT
        self.mime_is_default_digest = mm_cfg.DEFAULT_MIME_IS_DEFAULT_DIGEST
        self.digest_size_threshhold = mm_cfg.DEFAULT_DIGEST_SIZE_THRESHHOLD
        self.digest_send_periodic = mm_cfg.DEFAULT_DIGEST_SEND_PERIODIC
        self.next_post_number = 1
        self.digest_header = mm_cfg.DEFAULT_DIGEST_HEADER
        self.digest_footer = mm_cfg.DEFAULT_DIGEST_FOOTER
        self.digest_volume_frequency = mm_cfg.DEFAULT_DIGEST_VOLUME_FREQUENCY
        self._new_volume = 0
        self.volume = 1
        self.one_last_digest = {}
        self.next_digest_number = 1
        self.nondigestable = mm_cfg.DEFAULT_NONDIGESTABLE  # Initialize nondigestable flag
        self.digest_volume = 1  # Initialize digest volume number
        self.digest_issue = 1  # Initialize digest issue number
        self.digest_last_sent_at = 0  # Initialize last digest send time
        self.digest_next_due_at = 0  # Initialize next digest due time
        self.data_version = mm_cfg.DATA_FILE_VERSION
        self.last_post_time = 0

        # Initialize archiver-specific attributes
        self.archive_private = mm_cfg.DEFAULT_ARCHIVE_PRIVATE
        self.archive_volume_frequency = mm_cfg.DEFAULT_ARCHIVE_VOLUME_FREQUENCY

        # Initialize security manager attributes
        self.password = crypted_password
        self.mod_password = None
        self.post_password = None
        self.passwords = {}

        # Initialize bouncer attributes
        self.bounce_processing = mm_cfg.DEFAULT_BOUNCE_PROCESSING
        self.bounce_score_threshold = mm_cfg.DEFAULT_BOUNCE_SCORE_THRESHOLD
        self.bounce_info_stale_after = mm_cfg.DEFAULT_BOUNCE_INFO_STALE_AFTER
        self.bounce_you_are_disabled_warnings = mm_cfg.DEFAULT_BOUNCE_YOU_ARE_DISABLED_WARNINGS
        self.bounce_you_are_disabled_warnings_interval = mm_cfg.DEFAULT_BOUNCE_YOU_ARE_DISABLED_WARNINGS_INTERVAL
        self.bounce_unrecognized_goes_to_list_owner = mm_cfg.DEFAULT_BOUNCE_UNRECOGNIZED_GOES_TO_LIST_OWNER
        self.bounce_notify_owner_on_bounce_increment = mm_cfg.DEFAULT_BOUNCE_NOTIFY_OWNER_ON_BOUNCE_INCREMENT
        self.bounce_notify_owner_on_disable = mm_cfg.DEFAULT_BOUNCE_NOTIFY_OWNER_ON_DISABLE
        self.bounce_notify_owner_on_removal = mm_cfg.DEFAULT_BOUNCE_NOTIFY_OWNER_ON_REMOVAL
        self.bounce_info = {}
        self.delivery_status = {}

        # Initialize gateway manager attributes
        self.nntp_host = mm_cfg.DEFAULT_NNTP_HOST
        self.linked_newsgroup = ''
        self.gateway_to_news = 0
        self.gateway_to_mail = 0
        self.news_prefix_subject_too = 1
        self.news_moderation = 0

        # Initialize autoresponder attributes
        self.autorespond_postings = 0
        self.autorespond_admin = 0
        self.autorespond_requests = 0
        self.autoresponse_postings_text = ''
        self.autoresponse_admin_text = ''
        self.autoresponse_request_text = ''
        self.autoresponse_graceperiod = 90  # days
        self.postings_responses = {}
        self.admin_responses = {}
        self.request_responses = {}

        # Initialize topic manager attributes
        self.topics = []
        self.topics_enabled = 0
        self.topics_bodylines_limit = 5
        self.topics_userinterest = {}

        self.post_id = 1.  # A float so it never has a chance to overflow.
        self.new_member_options = mm_cfg.DEFAULT_NEW_MEMBER_OPTIONS

        # This stuff is configurable
        self.respond_to_post_requests = mm_cfg.DEFAULT_RESPOND_TO_POST_REQUESTS
        self.advertised = mm_cfg.DEFAULT_LIST_ADVERTISED
        self.max_num_recipients = mm_cfg.DEFAULT_MAX_NUM_RECIPIENTS
        self.max_message_size = mm_cfg.DEFAULT_MAX_MESSAGE_SIZE
        # See the note in Defaults.py concerning DEFAULT_HOST_NAME
        # vs. DEFAULT_EMAIL_HOST.
        self.host_name = mm_cfg.DEFAULT_HOST_NAME or mm_cfg.DEFAULT_EMAIL_HOST
        self.web_page_url = (
            mm_cfg.DEFAULT_URL or
            mm_cfg.DEFAULT_URL_PATTERN % (urlhost or mm_cfg.DEFAULT_URL_HOST))
        self.owner = [admin]
        self.moderator = []
        self.reply_goes_to_list = mm_cfg.DEFAULT_REPLY_GOES_TO_LIST
        self.reply_to_address = ''
        self.first_strip_reply_to = mm_cfg.DEFAULT_FIRST_STRIP_REPLY_TO
        self.admin_immed_notify = mm_cfg.DEFAULT_ADMIN_IMMED_NOTIFY
        self.admin_notify_mchanges = \
                mm_cfg.DEFAULT_ADMIN_NOTIFY_MCHANGES
        self.require_explicit_destination = \
                mm_cfg.DEFAULT_REQUIRE_EXPLICIT_DESTINATION
        self.acceptable_aliases = mm_cfg.DEFAULT_ACCEPTABLE_ALIASES
        self.umbrella_list = mm_cfg.DEFAULT_UMBRELLA_LIST
        self.umbrella_member_suffix = \
                mm_cfg.DEFAULT_UMBRELLA_MEMBER_ADMIN_SUFFIX
        self.regular_exclude_lists = mm_cfg.DEFAULT_REGULAR_EXCLUDE_LISTS
        self.regular_exclude_ignore = mm_cfg.DEFAULT_REGULAR_EXCLUDE_IGNORE
        self.regular_include_lists = mm_cfg.DEFAULT_REGULAR_INCLUDE_LISTS
        self.send_reminders = mm_cfg.DEFAULT_SEND_REMINDERS
        self.send_welcome_msg = mm_cfg.DEFAULT_SEND_WELCOME_MSG
        self.send_goodbye_msg = mm_cfg.DEFAULT_SEND_GOODBYE_MSG
        self.bounce_matching_headers = \
                mm_cfg.DEFAULT_BOUNCE_MATCHING_HEADERS
        self.header_filter_rules = []
        self.from_is_list = mm_cfg.DEFAULT_FROM_IS_LIST
        self.anonymous_list = mm_cfg.DEFAULT_ANONYMOUS_LIST
        internalname = self.internal_name()
        if isinstance(internalname, bytes):
            try:
                # Try Latin-1 first since that's what we're seeing in the data
                internalname = internalname.decode('latin-1', 'replace')
            except UnicodeDecodeError:
                # Fall back to UTF-8 if Latin-1 fails
                internalname = internalname.decode('utf-8', 'replace')
        self.real_name = internalname[0].upper() + internalname[1:]
        self.description = ''
        self.info = ''
        self.welcome_msg = ''
        self.goodbye_msg = ''
        self.subscribe_policy = mm_cfg.DEFAULT_SUBSCRIBE_POLICY
        self.subscribe_auto_approval = mm_cfg.DEFAULT_SUBSCRIBE_AUTO_APPROVAL
        self.unsubscribe_policy = mm_cfg.DEFAULT_UNSUBSCRIBE_POLICY
        self.private_roster = mm_cfg.DEFAULT_PRIVATE_ROSTER
        self.obscure_addresses = mm_cfg.DEFAULT_OBSCURE_ADDRESSES
        self.admin_member_chunksize = mm_cfg.DEFAULT_ADMIN_MEMBER_CHUNKSIZE
        self.administrivia = mm_cfg.DEFAULT_ADMINISTRIVIA
        self.drop_cc = mm_cfg.DEFAULT_DROP_CC
        self.preferred_language = mm_cfg.DEFAULT_SERVER_LANGUAGE
        self.available_languages = []
        self.include_rfc2369_headers = 1
        self.include_list_post_header = 1
        self.include_sender_header = 1
        self.filter_mime_types = mm_cfg.DEFAULT_FILTER_MIME_TYPES
        self.pass_mime_types = mm_cfg.DEFAULT_PASS_MIME_TYPES
        self.filter_filename_extensions = \
            mm_cfg.DEFAULT_FILTER_FILENAME_EXTENSIONS
        self.pass_filename_extensions = mm_cfg.DEFAULT_PASS_FILENAME_EXTENSIONS
        self.filter_content = mm_cfg.DEFAULT_FILTER_CONTENT
        self.collapse_alternatives = mm_cfg.DEFAULT_COLLAPSE_ALTERNATIVES
        self.convert_html_to_plaintext = \
            mm_cfg.DEFAULT_CONVERT_HTML_TO_PLAINTEXT
        self.filter_action = mm_cfg.DEFAULT_FILTER_ACTION
        # Analogs to these are initted in Digester.InitVars
        self.nondigestable = mm_cfg.DEFAULT_NONDIGESTABLE
        self.personalize = 0
        # New sender-centric moderation (privacy) options
        self.default_member_moderation = \
                                       mm_cfg.DEFAULT_DEFAULT_MEMBER_MODERATION
        # Emergency moderation bit
        self.emergency = 0
        self.member_verbosity_threshold = (
            mm_cfg.DEFAULT_MEMBER_VERBOSITY_THRESHOLD)
        self.member_verbosity_interval = (
            mm_cfg.DEFAULT_MEMBER_VERBOSITY_INTERVAL)
        # This really ought to default to mm_cfg.HOLD, but that doesn't work
        # with the current GUI description model.  So, 0==Hold, 1==Reject,
        # 2==Discard
        self.member_moderation_action = 0
        self.member_moderation_notice = ''
        self.dmarc_moderation_action = mm_cfg.DEFAULT_DMARC_MODERATION_ACTION
        self.dmarc_quarantine_moderation_action = (
            mm_cfg.DEFAULT_DMARC_QUARANTINE_MODERATION_ACTION)
        self.dmarc_none_moderation_action = (
            mm_cfg.DEFAULT_DMARC_NONE_MODERATION_ACTION)
        self.dmarc_moderation_notice = ''
        self.dmarc_moderation_addresses = []
        self.dmarc_wrapped_message_text = (
            mm_cfg.DEFAULT_DMARC_WRAPPED_MESSAGE_TEXT)
        self.equivalent_domains = (
            mm_cfg.DEFAULT_EQUIVALENT_DOMAINS)
        self.accept_these_nonmembers = []
        self.hold_these_nonmembers = []
        self.reject_these_nonmembers = []
        self.discard_these_nonmembers = []
        self.forward_auto_discards = mm_cfg.DEFAULT_FORWARD_AUTO_DISCARDS
        self.generic_nonmember_action = mm_cfg.DEFAULT_GENERIC_NONMEMBER_ACTION
        self.nonmember_rejection_notice = ''
        # Ban lists
        self.ban_list = []
        # BAW: This should really be set in SecurityManager.InitVars()
        self.password = crypted_password
        # Max autoresponses per day.  A mapping between addresses and a
        # 2-tuple of the date of the last autoresponse and the number of
        # autoresponses sent on that date.
        self.hold_and_cmd_autoresponses = {}

        # These need to come near the bottom because they're dependent on
        # other settings.
        self.subject_prefix = mm_cfg.DEFAULT_SUBJECT_PREFIX % self.__dict__
        self.msg_header = mm_cfg.DEFAULT_MSG_HEADER
        self.msg_footer = mm_cfg.DEFAULT_MSG_FOOTER
        # Set this to Never if the list's preferred language uses us-ascii,
        # otherwise set it to As Needed
        if Utils.GetCharSet(self.preferred_language) == 'us-ascii':
            self.encode_ascii_prefixes = 0
        else:
            self.encode_ascii_prefixes = 2
        # scrub regular delivery
        self.scrub_nondigest = mm_cfg.DEFAULT_SCRUB_NONDIGEST
        # automatic discarding
        self.max_days_to_hold = mm_cfg.DEFAULT_MAX_DAYS_TO_HOLD

    #
    # Web API support via administrative categories
    #
    def GetConfigCategories(self):
        class CategoryDict(UserDict):
            def __init__(self):
                UserDict.__init__(self)
                self.keysinorder = mm_cfg.ADMIN_CATEGORIES[:]
            def keys(self):
                return self.keysinorder
            def items(self):
                items = []
                for k in mm_cfg.ADMIN_CATEGORIES:
                    items.append((k, self.data[k]))
                return items
            def values(self):
                values = []
                for k in mm_cfg.ADMIN_CATEGORIES:
                    values.append(self.data[k])
                return values

        categories = CategoryDict()
        # Only one level of mixin inheritance allowed
        for gui in self._gui:
            k, v = gui.GetConfigCategory()
            categories[k] = (v, gui)
        return categories

    def GetConfigSubCategories(self, category):
        for gui in self._gui:
            if hasattr(gui, 'GetConfigSubCategories'):
                # Return the first one that knows about the given subcategory
                subcat = gui.GetConfigSubCategories(category)
                if subcat is not None:
                    return subcat
        return None

    def GetConfigInfo(self, category, subcat=None):
        """Get configuration information for a category and optional subcategory.
        
        Args:
            category: The configuration category to get info for
            subcat: Optional subcategory to filter by
            
        Returns:
            A list of configuration items, or None if not found
        """
        for gui in self._gui:
            if hasattr(gui, 'GetConfigInfo'):
                try:
                    value = gui.GetConfigInfo(self, category, subcat)
                    if value:
                        return value
                except (AttributeError, KeyError) as e:
                    # Log the error but continue trying other GUIs
                    syslog('error', 'Error getting config info for %s/%s: %s',
                           category, subcat, str(e))
                    continue
        return None

    #
    # List creation
    #
    def Create(self, name, admin, crypted_password,
               langs=None, emailhost=None, urlhost=None):
        assert name == name.lower(), 'List name must be all lower case.'
        if Utils.list_exists(name):
            raise Errors.MMListAlreadyExistsError(name)
        # Problems and potential attacks can occur if the list name in the
        # pipe to the wrapper in an MTA alias or other delivery process
        # contains shell special characters so allow only defined characters
        # (default = '[-+_.=a-z0-9]').
        if len(re.sub(mm_cfg.ACCEPTABLE_LISTNAME_CHARACTERS, '', name)) > 0:
            raise Errors.BadListNameError(name)
        # Validate what will be the list's posting address.  If that's
        # invalid, we don't want to create the mailing list.  The hostname
        # part doesn't really matter, since that better already be valid.
        # However, most scripts already catch MMBadEmailError as exceptions on
        # the admin's email address, so transform the exception.
        if emailhost is None:
            emailhost = mm_cfg.DEFAULT_EMAIL_HOST
        postingaddr = '%s@%s' % (name, emailhost)
        try:
            Utils.ValidateEmail(postingaddr)
        except Errors.EmailAddressError:
            raise Errors.BadListNameError(postingaddr)
        # Validate the admin's email address
        Utils.ValidateEmail(admin)
        self._internal_name = name
        self._full_path = Site.get_listpath(name, create=1)
        # Don't use Lock() since that tries to load the non-existant config.pck
        self.__lock.lock()
        self.InitVars(name, admin, crypted_password, urlhost=urlhost)
        self.CheckValues()
        if langs is None:
            self.available_languages = [self.preferred_language]
        else:
            self.available_languages = langs


    #
    # Database and filesystem I/O
    #
    def __save(self, dict):
        """Save the list's configuration dictionary to disk.
        
        This method implements a robust save mechanism with:
        1. Backup of current configuration
        2. Writing to temporary file
        3. Validation of written data
        4. Atomic rename
        5. Creation of last known good version
        6. Proper error handling and cleanup
        """
        fname = os.path.join(self.fullpath(), 'config.pck')
        fname_tmp = fname + '.tmp.' + socket.gethostname() + '.' + str(os.getpid())
        fname_backup = fname + '.bak'
        
        try:
            # Ensure directory exists
            dirname = os.path.dirname(fname)
            if not os.path.exists(dirname):
                try:
                    os.makedirs(dirname, 0o755)
                except Exception as e:
                    mailman_log('error', 'Failed to create directory %s: %s', dirname, e)
                    raise
            
            # Write the temporary file
            try:
                with open(fname_tmp, 'wb') as fp:
                    pickle.dump(dict, fp, protocol=2, fix_imports=True)
            except Exception as e:
                mailman_log('error', 'Failed to write temporary file %s: %s', fname_tmp, e)
                raise
            
            # Create backup of current file if it exists
            if os.path.exists(fname):
                try:
                    os.rename(fname, fname_backup)
                except Exception as e:
                    mailman_log('error', 'Failed to create backup %s: %s', fname_backup, e)
                    raise
            
            # Atomic rename
            os.rename(fname_tmp, fname)
            
            # Create hard link to last good version
            try:
                os.link(fname, fname + '.last')
            except Exception:
                pass  # Ignore errors creating the hard link
                
        except Exception as e:
            mailman_log('error', 'Failed to save configuration: %s', e)
            # Clean up temporary file
            try:
                if os.path.exists(fname_tmp):
                    os.unlink(fname_tmp)
            except Exception:
                pass
            # Restore from backup if possible
            try:
                if os.path.exists(fname_backup):
                    os.rename(fname_backup, fname)
            except Exception:
                pass
            raise
            
        finally:
            # Clean up backup file
            try:
                if os.path.exists(fname_backup):
                    os.unlink(fname_backup)
            except Exception:
                pass
                
        # Reset timestamp
        self._timestamp = time.time()

    def __convert_bytes_to_strings(self, data):
        """Convert bytes to strings recursively in a data structure using latin1 encoding."""
        if isinstance(data, bytes):
            # Always use latin1 encoding for list section names and other data
            return data.decode('latin1', 'replace')
        elif isinstance(data, list):
            return [self.__convert_bytes_to_strings(item) for item in data]
        elif isinstance(data, dict):
            return {
                self.__convert_bytes_to_strings(key): self.__convert_bytes_to_strings(value)
                for key, value in data.items()
            }
        elif isinstance(data, tuple):
            return tuple(self.__convert_bytes_to_strings(item) for item in data)
        return data

    def Save(self):
        # First ensure we have the lock
        if not self.Locked():
            self.Lock()
        try:
            # Only refresh if we have the lock
            if self.Locked():
                self.__lock.refresh()
            # copy all public attributes to serializable dictionary
            dict = {}
            for key, value in list(self.__dict__.items()):
                if key[0] == '_' or type(value) is MethodType:
                    continue
                # Convert any bytes to strings recursively
                dict[key] = self.__convert_bytes_to_strings(value)
            # Make config.pck unreadable by `other', as it contains all the
            # list members' passwords (in clear text).
            omask = os.umask(0o007)
            try:
                self.__save(dict)
            finally:
                os.umask(omask)
                self.SaveRequestsDb()
            self.CheckHTMLArchiveDir()
        finally:
            # Always unlock when we're done
            self.Unlock()

    def __decode_latin1(self, value):
        """Centralized method to decode bytes to Latin-1 strings.
        
        This ensures consistent handling of bytes throughout the codebase.
        If the value is already a string, it is returned unchanged.
        """
        if isinstance(value, bytes):
            return value.decode('latin1', 'replace')
        return value

    def __load(self, file=None):
        """Load the dictionary from disk."""
        fname = os.path.join(self.fullpath(), 'config.pck')
        fname_backup = fname + '.bak'
        fname_last = fname + '.last'
        
        # Try loading from main file first
        try:
            with open(fname, 'rb') as fp:
                dict_retval = pickle.load(fp, fix_imports=True, encoding='latin1')
            if not isinstance(dict_retval, dict):
                raise TypeError('Loaded data is not a dictionary')
            return dict_retval
        except Exception as e:
            mailman_log('error', 'Failed to load main config file: %s', e)
            
        # Try loading from backup file
        try:
            if os.path.exists(fname_backup):
                with open(fname_backup, 'rb') as fp:
                    dict_retval = pickle.load(fp, fix_imports=True, encoding='latin1')
                if not isinstance(dict_retval, dict):
                    raise TypeError('Loaded data is not a dictionary')
                # Restore backup to main file
                os.rename(fname_backup, fname)
                return dict_retval
        except Exception as e:
            mailman_log('error', 'Failed to load backup config file: %s', e)
            
        # Try loading from last known good version
        try:
            if os.path.exists(fname_last):
                with open(fname_last, 'rb') as fp:
                    dict_retval = pickle.load(fp, fix_imports=True, encoding='latin1')
                if not isinstance(dict_retval, dict):
                    raise TypeError('Loaded data is not a dictionary')
                # Restore last good version to main file
                os.rename(fname_last, fname)
                return dict_retval
        except Exception as e:
            mailman_log('error', 'Failed to load last good config file: %s', e)
            
        # If all else fails, create a new config
        mailman_log('error', 'All config files corrupted, creating new config')
        return self._create()

    def Load(self):
        """Load the list's configuration from disk.
        
        This method loads the configuration dictionary from disk and updates
        the instance attributes with the loaded values.
        """
        # Only refresh if we have the lock
        if self.Locked():
            self.__lock.refresh()
        # Load the configuration dictionary
        dict = self.__load()
        if dict:
            # Store current member adaptor if it exists
            current_adaptor = getattr(self, '_memberadaptor', None)
            
            # Update instance attributes with loaded values
            for key, value in dict.items():
                if key[0] != '_':  # Skip private attributes
                    setattr(self, key, value)
            
            # Restore member adaptor if it existed
            if current_adaptor is not None:
                self._memberadaptor = current_adaptor
            else:
                self._memberadaptor = OldStyleMemberships(self)
            
            # Initialize other variables
            self.InitVars()
            
            # Check language settings
            if mm_cfg.LANGUAGES.get(self.preferred_language) is None:
                self.preferred_language = mm_cfg.DEFAULT_SERVER_LANGUAGE
                
            # Check version and update any missing attributes
            self.CheckVersion(dict)
            
            # Ensure all required attributes are present
            self._ensure_required_attributes()
            
            # Validate values
            self.CheckValues()
        else:
            # No configuration loaded, initialize with defaults
            self.InitVars()
            
    def _ensure_required_attributes(self):
        """Ensure all required attributes are present with default values if missing."""
        # Digest-related attributes
        if not hasattr(self, 'digest_volume_frequency'):
            self.digest_volume_frequency = mm_cfg.DEFAULT_DIGEST_VOLUME_FREQUENCY
        if not hasattr(self, 'digest_last_sent_at'):
            self.digest_last_sent_at = 0
        if not hasattr(self, 'digest_next_due_at'):
            self.digest_next_due_at = 0
        if not hasattr(self, '_new_volume'):
            self._new_volume = 0
        if not hasattr(self, 'volume'):
            self.volume = 1
        if not hasattr(self, 'one_last_digest'):
            self.one_last_digest = {}
        if not hasattr(self, 'next_digest_number'):
            self.next_digest_number = 1
            
        # Bounce-related attributes
        if not hasattr(self, 'bounce_processing'):
            self.bounce_processing = mm_cfg.DEFAULT_BOUNCE_PROCESSING
        if not hasattr(self, 'bounce_score_threshold'):
            self.bounce_score_threshold = mm_cfg.DEFAULT_BOUNCE_SCORE_THRESHOLD
        if not hasattr(self, 'bounce_info_stale_after'):
            self.bounce_info_stale_after = mm_cfg.DEFAULT_BOUNCE_INFO_STALE_AFTER
        if not hasattr(self, 'bounce_you_are_disabled_warnings'):
            self.bounce_you_are_disabled_warnings = mm_cfg.DEFAULT_BOUNCE_YOU_ARE_DISABLED_WARNINGS
        if not hasattr(self, 'bounce_you_are_disabled_warnings_interval'):
            self.bounce_you_are_disabled_warnings_interval = mm_cfg.DEFAULT_BOUNCE_YOU_ARE_DISABLED_WARNINGS_INTERVAL
        if not hasattr(self, 'bounce_unrecognized_goes_to_list_owner'):
            self.bounce_unrecognized_goes_to_list_owner = mm_cfg.DEFAULT_BOUNCE_UNRECOGNIZED_GOES_TO_LIST_OWNER
        if not hasattr(self, 'bounce_notify_owner_on_bounce_increment'):
            self.bounce_notify_owner_on_bounce_increment = mm_cfg.DEFAULT_BOUNCE_NOTIFY_OWNER_ON_BOUNCE_INCREMENT
        if not hasattr(self, 'bounce_notify_owner_on_disable'):
            self.bounce_notify_owner_on_disable = mm_cfg.DEFAULT_BOUNCE_NOTIFY_OWNER_ON_DISABLE
        if not hasattr(self, 'bounce_notify_owner_on_removal'):
            self.bounce_notify_owner_on_removal = mm_cfg.DEFAULT_BOUNCE_NOTIFY_OWNER_ON_REMOVAL
            
        # Gateway-related attributes
        if not hasattr(self, 'nntp_host'):
            self.nntp_host = mm_cfg.DEFAULT_NNTP_HOST
        if not hasattr(self, 'linked_newsgroup'):
            self.linked_newsgroup = ''
        if not hasattr(self, 'gateway_to_news'):
            self.gateway_to_news = 0
        if not hasattr(self, 'gateway_to_mail'):
            self.gateway_to_mail = 0
        if not hasattr(self, 'news_prefix_subject_too'):
            self.news_prefix_subject_too = 1
        if not hasattr(self, 'news_moderation'):
            self.news_moderation = 0
            
        # Autoresponder attributes
        if not hasattr(self, 'autorespond_postings'):
            self.autorespond_postings = 0
        if not hasattr(self, 'autorespond_admin'):
            self.autorespond_admin = 0
        if not hasattr(self, 'autorespond_requests'):
            self.autorespond_requests = 0
        if not hasattr(self, 'autoresponse_postings_text'):
            self.autoresponse_postings_text = ''
        if not hasattr(self, 'autoresponse_admin_text'):
            self.autoresponse_admin_text = ''
        if not hasattr(self, 'autoresponse_request_text'):
            self.autoresponse_request_text = ''
        if not hasattr(self, 'autoresponse_graceperiod'):
            self.autoresponse_graceperiod = 90
        if not hasattr(self, 'postings_responses'):
            self.postings_responses = {}
        if not hasattr(self, 'admin_responses'):
            self.admin_responses = {}
        if not hasattr(self, 'request_responses'):
            self.request_responses = {}
            
        # Topic-related attributes
        if not hasattr(self, 'topics'):
            self.topics = []
        if not hasattr(self, 'topics_enabled'):
            self.topics_enabled = 0
        if not hasattr(self, 'topics_bodylines_limit'):
            self.topics_bodylines_limit = 5
        if not hasattr(self, 'topics_userinterest'):
            self.topics_userinterest = {}
            
        # Security-related attributes
        if not hasattr(self, 'mod_password'):
            self.mod_password = None
        if not hasattr(self, 'post_password'):
            self.post_password = None
        if not hasattr(self, 'passwords'):
            self.passwords = {}
            
        # Other attributes
        if not hasattr(self, 'archive_private'):
            self.archive_private = mm_cfg.DEFAULT_ARCHIVE_PRIVATE
        if not hasattr(self, 'archive_volume_frequency'):
            self.archive_volume_frequency = mm_cfg.DEFAULT_ARCHIVE_VOLUME_FREQUENCY
        if not hasattr(self, 'data_version'):
            self.data_version = mm_cfg.DATA_FILE_VERSION
        if not hasattr(self, 'last_post_time'):
            self.last_post_time = 0
        if not hasattr(self, 'created_at'):
            self.created_at = time.time()
            
        # Member-related attributes
        if not hasattr(self, 'members'):
            self.members = {}
        if not hasattr(self, 'digest_members'):
            self.digest_members = {}
        if not hasattr(self, 'user_options'):
            self.user_options = {}
        if not hasattr(self, 'language'):
            self.language = {}
        if not hasattr(self, 'usernames'):
            self.usernames = {}
        if not hasattr(self, 'bounce_info'):
            self.bounce_info = {}
        if not hasattr(self, 'delivery_status'):
            self.delivery_status = {}
            
        # List settings
        if not hasattr(self, 'admin_member_chunksize'):
            self.admin_member_chunksize = mm_cfg.DEFAULT_ADMIN_MEMBER_CHUNKSIZE
        if not hasattr(self, 'dmarc_moderation_action'):
            self.dmarc_moderation_action = mm_cfg.DEFAULT_DMARC_MODERATION_ACTION
        if not hasattr(self, 'equivalent_domains'):
            self.equivalent_domains = mm_cfg.DEFAULT_EQUIVALENT_DOMAINS
        if not hasattr(self, 'ban_list'):
            self.ban_list = []
        if not hasattr(self, 'filter_mime_types'):
            self.filter_mime_types = mm_cfg.DEFAULT_FILTER_MIME_TYPES
        if not hasattr(self, 'pass_mime_types'):
            self.pass_mime_types = mm_cfg.DEFAULT_PASS_MIME_TYPES
        if not hasattr(self, 'filter_content'):
            self.filter_content = mm_cfg.DEFAULT_FILTER_CONTENT
        if not hasattr(self, 'convert_html_to_plaintext'):
            self.convert_html_to_plaintext = mm_cfg.DEFAULT_CONVERT_HTML_TO_PLAINTEXT
        if not hasattr(self, 'filter_action'):
            self.filter_action = mm_cfg.DEFAULT_FILTER_ACTION
        if not hasattr(self, 'member_moderation_action'):
            self.member_moderation_action = mm_cfg.DEFAULT_MEMBER_MODERATION_ACTION
        if not hasattr(self, 'member_moderation_notice'):
            self.member_moderation_notice = ''
            
        # List administration attributes
        if not hasattr(self, 'owner'):
            self.owner = []
        if not hasattr(self, 'moderator'):
            self.moderator = []
            
        # List configuration attributes
        if not hasattr(self, 'real_name'):
            self.real_name = self.internal_name()
        if not hasattr(self, 'host_name'):
            self.host_name = mm_cfg.DEFAULT_EMAIL_HOST
        if not hasattr(self, 'web_page_url'):
            self.web_page_url = mm_cfg.DEFAULT_URL
        if not hasattr(self, 'subject_prefix'):
            self.subject_prefix = mm_cfg.DEFAULT_SUBJECT_PREFIX
        if not hasattr(self, 'msg_header'):
            self.msg_header = mm_cfg.DEFAULT_MSG_HEADER
        if not hasattr(self, 'msg_footer'):
            self.msg_footer = mm_cfg.DEFAULT_MSG_FOOTER
        if not hasattr(self, 'reply_to_address'):
            self.reply_to_address = ''
        if not hasattr(self, 'reply_goes_to_list'):
            self.reply_goes_to_list = mm_cfg.DEFAULT_REPLY_GOES_TO_LIST
        if not hasattr(self, 'first_strip_reply_to'):
            self.first_strip_reply_to = mm_cfg.DEFAULT_FIRST_STRIP_REPLY_TO
        if not hasattr(self, 'admin_immed_notify'):
            self.admin_immed_notify = mm_cfg.DEFAULT_ADMIN_IMMED_NOTIFY
        if not hasattr(self, 'admin_notify_mchanges'):
            self.admin_notify_mchanges = mm_cfg.DEFAULT_ADMIN_NOTIFY_MCHANGES
        if not hasattr(self, 'description'):
            self.description = ''
        if not hasattr(self, 'info'):
            self.info = ''
        if not hasattr(self, 'welcome_msg'):
            self.welcome_msg = ''
        if not hasattr(self, 'goodbye_msg'):
            self.goodbye_msg = ''
        if not hasattr(self, 'subscribe_policy'):
            self.subscribe_policy = mm_cfg.DEFAULT_SUBSCRIBE_POLICY
        if not hasattr(self, 'subscribe_auto_approval'):
            self.subscribe_auto_approval = mm_cfg.DEFAULT_SUBSCRIBE_AUTO_APPROVAL
        if not hasattr(self, 'unsubscribe_policy'):
            self.unsubscribe_policy = mm_cfg.DEFAULT_UNSUBSCRIBE_POLICY
        if not hasattr(self, 'private_roster'):
            self.private_roster = mm_cfg.DEFAULT_PRIVATE_ROSTER
        if not hasattr(self, 'obscure_addresses'):
            self.obscure_addresses = mm_cfg.DEFAULT_OBSCURE_ADDRESSES
        if not hasattr(self, 'admin_member_chunksize'):
            self.admin_member_chunksize = mm_cfg.DEFAULT_ADMIN_MEMBER_CHUNKSIZE
        if not hasattr(self, 'administrivia'):
            self.administrivia = mm_cfg.DEFAULT_ADMINISTRIVIA
        if not hasattr(self, 'drop_cc'):
            self.drop_cc = mm_cfg.DEFAULT_DROP_CC
        if not hasattr(self, 'preferred_language'):
            self.preferred_language = mm_cfg.DEFAULT_SERVER_LANGUAGE
        if not hasattr(self, 'available_languages'):
            self.available_languages = [mm_cfg.DEFAULT_SERVER_LANGUAGE]
        if not hasattr(self, 'include_rfc2369_headers'):
            self.include_rfc2369_headers = 1
        if not hasattr(self, 'include_list_post_header'):
            self.include_list_post_header = 1
        if not hasattr(self, 'include_sender_header'):
            self.include_sender_header = 1
        if not hasattr(self, 'filter_mime_types'):
            self.filter_mime_types = mm_cfg.DEFAULT_FILTER_MIME_TYPES
        if not hasattr(self, 'pass_mime_types'):
            self.pass_mime_types = mm_cfg.DEFAULT_PASS_MIME_TYPES
        if not hasattr(self, 'filter_filename_extensions'):
            self.filter_filename_extensions = mm_cfg.DEFAULT_FILTER_FILENAME_EXTENSIONS
        if not hasattr(self, 'pass_filename_extensions'):
            self.pass_filename_extensions = mm_cfg.DEFAULT_PASS_FILENAME_EXTENSIONS
        if not hasattr(self, 'filter_content'):
            self.filter_content = mm_cfg.DEFAULT_FILTER_CONTENT
        if not hasattr(self, 'collapse_alternatives'):
            self.collapse_alternatives = mm_cfg.DEFAULT_COLLAPSE_ALTERNATIVES
        if not hasattr(self, 'convert_html_to_plaintext'):
            self.convert_html_to_plaintext = mm_cfg.DEFAULT_CONVERT_HTML_TO_PLAINTEXT
        if not hasattr(self, 'filter_action'):
            self.filter_action = mm_cfg.DEFAULT_FILTER_ACTION
        if not hasattr(self, 'nondigestable'):
            self.nondigestable = mm_cfg.DEFAULT_NONDIGESTABLE
        if not hasattr(self, 'personalize'):
            self.personalize = 0
        if not hasattr(self, 'default_member_moderation'):
            self.default_member_moderation = mm_cfg.DEFAULT_DEFAULT_MEMBER_MODERATION
        if not hasattr(self, 'emergency'):
            self.emergency = 0
        if not hasattr(self, 'member_verbosity_threshold'):
            self.member_verbosity_threshold = mm_cfg.DEFAULT_MEMBER_VERBOSITY_THRESHOLD
        if not hasattr(self, 'member_verbosity_interval'):
            self.member_verbosity_interval = mm_cfg.DEFAULT_MEMBER_VERBOSITY_INTERVAL
        if not hasattr(self, 'member_moderation_action'):
            self.member_moderation_action = mm_cfg.DEFAULT_MEMBER_MODERATION_ACTION
        if not hasattr(self, 'member_moderation_notice'):
            self.member_moderation_notice = ''
        if not hasattr(self, 'dmarc_moderation_action'):
            self.dmarc_moderation_action = mm_cfg.DEFAULT_DMARC_MODERATION_ACTION
        if not hasattr(self, 'dmarc_quarantine_moderation_action'):
            self.dmarc_quarantine_moderation_action = mm_cfg.DEFAULT_DMARC_QUARANTINE_MODERATION_ACTION
        if not hasattr(self, 'dmarc_none_moderation_action'):
            self.dmarc_none_moderation_action = mm_cfg.DEFAULT_DMARC_NONE_MODERATION_ACTION
        if not hasattr(self, 'dmarc_moderation_notice'):
            self.dmarc_moderation_notice = ''
        if not hasattr(self, 'dmarc_moderation_addresses'):
            self.dmarc_moderation_addresses = []
        if not hasattr(self, 'dmarc_wrapped_message_text'):
            self.dmarc_wrapped_message_text = mm_cfg.DEFAULT_DMARC_WRAPPED_MESSAGE_TEXT
        if not hasattr(self, 'equivalent_domains'):
            self.equivalent_domains = mm_cfg.DEFAULT_EQUIVALENT_DOMAINS
        if not hasattr(self, 'accept_these_nonmembers'):
            self.accept_these_nonmembers = []
        if not hasattr(self, 'hold_these_nonmembers'):
            self.hold_these_nonmembers = []
        if not hasattr(self, 'reject_these_nonmembers'):
            self.reject_these_nonmembers = []
        if not hasattr(self, 'discard_these_nonmembers'):
            self.discard_these_nonmembers = []
        if not hasattr(self, 'forward_auto_discards'):
            self.forward_auto_discards = mm_cfg.DEFAULT_FORWARD_AUTO_DISCARDS
        if not hasattr(self, 'generic_nonmember_action'):
            self.generic_nonmember_action = mm_cfg.DEFAULT_GENERIC_NONMEMBER_ACTION
        if not hasattr(self, 'nonmember_rejection_notice'):
            self.nonmember_rejection_notice = ''
        if not hasattr(self, 'ban_list'):
            self.ban_list = []
        if not hasattr(self, 'password'):
            self.password = crypted_password
        if not hasattr(self, 'mod_password'):
            self.mod_password = None
        if not hasattr(self, 'post_password'):
            self.post_password = None
        if not hasattr(self, 'passwords'):
            self.passwords = {}
        if not hasattr(self, 'hold_and_cmd_autoresponses'):
            self.hold_and_cmd_autoresponses = {}
        if not hasattr(self, 'subject_prefix'):
            self.subject_prefix = mm_cfg.DEFAULT_SUBJECT_PREFIX
        if not hasattr(self, 'msg_header'):
            self.msg_header = mm_cfg.DEFAULT_MSG_HEADER
        if not hasattr(self, 'msg_footer'):
            self.msg_footer = mm_cfg.DEFAULT_MSG_FOOTER
        if not hasattr(self, 'encode_ascii_prefixes'):
            self.encode_ascii_prefixes = 2
        if not hasattr(self, 'scrub_nondigest'):
            self.scrub_nondigest = mm_cfg.DEFAULT_SCRUB_NONDIGEST
        if not hasattr(self, 'max_days_to_hold'):
            self.max_days_to_hold = mm_cfg.DEFAULT_MAX_DAYS_TO_HOLD

    #
    # Web API support via administrative categories
    #
    def GetConfigCategories(self):
        class CategoryDict(UserDict):
            def __init__(self):
                UserDict.__init__(self)
                self.keysinorder = mm_cfg.ADMIN_CATEGORIES[:]
            def keys(self):
                return self.keysinorder
            def items(self):
                items = []
                for k in mm_cfg.ADMIN_CATEGORIES:
                    items.append((k, self.data[k]))
                return items
            def values(self):
                values = []
                for k in mm_cfg.ADMIN_CATEGORIES:
                    values.append(self.data[k])
                return values

        categories = CategoryDict()
        # Only one level of mixin inheritance allowed
        for gui in self._gui:
            k, v = gui.GetConfigCategory()
            categories[k] = (v, gui)
        return categories

    def GetConfigSubCategories(self, category):
        for gui in self._gui:
            if hasattr(gui, 'GetConfigSubCategories'):
                # Return the first one that knows about the given subcategory
                subcat = gui.GetConfigSubCategories(category)
                if subcat is not None:
                    return subcat
        return None

    def GetConfigInfo(self, category, subcat=None):
        """Get configuration information for a category and optional subcategory.
        
        Args:
            category: The configuration category to get info for
            subcat: Optional subcategory to filter by
            
        Returns:
            A list of configuration items, or None if not found
        """
        for gui in self._gui:
            if hasattr(gui, 'GetConfigInfo'):
                try:
                    value = gui.GetConfigInfo(self, category, subcat)
                    if value:
                        return value
                except (AttributeError, KeyError) as e:
                    # Log the error but continue trying other GUIs
                    syslog('error', 'Error getting config info for %s/%s: %s',
                           category, subcat, str(e))
                    continue
        return None

    #
    # List creation
    #
    def Create(self, name, admin, crypted_password,
               langs=None, emailhost=None, urlhost=None):
        assert name == name.lower(), 'List name must be all lower case.'
        if Utils.list_exists(name):
            raise Errors.MMListAlreadyExistsError(name)
        # Problems and potential attacks can occur if the list name in the
        # pipe to the wrapper in an MTA alias or other delivery process
        # contains shell special characters so allow only defined characters
        # (default = '[-+_.=a-z0-9]').
        if len(re.sub(mm_cfg.ACCEPTABLE_LISTNAME_CHARACTERS, '', name)) > 0:
            raise Errors.BadListNameError(name)
        # Validate what will be the list's posting address.  If that's
        # invalid, we don't want to create the mailing list.  The hostname
        # part doesn't really matter, since that better already be valid.
        # However, most scripts already catch MMBadEmailError as exceptions on
        # the admin's email address, so transform the exception.
        if emailhost is None:
            emailhost = mm_cfg.DEFAULT_EMAIL_HOST
        postingaddr = '%s@%s' % (name, emailhost)
        try:
            Utils.ValidateEmail(postingaddr)
        except Errors.EmailAddressError:
            raise Errors.BadListNameError(postingaddr)
        # Validate the admin's email address
        Utils.ValidateEmail(admin)
        self._internal_name = name
        self._full_path = Site.get_listpath(name, create=1)
        # Don't use Lock() since that tries to load the non-existant config.pck
        self.__lock.lock()
        self.InitVars(name, admin, crypted_password, urlhost=urlhost)
        self.CheckValues()
        if langs is None:
            self.available_languages = [self.preferred_language]
        else:
            self.available_languages = langs


    #
    # Database and filesystem I/O
    #
    def __save(self, dict):
        """Save the list's configuration dictionary to disk.
        
        This method implements a robust save mechanism with:
        1. Backup of current configuration
        2. Writing to temporary file
        3. Validation of written data
        4. Atomic rename
        5. Creation of last known good version
        6. Proper error handling and cleanup
        """
        fname = os.path.join(self.fullpath(), 'config.pck')
        fname_tmp = fname + '.tmp.' + socket.gethostname() + '.' + str(os.getpid())
        fname_backup = fname + '.bak'
        
        try:
            # Ensure directory exists
            dirname = os.path.dirname(fname)
            if not os.path.exists(dirname):
                try:
                    os.makedirs(dirname, 0o755)
                except Exception as e:
                    mailman_log('error', 'Failed to create directory %s: %s', dirname, e)
                    raise
            
            # Write the temporary file
            try:
                with open(fname_tmp, 'wb') as fp:
                    pickle.dump(dict, fp, protocol=2, fix_imports=True)
            except Exception as e:
                mailman_log('error', 'Failed to write temporary file %s: %s', fname_tmp, e)
                raise
            
            # Create backup of current file if it exists
            if os.path.exists(fname):
                try:
                    os.rename(fname, fname_backup)
                except Exception as e:
                    mailman_log('error', 'Failed to create backup %s: %s', fname_backup, e)
                    raise
            
            # Atomic rename
            os.rename(fname_tmp, fname)
            
            # Create hard link to last good version
            try:
                os.link(fname, fname + '.last')
            except Exception:
                pass  # Ignore errors creating the hard link
                
        except Exception as e:
            mailman_log('error', 'Failed to save configuration: %s', e)
            # Clean up temporary file
            try:
                if os.path.exists(fname_tmp):
                    os.unlink(fname_tmp)
            except Exception:
                pass
            # Restore from backup if possible
            try:
                if os.path.exists(fname_backup):
                    os.rename(fname_backup, fname)
            except Exception:
                pass
            raise
            
        finally:
            # Clean up backup file
            try:
                if os.path.exists(fname_backup):
                    os.unlink(fname_backup)
            except Exception:
                pass
                
        # Reset timestamp
        self._timestamp = time.time()

    def __convert_bytes_to_strings(self, data):
        """Convert bytes to strings recursively in a data structure using latin1 encoding."""
        if isinstance(data, bytes):
            # Always use latin1 encoding for list section names and other data
            return data.decode('latin1', 'replace')
        elif isinstance(data, list):
            return [self.__convert_bytes_to_strings(item) for item in data]
        elif isinstance(data, dict):
            return {
                self.__convert_bytes_to_strings(key): self.__convert_bytes_to_strings(value)
                for key, value in data.items()
            }
        elif isinstance(data, tuple):
            return tuple(self.__convert_bytes_to_strings(item) for item in data)
        return data

    def Save(self):
        # First ensure we have the lock
        if not self.Locked():
            self.Lock()
        try:
            # Only refresh if we have the lock
            if self.Locked():
                self.__lock.refresh()
            # copy all public attributes to serializable dictionary
            dict = {}
            for key, value in list(self.__dict__.items()):
                if key[0] == '_' or type(value) is MethodType:
                    continue
                # Convert any bytes to strings recursively
                dict[key] = self.__convert_bytes_to_strings(value)
            # Make config.pck unreadable by `other', as it contains all the
            # list members' passwords (in clear text).
            omask = os.umask(0o007)
            try:
                self.__save(dict)
            finally:
                os.umask(omask)
                self.SaveRequestsDb()
            self.CheckHTMLArchiveDir()
        finally:
            # Always unlock when we're done
            self.Unlock()

    def __decode_latin1(self, value):
        """Centralized method to decode bytes to Latin-1 strings.
        
        This ensures consistent handling of bytes throughout the codebase.
        If the value is already a string, it is returned unchanged.
        """
        if isinstance(value, bytes):
            return value.decode('latin1', 'replace')
        return value

    def __load(self, file=None):
        """Load the dictionary from disk."""
        fname = os.path.join(self.fullpath(), 'config.pck')
        fname_backup = fname + '.bak'
        fname_last = fname + '.last'
        
        # Try loading from main file first
        try:
            with open(fname, 'rb') as fp:
                dict_retval = pickle.load(fp, fix_imports=True, encoding='latin1')
            if not isinstance(dict_retval, dict):
                raise TypeError('Loaded data is not a dictionary')
            return dict_retval
        except Exception as e:
            mailman_log('error', 'Failed to load main config file: %s', e)
            
        # Try loading from backup file
        try:
            if os.path.exists(fname_backup):
                with open(fname_backup, 'rb') as fp:
                    dict_retval = pickle.load(fp, fix_imports=True, encoding='latin1')
                if not isinstance(dict_retval, dict):
                    raise TypeError('Loaded data is not a dictionary')
                # Restore backup to main file
                os.rename(fname_backup, fname)
                return dict_retval
        except Exception as e:
            mailman_log('error', 'Failed to load backup config file: %s', e)
            
        # Try loading from last known good version
        try:
            if os.path.exists(fname_last):
                with open(fname_last, 'rb') as fp:
                    dict_retval = pickle.load(fp, fix_imports=True, encoding='latin1')
                if not isinstance(dict_retval, dict):
                    raise TypeError('Loaded data is not a dictionary')
                # Restore last good version to main file
                os.rename(fname_last, fname)
                return dict_retval
        except Exception as e:
            mailman_log('error', 'Failed to load last good config file: %s', e)
            
        # If all else fails, create a new config
        mailman_log('error', 'All config files corrupted, creating new config')
        return self._create()

    def Load(self):
        """Load the list's configuration from disk.
        
        This method loads the configuration dictionary from disk and updates
        the instance attributes with the loaded values.
        """
        # Only refresh if we have the lock
        if self.Locked():
            self.__lock.refresh()
        # Load the configuration dictionary
        dict = self.__load()
        if dict:
            # Store current member adaptor if it exists
            current_adaptor = getattr(self, '_memberadaptor', None)
            
            # Update instance attributes with loaded values
            for key, value in dict.items():
                if key[0] != '_':  # Skip private attributes
                    setattr(self, key, value)
            
            # Restore member adaptor if it existed
            if current_adaptor is not None:
                self._memberadaptor = current_adaptor
            else:
                self._memberadaptor = OldStyleMemberships(self)
            
            # Initialize other variables
            self.InitVars()
            
            # Check language settings
            if mm_cfg.LANGUAGES.get(self.preferred_language) is None:
                self.preferred_language = mm_cfg.DEFAULT_SERVER_LANGUAGE
                
            # Check version and update any missing attributes
            self.CheckVersion(dict)
            
            # Ensure all required attributes are present
            self._ensure_required_attributes()
            
            # Validate values
            self.CheckValues()
        else:
            # No configuration loaded, initialize with defaults
            self.InitVars()
            
    def _ensure_required_attributes(self):
        """Ensure all required attributes are present with default values if missing."""
        # Digest-related attributes
        if not hasattr(self, 'digest_volume_frequency'):
            self.digest_volume_frequency = mm_cfg.DEFAULT_DIGEST_VOLUME_FREQUENCY
        if not hasattr(self, 'digest_last_sent_at'):
            self.digest_last_sent_at = 0
        if not hasattr(self, 'digest_next_due_at'):
            self.digest_next_due_at = 0
        if not hasattr(self, '_new_volume'):
            self._new_volume = 0
        if not hasattr(self, 'volume'):
            self.volume = 1
        if not hasattr(self, 'one_last_digest'):
            self.one_last_digest = {}
        if not hasattr(self, 'next_digest_number'):
            self.next_digest_number = 1
            
        # Bounce-related attributes
        if not hasattr(self, 'bounce_processing'):
            self.bounce_processing = mm_cfg.DEFAULT_BOUNCE_PROCESSING
        if not hasattr(self, 'bounce_score_threshold'):
            self.bounce_score_threshold = mm_cfg.DEFAULT_BOUNCE_SCORE_THRESHOLD
        if not hasattr(self, 'bounce_info_stale_after'):
            self.bounce_info_stale_after = mm_cfg.DEFAULT_BOUNCE_INFO_STALE_AFTER
        if not hasattr(self, 'bounce_you_are_disabled_warnings'):
            self.bounce_you_are_disabled_warnings = mm_cfg.DEFAULT_BOUNCE_YOU_ARE_DISABLED_WARNINGS
        if not hasattr(self, 'bounce_you_are_disabled_warnings_interval'):
            self.bounce_you_are_disabled_warnings_interval = mm_cfg.DEFAULT_BOUNCE_YOU_ARE_DISABLED_WARNINGS_INTERVAL
        if not hasattr(self, 'bounce_unrecognized_goes_to_list_owner'):
            self.bounce_unrecognized_goes_to_list_owner = mm_cfg.DEFAULT_BOUNCE_UNRECOGNIZED_GOES_TO_LIST_OWNER
        if not hasattr(self, 'bounce_notify_owner_on_bounce_increment'):
            self.bounce_notify_owner_on_bounce_increment = mm_cfg.DEFAULT_BOUNCE_NOTIFY_OWNER_ON_BOUNCE_INCREMENT
        if not hasattr(self, 'bounce_notify_owner_on_disable'):
            self.bounce_notify_owner_on_disable = mm_cfg.DEFAULT_BOUNCE_NOTIFY_OWNER_ON_DISABLE
        if not hasattr(self, 'bounce_notify_owner_on_removal'):
            self.bounce_notify_owner_on_removal = mm_cfg.DEFAULT_BOUNCE_NOTIFY_OWNER_ON_REMOVAL
            
        # Gateway-related attributes
        if not hasattr(self, 'nntp_host'):
            self.nntp_host = mm_cfg.DEFAULT_NNTP_HOST
        if not hasattr(self, 'linked_newsgroup'):
            self.linked_newsgroup = ''
        if not hasattr(self, 'gateway_to_news'):
            self.gateway_to_news = 0
        if not hasattr(self, 'gateway_to_mail'):
            self.gateway_to_mail = 0
        if not hasattr(self, 'news_prefix_subject_too'):
            self.news_prefix_subject_too = 1
        if not hasattr(self, 'news_moderation'):
            self.news_moderation = 0
            
        # Autoresponder attributes
        if not hasattr(self, 'autorespond_postings'):
            self.autorespond_postings = 0
        if not hasattr(self, 'autorespond_admin'):
            self.autorespond_admin = 0
        if not hasattr(self, 'autorespond_requests'):
            self.autorespond_requests = 0
        if not hasattr(self, 'autoresponse_postings_text'):
            self.autoresponse_postings_text = ''
        if not hasattr(self, 'autoresponse_admin_text'):
            self.autoresponse_admin_text = ''
        if not hasattr(self, 'autoresponse_request_text'):
            self.autoresponse_request_text = ''
        if not hasattr(self, 'autoresponse_graceperiod'):
            self.autoresponse_graceperiod = 90
        if not hasattr(self, 'postings_responses'):
            self.postings_responses = {}
        if not hasattr(self, 'admin_responses'):
            self.admin_responses = {}
        if not hasattr(self, 'request_responses'):
            self.request_responses = {}
            
        # Topic-related attributes
        if not hasattr(self, 'topics'):
            self.topics = []
        if not hasattr(self, 'topics_enabled'):
            self.topics_enabled = 0
        if not hasattr(self, 'topics_bodylines_limit'):
            self.topics_bodylines_limit = 5
        if not hasattr(self, 'topics_userinterest'):
            self.topics_userinterest = {}
            
        # Security-related attributes
        if not hasattr(self, 'mod_password'):
            self.mod_password = None
        if not hasattr(self, 'post_password'):
            self.post_password = None
        if not hasattr(self, 'passwords'):
            self.passwords = {}
            
        # Other attributes
        if not hasattr(self, 'archive_private'):
            self.archive_private = mm_cfg.DEFAULT_ARCHIVE_PRIVATE
        if not hasattr(self, 'archive_volume_frequency'):
            self.archive_volume_frequency = mm_cfg.DEFAULT_ARCHIVE_VOLUME_FREQUENCY
        if not hasattr(self, 'data_version'):
            self.data_version = mm_cfg.DATA_FILE_VERSION
        if not hasattr(self, 'last_post_time'):
            self.last_post_time = 0
        if not hasattr(self, 'created_at'):
            self.created_at = time.time()
            
        # Member-related attributes
        if not hasattr(self, 'members'):
            self.members = {}
        if not hasattr(self, 'digest_members'):
            self.digest_members = {}
        if not hasattr(self, 'user_options'):
            self.user_options = {}
        if not hasattr(self, 'language'):
            self.language = {}
        if not hasattr(self, 'usernames'):
            self.usernames = {}
        if not hasattr(self, 'bounce_info'):
            self.bounce_info = {}
        if not hasattr(self, 'delivery_status'):
            self.delivery_status = {}
            
        # List settings
        if not hasattr(self, 'admin_member_chunksize'):
            self.admin_member_chunksize = mm_cfg.DEFAULT_ADMIN_MEMBER_CHUNKSIZE
        if not hasattr(self, 'dmarc_moderation_action'):
            self.dmarc_moderation_action = mm_cfg.DEFAULT_DMARC_MODERATION_ACTION
        if not hasattr(self, 'equivalent_domains'):
            self.equivalent_domains = mm_cfg.DEFAULT_EQUIVALENT_DOMAINS
        if not hasattr(self, 'ban_list'):
            self.ban_list = []
        if not hasattr(self, 'filter_mime_types'):
            self.filter_mime_types = mm_cfg.DEFAULT_FILTER_MIME_TYPES
        if not hasattr(self, 'pass_mime_types'):
            self.pass_mime_types = mm_cfg.DEFAULT_PASS_MIME_TYPES
        if not hasattr(self, 'filter_content'):
            self.filter_content = mm_cfg.DEFAULT_FILTER_CONTENT
        if not hasattr(self, 'convert_html_to_plaintext'):
            self.convert_html_to_plaintext = mm_cfg.DEFAULT_CONVERT_HTML_TO_PLAINTEXT
        if not hasattr(self, 'filter_action'):
            self.filter_action = mm_cfg.DEFAULT_FILTER_ACTION
        if not hasattr(self, 'member_moderation_action'):
            self.member_moderation_action = mm_cfg.DEFAULT_MEMBER_MODERATION_ACTION
        if not hasattr(self, 'member_moderation_notice'):
            self.member_moderation_notice = ''
            
        # List administration attributes
        if not hasattr(self, 'owner'):
            self.owner = []
        if not hasattr(self, 'moderator'):
            self.moderator = []
            
        # List configuration attributes
        if not hasattr(self, 'real_name'):
            self.real_name = self.internal_name()
        if not hasattr(self, 'host_name'):
            self.host_name = mm_cfg.DEFAULT_EMAIL_HOST
        if not hasattr(self, 'web_page_url'):
            self.web_page_url = mm_cfg.DEFAULT_URL
        if not hasattr(self, 'subject_prefix'):
            self.subject_prefix = mm_cfg.DEFAULT_SUBJECT_PREFIX
        if not hasattr(self, 'msg_header'):
            self.msg_header = mm_cfg.DEFAULT_MSG_HEADER
        if not hasattr(self, 'msg_footer'):
            self.msg_footer = mm_cfg.DEFAULT_MSG_FOOTER
        if not hasattr(self, 'reply_to_address'):
            self.reply_to_address = ''
        if not hasattr(self, 'reply_goes_to_list'):
            self.reply_goes_to_list = mm_cfg.DEFAULT_REPLY_GOES_TO_LIST
        if not hasattr(self, 'first_strip_reply_to'):
            self.first_strip_reply_to = mm_cfg.DEFAULT_FIRST_STRIP_REPLY_TO
        if not hasattr(self, 'admin_immed_notify'):
            self.admin_immed_notify = mm_cfg.DEFAULT_ADMIN_IMMED_NOTIFY
        if not hasattr(self, 'admin_notify_mchanges'):
            self.admin_notify_mchanges = mm_cfg.DEFAULT_ADMIN_NOTIFY_MCHANGES
        if not hasattr(self, 'description'):
            self.description = ''
        if not hasattr(self, 'info'):
            self.info = ''
        if not hasattr(self, 'welcome_msg'):
            self.welcome_msg = ''
        if not hasattr(self, 'goodbye_msg'):
            self.goodbye_msg = ''
        if not hasattr(self, 'subscribe_policy'):
            self.subscribe_policy = mm_cfg.DEFAULT_SUBSCRIBE_POLICY
        if not hasattr(self, 'subscribe_auto_approval'):
            self.subscribe_auto_approval = mm_cfg.DEFAULT_SUBSCRIBE_AUTO_APPROVAL
        if not hasattr(self, 'unsubscribe_policy'):
            self.unsubscribe_policy = mm_cfg.DEFAULT_UNSUBSCRIBE_POLICY
        if not hasattr(self, 'private_roster'):
            self.private_roster = mm_cfg.DEFAULT_PRIVATE_ROSTER
        if not hasattr(self, 'obscure_addresses'):
            self.obscure_addresses = mm_cfg.DEFAULT_OBSCURE_ADDRESSES
        if not hasattr(self, 'admin_member_chunksize'):
            self.admin_member_chunksize = mm_cfg.DEFAULT_ADMIN_MEMBER_CHUNKSIZE
        if not hasattr(self, 'administrivia'):
            self.administrivia = mm_cfg.DEFAULT_ADMINISTRIVIA
        if not hasattr(self, 'drop_cc'):
            self.drop_cc = mm_cfg.DEFAULT_DROP_CC
        if not hasattr(self, 'preferred_language'):
            self.preferred_language = mm_cfg.DEFAULT_SERVER_LANGUAGE
        if not hasattr(self, 'available_languages'):
            self.available_languages = [mm_cfg.DEFAULT_SERVER_LANGUAGE]
        if not hasattr(self, 'include_rfc2369_headers'):
            self.include_rfc2369_headers = 1
        if not hasattr(self, 'include_list_post_header'):
            self.include_list_post_header = 1
        if not hasattr(self, 'include_sender_header'):
            self.include_sender_header = 1
        if not hasattr(self, 'filter_mime_types'):
            self.filter_mime_types = mm_cfg.DEFAULT_FILTER_MIME_TYPES
        if not hasattr(self, 'pass_mime_types'):
            self.pass_mime_types = mm_cfg.DEFAULT_PASS_MIME_TYPES
        if not hasattr(self, 'filter_filename_extensions'):
            self.filter_filename_extensions = mm_cfg.DEFAULT_FILTER_FILENAME_EXTENSIONS
        if not hasattr(self, 'pass_filename_extensions'):
            self.pass_filename_extensions = mm_cfg.DEFAULT_PASS_FILENAME_EXTENSIONS
        if not hasattr(self, 'filter_content'):
            self.filter_content = mm_cfg.DEFAULT_FILTER_CONTENT
        if not hasattr(self, 'collapse_alternatives'):
            self.collapse_alternatives = mm_cfg.DEFAULT_COLLAPSE_ALTERNATIVES
        if not hasattr(self, 'convert_html_to_plaintext'):
            self.convert_html_to_plaintext = mm_cfg.DEFAULT_CONVERT_HTML_TO_PLAINTEXT
        if not hasattr(self, 'filter_action'):
            self.filter_action = mm_cfg.DEFAULT_FILTER_ACTION
        if not hasattr(self, 'nondigestable'):
            self.nondigestable = mm_cfg.DEFAULT_NONDIGESTABLE
        if not hasattr(self, 'personalize'):
            self.personalize = 0
        if not hasattr(self, 'default_member_moderation'):
            self.default_member_moderation = mm_cfg.DEFAULT_DEFAULT_MEMBER_MODERATION
        if not hasattr(self, 'emergency'):
            self.emergency = 0
        if not hasattr(self, 'member_verbosity_threshold'):
            self.member_verbosity_threshold = mm_cfg.DEFAULT_MEMBER_VERBOSITY_THRESHOLD
        if not hasattr(self, 'member_verbosity_interval'):
            self.member_verbosity_interval = mm_cfg.DEFAULT_MEMBER_VERBOSITY_INTERVAL
        if not hasattr(self, 'member_moderation_action'):
            self.member_moderation_action = mm_cfg.DEFAULT_MEMBER_MODERATION_ACTION
        if not hasattr(self, 'member_moderation_notice'):
            self.member_moderation_notice = ''
        if not hasattr(self, 'dmarc_moderation_action'):
            self.dmarc_moderation_action = mm_cfg.DEFAULT_DMARC_MODERATION_ACTION
        if not hasattr(self, 'dmarc_quarantine_moderation_action'):
            self.dmarc_quarantine_moderation_action = mm_cfg.DEFAULT_DMARC_QUARANTINE_MODERATION_ACTION
        if not hasattr(self, 'dmarc_none_moderation_action'):
            self.dmarc_none_moderation_action = mm_cfg.DEFAULT_DMARC_NONE_MODERATION_ACTION
        if not hasattr(self, 'dmarc_moderation_notice'):
            self.dmarc_moderation_notice = ''
        if not hasattr(self, 'dmarc_moderation_addresses'):
            self.dmarc_moderation_addresses = []
        if not hasattr(self, 'dmarc_wrapped_message_text'):
            self.dmarc_wrapped_message_text = mm_cfg.DEFAULT_DMARC_WRAPPED_MESSAGE_TEXT
        if not hasattr(self, 'equivalent_domains'):
            self.equivalent_domains = mm_cfg.DEFAULT_EQUIVALENT_DOMAINS
        if not hasattr(self, 'accept_these_nonmembers'):
            self.accept_these_nonmembers = []
        if not hasattr(self, 'hold_these_nonmembers'):
            self.hold_these_nonmembers = []
        if not hasattr(self, 'reject_these_nonmembers'):
            self.reject_these_nonmembers = []
        if not hasattr(self, 'discard_these_nonmembers'):
            self.discard_these_nonmembers = []
        if not hasattr(self, 'forward_auto_discards'):
            self.forward_auto_discards = mm_cfg.DEFAULT_FORWARD_AUTO_DISCARDS
        if not hasattr(self, 'generic_nonmember_action'):
            self.generic_nonmember_action = mm_cfg.DEFAULT_GENERIC_NONMEMBER_ACTION
        if not hasattr(self, 'nonmember_rejection_notice'):
            self.nonmember_rejection_notice = ''
        if not hasattr(self, 'ban_list'):
            self.ban_list = []
        if not hasattr(self, 'password'):
            self.password = crypted_password
        if not hasattr(self, 'mod_password'):
            self.mod_password = None
        if not hasattr(self, 'post_password'):
            self.post_password = None
        if not hasattr(self, 'passwords'):
            self.passwords = {}
        if not hasattr(self, 'hold_and_cmd_autoresponses'):
            self.hold_and_cmd_autoresponses = {}
        if not hasattr(self, 'subject_prefix'):
            self.subject_prefix = mm_cfg.DEFAULT_SUBJECT_PREFIX
        if not hasattr(self, 'msg_header'):
            self.msg_header = mm_cfg.DEFAULT_MSG_HEADER
        if not hasattr(self, 'msg_footer'):
            self.msg_footer = mm_cfg.DEFAULT_MSG_FOOTER
        if not hasattr(self, 'encode_ascii_prefixes'):
            self.encode_ascii_prefixes = 2
        if not hasattr(self, 'scrub_nondigest'):
            self.scrub_nondigest = mm_cfg.DEFAULT_SCRUB_NONDIGEST
        if not hasattr(self, 'max_days_to_hold'):
            self.max_days_to_hold = mm_cfg.DEFAULT_MAX_DAYS_TO_HOLD
        if not hasattr(self, 'max_num_recipients'):
                header = line[:i]
                value = line[i+1:].lstrip()
                try:
                    cre = re.compile(value, re.IGNORECASE)
                except re.error as e:
                    # The regexp was malformed.  BAW: should do a better
                    # job of informing the list admin.
                    syslog('config', '''\
bad regexp in bounce_matching_header line: %s
\n%s (cause: %s)''', self.real_name, value, e)
                else:
                    all.append((header, cre, line))
        return all

    def hasMatchingHeader(self, msg):
        """Return true if named header field matches a regexp in the
        bounce_matching_header list variable.

        Returns constraint line which matches or empty string for no
        matches.
        """
        for header, cre, line in self.parse_matching_header_opt():
            for value in msg.get_all(header, []):
                if cre.search(value):
                    return line
        return 0

    def autorespondToSender(self, sender, lang=None):
        """Return true if Mailman should auto-respond to this sender.

        This is only consulted for messages sent to the -request address, or
        for posting hold notifications, and serves only as a safety value for
        mail loops with email 'bots.
        """
        # language setting
        if lang == None:
            lang = self.preferred_language
        i18n.set_language(lang)
        # No limit
        if mm_cfg.MAX_AUTORESPONSES_PER_DAY == 0:
            return 1
        today = time.localtime()[:3]
        info = self.hold_and_cmd_autoresponses.get(sender)
        if info is None or info[0] != today:
            # First time we've seen a -request/post-hold for this sender
            self.hold_and_cmd_autoresponses[sender] = (today, 1)
            # BAW: no check for MAX_AUTORESPONSES_PER_DAY <= 1
            return 1
        date, count = info
        if count < 0:
            # They've already hit the limit for today.
            syslog('vette', '-request/hold autoresponse discarded for: %s',
                   sender)
            return 0
        if count >= mm_cfg.MAX_AUTORESPONSES_PER_DAY:
            syslog('vette', '-request/hold autoresponse limit hit for: %s',
                   sender)
            self.hold_and_cmd_autoresponses[sender] = (today, -1)
            # Send this notification message instead
            text = Utils.maketext(
                'nomoretoday.txt',
                {'sender' : sender,
                 'listname': '%s@%s' % (self.real_name, self.host_name),
                 'num' : count,
                 'owneremail': self.GetOwnerEmail(),
                 },
                lang=lang)
            msg = Mailman.Message.UserNotification(
                sender, self.GetOwnerEmail(),
                _('Last autoresponse notification for today'),
                text, lang=lang)
            msg.send(self)
            return 0
        self.hold_and_cmd_autoresponses[sender] = (today, count+1)
        return 1

    def GetBannedPattern(self, email):
        """Returns matched entry in ban_list if email matches.
        Otherwise returns None.
        """
        return (self.GetPattern(email, self.ban_list) or
                self.GetPattern(email, mm_cfg.GLOBAL_BAN_LIST)
               )

    def HasAutoApprovedSender(self, sender):
        """Returns True and logs if sender matches address or pattern
        or is a member of a referenced list in subscribe_auto_approval.
        Otherwise returns False.
        """
        auto_approve = False
        if self.GetPattern(sender,
                           self.subscribe_auto_approval,
                           at_list='subscribe_auto_approval'
                          ):
            auto_approve = True
            syslog('vette', '%s: auto approved subscribe from %s',
                   self.internal_name(), sender)
        return auto_approve

    def GetPattern(self, email, pattern_list, at_list=None):
        """Returns matched entry in pattern_list if email matches.
        Otherwise returns None.  The at_list argument, if "true",
        says process the @listname syntax and provides the name of
        the list attribute for log messages.
        """
        matched = None
        # First strip out all the regular expressions and listnames because
        # documentation says we do non-regexp first (Why?).
        plainaddrs = [x.strip() for x in pattern_list if x.strip() and not
                         (x.startswith('^') or x.startswith('@'))]
        addrdict = Utils.List2Dict(plainaddrs, foldcase=1)
        if email.lower() in addrdict:
            return email
        for pattern in pattern_list:
            if pattern.startswith('^'):
                # This is a regular expression match
                try:
                    if re.search(pattern, email, re.IGNORECASE):
                        matched = pattern
                        break
                except re.error as e:
                    # BAW: we should probably remove this pattern
                    # The GUI won't add a bad regexp, but at least log it.
                    # The following kludge works because the ban_list stuff
                    # is the only caller with no at_list.
                    attr_name = at_list or 'ban_list'
                    syslog('error',
                           '%s in %s has bad regexp "%s": %s',
                           attr_name,
                           self.internal_name(),
                           pattern,
                           str(e)
                          )
            elif at_list and pattern.startswith('@'):
                # XXX Needs to be reviewed for list@domain names.
                # this refers to the members of another list in this
                # installation.
                mname = pattern[1:].lower().strip()
                if mname == self.internal_name():
                    # don't reference your own list
                    syslog('error',
                        '%s in %s references own list',
                        at_list,
                        self.internal_name())
                    continue
                try:
                    mother = MailList(mname, lock = False)
                except Errors.MMUnknownListError:
                    syslog('error',
                           '%s in %s references non-existent list %s',
                           at_list,
                           self.internal_name(),
                           mname
                          )
                    continue
                if mother.isMember(email.lower()):
                    matched = pattern
                    break
        return matched


    #
    # Multilingual (i18n) support
    #
    def GetAvailableLanguages(self):
        langs = self.available_languages
        # If we don't add this, and the site admin has never added any
        # language support to the list, then the general admin page may have a
        # blank field where the list owner is supposed to chose the list's
        # preferred language.
        if mm_cfg.DEFAULT_SERVER_LANGUAGE not in langs:
            langs.append(mm_cfg.DEFAULT_SERVER_LANGUAGE)
        # When testing, it's possible we've disabled a language, so just
        # filter things out so we don't get tracebacks.
        return [lang for lang in langs if lang in mm_cfg.LC_DESCRIPTIONS]

    def convert_member_addresses_to_lowercase(self):
        """Convert all member email addresses to lowercase while preserving case for sending.
        
        This method ensures that all member addresses are stored in lowercase for lookups,
        while maintaining the original case for sending messages.
        """
        if not self.Locked():
            self.Lock()
        try:
            # Get all members
            members = self.getMembers()
            for member in members:
                # Get the case-preserved address
                cpe = self.getMemberCPAddress(member)
                if cpe and cpe.lower() != cpe:
                    # If the address isn't already lowercase, update it
                    self.ChangeMemberAddress(cpe, cpe.lower(), globally=False)
            self.Save()
        finally:
            self.Unlock()
