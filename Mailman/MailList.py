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
from types import *

import email.iterators
from email.utils import getaddresses, formataddr, parseaddr
from email.header import Header

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
        if lock:
            # This will load the database.
            self.Lock()
        else:
            self.Load()

    def __getattr__(self, name):
        # Because we're using delegation, we want to be sure that attribute
        # access to a delegated member function gets passed to the
        # sub-objects.  This of course imposes a specific name resolution
        # order.
        # Some attributes should not be delegated to the member adaptor
        # because they belong to the main list object or other mixins
        non_delegated_attrs = {
            'topics', 'delivery_status', 'bounce_info', 'bounce_info_stale_after',
            'archive_private', 'usenet_watermark', 'digest_members', 'members',
            'passwords', 'user_options', 'language', 'usernames', 'topics_userinterest',
            'new_member_options', 'digestable', 'nondigestable', 'one_last_digest',
            'archive', 'archive_volume_frequency'
        }
        if name not in non_delegated_attrs:
            try:
                return getattr(self._memberadaptor, name)
            except AttributeError:
                pass
        for guicomponent in self._gui:
            try:
                return getattr(guicomponent, name)
            except AttributeError:
                pass
        # For certain attributes that should exist but might not be initialized yet,
        # return a default value instead of raising an AttributeError
        if name in non_delegated_attrs:
            if name == 'topics':
                return []
            elif name == 'delivery_status':
                return {}
            elif name == 'bounce_info':
                return {}
            elif name == 'bounce_info_stale_after':
                return mm_cfg.DEFAULT_BOUNCE_INFO_STALE_AFTER
            elif name == 'archive_private':
                return mm_cfg.DEFAULT_ARCHIVE_PRIVATE
            elif name == 'usenet_watermark':
                return None
            elif name == 'digest_members':
                return {}
            elif name == 'members':
                return {}
            elif name == 'passwords':
                return {}
            elif name == 'user_options':
                return {}
            elif name == 'language':
                return {}
            elif name == 'usernames':
                return {}
            elif name == 'topics_userinterest':
                return {}
            elif name == 'new_member_options':
                return 0
            elif name == 'digestable':
                return 0
            elif name == 'nondigestable':
                return 0
            elif name == 'one_last_digest':
                return {}
            elif name == 'archive':
                return 0
            elif name == 'archive_volume_frequency':
                return 0
        # For any other attribute not explicitly handled, return a sensible default
        # based on the attribute name pattern
        if name.startswith('_'):
            return 0  # Private attributes default to 0
        elif name.endswith('_msg') or name.endswith('_text'):
            return ''  # Message/text attributes default to empty string
        elif name.endswith('_list') or name.endswith('_lists'):
            return []  # List attributes default to empty list
        elif name.endswith('_dict') or name.endswith('_info'):
            return {}  # Dictionary attributes default to empty dict
        elif name in ('host_name', 'real_name', 'description', 'info', 'subject_prefix', 
                     'reply_to_address', 'umbrella_member_suffix'):
            return ''  # String attributes default to empty string
        elif name in ('max_message_size', 'admin_member_chunksize', 'max_days_to_hold',
                     'bounce_score_threshold', 'bounce_info_stale_after',
                     'bounce_you_are_disabled_warnings', 'bounce_you_are_disabled_warnings_interval',
                     'member_verbosity_threshold', 'member_verbosity_interval',
                     'digest_size_threshhold', 'topics_bodylines_limit',
                     'autoresponse_graceperiod'):
            return 0  # Number attributes default to 0
        else:
            return 0  # Default for any other attribute

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
        self.__lock.lock(timeout)
        # Must reload our database for consistency.  Watch out for lists that
        # don't exist.
        try:
            self.Load()
        except Exception:
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
        return self._internal_name

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

    def InitVars(self, name=None, admin='', crypted_password='',
                 urlhost=None):
        """Assign default values - some will be overriden by stored state."""
        # Non-configurable list info
        if name:
          self._internal_name = name

        # When was the list created?
        self.created_at = time.time()

        # Must save this state, even though it isn't configurable
        self.volume = 1
        self.members = {} # self.digest_members is initted in mm_digest
        self.data_version = mm_cfg.DATA_FILE_VERSION
        self.last_post_time = 0

        self.post_id = 1.  # A float so it never has a chance to overflow.
        self.user_options = {}
        self.language = {}
        self.usernames = {}
        self.passwords = {}
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
        # Only one level of mixin inheritance allowed
        for baseclass in self.__class__.__bases__:
            if hasattr(baseclass, 'InitVars'):
                baseclass.InitVars(self)

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
        for gui in self._gui:
            if hasattr(gui, 'GetConfigInfo'):
                value = gui.GetConfigInfo(self, category, subcat)
                if value:
                    return value


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
        # Save the file as a binary pickle, and rotate the old version to a
        # backup file.  We must guarantee that config.pck is always valid so
        # we never rotate unless the we've successfully written the temp file.
        # We use pickle now because marshal is not guaranteed to be compatible
        # between Python versions.
        fname = os.path.join(self.fullpath(), 'config.pck')
        fname_tmp = fname + '.tmp.%s.%d' % (socket.gethostname(), os.getpid())
        fname_last = fname + '.last'
        fp = None
        try:
            fp = open(fname_tmp, 'wb')
            # Use a binary format... it's more efficient.
            pickle.dump(dict, fp, 1)
            fp.flush()
            if mm_cfg.SYNC_AFTER_WRITE:
                os.fsync(fp.fileno())
            fp.close()
        except IOError as e:
            syslog('error',
                   'Failed config.pck write, retaining old state.\n%s', e)
            if fp is not None:
                os.unlink(fname_tmp)
            raise
        # Now do config.pck.tmp.xxx -> config.pck -> config.pck.last rotation
        # as safely as possible.
        try:
            # might not exist yet
            os.unlink(fname_last)
        except OSError as e:
            if e.errno != errno.ENOENT: raise
        try:
            # might not exist yet
            os.link(fname, fname_last)
        except OSError as e:
            if e.errno != errno.ENOENT: raise
        os.rename(fname_tmp, fname)
        # Reset the timestamp
        self.__timestamp = os.path.getmtime(fname)

    def Save(self):
        # Refresh the lock, just to let other processes know we're still
        # interested in it.  This will raise a NotLockedError if we don't have
        # the lock (which is a serious problem!).  TBD: do we need to be more
        # defensive?
        self.__lock.refresh()
        # copy all public attributes to serializable dictionary
        dict = {}
        for key, value in list(self.__dict__.items()):
            if key[0] == '_' or type(value) is MethodType:
                continue
            dict[key] = value
        # Make config.pck unreadable by `other', as it contains all the
        # list members' passwords (in clear text).
        omask = os.umask(0o007)
        try:
            self.__save(dict)
        finally:
            os.umask(omask)
            self.SaveRequestsDb()
        self.CheckHTMLArchiveDir()

    def __load(self, dbfile):
        # Attempt to load and unserialize the specified database file.  This
        # could actually be a config.db (for pre-2.1alpha3) or config.pck,
        # i.e. a marshal or a binary pickle.  Actually, it could also be a
        # .last backup file if the primary storage file was corrupt.  The
        # decision on whether to unpickle or unmarshal is based on the file
        # extension, but we always save it using pickle (since only it, and
        # not marshal is guaranteed to be compatible across Python versions).
        #
        # On success return a 2-tuple of (dictionary, None).  On error, return
        # a 2-tuple of the form (None, errorobj).
        if dbfile.endswith('.db') or dbfile.endswith('.db.last'):
            loadfunc = marshal.load
        elif dbfile.endswith('.pck') or dbfile.endswith('.pck.last'):
            loadfunc = pickle.load
        else:
            assert 0, 'Bad database file name'
        try:
            # Check the mod time of the file first.  If it matches our
            # timestamp, then the state hasn't change since the last time we
            # loaded it.  Otherwise open the file for loading, below.  If the
            # file doesn't exist, we'll get an EnvironmentError with errno set
            # to ENOENT (EnvironmentError is the base class of IOError and
            # OSError).
            # We test strictly less than here because the resolution is whole
            # seconds and we have seen cases of the file being updated by
            # another process in the same second.
            # Even this is not sufficient in shared file system environments
            # if there is time skew between servers.  In those cases, the test
            # could be
            # if mtime + MAX_SKEW < self.__timestamp:
            # or the "if ...: return" just deleted.
            mtime = os.path.getmtime(dbfile)
            if mtime < self.__timestamp:
                # File is not newer
                return None, None
            fp = open(dbfile, mode='rb')
        except EnvironmentError as e:
            if e.errno != errno.ENOENT: raise
            # The file doesn't exist yet
            return None, e
        now = int(time.time())
        try:
            try:
                if dbfile.endswith('.db') or dbfile.endswith('.db.last'):
                    dict_retval = marshal.load(fp)
                elif dbfile.endswith('.pck') or dbfile.endswith('.pck.last'):
                    dict_retval = Utils.load_pickle(dbfile)
                if not isinstance(dict_retval, dict):
                    return None, 'Load() expected to return a dictionary'
            except (EOFError, ValueError, TypeError, MemoryError,
                    pickle.PicklingError, pickle.UnpicklingError) as e:
                return None, e
        finally:
            fp.close()
        # Update the timestamp.  We use current time here rather than mtime
        # so the test above might succeed the next time.  And we get the time
        # before unpickling in case it takes more than a second.  (LP: #266464)
        self.__timestamp = now
        return dict_retval, None

    def Load(self, check_version=True):
        if not Utils.list_exists(self.internal_name()):
            raise Errors.MMUnknownListError
        # We first try to load config.pck, which contains the up-to-date
        # version of the database.  If that fails, perhaps because it's
        # corrupted or missing, we'll try to load the backup file
        # config.pck.last.
        #
        # Should both of those fail, we'll look for config.db and
        # config.db.last for backwards compatibility with pre-2.1alpha3
        pfile = os.path.join(self.fullpath(), 'config.pck')
        plast = pfile + '.last'
        dfile = os.path.join(self.fullpath(), 'config.db')
        dlast = dfile + '.last'
        for file in (pfile, plast, dfile, dlast):
            dict_retval, e = self.__load(file)
            if dict_retval is None:
                if e is not None:
                    # Had problems with this file; log it and try the next one.
                    syslog('error', "couldn't load config file %s\n%s",
                           file, e)
                else:
                    # We already have the most up-to-date state
                    return
            else:
                break
        else:
            # Nothing worked, so we have to give up
            syslog('error', 'All %s fallbacks were corrupt, giving up',
                   self.internal_name())
            raise Errors.MMCorruptListDatabaseError(e)
        # Now, if we didn't end up using the primary database file, we want to
        # copy the fallback into the primary so that the logic in Save() will
        # still work.  For giggles, we'll copy it to a safety backup.  Note we
        # MUST do this with the underlying list lock acquired.
        if file == plast or file == dlast:
            syslog('error', 'fixing corrupt config file, using: %s', file)
            unlock = True
            try:
                try:
                    self.__lock.lock()
                except LockFile.AlreadyLockedError:
                    unlock = False
                self.__fix_corrupt_pckfile(file, pfile, plast, dfile, dlast)
            finally:
                if unlock:
                    self.__lock.unlock()
        # Copy the loaded dictionary into the attributes of the current
        # mailing list object, then run sanity check on the data.
        self.__dict__.update(dict_retval)
        if check_version:
            self.CheckVersion(dict_retval)
            self.CheckValues()

    def __fix_corrupt_pckfile(self, file, pfile, plast, dfile, dlast):
        if file == plast:
            # Move aside any existing pickle file and delete any existing
            # safety file.  This avoids EPERM errors inside the shutil.copy()
            # calls if those files exist with different ownership.
            try:
                os.rename(pfile, pfile + '.corrupt')
            except OSError as e:
                if e.errno != errno.ENOENT: raise
            try:
                os.remove(pfile + '.safety')
            except OSError as e:
                if e.errno != errno.ENOENT: raise
            shutil.copy(file, pfile)
            shutil.copy(file, pfile + '.safety')
        elif file == dlast:
            # Move aside any existing marshal file and delete any existing
            # safety file.  This avoids EPERM errors inside the shutil.copy()
            # calls if those files exist with different ownership.
            try:
                os.rename(dfile, dfile + '.corrupt')
            except OSError as e:
                if e.errno != errno.ENOENT: raise
            try:
                os.remove(dfile + '.safety')
            except OSError as e:
                if e.errno != errno.ENOENT: raise
            shutil.copy(file, dfile)
            shutil.copy(file, dfile + '.safety')


    #
    # Sanity checks
    #
    def CheckVersion(self, stored_state):
        """Auto-update schema if necessary."""
        if self.data_version >= mm_cfg.DATA_FILE_VERSION:
            return
        # Initialize any new variables
        self.InitVars()
        # Then reload the database (but don't recurse).  Force a reload even
        # if we have the most up-to-date state.
        self.__timestamp = 0
        self.Load(check_version=0)
        # We must hold the list lock in order to update the schema
        waslocked = self.Locked()
        if not waslocked:
            self.Lock()
        try:
            from .versions import Update
            Update(self, stored_state)
            self.data_version = mm_cfg.DATA_FILE_VERSION
            self.Save()
        finally:
            if not waslocked:
                self.Unlock()

    def CheckValues(self):
        """Normalize selected values to known formats."""
        if '' in urlparse(self.web_page_url)[:2]:
            # Either the "scheme" or the "network location" part of the parsed
            # URL is empty; substitute faulty value with (hopefully sane)
            # default.  Note that DEFAULT_URL is obsolete.
            self.web_page_url = (
                mm_cfg.DEFAULT_URL or
                mm_cfg.DEFAULT_URL_PATTERN % mm_cfg.DEFAULT_URL_HOST)
        if self.web_page_url and self.web_page_url[-1] != '/':
            self.web_page_url = self.web_page_url + '/'
        # Legacy reply_to_address could be an illegal value.  We now verify
        # upon setting and don't check it at the point of use.
        try:
            if self.reply_to_address.strip() and self.reply_goes_to_list:
                Utils.ValidateEmail(self.reply_to_address)
        except Errors.EmailAddressError:
            syslog('error', 'Bad reply_to_address "%s" cleared for list: %s',
                   self.reply_to_address, self.internal_name())
            self.reply_to_address = ''
            self.reply_goes_to_list = 0
        # Legacy topics may have bad regular expressions in their patterns
        # Also, someone may have broken topics with, e.g., config_list.
        goodtopics = []
        # Check if topics attribute exists before trying to access it
        if hasattr(self, 'topics'):
            for value in self.topics:
                try:
                    name, pattern, desc, emptyflag = value
                except ValueError:
                    # This value is not a 4-tuple. Just log and drop it.
                    syslog('error', 'Bad topic "%s" for list: %s',
                           value, self.internal_name())
                    continue
                try:
                    orpattern = OR.join(pattern.splitlines())
                    re.compile(orpattern)
                except (re.error, TypeError):
                    syslog('error', 'Bad topic pattern "%s" for list: %s',
                           orpattern, self.internal_name())
                else:
                    goodtopics.append((name, pattern, desc, emptyflag))
            self.topics = goodtopics


    #
    # Membership management front-ends and assertion checks
    #
    def CheckPending(self, email, unsub=False):
        """Check if there is already an unexpired pending (un)subscription for
        this email.
        """
        if not mm_cfg.REFUSE_SECOND_PENDING:
            return False
        pends = self._Pending__load()
        # Save and reload the db to evict expired pendings.
        self._Pending__save(pends)
        pends = self._Pending__load()
        for k, v in list(pends.items()):
            if k in ('evictions', 'version'):
                continue
            op, data = v[:2]
            if (op == Pending.SUBSCRIPTION and not unsub and
                    data.address.lower() == email.lower() or
                    op == Pending.UNSUBSCRIPTION and unsub and
                    data.lower() == email.lower()):
                return True
        return False

    def InviteNewMember(self, userdesc, text=''):
        """Invite a new member to the list.

        This is done by creating a subscription pending for the user, and then
        crafting a message to the member informing them of the invitation.
        """
        invitee = userdesc.address
        Utils.ValidateEmail(invitee)
        # check for banned address
        pattern = self.GetBannedPattern(invitee)
        if pattern:
            syslog('vette', '%s banned invitation: %s (matched: %s)',
                   self.real_name, invitee, pattern)
            raise Errors.MembershipIsBanned(pattern)
        # Hack alert!  Squirrel away a flag that only invitations have, so
        # that we can do something slightly different when an invitation
        # subscription is confirmed.  In those cases, we don't need further
        # admin approval, even if the list is so configured.  The flag is the
        # list name to prevent invitees from cross-subscribing.
        userdesc.invitation = self.internal_name()
        cookie = self.pend_new(Pending.SUBSCRIPTION, userdesc)
        requestaddr = self.getListAddress('request')
        confirmurl = '%s/%s' % (self.GetScriptURL('confirm', absolute=1),
                                cookie)
        listname = self.real_name
        text += Utils.maketext(
            'invite.txt',
            {'email'      : invitee,
             'listname'   : listname,
             'hostname'   : self.host_name,
             'confirmurl' : confirmurl,
             'requestaddr': requestaddr,
             'cookie'     : cookie,
             'listowner'  : self.GetOwnerEmail(),
             }, mlist=self)
        sender = self.GetRequestEmail(cookie)
        msg = Message.UserNotification(
            invitee, sender,
            text=text, lang=self.preferred_language)
        subj = self.GetConfirmJoinSubject(listname, cookie)
        del msg['subject']
        msg['Subject'] = subj
        del msg['auto-submitted']
        msg['Auto-Submitted'] = 'auto-generated'
        msg.send(self)

    def AddMember(self, userdesc, remote=None):
        """Front end to member subscription.

        This method enforces subscription policy, validates values, sends
        notifications, and any other grunt work involved in subscribing a
        user.  It eventually calls ApprovedAddMember() to do the actual work
        of subscribing the user.

        userdesc is an instance with the following public attributes:

            address  -- the unvalidated email address of the member
            fullname -- the member's full name (i.e. John Smith)
            digest   -- a flag indicating whether the user wants digests or not
            language -- the requested default language for the user
            password -- the user's password

        Other attributes may be defined later.  Only address is required; the
        others all have defaults (fullname='', digests=0, language=list's
        preferred language, password=generated).

        remote is a string which describes where this add request came from.
        """
        assert self.Locked()
        # Suck values out of userdesc, apply defaults, and reset the userdesc
        # attributes (for passing on to ApprovedAddMember()).  Lowercase the
        # addr's domain part.
        email = Utils.LCDomain(userdesc.address)
        name = getattr(userdesc, 'fullname', '')
        lang = getattr(userdesc, 'language', self.preferred_language)
        digest = getattr(userdesc, 'digest', None)
        password = getattr(userdesc, 'password', Utils.MakeRandomPassword())
        if digest is None:
            if self.nondigestable:
                digest = 0
            else:
                digest = 1
        # Validate the e-mail address to some degree.
        Utils.ValidateEmail(email)
        if self.isMember(email):
            raise Errors.MMAlreadyAMember(email)
        if self.CheckPending(email):
            raise Errors.MMAlreadyPending(email)
        if email.lower() == self.GetListEmail().lower():
            # Trying to subscribe the list to itself!
            raise Errors.MMBadEmailError
        realname = self.real_name
        # Is the subscribing address banned from this list?
        pattern = self.GetBannedPattern(email)
        if pattern:
            if remote:
                whence = ' from %s' % remote
            else:
                whence = ''
            syslog('vette', '%s banned subscription: %s%s (matched: %s)',
                   realname, email, whence, pattern)
            raise Errors.MembershipIsBanned(pattern)
        # See if this is from a spamhaus listed IP.
        if remote and mm_cfg.BLOCK_SPAMHAUS_LISTED_IP_SUBSCRIBE:
            if Utils.banned_ip(remote):
                whence = ' from %s' % remote
                syslog('vette', '%s banned subscription: %s%s (Spamhaus IP)',
                       realname, email, whence)
                raise Errors.MembershipIsBanned('Spamhaus IP')
        # See if this is from a spamhaus listed domain.
        if email and mm_cfg.BLOCK_SPAMHAUS_LISTED_DBL_SUBSCRIBE:
            if Utils.banned_domain(email):
                syslog('vette', '%s banned subscription: %s (Spamhaus DBL)',
                       realname, email)
                raise Errors.MembershipIsBanned('Spamhaus DBL')
        # Sanity check the digest flag
        if digest and not self.digestable:
            raise Errors.MMCantDigestError
        elif not digest and not self.nondigestable:
            raise Errors.MMMustDigestError

        userdesc.address = email
        userdesc.fullname = name
        userdesc.digest = digest
        userdesc.language = lang
        userdesc.password = password

        # Apply the list's subscription policy.  0 means open subscriptions; 1
        # means the user must confirm; 2 means the admin must approve; 3 means
        # the user must confirm and then the admin must approve
        if self.subscribe_policy == 0:
            self.ApprovedAddMember(userdesc, whence=remote or '')
        elif self.subscribe_policy == 1 or self.subscribe_policy == 3:
            # User confirmation required.  BAW: this should probably just
            # accept a userdesc instance.
            cookie = self.pend_new(Pending.SUBSCRIPTION, userdesc)
            # Send the user the confirmation mailback
            if remote is None:
                oremote = by = remote = ''
            else:
                oremote = remote
                by = ' ' + remote
                remote = _(' from %(remote)s')

            recipient = self.GetMemberAdminEmail(email)
            confirmurl = '%s/%s' % (self.GetScriptURL('confirm', absolute=1),
                                    cookie)
            text = Utils.maketext(
                'verify.txt',
                {'email'       : email,
                 'listaddr'    : self.GetListEmail(),
                 'listname'    : realname,
                 'cookie'      : cookie,
                 'requestaddr' : self.getListAddress('request'),
                 'remote'      : remote,
                 'listadmin'   : self.GetOwnerEmail(),
                 'confirmurl'  : confirmurl,
                 }, lang=lang, mlist=self)
            msg = Message.UserNotification(
                recipient, self.GetRequestEmail(cookie),
                text=text, lang=lang)
            # BAW: See ChangeMemberAddress() for why we do it this way...
            del msg['subject']
            msg['Subject'] = self.GetConfirmJoinSubject(realname, cookie)
            msg['Reply-To'] = self.GetRequestEmail(cookie)
            # Is this confirmation a reply to an email subscribe from this
            # address?
            if oremote.lower().endswith(email.lower()):
                autosub = 'auto-replied'
            else:
                autosub = 'auto-generated'
            del msg['auto-submitted']
            msg['Auto-Submitted'] = autosub
            msg.send(self)

            # formataddr() expects a str and does its own encoding
            if isinstance(name, bytes):
                name = name.decode(Utils.GetCharSet(lang))

            who = formataddr((name, email))
            syslog('subscribe', '%s: pending %s %s',
                   self.internal_name(), who, by)
            raise Errors.MMSubscribeNeedsConfirmation
        elif self.HasAutoApprovedSender(email):
            # no approval necessary:
            self.ApprovedAddMember(userdesc)
        else:
            # Subscription approval is required.  Add this entry to the admin
            # requests database.  BAW: this should probably take a userdesc
            # just like above.
            self.HoldSubscription(email, name, password, digest, lang)
            raise Errors.MMNeedApproval(
                'subscriptions to %(realname)s require moderator approval')

    def ApprovedAddMember(self, userdesc, ack=None, admin_notif=None, text='',
                          whence=''):
        """Add a member right now.

        The member's subscription must be approved by what ever policy the
        list enforces.

        userdesc is as above in AddMember().

        ack is a flag that specifies whether the user should get an
        acknowledgement of their being subscribed.  Default is to use the
        list's default flag value.

        admin_notif is a flag that specifies whether the list owner should get
        an acknowledgement of this subscription.  Default is to use the list's
        default flag value.
        """
        assert self.Locked()
        # Set up default flag values
        if ack is None:
            ack = self.send_welcome_msg
        if admin_notif is None:
            admin_notif = self.admin_notify_mchanges
        # Suck values out of userdesc, and apply defaults.
        email = Utils.LCDomain(userdesc.address)
        name = getattr(userdesc, 'fullname', '')
        lang = getattr(userdesc, 'language', self.preferred_language)
        digest = getattr(userdesc, 'digest', None)
        password = getattr(userdesc, 'password', Utils.MakeRandomPassword())
        if digest is None:
            if self.nondigestable:
                digest = 0
            else:
                digest = 1
        # Let's be extra cautious
        Utils.ValidateEmail(email)
        if self.isMember(email):
            raise Errors.MMAlreadyAMember(email)
        # Check for banned address here too for admin mass subscribes
        # and confirmations.
        pattern = self.GetBannedPattern(email)
        if pattern:
            if whence:
                source = ' from %s' % whence
            else:
                source = ''
            syslog('vette', '%s banned subscription: %s%s (matched: %s)',
                   self.real_name, email, source, pattern)
            raise Errors.MembershipIsBanned(pattern)
        # Do the actual addition
        self.addNewMember(email, realname=name, digest=digest,
                          password=password, language=lang)
        self.setMemberOption(email, mm_cfg.DisableMime,
                             1 - self.mime_is_default_digest)
        self.setMemberOption(email, mm_cfg.Moderate,
                             self.default_member_moderation)
        # Now send and log results
        if digest:
            kind = ' (digest)'
        else:
            kind = ''

        # The formataddr() function, used in two places below, takes a str and performs
        # its own encoding, so we should not allow the name to be pre-encoded.
        if isinstance(name, bytes):
            name = name.decode(Utils.GetCharSet(lang))

        syslog('subscribe', '%s: new%s %s, %s', self.internal_name(),
               kind, formataddr((name, email)), whence)
        if ack:
            lang = self.preferred_language
            otrans = i18n.get_translation()
            i18n.set_language(lang)
            try:
                self.SendSubscribeAck(email, self.getMemberPassword(email),
                                      digest, text)
            finally:
                i18n.set_translation(otrans)
        if admin_notif:
            lang = self.preferred_language
            otrans = i18n.get_translation()
            i18n.set_language(lang)
            try:
                whence = "" if whence is None else "(" + _(whence) + ")"
                realname = self.real_name
                subject = _('%(realname)s subscription notification')
            finally:
                i18n.set_translation(otrans)

            text = Utils.maketext(
                "adminsubscribeack.txt",
                {"listname" : realname,
                 "member"   : formataddr((name, email)),
                 "whence"   : whence
                 }, mlist=self)
            msg = Message.OwnerNotification(self, subject, text)
            msg.send(self)

    def DeleteMember(self, name, whence=None, admin_notif=None, userack=True):
        realname, email = parseaddr(name)
        if self.unsubscribe_policy == 0:
            self.ApprovedDeleteMember(name, whence, admin_notif, userack)
        else:
            self.HoldUnsubscription(email)
            raise Errors.MMNeedApproval('unsubscriptions require moderator approval')

    def ApprovedDeleteMember(self, name, whence=None,
                             admin_notif=None, userack=None):
        if userack is None:
            userack = self.send_goodbye_msg
        if admin_notif is None:
            admin_notif = self.admin_notify_mchanges
        # Delete a member, for which we know the approval has been made
        fullname, emailaddr = parseaddr(name)
        userlang = self.getMemberLanguage(emailaddr)
        # Remove the member
        self.removeMember(emailaddr)
        # And send an acknowledgement to the user...
        if userack:
            self.SendUnsubscribeAck(emailaddr, userlang)
        # ...and to the administrator in the correct language.  (LP: #1308655)
        i18n.set_language(self.preferred_language)
        if admin_notif:
            realname = self.real_name
            subject = _('%(realname)s unsubscribe notification')
            text = Utils.maketext(
                'adminunsubscribeack.txt',
                {'member'  : name,
                 'listname': self.real_name,
                 "whence"   : "" if whence is None else "(" + _(whence) + ")"
                 }, mlist=self)
            msg = Message.OwnerNotification(self, subject, text)
            msg.send(self)
        if whence:
            whence = "; %s" % whence
        else:
            whence = ""
        syslog('subscribe', '%s: deleted %s%s',
               self.internal_name(), name, whence)

    def ChangeMemberName(self, addr, name, globally):
        self.setMemberName(addr, name)
        if not globally:
            return
        for listname in Utils.list_names():
            # Don't bother with ourselves
            if listname == self.internal_name():
                continue
            mlist = MailList(listname, lock=0)
            if mlist.host_name != self.host_name:
                continue
            if not mlist.isMember(addr):
                continue
            mlist.Lock()
            try:
                mlist.setMemberName(addr, name)
                mlist.Save()
            finally:
                mlist.Unlock()

    def ChangeMemberAddress(self, oldaddr, newaddr, globally):
        # Changing a member address consists of verifying the new address,
        # making sure the new address isn't already a member, and optionally
        # going through the confirmation process.
        #
        # Most of these checks are copied from AddMember
        newaddr = Utils.LCDomain(newaddr)
        Utils.ValidateEmail(newaddr)
        # Raise an exception if this email address is already a member of the
        # list, but only if the new address is the same case-wise as the
        # existing member address and we're not doing a global change.
        if not globally and (self.isMember(newaddr) and
                newaddr == self.getMemberCPAddress(newaddr)):
            raise Errors.MMAlreadyAMember
        if newaddr == self.GetListEmail().lower():
            raise Errors.MMBadEmailError
        realname = self.real_name
        # Don't allow changing to a banned address. MAS: maybe we should
        # unsubscribe the oldaddr too just for trying, but that's probably
        # too harsh.
        pattern = self.GetBannedPattern(newaddr)
        if pattern:
            syslog('vette',
                   '%s banned address change: %s -> %s (matched: %s)',
                   realname, oldaddr, newaddr, pattern)
            raise Errors.MembershipIsBanned(pattern)
        # Pend the subscription change
        cookie = self.pend_new(Pending.CHANGE_OF_ADDRESS,
                               oldaddr, newaddr, globally)
        confirmurl = '%s/%s' % (self.GetScriptURL('confirm', absolute=1),
                                cookie)
        lang = self.getMemberLanguage(oldaddr)
        text = Utils.maketext(
            'verify.txt',
            {'email'      : newaddr,
             'listaddr'   : self.GetListEmail(),
             'listname'   : realname,
             'cookie'     : cookie,
             'requestaddr': self.getListAddress('request'),
             'remote'     : '',
             'listadmin'  : self.GetOwnerEmail(),
             'confirmurl' : confirmurl,
             }, lang=lang, mlist=self)
        # BAW: We don't pass the Subject: into the UserNotification
        # constructor because it will encode it in the charset of the language
        # being used.  For non-us-ascii charsets, this means it will probably
        # quopri quote it, and thus replies will also be quopri encoded.  But
        # CommandRunner doesn't yet grok such headers.  So, just set the
        # Subject: in a separate step, although we have to delete the one
        # UserNotification adds.
        msg = Message.UserNotification(
            newaddr, self.GetRequestEmail(cookie),
            text=text, lang=lang)
        del msg['subject']
        msg['Subject'] = self.GetConfirmJoinSubject(realname, cookie)
        msg['Reply-To'] = self.GetRequestEmail(cookie)
        msg.send(self)

    def ApprovedChangeMemberAddress(self, oldaddr, newaddr, globally):
        # Check here for banned address in case address was banned after
        # confirmation was mailed. MAS: If it's global change should we just
        # skip this list and proceed to the others? For now we'll throw the
        # exception.
        pattern = self.GetBannedPattern(newaddr)
        if pattern:
            syslog('vette',
                   '%s banned address change: %s -> %s (matched: %s)',
                   self.real_name, oldaddr, newaddr, pattern)
            raise Errors.MembershipIsBanned(pattern)
        # It's possible they were a member of this list, but choose to change
        # their membership globally.  In that case, we simply remove the old
        # address.  This gets tricky with case changes.  We can't just remove
        # the old address if it differs from the new only by case, because
        # that removes the new, so the condition is if the new address is the
        # CP address of a member, then if the old address yields a different
        # CP address, we can simply remove the old address, otherwise we can
        # do nothing.
        cpoldaddr = self.getMemberCPAddress(oldaddr)
        if self.isMember(newaddr) and (self.getMemberCPAddress(newaddr) ==
                newaddr):
            if cpoldaddr != newaddr:
                self.removeMember(oldaddr)
        else:
            self.changeMemberAddress(oldaddr, newaddr)
            self.log_and_notify_admin(cpoldaddr, newaddr)
        # If globally is true, then we also include every list for which
        # oldaddr is a member.
        if not globally:
            return
        for listname in Utils.list_names():
            # Don't bother with ourselves
            if listname == self.internal_name():
                continue
            mlist = MailList(listname, lock=0)
            if mlist.host_name != self.host_name:
                continue
            if not mlist.isMember(oldaddr):
                continue
            # If new address is banned from this list, just skip it.
            if mlist.GetBannedPattern(newaddr):
                continue
            mlist.Lock()
            try:
                # Same logic as above, re newaddr is already a member
                cpoldaddr = mlist.getMemberCPAddress(oldaddr)
                if mlist.isMember(newaddr) and (
                        mlist.getMemberCPAddress(newaddr) == newaddr):
                    if cpoldaddr != newaddr:
                        mlist.removeMember(oldaddr)
                else:
                    mlist.changeMemberAddress(oldaddr, newaddr)
                    mlist.log_and_notify_admin(cpoldaddr, newaddr)
                mlist.Save()
            finally:
                mlist.Unlock()

    def log_and_notify_admin(self, oldaddr, newaddr):
        """Log member address change and notify admin if requested."""
        syslog('subscribe', '%s: changed member address from %s to %s',
               self.internal_name(), oldaddr, newaddr)
        if self.admin_notify_mchanges:
            lang = self.preferred_language
            otrans = i18n.get_translation()
            i18n.set_language(lang)
            try:
                realname = self.real_name
                subject = _('%(realname)s address change notification')
            finally:
                i18n.set_translation(otrans)
            name = self.getMemberName(newaddr)
            if name is None:
                name = ''
            if isinstance(name, str):
                name = name.encode(Utils.GetCharSet(lang), 'replace')
            text = Utils.maketext(
                'adminaddrchgack.txt',
                {'name'    : name,
                 'oldaddr' : oldaddr,
                 'newaddr' : newaddr,
                 'listname': self.real_name,
                 }, mlist=self)
            msg = Message.OwnerNotification(self, subject, text)
            msg.send(self)


    #
    # Confirmation processing
    #
    def ProcessConfirmation(self, cookie, context=None):
        global _
        rec = self.pend_confirm(cookie)
        if rec is None:
            raise Errors.MMBadConfirmation('No cookie record for %s' % cookie)
        try:
            op = rec[0]
            data = rec[1:]
        except ValueError:
            raise Errors.MMBadConfirmation('op-less data %s' % (rec,))
        if op == Pending.SUBSCRIPTION:
            _ = D_
            whence = _('via email confirmation')
            try:
                userdesc = data[0]
                # If confirmation comes from the web, context should be a
                # UserDesc instance which contains overrides of the original
                # subscription information.  If it comes from email, then
                # context is a Message and isn't relevant, so ignore it.
                if isinstance(context, UserDesc):
                    userdesc += context
                    whence = _('via web confirmation')
                addr = userdesc.address
                fullname = userdesc.fullname
                password = userdesc.password
                digest = userdesc.digest
                lang = userdesc.language
            except ValueError:
                raise Errors.MMBadConfirmation('bad subscr data %s' % (data,))
            _ = i18n._
            # Hack alert!  Was this a confirmation of an invitation?
            invitation = getattr(userdesc, 'invitation', False)
            # We check for both 2 (approval required) and 3 (confirm +
            # approval) because the policy could have been changed in the
            # middle of the confirmation dance.
            if invitation:
                if invitation != self.internal_name():
                    # Not cool.  The invitee was trying to subscribe to a
                    # different list than they were invited to.  Alert both
                    # list administrators.
                    self.SendHostileSubscriptionNotice(invitation, addr)
                    raise Errors.HostileSubscriptionError
            elif self.subscribe_policy in (2, 3) and \
                    not self.HasAutoApprovedSender(addr):
                self.HoldSubscription(addr, fullname, password, digest, lang)
                name = self.real_name
                raise Errors.MMNeedApproval(
                    'subscriptions to %(name)s require administrator approval')
            self.ApprovedAddMember(userdesc, whence=whence)
            return op, addr, password, digest, lang
        elif op == Pending.UNSUBSCRIPTION:
            addr = data[0]
            # Log file messages don't need to be i18n'd, but this is now in a
            # notice.
            _ = D_
            if isinstance(context, Message.Message):
                whence = _('email confirmation')
            else:
                whence = _('web confirmation')
            _ = i18n._
            # Can raise NotAMemberError if they unsub'd via other means
            self.ApprovedDeleteMember(addr, whence=whence)
            return op, addr
        elif op == Pending.CHANGE_OF_ADDRESS:
            oldaddr, newaddr, globally = data
            self.ApprovedChangeMemberAddress(oldaddr, newaddr, globally)
            return op, oldaddr, newaddr
        elif op == Pending.HELD_MESSAGE:
            id = data[0]
            approved = None
            # Confirmation should be coming from email, where context should
            # be the confirming message.  If the message does not have an
            # Approved: header, this is a discard.  If it has an Approved:
            # header that does not match the list password, then we'll notify
            # the list administrator that they used the wrong password.
            # Otherwise it's an approval.
            if isinstance(context, Message.Message):
                # See if it's got an Approved: header, either in the headers,
                # or in the first text/plain section of the response.  For
                # robustness, we'll accept Approve: as well.
                approved = context.get('Approved', context.get('Approve'))
                if not approved:
                    try:
                        subpart = list(email.iterators.typed_subpart_iterator(
                            context, 'text', 'plain'))[0]
                    except IndexError:
                        subpart = None
                    if subpart:
                        s = StringIO(subpart.get_payload(decode=True))
                        while True:
                            line = s.readline()
                            if not line:
                                break
                            if not line.strip():
                                continue
                            i = line.find(':')
                            if i > 0:
                                if (line[:i].strip().lower() == 'approve' or
                                    line[:i].strip().lower() == 'approved'):
                                    # then
                                    approved = line[i+1:].strip()
                            break
            # Is there an approved header?
            if approved is not None:
                # Does it match the list password?  Note that we purposefully
                # do not allow the site password here.
                if self.Authenticate([mm_cfg.AuthListAdmin,
                                      mm_cfg.AuthListModerator],
                                     approved) != mm_cfg.UnAuthorized:
                    action = mm_cfg.APPROVE
                else:
                    # The password didn't match.  Re-pend the message and
                    # inform the list moderators about the problem.
                    self.pend_repend(cookie, rec)
                    raise Errors.MMBadPasswordError
            else:
                action = mm_cfg.DISCARD
            try:
                self.HandleRequest(id, action)
            except KeyError:
                # Most likely because the message has already been disposed of
                # via the admindb page.
                syslog('error', 'Could not process HELD_MESSAGE: %s', id)
            return op, action
        elif op == Pending.RE_ENABLE:
            member = data[1]
            self.setDeliveryStatus(member, MemberAdaptor.ENABLED)
            return op, member
        else:
            assert 0, 'Bad op: %s' % op

    def ConfirmUnsubscription(self, addr, lang=None, remote=None):
        if self.CheckPending(addr, unsub=True):
            raise Errors.MMAlreadyPending(email)
        if lang is None:
            lang = self.getMemberLanguage(addr)
        cookie = self.pend_new(Pending.UNSUBSCRIPTION, addr)
        confirmurl = '%s/%s' % (self.GetScriptURL('confirm', absolute=1),
                                cookie)
        realname = self.real_name
        if remote is not None:
            by = " " + remote
            remote = _(" from %(remote)s")
        else:
            by = ""
            remote = ""
        text = Utils.maketext(
            'unsub.txt',
            {'email'       : addr,
             'listaddr'    : self.GetListEmail(),
             'listname'    : realname,
             'cookie'      : cookie,
             'requestaddr' : self.getListAddress('request'),
             'remote'      : remote,
             'listadmin'   : self.GetOwnerEmail(),
             'confirmurl'  : confirmurl,
             }, lang=lang, mlist=self)
        msg = Message.UserNotification(
            addr, self.GetRequestEmail(cookie),
            text=text, lang=lang)
            # BAW: See ChangeMemberAddress() for why we do it this way...
        del msg['subject']
        msg['Subject'] = self.GetConfirmLeaveSubject(realname, cookie)
        msg['Reply-To'] = self.GetRequestEmail(cookie)
        del msg['auto-submitted']
        msg['Auto-Submitted'] = 'auto-generated'
        msg.send(self)


    #
    # Miscellaneous stuff
    #
    def HasExplicitDest(self, msg):
        """True if list name or any acceptable_alias is included among the
        addresses in the recipient headers.
        """
        # This is the list's full address.
        listfullname = '%s@%s' % (self.internal_name(), self.host_name)
        recips = []
        # Check all recipient addresses against the list's explicit addresses,
        # specifically To: Cc: and Resent-to:
        to = []
        for header in ('to', 'cc', 'resent-to', 'resent-cc'):
            to.extend(getaddresses(msg.get_all(header, [])))
        for fullname, addr in to:
            # It's possible that if the header doesn't have a valid RFC 2822
            # value, we'll get None for the address.  So skip it.
            if addr is None:
                continue
            addr = addr.lower()
            localpart = addr.split('@')[0]
            if (# TBD: backwards compatibility: deprecated
                    localpart == self.internal_name() or
                    # exact match against the complete list address
                    addr == listfullname):
                return True
            recips.append((addr, localpart))
        # Helper function used to match a pattern against an address.
        def domatch(pattern, addr):
            try:
                if re.match(pattern, addr, re.IGNORECASE):
                    return True
            except re.error:
                # The pattern is a malformed regexp -- try matching safely,
                # with all non-alphanumerics backslashed:
                if re.match(re.escape(pattern), addr, re.IGNORECASE):
                    return True
            return False
        # Here's the current algorithm for matching acceptable_aliases:
        #
        # 1. If the pattern does not have an `@' in it, we first try matching
        #    it against just the localpart.  This was the behavior prior to
        #    2.0beta3, and is kept for backwards compatibility.  (deprecated).
        #
        # 2. If that match fails, or the pattern does have an `@' in it, we
        #    try matching against the entire recip address.
        aliases = self.acceptable_aliases.splitlines()
        for addr, localpart in recips:
            for alias in aliases:
                stripped = alias.strip()
                if not stripped:
                    # Ignore blank or empty lines
                    continue
                if '@' not in stripped and domatch(stripped, localpart):
                    return True
                if domatch(stripped, addr):
                    return True
        return False

    def parse_matching_header_opt(self):
        """Return a list of triples [(field name, regex, line), ...]."""
        # - Blank lines and lines with '#' as first char are skipped.
        # - Leading whitespace in the matchexp is trimmed - you can defeat
        #   that by, eg, containing it in gratuitous square brackets.
        all = []
        for line in self.bounce_matching_headers.split('\n'):
            line = line.strip()
            # Skip blank lines and lines *starting* with a '#'.
            if not line or line[0] == "#":
                continue
            i = line.find(':')
            if i < 0:
                # This didn't look like a header line.  BAW: should do a
                # better job of informing the list admin.
                syslog('config', 'bad bounce_matching_header line: %s\n%s',
                       self.real_name, line)
            else:
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
            msg = Message.UserNotification(
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
