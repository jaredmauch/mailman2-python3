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

"""Mixin class for MailList which handles administrative requests.

Two types of admin requests are currently supported: adding members to a
closed or semi-closed list, and moderated posts.

Pending subscriptions which are requiring a user's confirmation are handled
elsewhere.
"""

from builtins import str
from builtins import object
import os
import time
import errno
import pickle
import marshal
from io import StringIO
import socket
import pwd
import grp

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

# Constants for request types
IGN = 0
HELDMSG = 1
SUBSCRIPTION = 2
UNSUBSCRIPTION = 3

# Return status from __handlepost()
DEFER = 0
REMOVE = 1
LOST = 2

DASH = '-'
NL = '\n'

class PermissionError(Exception):
    """Exception raised when there are permission issues with database operations."""
    def __init__(self, message):
        self.message = message
        super().__init__(message)

class ListAdmin(object):
    def InitVars(self):
        # non-configurable data
        self.next_request_id = 1

    def InitTempVars(self):
        self.__db = None
        self.__filename = os.path.join(self.fullpath(), 'request.pck')

    def __opendb(self):
        """Open the database file and load data with improved error handling."""
        if self.__db is not None:
            return

        filename = os.path.join(mm_cfg.DATA_DIR, 'pending.pck')
        filename_backup = filename + '.bak'

        def log_file_info(path):
            try:
                # Log process identity information
                euid = os.geteuid()
                egid = os.getegid()
                ruid = os.getuid()
                rgid = os.getgid()
                groups = os.getgroups()
                
                # Get group names for supplementary groups
                group_names = []
                for gid in groups:
                    try:
                        group_names.append(grp.getgrgid(gid)[0])
                    except KeyError:
                        group_names.append(f'gid {gid}')
                
                mailman_log('error', 
                           'Process identity - EUID: %d, EGID: %d, RUID: %d, RGID: %d, Groups: %s',
                           euid, egid, ruid, rgid, ', '.join(group_names))
                
                # Get file information
                stat = os.stat(path)
                mode = stat.st_mode
                uid = stat.st_uid
                gid = stat.st_gid
                
                # Get current ownership info
                try:
                    current_user = pwd.getpwuid(uid)[0]
                except KeyError:
                    current_user = f'uid {uid}'
                try:
                    current_group = grp.getgrgid(gid)[0]
                except KeyError:
                    current_group = f'gid {gid}'
                
                # Get expected ownership info
                try:
                    expected_user = pwd.getpwnam(mm_cfg.MAILMAN_USER)[0]
                    expected_uid = pwd.getpwnam(mm_cfg.MAILMAN_USER)[2]
                except KeyError:
                    expected_user = f'user {mm_cfg.MAILMAN_USER}'
                    expected_uid = None
                
                try:
                    expected_group = grp.getgrnam(mm_cfg.MAILMAN_GROUP)[0]
                    expected_gid = grp.getgrnam(mm_cfg.MAILMAN_GROUP)[2]
                except KeyError:
                    expected_group = f'group {mm_cfg.MAILMAN_GROUP}'
                    expected_gid = None
                
                # Log current and expected ownership
                mailman_log('error', 
                           'File %s: mode=%o, owner=%s (current) vs %s (expected), group=%s (current) vs %s (expected)',
                           path, mode, current_user, expected_user, current_group, expected_group)
                
                # Log specific permission issues
                if expected_uid is not None and uid != expected_uid:
                    mailman_log('error', 'File %s has incorrect owner (uid %d vs expected %d)',
                               path, uid, expected_uid)
                if expected_gid is not None and gid != expected_gid:
                    mailman_log('error', 'File %s has incorrect group (gid %d vs expected %d)',
                               path, gid, expected_gid)
                if mode & 0o002:  # World writable
                    mailman_log('error', 'File %s is world writable (mode %o)',
                               path, mode)
                if mode & 0o020 and (expected_gid is None or gid != expected_gid):  # Group writable but not owned by mailman group
                    mailman_log('error', 'File %s is group writable but not owned by mailman group',
                               path)
            except OSError as e:
                mailman_log('error', 'Could not stat %s: %s', path, str(e))

        # Try loading the main file first
        try:
            with open(filename, 'rb') as fp:
                try:
                    self.__db = pickle.load(fp, fix_imports=True, encoding='latin1')
                    if not isinstance(self.__db, dict):
                        raise ValueError("Database not a dictionary")
                    return
                except (EOFError, ValueError, TypeError, pickle.UnpicklingError) as e:
                    mailman_log('error', 'Error loading pending.pck: %s', str(e))
                    log_file_info(filename)

            # If we get here, the main file failed to load properly
            if os.path.exists(filename_backup):
                mailman_log('info', 'Attempting to load from backup file')
                with open(filename_backup, 'rb') as fp:
                    try:
                        self.__db = pickle.load(fp, fix_imports=True, encoding='latin1')
                        if not isinstance(self.__db, dict):
                            raise ValueError("Backup database not a dictionary")
                        # Successfully loaded backup, restore it as main
                        import shutil
                        shutil.copy2(filename_backup, filename)
                        return
                    except (EOFError, ValueError, TypeError, pickle.UnpicklingError) as e:
                        mailman_log('error', 'Error loading backup pending.pck: %s', str(e))
                        log_file_info(filename_backup)

        except IOError as e:
            if e.errno != errno.ENOENT:
                mailman_log('error', 'IOError loading pending.pck: %s', str(e))
                log_file_info(filename)
                if os.path.exists(filename_backup):
                    log_file_info(filename_backup)

        # If we get here, both main and backup files failed or don't exist
        self.__db = {}

    def __closedb(self):
        """Save the database with atomic operations and backup."""
        if self.__db is None:
            return

        filename = os.path.join(mm_cfg.DATA_DIR, 'pending.pck')
        filename_tmp = filename + '.tmp.%s.%d' % (socket.gethostname(), os.getpid())
        filename_backup = filename + '.bak'

        def log_file_info(path):
            try:
                # Log process identity information
                euid = os.geteuid()
                egid = os.getegid()
                ruid = os.getuid()
                rgid = os.getgid()
                groups = os.getgroups()
                
                # Get group names for supplementary groups
                group_names = []
                for gid in groups:
                    try:
                        group_names.append(grp.getgrgid(gid)[0])
                    except KeyError:
                        group_names.append(f'gid {gid}')
                
                mailman_log('error', 
                           'Process identity - EUID: %d, EGID: %d, RUID: %d, RGID: %d, Groups: %s',
                           euid, egid, ruid, rgid, ', '.join(group_names))
                
                # Get file information
                stat = os.stat(path)
                mode = stat.st_mode
                uid = stat.st_uid
                gid = stat.st_gid
                
                # Get current ownership info
                try:
                    current_user = pwd.getpwuid(uid)[0]
                except KeyError:
                    current_user = f'uid {uid}'
                try:
                    current_group = grp.getgrgid(gid)[0]
                except KeyError:
                    current_group = f'gid {gid}'
                
                # Get expected ownership info
                try:
                    expected_user = pwd.getpwnam(mm_cfg.MAILMAN_USER)[0]
                    expected_uid = pwd.getpwnam(mm_cfg.MAILMAN_USER)[2]
                except KeyError:
                    expected_user = f'user {mm_cfg.MAILMAN_USER}'
                    expected_uid = None
                
                try:
                    expected_group = grp.getgrnam(mm_cfg.MAILMAN_GROUP)[0]
                    expected_gid = grp.getgrnam(mm_cfg.MAILMAN_GROUP)[2]
                except KeyError:
                    expected_group = f'group {mm_cfg.MAILMAN_GROUP}'
                    expected_gid = None
                
                # Log current and expected ownership
                mailman_log('error', 
                           'File %s: mode=%o, owner=%s (current) vs %s (expected), group=%s (current) vs %s (expected)',
                           path, mode, current_user, expected_user, current_group, expected_group)
                
                # Log specific permission issues
                if expected_uid is not None and uid != expected_uid:
                    mailman_log('error', 'File %s has incorrect owner (uid %d vs expected %d)',
                               path, uid, expected_uid)
                if expected_gid is not None and gid != expected_gid:
                    mailman_log('error', 'File %s has incorrect group (gid %d vs expected %d)',
                               path, gid, expected_gid)
                if mode & 0o002:  # World writable
                    mailman_log('error', 'File %s is world writable (mode %o)',
                               path, mode)
                if mode & 0o020 and (expected_gid is None or gid != expected_gid):  # Group writable but not owned by mailman group
                    mailman_log('error', 'File %s is group writable but not owned by mailman group',
                               path)
            except OSError as e:
                mailman_log('error', 'Could not stat %s: %s', path, str(e))

        # First check if we can access the directory
        try:
            dirname = os.path.dirname(filename)
            if os.path.exists(dirname):
                log_file_info(dirname)
        except OSError as e:
            log_file_info(dirname)
            raise PermissionError(f'Cannot access directory {dirname}: {str(e)}')

        # Check existing files
        if os.path.exists(filename):
            log_file_info(filename)
        if os.path.exists(filename_backup):
            log_file_info(filename_backup)

        # Try to create backup, but don't fail if we can't
        try:
            if os.path.exists(filename):
                import shutil
                shutil.copy2(filename, filename_backup)
        except (IOError, OSError) as e:
            mailman_log('error', 'Could not create backup file %s: %s', filename_backup, str(e))
            log_file_info(filename)
            if os.path.exists(filename_backup):
                log_file_info(filename_backup)
            # Continue with save operation even if backup fails

        # Try to save the new file
        try:
            with open(filename_tmp, 'wb') as fp:
                pickle.dump(self.__db, fp, protocol=0)
                fp.flush()
                os.fsync(fp.fileno())
            os.rename(filename_tmp, filename)
        except (IOError, OSError) as e:
            log_file_info(filename_tmp)
            if os.path.exists(filename):
                log_file_info(filename)
            mailman_log('error', 'Error saving database: %s', str(e))
            raise PermissionError(f'Error saving database: {str(e)}')

        self.__db = None

    def __validate_and_clean_db(self):
        """Validate database entries and clean up invalid ones."""
        if not self.__db:
            return

        now = time.time()
        to_delete = []

        for key, value in self.__db.items():
            try:
                # Check if value is a valid tuple/list with at least 2 elements
                if not isinstance(value, (tuple, list)) or len(value) < 2:
                    to_delete.append(key)
                    continue

                # Check if timestamp is valid
                timestamp = value[1]
                if not isinstance(timestamp, (int, float)) or timestamp < 0:
                    to_delete.append(key)
                    continue

                # Remove expired entries
                if timestamp < now:
                    to_delete.append(key)
                    continue

            except (TypeError, IndexError):
                to_delete.append(key)

        # Remove invalid entries
        for key in to_delete:
            del self.__db[key]

    def SaveRequestsDb(self):
        """Save the requests database with validation."""
        if self.__db is not None:
            self.__validate_and_clean_db()
            self.__closedb()

    def NumRequestsPending(self):
        self.__opendb()
        # Subtract one for the version pseudo-entry
        return len(self.__db) - 1

    def __getmsgids(self, rtype):
        self.__opendb()
        ids = [k for k, (op, data) in list(self.__db.items()) if op == rtype]
        ids.sort()
        return ids

    def GetHeldMessageIds(self):
        return self.__getmsgids(HELDMSG)

    def GetSubscriptionIds(self):
        return self.__getmsgids(SUBSCRIPTION)

    def GetUnsubscriptionIds(self):
        return self.__getmsgids(UNSUBSCRIPTION)

    def GetRecord(self, id):
        self.__opendb()
        type, data = self.__db[id]
        return data

    def GetRecordType(self, id):
        self.__opendb()
        type, data = self.__db[id]
        return type

    def HandleRequest(self, id, value, comment=None, preserve=None,
                      forward=None, addr=None):
        self.__opendb()
        rtype, data = self.__db[id]
        if rtype == HELDMSG:
            status = self.__handlepost(data, value, comment, preserve,
                                       forward, addr)
        elif rtype == UNSUBSCRIPTION:
            status = self.__handleunsubscription(data, value, comment)
        else:
            assert rtype == SUBSCRIPTION
            status = self.__handlesubscription(data, value, comment)
        if status != DEFER:
            # BAW: Held message ids are linked to Pending cookies, allowing
            # the user to cancel their post before the moderator has approved
            # it.  We should probably remove the cookie associated with this
            # id, but we have no way currently of correlating them. :(
            del self.__db[id]

    def HoldMessage(self, msg, reason, msgdata={}):
        # Make a copy of msgdata so that subsequent changes won't corrupt the
        # request database.  TBD: remove the `filebase' key since this will
        # not be relevant when the message is resurrected.
        msgdata = msgdata.copy()
        # assure that the database is open for writing
        self.__opendb()
        # get the next unique id
        id = self.__nextid()
        # get the message sender
        sender = msg.get_sender()
        # calculate the file name for the message text and write it to disk
        if mm_cfg.HOLD_MESSAGES_AS_PICKLES:
            ext = 'pck'
        else:
            ext = 'txt'
        filename = 'heldmsg-%s-%d.%s' % (self.internal_name(), id, ext)
        omask = os.umask(0o007)
        try:
            fp = open(os.path.join(mm_cfg.DATA_DIR, filename), 'wb')
            try:
                if mm_cfg.HOLD_MESSAGES_AS_PICKLES:
                    pickle.dump(msg, fp, protocol=2, fix_imports=True)
                else:
                    g = Generator(fp)
                    g.flatten(msg, 1)
                fp.flush()
                os.fsync(fp.fileno())
            finally:
                fp.close()
        finally:
            os.umask(omask)
        # save the information to the request database.  for held message
        # entries, each record in the database will be of the following
        # format:
        #
        # the time the message was received
        # the sender of the message
        # the message's subject
        # a string description of the problem
        # name of the file in $PREFIX/data containing the msg text
        # an additional dictionary of message metadata
        #
        msgsubject = msg.get('subject', _('(no subject)'))
        if not sender:
            sender = _('<missing>')
        data = (time.time(), sender, msgsubject, reason, filename, msgdata)
        self.__db[id] = (HELDMSG, data)
        return id

    def __handlepost(self, record, value, comment, preserve, forward, addr):
        # For backwards compatibility with pre 2.0beta3
        ptime, sender, subject, reason, filename, msgdata = record
        path = os.path.join(mm_cfg.DATA_DIR, filename)
        # Handle message preservation
        if preserve:
            parts = os.path.split(path)[1].split(DASH)
            parts[0] = 'spam'
            spamfile = DASH.join(parts)
            # Preserve the message as plain text, not as a pickle
            try:
                fp = open(path, 'rb')
            except IOError as e:
                if e.errno != errno.ENOENT: raise
                return LOST
            try:
                if path.endswith('.pck'):
                    msg = pickle.load(fp, fix_imports=True, encoding='latin1')
                else:
                    assert path.endswith('.txt'), '%s not .pck or .txt' % path
                    msg = fp.read()
            finally:
                fp.close()
            # Save the plain text to a .msg file, not a .pck file
            outpath = os.path.join(mm_cfg.SPAM_DIR, spamfile)
            head, ext = os.path.splitext(outpath)
            outpath = head + '.msg'
            outfp = open(outpath, 'wb')
            try:
                if path.endswith('.pck'):
                    g = Generator(outfp)
                    g.flatten(msg, 1)
                else:
                    outfp.write(msg)
            finally:
                outfp.close()
        # Now handle updates to the database
        rejection = None
        fp = None
        msg = None
        status = REMOVE
        if value == mm_cfg.DEFER:
            # Defer
            status = DEFER
        elif value == mm_cfg.APPROVE:
            # Approved.
            try:
                msg = email.message_from_file(fp, Message)
            except IOError as e:
                if e.errno != errno.ENOENT: raise
                return LOST
            # Convert to Mailman.Message if needed
            if isinstance(msg, email.message.Message) and not isinstance(msg, Message.Message):
                mailman_msg = Message.Message()
                # Copy all attributes from the original message
                for key, value in msg.items():
                    mailman_msg[key] = value
                # Copy the payload
                if msg.is_multipart():
                    for part in msg.get_payload():
                        mailman_msg.attach(part)
                else:
                    mailman_msg.set_payload(msg.get_payload())
                msg = mailman_msg
            msgdata['approved'] = 1
            # adminapproved is used by the Emergency handler
            msgdata['adminapproved'] = 1
            # Calculate a new filebase for the approved message, otherwise
            # delivery errors will cause duplicates.
            try:
                del msgdata['filebase']
            except KeyError:
                pass
            # Queue the file for delivery by qrunner.  Trying to deliver the
            # message directly here can lead to a huge delay in web
            # turnaround.  Log the moderation and add a header.
            msg['X-Mailman-Approved-At'] = email.utils.formatdate(localtime=1)
            mailman_log('vette', '%s: held message approved, message-id: %s',
                   self.internal_name(),
                   msg.get('message-id', 'n/a'))
            # Stick the message back in the incoming queue for further
            # processing.
            inq = get_switchboard(mm_cfg.INQUEUE_DIR)
            inq.enqueue(msg, _metadata=msgdata)
        elif value == mm_cfg.REJECT:
            # Rejected
            rejection = 'Refused'
            lang = self.getMemberLanguage(sender)
            subject = Utils.oneline(subject, Utils.GetCharSet(lang))
            self.__refuse(_('Posting of your message titled "%(subject)s"'),
                          sender, comment or _('[No reason given]'),
                          lang=lang)
        else:
            assert value == mm_cfg.DISCARD
            # Discarded
            rejection = 'Discarded'
        # Forward the message
        if forward and addr:
            # If we've approved the message, we need to be sure to craft a
            # completely unique second message for the forwarding operation,
            # since we don't want to share any state or information with the
            # normal delivery.
            try:
                copy = email.message_from_file(fp, Message)
            except IOError as e:
                if e.errno != errno.ENOENT: raise
                raise Errors.LostHeldMessage(path)
            # Convert to Mailman.Message if needed
            if isinstance(copy, email.message.Message) and not isinstance(copy, Message.Message):
                mailman_msg = Message.Message()
                # Copy all attributes from the original message
                for key, value in copy.items():
                    mailman_msg[key] = value
                # Copy the payload
                if copy.is_multipart():
                    for part in copy.get_payload():
                        mailman_msg.attach(part)
                else:
                    mailman_msg.set_payload(copy.get_payload())
                copy = mailman_msg
            # It's possible the addr is a comma separated list of addresses.
            addrs = getaddresses([addr])
            if len(addrs) == 1:
                realname, addr = addrs[0]
                # If the address getting the forwarded message is a member of
                # the list, we want the headers of the outer message to be
                # encoded in their language.  Otherwise it'll be the preferred
                # language of the mailing list.
                lang = self.getMemberLanguage(addr)
            else:
                # Throw away the realnames
                addr = [a for realname, a in addrs]
                # Which member language do we attempt to use?  We could use
                # the first match or the first address, but in the face of
                # ambiguity, let's just use the list's preferred language
                lang = self.preferred_language
            otrans = i18n.get_translation()
            i18n.set_language(lang)
            try:
                fmsg = Message.UserNotification(
                    addr, self.GetBouncesEmail(),
                    _('Forward of moderated message'),
                    lang=lang)
            finally:
                i18n.set_translation(otrans)
            fmsg.set_type('message/rfc822')
            fmsg.attach(copy)
            fmsg.send(self)
        # Log the rejection
        if rejection:
            note = '''%(listname)s: %(rejection)s posting:
\tFrom: %(sender)s
\tSubject: %(subject)s''' % {
                'listname' : self.internal_name(),
                'rejection': rejection,
                'sender'   : str(sender).replace('%', '%%'),
                'subject'  : str(subject).replace('%', '%%'),
                }
            if comment:
                note += '\n\tReason: ' + comment.replace('%', '%%')
            mailman_log('vette', note)
        # Always unlink the file containing the message text.  It's not
        # necessary anymore, regardless of the disposition of the message.
        if status != DEFER:
            try:
                os.unlink(path)
            except OSError as e:
                if e.errno != errno.ENOENT: raise
                # We lost the message text file.  Clean up our housekeeping
                # and inform of this status.
                return LOST
        return status

    def HoldSubscription(self, addr, fullname, password, digest, lang):
        # Assure that the database is open for writing
        self.__opendb()
        # Get the next unique id
        id = self.__nextid()
        # Save the information to the request database. for held subscription
        # entries, each record in the database will be one of the following
        # format:
        #
        # the time the subscription request was received
        # the subscriber's address
        # the subscriber's selected password (TBD: is this safe???)
        # the digest flag
        # the user's preferred language
        data = time.time(), addr, fullname, password, digest, lang
        self.__db[id] = (SUBSCRIPTION, data)
        #
        # TBD: this really shouldn't go here but I'm not sure where else is
        # appropriate.
        mailman_log('vette', '%s: held subscription request from %s',
               self.internal_name(), addr)
        # Possibly notify the administrator in default list language
        if self.admin_immed_notify:
            i18n.set_language(self.preferred_language)
            realname = self.real_name
            subject = _(
                'New subscription request to list %(realname)s from %(addr)s')
            text = Utils.maketext(
                'subauth.txt',
                {'username'   : addr,
                 'listname'   : self.internal_name(),
                 'hostname'   : self.host_name,
                 'admindb_url': self.GetScriptURL('admindb', absolute=1),
                 }, mlist=self)
            # This message should appear to come from the <list>-owner so as
            # to avoid any useless bounce processing.
            owneraddr = self.GetOwnerEmail()
            msg = Message.UserNotification(owneraddr, owneraddr, subject, text,
                                           self.preferred_language)
            msg.send(self, **{'tomoderators': 1})
            # Restore the user's preferred language.
            i18n.set_language(lang)

    def __handlesubscription(self, record, value, comment):
        global _
        stime, addr, fullname, password, digest, lang = record
        if value == mm_cfg.DEFER:
            return DEFER
        elif value == mm_cfg.DISCARD:
            mailman_log('vette', '%s: discarded subscription request from %s',
                   self.internal_name(), addr)
        elif value == mm_cfg.REJECT:
            self.__refuse(_('Subscription request'), addr,
                          comment or _('[No reason given]'),
                          lang=lang)
            mailman_log('vette', """%s: rejected subscription request from %s
\tReason: %s""", self.internal_name(), addr, comment or '[No reason given]')
        else:
            # subscribe
            assert value == mm_cfg.SUBSCRIBE
            try:
                _ = D_
                whence = _('via admin approval')
                _ = i18n._
                userdesc = UserDesc(addr, fullname, password, digest, lang)
                self.ApprovedAddMember(userdesc, whence=whence)
            except Errors.MMAlreadyAMember:
                # User has already been subscribed, after sending the request
                pass
            # TBD: disgusting hack: ApprovedAddMember() can end up closing
            # the request database.
            self.__opendb()
        return REMOVE

    def HoldUnsubscription(self, addr):
        # Assure the database is open for writing
        self.__opendb()
        # Get the next unique id
        id = self.__nextid()
        # All we need to do is save the unsubscribing address
        self.__db[id] = (UNSUBSCRIPTION, addr)
        mailman_log('vette', '%s: held unsubscription request from %s',
               self.internal_name(), addr)
        # Possibly notify the administrator of the hold
        if self.admin_immed_notify:
            realname = self.real_name
            subject = _(
                'New unsubscription request from %(realname)s by %(addr)s')
            text = Utils.maketext(
                'unsubauth.txt',
                {'username'   : addr,
                 'listname'   : self.internal_name(),
                 'hostname'   : self.host_name,
                 'admindb_url': self.GetScriptURL('admindb', absolute=1),
                 }, mlist=self)
            # This message should appear to come from the <list>-owner so as
            # to avoid any useless bounce processing.
            owneraddr = self.GetOwnerEmail()
            msg = Message.UserNotification(owneraddr, owneraddr, subject, text,
                                           self.preferred_language)
            msg.send(self, **{'tomoderators': 1})

    def __handleunsubscription(self, record, value, comment):
        addr = record
        if value == mm_cfg.DEFER:
            return DEFER
        elif value == mm_cfg.DISCARD:
            mailman_log('vette', '%s: discarded unsubscription request from %s',
                   self.internal_name(), addr)
        elif value == mm_cfg.REJECT:
            self.__refuse(_('Unsubscription request'), addr, comment)
            mailman_log('vette', """%s: rejected unsubscription request from %s
\tReason: %s""", self.internal_name(), addr, comment or '[No reason given]')
        else:
            assert value == mm_cfg.UNSUBSCRIBE
            try:
                self.ApprovedDeleteMember(addr)
            except Errors.NotAMemberError:
                # User has already been unsubscribed
                pass
        return REMOVE

    def __refuse(self, request, recip, comment, origmsg=None, lang=None):
        # As this message is going to the requestor, try to set the language
        # to his/her language choice, if they are a member.  Otherwise use the
        # list's preferred language.
        realname = self.real_name
        if lang is None:
            lang = self.getMemberLanguage(recip)
        text = Utils.maketext(
            'refuse.txt',
            {'listname' : realname,
             'request'  : request,
             'reason'   : comment,
             'adminaddr': self.GetOwnerEmail(),
            }, lang=lang, mlist=self)
        otrans = i18n.get_translation()
        i18n.set_language(lang)
        try:
            # add in original message, but not wrap/filled
            if origmsg:
                text = NL.join(
                    [text,
                     '---------- ' + _('Original Message') + ' ----------',
                     str(origmsg)
                     ])
            subject = _('Request to mailing list %(realname)s rejected')
        finally:
            i18n.set_translation(otrans)
        msg = Message.UserNotification(recip, self.GetOwnerEmail(),
                                       subject, text, lang)
        msg.send(self)

    def _UpdateRecords(self):
        # Subscription records have changed since MM2.0.x.  In that family,
        # the records were of length 4, containing the request time, the
        # address, the password, and the digest flag.  In MM2.1a2, they grew
        # an additional language parameter at the end.  In MM2.1a4, they grew
        # a fullname slot after the address.  This semi-public method is used
        # by the update script to coerce all subscription records to the
        # latest MM2.1 format.
        #
        # Held message records have historically either 5 or 6 items too.
        # These always include the requests time, the sender, subject, default
        # rejection reason, and message text.  When of length 6, it also
        # includes the message metadata dictionary on the end of the tuple.
        #
        # In Mailman 2.1.5 we converted these files to pickles.
        filename = os.path.join(self.fullpath(), 'request.db')
        try:
            fp = open(filename, 'rb')
            try:
                self.__db = marshal.load(fp)
            finally:
                fp.close()
            os.unlink(filename)
        except IOError as e:
            if e.errno != errno.ENOENT: raise
            filename = os.path.join(self.fullpath(), 'request.pck')
            try:
                fp = open(filename, 'rb')
                try:
                    self.__db = pickle.load(fp, fix_imports=True, encoding='latin1')
                finally:
                    fp.close()
            except IOError as e:
                if e.errno != errno.ENOENT: raise
                self.__db = {}
        for id, x in list(self.__db.items()):
            # A bug in versions 2.1.1 through 2.1.11 could have resulted in
            # just info being stored instead of (op, info)
            if len(x) == 2:
                op, info = x
            elif len(x) == 6:
                # This is the buggy info. Check for digest flag.
                if x[4] in (0, 1):
                    op = SUBSCRIPTION
                else:
                    op = HELDMSG
                self.__db[id] = op, x
                continue
            else:
                assert False, 'Unknown record format in %s' % self.__filename
            if op == SUBSCRIPTION:
                if len(info) == 4:
                    # pre-2.1a2 compatibility
                    when, addr, passwd, digest = info
                    fullname = ''
                    lang = self.preferred_language
                elif len(info) == 5:
                    # pre-2.1a4 compatibility
                    when, addr, passwd, digest, lang = info
                    fullname = ''
                else:
                    assert len(info) == 6, 'Unknown subscription record layout'
                    continue
                # Here's the new layout
                self.__db[id] = op, (when, addr, fullname, passwd,
                                     digest, lang)
            elif op == HELDMSG:
                if len(info) == 5:
                    when, sender, subject, reason, text = info
                    msgdata = {}
                else:
                    assert len(info) == 6, 'Unknown held msg record layout'
                    continue
                # Here's the new layout
                self.__db[id] = op, (when, sender, subject, reason,
                                     text, msgdata)
        # All done
        self.__closedb()


def readMessage(path):
    # For backwards compatibility, we must be able to read either a flat text
    # file or a pickle.
    ext = os.path.splitext(path)[1]
    fp = open(path, 'rb')
    try:
        if ext == '.txt':
            msg = email.message_from_file(fp, Message)
            # Convert to Mailman.Message if needed
            if isinstance(msg, email.message.Message) and not isinstance(msg, Message.Message):
                mailman_msg = Message.Message()
                # Copy all attributes from the original message
                for key, value in msg.items():
                    mailman_msg[key] = value
                # Copy the payload
                if msg.is_multipart():
                    for part in msg.get_payload():
                        mailman_msg.attach(part)
                else:
                    mailman_msg.set_payload(msg.get_payload())
                msg = mailman_msg
        else:
            assert ext == '.pck'
            msg = pickle.load(fp, fix_imports=True, encoding='latin1')
            # Convert to Mailman.Message if needed
            if isinstance(msg, email.message.Message) and not isinstance(msg, Message.Message):
                mailman_msg = Message.Message()
                # Copy all attributes from the original message
                for key, value in msg.items():
                    mailman_msg[key] = value
                # Copy the payload
                if msg.is_multipart():
                    for part in msg.get_payload():
                        mailman_msg.attach(part)
                else:
                    mailman_msg.set_payload(msg.get_payload())
                msg = mailman_msg
    finally:
        fp.close()
    return msg
