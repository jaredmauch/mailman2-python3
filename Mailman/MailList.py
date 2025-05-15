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
from Mailman.LockFile import NotLockedError, AlreadyLockedError, TimeOutError
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
from Mailman.Message import Message
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
        # Validate list name early if provided
        if name is not None:
            # Problems and potential attacks can occur if the list name in the
            # pipe to the wrapper in an MTA alias or other delivery process
            # contains shell special characters so allow only defined characters
            # (default = '[-+_.=a-z0-9]').
            if not re.match(r'^' + mm_cfg.ACCEPTABLE_LISTNAME_CHARACTERS + r'+$', name, re.IGNORECASE):
                raise Errors.BadListNameError(name)
            # Validate what will be the list's posting address
            postingaddr = '%s@%s' % (name, mm_cfg.DEFAULT_EMAIL_HOST)
            try:
                Utils.ValidateEmail(postingaddr)
            except Errors.EmailAddressError:
                raise Errors.BadListNameError(postingaddr)

        # Only one level of mixin inheritance allowed
        for baseclass in self.__class__.__bases__:
            if hasattr(baseclass, '__init__'):
                baseclass.__init__(self)
        # Initialize volatile attributes
        self.InitTempVars(name)
        # Initialize data_version before any other operations
        self.data_version = mm_cfg.DATA_FILE_VERSION
        # Initialize default values
        self.InitVars(name)
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
        try:
            return getattr(self._memberadaptor, name)
        except AttributeError:
            for guicomponent in self._gui:
                try:
                    return getattr(guicomponent, name)
                except AttributeError:
                    pass
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
        if self.umbrella_list:
            return self.getListAddress('admin')
        return member

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

    def GetAvailableLanguages(self):
        """Return the list of available languages for this mailing list.
        
        This method ensures that the default server language is always included
        and filters out any languages that aren't in LC_DESCRIPTIONS.
        """
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

    #
    # Instance and subcomponent initialization
    #
    def InitTempVars(self, name):
        """Set transient variables of this and inherited classes."""
        # The timestamp is set whenever we load the state from disk.  If our
        # timestamp is newer than the modtime of the config.pck file, we don't
        # need to reload, otherwise... we do.
        self.__timestamp = 0
        # Ensure name is a string before using it in os.path.join
        if isinstance(name, bytes):
            try:
                # Try Latin-1 first since that's what we're seeing in the data
                name = name.decode('latin-1', 'replace')
            except UnicodeDecodeError:
                # Fall back to UTF-8 if Latin-1 fails
                name = name.decode('utf-8', 'replace')
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
        """Get configuration categories for the mailing list.
        
        Returns a custom dictionary-like object that maintains category order
        according to mm_cfg.ADMIN_CATEGORIES. Each category is stored as a
        tuple of (label, gui_object).
        """
        class CategoryDict(dict):
            def __init__(self):
                super(CategoryDict, self).__init__()
                self.keysinorder = mm_cfg.ADMIN_CATEGORIES[:]
            
            def keys(self):
                return self.keysinorder
            
            def items(self):
                items = []
                for k in mm_cfg.ADMIN_CATEGORIES:
                    if k in self:
                        items.append((k, self[k]))
                return items
            
            def values(self):
                values = []
                for k in mm_cfg.ADMIN_CATEGORIES:
                    if k in self:
                        values.append(self[k])
                return values

        categories = CategoryDict()
        # Only one level of mixin inheritance allowed
        for gui in self._gui:
            k, v = gui.GetConfigCategory()
            if isinstance(v, tuple):
                syslog('error', 'Category %s has tuple value: %s', k, str(v))
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
        # Get the category tuple from our categories dictionary
        category_info = self.GetConfigCategories().get(category)
        if not category_info:
            syslog('error', 'Category %s not found in configuration', category)
            return None
            
        # Extract the GUI object from the tuple (label, gui_object)
        gui_object = category_info[1]
        
        try:
            value = gui_object.GetConfigInfo(self, category, subcat)
            if value:
                return value
        except (AttributeError, KeyError) as e:
            # Log the error but continue trying other GUIs
            syslog('error', 'Error getting config info for %s/%s: %s',
                   category, subcat, str(e))
        return None

    #
    # List creation
    #
    def Create(self, name, admin, crypted_password,
               langs=None, emailhost=None, urlhost=None):
        # Ensure name is a string
        if isinstance(name, bytes):
            try:
                # Try Latin-1 first since that's what we're seeing in the data
                name = name.decode('latin-1', 'replace')
            except UnicodeDecodeError:
                # Fall back to UTF-8 if Latin-1 fails
                name = name.decode('utf-8', 'replace')
        if name != name.lower():
            raise ValueError('List name must be all lower case.')
        if Utils.list_exists(name):
            raise Errors.MMListAlreadyExistsError(name)
        # Problems and potential attacks can occur if the list name in the
        # pipe to the wrapper in an MTA alias or other delivery process
        # contains shell special characters so allow only defined characters
        # (default = '[-+_.=a-z0-9]').
        if len(re.sub(r'^' + mm_cfg.ACCEPTABLE_LISTNAME_CHARACTERS + r'+$', '', name, flags=re.IGNORECASE)) > 0:
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
    def __save(self, data_dict):
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
            # Use protocol 4 for Python 2/3 compatibility, with fix_imports for backward compatibility
            pickle.dump(data_dict, fp, protocol=4, fix_imports=True)
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
            # Remove existing backup file if it exists
            try:
                os.unlink(fname_last)
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise
            # Create new backup file
            os.link(fname, fname_last)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise
        os.rename(fname_tmp, fname)
        # Reset the timestamp
        self.__timestamp = os.path.getmtime(fname)

    def Save(self):
        """Save the mailing list's configuration to disk.
        
        This method refreshes the lock and saves all public attributes to disk.
        It handles lock errors gracefully and ensures proper cleanup.
        """
        # Refresh the lock, just to let other processes know we're still
        # interested in it.  This will raise a NotLockedError if we don't have
        # the lock (which is a serious problem!).  TBD: do we need to be more
        # defensive?
        try:
            self.__lock.refresh()
        except NotLockedError:
            # Lock was lost, try to reacquire it
            try:
                self.__lock.lock(timeout=10)  # Give it 10 seconds to acquire
            except (AlreadyLockedError, TimeOutError) as e:
                syslog('error', 'Could not reacquire lock during Save(): %s', str(e))
                raise
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
            def loadfunc(fp):
                try:
                    # Try UTF-8 first for newer files
                    return pickle.load(fp, fix_imports=True, encoding='utf-8')
                except (UnicodeDecodeError, pickle.UnpicklingError):
                    # Fall back to latin1 for older files
                    fp.seek(0)
                    return pickle.load(fp, fix_imports=True, encoding='latin1')
        else:
            raise ValueError('Bad database file name')
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
            # Open the file in binary mode to avoid any text decoding
            fp = open(dbfile, 'rb')
        except EnvironmentError as e:
            if e.errno != errno.ENOENT:
                raise
            # The file doesn't exist yet
            return None, e

        try:
            dict = loadfunc(fp)
            fp.close()
            return dict, None
        except Exception as e:
            fp.close()
            return None, e

    def Load(self, check_version=True):
        """Load the database file."""
        # We want to check the version number of the database file, but we
        # don't want to do this more than once per process.  We use a class
        # attribute to decide whether we need to check the version or not.
        # Note that this is a bit of a hack because we use the class
        # attribute to store state information.  We could use a global
        # variable, but that would be even worse.
        if check_version:
            self.CheckVersion()
        # Load the database file.  If it doesn't exist yet, we'll get an
        # EnvironmentError with errno set to ENOENT.  If it exists but is
        # corrupt, we'll get an IOError.  In either case, we want to try to
        # load the backup file.
        fname = os.path.join(self.fullpath(), 'config.pck')
        fname_last = fname + '.last'
        dict, e = self.__load(fname)
        if dict is None and e is not None:
            # Try loading the backup file.
            dict, e = self.__load(fname_last)
            if dict is None and e is not None:
                # Both files are corrupt or non-existent.  If they're
                # corrupt, we want to raise an error.  If they're
                # non-existent, we want to return an empty dictionary.
                if isinstance(e, EnvironmentError) and e.errno == errno.ENOENT:
                    dict = {}
            else:
                    raise Errors.MMCorruptListDatabaseError(self.internal_name())
        # Now update our current state with the database state.
        for k, v in list(dict.items()):
            if k[0] != '_':
                setattr(self, k, v)
        # Set the timestamp to the current time.
        self.__timestamp = os.path.getmtime(fname)

    def CheckVersion(self):
        """Check the version of the list's config database.

        If the database version is not current, update the database format.
        """
        # Increment this variable when the database format changes.  This allows
        # for a bit more graceful recovery when upgrading.  BAW: This algorithm
        # sucks.  We really should be using a version number on the class and
        # marshalling and unmarshalling based on that.  This should be fixed by
        # MM3.0.
        data_version = getattr(self, 'data_version', 0)
        if data_version >= mm_cfg.DATA_FILE_VERSION:
            return

        # Pre-2.1a3 versions did not have a data_version
        if data_version == 0:
            # First, convert to all lowercase
            keys = list(self.__dict__.keys())
            for k in keys:
                self.__dict__[k.lower()] = self.__dict__.pop(k)
            # Then look for old names and convert
            for oldname, newname in (('num_members', 'member_count'),
                                   ('num_digest_members', 'digest_member_count'),
                                   ('closed', 'subscribe_policy'),
                                   ('mlist', 'real_name'),
                                   ('msg_text', 'msg_footer'),
                                   ('msg_headers', 'msg_header'),
                                   ('digest_msg_text', 'digest_footer'),
                                   ('digest_headers', 'digest_header'),
                                   ('posters', 'accept_these_nonmembers'),
                                   ('members_list', 'members'),
                                   ('digest_members_list', 'digest_members'),
                                   ('passwords', 'member_passwords'),
                                   ('bad_posters', 'hold_these_nonmembers'),
                                   ('topics_list', 'topics'),
                                   ('topics_usernames', 'topics_userinterest'),
                                   ('bounce_info', 'bounce_info'),
                                   ('delivery_status', 'delivery_status'),
                                   ('usernames', 'usernames'),
                                   ('sender_filter_bypass', 'accept_these_nonmembers'),
                                   ('admin_member_chunksize', 'admin_member_chunksize'),
                                   ('administrivia', 'administrivia'),
                                   ('advertised', 'advertised'),
                                   ('anonymous_list', 'anonymous_list'),
                                   ('auto_subscribe', 'auto_subscribe'),
                                   ('bounce_matching_headers', 'bounce_matching_headers'),
                                   ('bounce_processing', 'bounce_processing'),
                                   ('convert_html_to_plaintext', 'convert_html_to_plaintext'),
                                   ('digestable', 'digestable'),
                                   ('digest_is_default', 'digest_is_default'),
                                   ('digest_size_threshhold', 'digest_size_threshhold'),
                                   ('filter_content', 'filter_content'),
                                   ('generic_nonmember_action', 'generic_nonmember_action'),
                                   ('include_list_post_header', 'include_list_post_header'),
                                   ('include_rfc2369_headers', 'include_rfc2369_headers'),
                                   ('max_message_size', 'max_message_size'),
                                   ('max_num_recipients', 'max_num_recipients'),
                                   ('member_moderation_notice', 'member_moderation_notice'),
                                   ('mime_is_default_digest', 'mime_is_default_digest'),
                                   ('moderator_password', 'moderator_password'),
                                   ('next_digest_number', 'next_digest_number'),
                                   ('nondigestable', 'nondigestable'),
                                   ('nonmember_rejection_notice', 'nonmember_rejection_notice'),
                                   ('obscure_addresses', 'obscure_addresses'),
                                   ('owner_password', 'owner_password'),
                                   ('post_password', 'post_password'),
                                   ('private_roster', 'private_roster'),
                                   ('real_name', 'real_name'),
                                   ('reject_these_nonmembers', 'reject_these_nonmembers'),
                                   ('reply_goes_to_list', 'reply_goes_to_list'),
                                   ('reply_to_address', 'reply_to_address'),
                                   ('require_explicit_destination', 'require_explicit_destination'),
                                   ('send_reminders', 'send_reminders'),
                                   ('send_welcome_msg', 'send_welcome_msg'),
                                   ('subject_prefix', 'subject_prefix'),
                                   ('topics', 'topics'),
                                   ('topics_enabled', 'topics_enabled'),
                                   ('umbrella_list', 'umbrella_list'),
                                   ('unsubscribe_policy', 'unsubscribe_policy'),
                                   ('volume', 'volume'),
                                   ('web_page_url', 'web_page_url'),
                                   ('welcome_msg', 'welcome_msg'),
                                   ('gateway_to_mail', 'gateway_to_mail'),
                                   ('gateway_to_news', 'gateway_to_news'),
                                   ('linked_newsgroup', 'linked_newsgroup'),
                                   ('nntp_host', 'nntp_host'),
                                   ('news_moderation', 'news_moderation'),
                                   ('news_prefix_subject_too', 'news_prefix_subject_too'),
                                   ('archive', 'archive'),
                                   ('archive_private', 'archive_private'),
                                   ('archive_volume_frequency', 'archive_volume_frequency'),
                                   ('clobber_date', 'clobber_date'),
                                   ('convert_html_to_plaintext', 'convert_html_to_plaintext'),
                                   ('filter_content', 'filter_content'),
                                   ('hold_these_nonmembers', 'hold_these_nonmembers'),
                                   ('linked_newsgroup', 'linked_newsgroup'),
                                   ('max_message_size', 'max_message_size'),
                                   ('max_num_recipients', 'max_num_recipients'),
                                   ('news_prefix_subject_too', 'news_prefix_subject_too'),
                                   ('nntp_host', 'nntp_host'),
                                   ('obscure_addresses', 'obscure_addresses'),
                                   ('private_roster', 'private_roster'),
                                   ('real_name', 'real_name'),
                                   ('subject_prefix', 'subject_prefix'),
                                   ('topics', 'topics'),
                                   ('topics_enabled', 'topics_enabled'),
                                   ('web_page_url', 'web_page_url')):
                if oldname in self.__dict__:
                    self.__dict__[newname] = self.__dict__.pop(oldname)
            # Convert the data version number
            self.data_version = mm_cfg.DATA_FILE_VERSION

    def GetPattern(self, addr, patterns, at_list=None):
        """Check if an address matches any of the patterns in the list.
        
        Args:
            addr: The email address to check
            patterns: List of patterns to check against
            at_list: Optional name of the list for logging
            
        Returns:
            True if the address matches any pattern, False otherwise
        """
        if not patterns:
            return False
            
        # Convert addr to lowercase for case-insensitive matching
        addr = addr.lower()
        
        # Check each pattern
        for pattern in patterns:
            # Skip empty patterns
            if not pattern.strip():
                continue
                
            # If pattern starts with @, it's a domain pattern
            if pattern.startswith('@'):
                domain = pattern[1:].lower()
                if addr.endswith(domain):
                    if at_list:
                        syslog('vette', '%s matches domain pattern %s in %s',
                               addr, pattern, at_list)
                    return True
            # Otherwise it's a regex pattern
            else:
                try:
                    cre = re.compile(pattern, re.IGNORECASE)
                    if cre.search(addr):
                        if at_list:
                            syslog('vette', '%s matches regex pattern %s in %s',
                                   addr, pattern, at_list)
                        return True
                except re.error:
                    syslog('error', 'Invalid regex pattern in %s: %s',
                           at_list or 'patterns', pattern)
                    continue
                    
        return False

    def HasExplicitDest(self, msg):
        """Check if the message has an explicit destination.
        
        Args:
            msg: The email message to check
            
        Returns:
            True if the message has an explicit destination, False otherwise
        """
        # Check if the message has a To: or Cc: header
        if msg.get('to') or msg.get('cc'):
            return True
            
        # Check if the message has a Resent-To: or Resent-Cc: header
        if msg.get('resent-to') or msg.get('resent-cc'):
            return True
            
        # Check if the message has a Delivered-To: header
        if msg.get('delivered-to'):
            return True
            
        return False

    def hasMatchingHeader(self, msg):
        """Check if the message has any headers that match the bounce_matching_headers list.
        
        Args:
            msg: The email message to check
            
        Returns:
            True if any header matches, False otherwise
        """
        if not self.bounce_matching_headers:
            return False
            
        # Check each header in the message
        for header in msg.keys():
            header_value = msg.get(header, '').lower()
            # Check if this header matches any pattern in bounce_matching_headers
            for pattern in self.bounce_matching_headers:
                try:
                    cre = re.compile(pattern, re.IGNORECASE)
                    if cre.search(header_value):
                        syslog('vette', 'Message header %s matches pattern %s',
                               header, pattern)
                        return True
                except re.error:
                    syslog('error', 'Invalid regex pattern in bounce_matching_headers: %s',
                           pattern)
                    continue
                    
        return False

    def _ListAdmin__nextid(self):
        """Generate the next unique ID for a held message.
        
        Returns:
            An integer containing the next unique ID
        """
        # Get the next ID number
        nextid = getattr(self, '_ListAdmin__nextid_counter', 0) + 1
        # Store the next ID number
        self._ListAdmin__nextid_counter = nextid
        # Return just the counter number
        return nextid

    def ConfirmUnsubscription(self, addr, lang=None, remote=None):
        """Confirm an unsubscription request.

        :param addr: The address to unsubscribe.
        :type addr: string
        :param lang: The language to use for the confirmation message.
        :type lang: string
        :param remote: The remote address making the request.
        :type remote: string
        :raises: MMAlreadyPending if there's already a pending request
        """
        # Make sure we have a lock
        assert self._locked, 'List must be locked before pending operations'
        
        # Get the member's language if not specified
        if lang is None:
            lang = self.getMemberLanguage(addr)
            
        # Create a pending request
        cookie = self.pend_new(Pending.UNSUBSCRIPTION, addr)
        
        # Craft the confirmation message
        d = {
            'listname': self.real_name,
            'email': addr,
            'listaddr': self.GetListEmail(),
            'remote': remote and f'from {remote}' or '',
            'confirmurl': '%s/%s' % (self.GetScriptURL('confirm', absolute=1), cookie),
            'requestaddr': self.GetRequestEmail(cookie),
            'cookie': cookie,
            'listadmin': self.GetOwnerEmail(),
        }
        
        # Send the confirmation message
        subject = self.GetConfirmLeaveSubject(self.real_name, cookie)
        text = Utils.maketext('unsub.txt', d, lang=lang, mlist=self)
        msg = Message.UserNotification(addr, self.GetRequestEmail(cookie),
                                     subject, text, lang)
        msg.send(self)
        
        return cookie

    def InviteNewMember(self, userdesc, text=''):
        """Invite a new member to the list."""
        invitee = userdesc.address
        Utils.ValidateEmail(invitee)
        pattern = self.GetBannedPattern(invitee)
        if pattern:
            syslog('vette', '%s banned invitation: %s (matched: %s)',
                   self.real_name, invitee, pattern)
            raise Errors.MembershipIsBanned(pattern)
        userdesc.invitation = self.internal_name()
        cookie = self.pend_new(Pending.SUBSCRIPTION, userdesc)
        requestaddr = self.getListAddress('request')
        confirmurl = '%s/%s' % (self.GetScriptURL('confirm', absolute=1), cookie)
        listname = self.real_name
        text += Utils.maketext(
            'invite.txt',
            {'email': invitee,
             'listname': listname,
             'hostname': self.host_name,
             'confirmurl': confirmurl,
             'requestaddr': requestaddr,
             'cookie': cookie,
             'listowner': self.GetOwnerEmail(),
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
        """Front end to member subscription."""
        assert self.Locked()
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
        Utils.ValidateEmail(email)
        if self.isMember(email):
            raise Errors.MMAlreadyAMember(email)
        if self.CheckPending(email):
            raise Errors.MMAlreadyPending(email)
        if email.lower() == self.GetListEmail().lower():
            raise Errors.MMBadEmailError
        realname = self.real_name
        pattern = self.GetBannedPattern(email)
        if pattern:
            whence = f' from {remote}' if remote else ''
            syslog('vette', '%s banned subscription: %s%s (matched: %s)',
                   realname, email, whence, pattern)
            raise Errors.MembershipIsBanned(pattern)
        if remote and getattr(mm_cfg, 'BLOCK_SPAMHAUS_LISTED_IP_SUBSCRIBE', False):
            if Utils.banned_ip(remote):
                whence = f' from {remote}'
                syslog('vette', '%s banned subscription: %s%s (Spamhaus IP)',
                       realname, email, whence)
                raise Errors.MembershipIsBanned('Spamhaus IP')
        if email and getattr(mm_cfg, 'BLOCK_SPAMHAUS_LISTED_DBL_SUBSCRIBE', False):
            if Utils.banned_domain(email):
                syslog('vette', '%s banned subscription: %s (Spamhaus DBL)',
                       realname, email)
                raise Errors.MembershipIsBanned('Spamhaus DBL')
        if digest and not self.digestable:
            raise Errors.MMCantDigestError
        elif not digest and not self.nondigestable:
            raise Errors.MMMustDigestError
        userdesc.address = email
        userdesc.fullname = name
        userdesc.digest = digest
        userdesc.language = lang
        userdesc.password = password
        if self.subscribe_policy == 0:
            self.ApprovedAddMember(userdesc, whence=remote or '')
        elif self.subscribe_policy == 1 or self.subscribe_policy == 3:
            cookie = self.pend_new(Pending.SUBSCRIPTION, userdesc)
            if remote is None:
                oremote = by = remote = ''
            else:
                oremote = remote
                by = ' ' + remote
                remote = _(' from %(remote)s')
            recipient = self.GetMemberAdminEmail(email)
            confirmurl = '%s/%s' % (self.GetScriptURL('confirm', absolute=1), cookie)
            text = Utils.maketext(
                'verify.txt',
                {'email': email,
                 'listaddr': self.GetListEmail(),
                 'listname': realname,
                 'cookie': cookie,
                 'requestaddr': self.getListAddress('request'),
                 'remote': remote,
                 'listadmin': self.GetOwnerEmail(),
                 'confirmurl': confirmurl,
                 }, lang=lang, mlist=self)
            msg = Message.UserNotification(
                recipient, self.GetRequestEmail(cookie),
                text=text, lang=lang)
            del msg['subject']
            msg['Subject'] = self.GetConfirmJoinSubject(realname, cookie)
            msg['Reply-To'] = self.GetRequestEmail(cookie)
            if oremote.lower().endswith(email.lower()):
                autosub = 'auto-replied'
            else:
                autosub = 'auto-generated'
            del msg['auto-submitted']
            msg['Auto-Submitted'] = autosub
            msg.send(self)
            who = formataddr((name, email))
            syslog('subscribe', '%s: pending %s %s',
                   self.internal_name(), who, by)
            raise Errors.MMSubscribeNeedsConfirmation
        elif self.HasAutoApprovedSender(email):
            self.ApprovedAddMember(userdesc)
        else:
            self.HoldSubscription(email, name, password, digest, lang)
            raise Errors.MMNeedApproval(
                f'subscriptions to {realname} require moderator approval')

    def ApprovedAddMember(self, userdesc, ack=None, admin_notif=None, text='', whence=''):
        """Add a member right now."""
        assert self.Locked()
        if ack is None:
            ack = self.send_welcome_msg
        if admin_notif is None:
            admin_notif = self.admin_notify_mchanges
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
        Utils.ValidateEmail(email)
        if self.isMember(email):
            raise Errors.MMAlreadyAMember(email)
        pattern = self.GetBannedPattern(email)
        if pattern:
            source = f' from {whence}' if whence else ''
            syslog('vette', '%s banned subscription: %s%s (matched: %s)',
                   self.real_name, email, source, pattern)
            raise Errors.MembershipIsBanned(pattern)
        self.addNewMember(email, realname=name, digest=digest,
                          password=password, language=lang)
        self.setMemberOption(email, mm_cfg.DisableMime,
                             1 - self.mime_is_default_digest)
        self.setMemberOption(email, mm_cfg.Moderate,
                             self.default_member_moderation)
        kind = ' (digest)' if digest else ''
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
                whence_str = "" if whence is None else f"({_(whence)})"
                realname = self.real_name
                subject = _('%(realname)s subscription notification')
            finally:
                i18n.set_translation(otrans)
            if isinstance(name, bytes):
                name = name.decode(Utils.GetCharSet(lang), 'replace')
            text = Utils.maketext(
                "adminsubscribeack.txt",
                {"listname": realname,
                 "member": formataddr((name, email)),
                 "whence": whence_str
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

    def ApprovedDeleteMember(self, name, whence=None, admin_notif=None, userack=None):
        if userack is None:
            userack = self.send_goodbye_msg
        if admin_notif is None:
            admin_notif = self.admin_notify_mchanges
        fullname, emailaddr = parseaddr(name)
        userlang = self.getMemberLanguage(emailaddr)
        self.removeMember(emailaddr)
        if userack:
            self.SendUnsubscribeAck(emailaddr, userlang)
        i18n.set_language(self.preferred_language)
        if admin_notif:
            realname = self.real_name
            subject = _('%(realname)s unsubscribe notification')
            text = Utils.maketext(
                'adminunsubscribeack.txt',
                {'member': name,
                 'listname': self.real_name,
                 "whence": "" if whence is None else f"({_(whence)})"
                 }, mlist=self)
            msg = Message.OwnerNotification(self, subject, text)
            msg.send(self)
        if whence:
            whence_str = f'; {whence}'
        else:
            whence_str = ''
        syslog('subscribe', '%s: deleted %s%s',
               self.internal_name(), name, whence_str)

    def ChangeMemberName(self, addr, name, globally):
        self.setMemberName(addr, name)
        if not globally:
            return
        for listname in Utils.list_names():
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
        newaddr = Utils.LCDomain(newaddr)
        Utils.ValidateEmail(newaddr)
        if not globally and (self.isMember(newaddr) and
                newaddr == self.getMemberCPAddress(newaddr)):
            raise Errors.MMAlreadyAMember
        if newaddr == self.GetListEmail().lower():
            raise Errors.MMBadEmailError
        realname = self.real_name
        pattern = self.GetBannedPattern(newaddr)
        if pattern:
            syslog('vette',
                   '%s banned address change: %s -> %s (matched: %s)',
                   realname, oldaddr, newaddr, pattern)
            raise Errors.MembershipIsBanned(pattern)
        cookie = self.pend_new(Pending.CHANGE_OF_ADDRESS,
                               oldaddr, newaddr, globally)
        confirmurl = '%s/%s' % (self.GetScriptURL('confirm', absolute=1),
                                cookie)
        lang = self.getMemberLanguage(oldaddr)
        text = Utils.maketext(
            'verify.txt',
            {'email': newaddr,
             'listaddr': self.GetListEmail(),
             'listname': realname,
             'cookie': cookie,
             'requestaddr': self.getListAddress('request'),
             'remote': '',
             'listadmin': self.GetOwnerEmail(),
             'confirmurl': confirmurl,
             }, lang=lang, mlist=self)
        msg = Message.UserNotification(
            newaddr, self.GetRequestEmail(cookie),
            text=text, lang=lang)
        del msg['subject']
        msg['Subject'] = self.GetConfirmJoinSubject(realname, cookie)
        msg['Reply-To'] = self.GetRequestEmail(cookie)
        msg.send(self)

    def ApprovedChangeMemberAddress(self, oldaddr, newaddr, globally):
        pattern = self.GetBannedPattern(newaddr)
        if pattern:
            syslog('vette',
                   '%s banned address change: %s -> %s (matched: %s)',
                   self.real_name, oldaddr, newaddr, pattern)
            raise Errors.MembershipIsBanned(pattern)
        cpoldaddr = self.getMemberCPAddress(oldaddr)
        if self.isMember(newaddr) and (self.getMemberCPAddress(newaddr) == newaddr):
            if cpoldaddr != newaddr:
                self.removeMember(oldaddr)
        else:
            self.changeMemberAddress(oldaddr, newaddr)
            self.log_and_notify_admin(cpoldaddr, newaddr)
        if not globally:
            return
        for listname in Utils.list_names():
            if listname == self.internal_name():
                continue
            mlist = MailList(listname, lock=0)
            if mlist.host_name != self.host_name:
                continue
            if not mlist.isMember(oldaddr):
                continue
            if mlist.GetBannedPattern(newaddr):
                continue
            mlist.Lock()
            try:
                mlist.ApprovedChangeMemberAddress(oldaddr, newaddr, False)
                mlist.Save()
            finally:
                mlist.Unlock()

    def log_and_notify_admin(self, oldaddr, newaddr):
        syslog('subscribe', '%s: changed address %s -> %s',
               self.internal_name(), oldaddr, newaddr)
