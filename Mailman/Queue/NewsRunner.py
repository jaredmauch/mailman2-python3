# Copyright (C) 2000-2018 by the Free Software Foundation, Inc.
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

"""NNTP queue runner."""

from builtins import str
import re
import socket
from io import StringIO
import time
import traceback
import os
import pickle

import email
from email.utils import getaddresses, parsedate_tz, mktime_tz
from email.iterators import body_line_iterator

COMMASPACE = ', '

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
from Mailman import i18n
from Mailman.Queue.Runner import Runner
from Mailman.Logging.Syslog import mailman_log, syslog
import Mailman.Message as Message
import Mailman.MailList as MailList

# Only import nntplib if NNTP support is enabled
try:
    import nntplib
    HAVE_NNTP = True
except ImportError:
    HAVE_NNTP = False

# Matches our Mailman crafted Message-IDs.  See Utils.unique_message_id()
mcre = re.compile(r"""
    <mailman.                                     # match the prefix
    \d+.                                          # serial number
    \d+.                                          # time in seconds since epoch
    \d+.                                          # pid
    (?P<listname>[^@]+)                           # list's internal_name()
    @                                             # localpart@dom.ain
    (?P<hostname>[^>]+)                           # list's host_name
    >                                             # trailer
    """, re.VERBOSE)


class NewsRunner(Runner):
    QDIR = mm_cfg.NEWSQUEUE_DIR

    def __init__(self, slice=None, numslices=1):
        # First check if NNTP support is enabled
        if not mm_cfg.NNTP_SUPPORT:
            syslog('warning', 'NNTP support is not enabled. NewsRunner will not process messages.')
            return
        if not mm_cfg.DEFAULT_NNTP_HOST:
            syslog('info', 'NewsRunner not processing messages due to DEFAULT_NNTP_HOST not being set')
            return
        # Initialize the base class
        Runner.__init__(self, slice, numslices)
        # Check if any lists require NNTP support
        self._nntp_lists = []
        for listname in Utils.list_names():
            try:
                mlist = MailList.MailList(listname, lock=False)
                if mlist.nntp_host:
                    self._nntp_lists.append(listname)
            except Errors.MMListError:
                continue
        if not self._nntp_lists:
            syslog('info', 'No lists require NNTP support. NewsRunner will not be started.')
            return
        # Initialize the NNTP connection
        self._nntp = None
        self._connect()

    def _connect(self):
        """Connect to the NNTP server."""
        try:
            self._nntp = nntplib.NNTP(mm_cfg.DEFAULT_NNTP_HOST,
                                    mm_cfg.DEFAULT_NNTP_PORT,
                                    mm_cfg.DEFAULT_NNTP_USER,
                                    mm_cfg.DEFAULT_NNTP_PASS)
        except Exception as e:
            syslog('error', 'NewsRunner error: %s', str(e))
            self._nntp = None

    def _validate_message(self, msg, msgdata):
        """Validate the message for news posting.
        
        Args:
            msg: The message to validate
            msgdata: Additional message metadata
            
        Returns:
            tuple: (msg, success) where success is True if validation passed
        """
        try:
            # Check if the message has a Message-ID
            if not msg.get('message-id'):
                syslog('error', 'Message validation failed for news message')
                return msg, False
            return msg, True
        except Exception as e:
            syslog('error', 'Error validating news message: %s', str(e))
            return msg, False

    def _dispose(self, mlist, msg, msgdata):
        """Post the message to the newsgroup."""
        try:
            # Get the newsgroup name
            newsgroup = mlist.nntp_host
            if not newsgroup:
                return False
            # Post the message
            self._nntp.post(str(msg))
            return False
        except Exception as e:
            syslog('error', 'Error posting message to newsgroup for list %s: %s',
                   mlist.internal_name(), str(e))
            return True

    def _onefile(self, msg, msgdata):
        """Process a single news message.
        
        This method overrides the base class's _onefile to add news-specific
        validation and processing.
        
        Args:
            msg: The message to process
            msgdata: Additional message metadata
        """
        try:
            # Validate the message
            msg, success = self._validate_message(msg, msgdata)
            if not success:
                syslog('error', 'NewsRunner._onefile: Message validation failed')
                self._shunt.enqueue(msg, msgdata)
                return
                
            # Get the list name from the message data
            listname = msgdata.get('listname')
            if not listname:
                syslog('error', 'NewsRunner._onefile: No listname in message data')
                self._shunt.enqueue(msg, msgdata)
                return
                
            # Open the list
            try:
                mlist = self._open_list(listname)
            except Exception as e:
                self.log_error('list_open_error', str(e), listname=listname)
                self._shunt.enqueue(msg, msgdata)
                return
                
            # Process the message
            try:
                keepqueued = self._dispose(mlist, msg, msgdata)
                if keepqueued:
                    self._switchboard.enqueue(msg, msgdata)
            except Exception as e:
                self._handle_error(e, msg=msg, mlist=mlist)
                
        except Exception as e:
            syslog('error', 'NewsRunner._onefile: Unexpected error: %s', str(e))
            self._shunt.enqueue(msg, msgdata)

    def _oneloop(self):
        """Process one batch of messages from the news queue."""
        try:
            # Get the list of files to process
            files = self._switchboard.files()
            filecnt = len(files)
            
            # Process each file
            for filebase in files:
                try:
                    # Check if the file exists before dequeuing
                    pckfile = os.path.join(self.QDIR, filebase + '.pck')
                    if not os.path.exists(pckfile):
                        syslog('error', 'NewsRunner._oneloop: File %s does not exist, skipping', pckfile)
                        continue
                        
                    # Check if file is locked
                    lockfile = os.path.join(self.QDIR, filebase + '.pck.lock')
                    if os.path.exists(lockfile):
                        syslog('debug', 'NewsRunner._oneloop: File %s is locked by another process, skipping', filebase)
                        continue
                    
                    # Dequeue the file
                    msg, msgdata = self._switchboard.dequeue(filebase)
                    if msg is None:
                        continue
                        
                    # Process the message
                    try:
                        self._onefile(msg, msgdata)
                    except Exception as e:
                        syslog('error', 'NewsRunner._oneloop: Error processing message %s: %s', filebase, str(e))
                        continue
                        
                except Exception as e:
                    syslog('error', 'NewsRunner._oneloop: Error dequeuing file %s: %s', filebase, str(e))
                    continue
                    
        except Exception as e:
            syslog('error', 'NewsRunner._oneloop: Error in main loop: %s', str(e))
            return 0
            
        return filecnt

    def _queue_news(self, listname, msg, msgdata):
        """Queue a news message for processing."""
        # Create a unique filename
        now = time.time()
        filename = os.path.join(mm_cfg.NEWSQUEUE_DIR,
                               '%d.%d.pck' % (os.getpid(), now))
        
        # Write the message and metadata to the pickle file
        try:
            # Use protocol 4 for Python 3 compatibility
            with open(filename, 'wb') as fp:
                pickle.dump(listname, fp, protocol=4, fix_imports=True)
                pickle.dump(msg, fp, protocol=4, fix_imports=True)
                pickle.dump(msgdata, fp, protocol=4, fix_imports=True)
            # Set the file's mode appropriately
            os.chmod(filename, 0o660)
        except (IOError, OSError) as e:
            try:
                os.unlink(filename)
            except (IOError, OSError):
                pass
            raise SwitchboardError('Could not save news message to %s: %s' %
                                 (filename, e))

    def _cleanup(self):
        """Clean up resources before termination."""
        # Close any open NNTP connections
        if hasattr(self, '_nntp') and self._nntp:
            try:
                self._nntp.quit()
            except Exception:
                pass
            self._nntp = None
        # Call parent cleanup
        super(NewsRunner, self)._cleanup()


def prepare_message(mlist, msg, msgdata):
    # If the newsgroup is moderated, we need to add this header for the Usenet
    # software to accept the posting, and not forward it on to the n.g.'s
    # moderation address.  The posting would not have gotten here if it hadn't
    # already been approved.  1 == open list, mod n.g., 2 == moderated
    if mlist.news_moderation in (1, 2):
        del msg['approved']
        msg['Approved'] = mlist.GetListEmail()
    # Should we restore the original, non-prefixed subject for gatewayed
    # messages? TK: We use stripped_subject (prefix stripped) which was
    # crafted in CookHeaders.py to ensure prefix was stripped from the subject
    # came from mailing list user.
    stripped_subject = msgdata.get('stripped_subject') \
                       or msgdata.get('origsubj')
    if not mlist.news_prefix_subject_too and stripped_subject is not None:
        del msg['subject']
        msg['Subject'] = stripped_subject
    # Make sure we have a non-blank subject.
    if not msg.get('subject', ''):
        del msg['subject']
        msg['Subject'] = '(no subject)'
    # Add the appropriate Newsgroups: header
    if msg['newsgroups'] is not None:
        # This message is gated from our list to it's associated usnet group.
        # If it has a Newsgroups: header mentioning other groups, it's not
        # up to us to post it to those groups.
        del msg['newsgroups']
    msg['Newsgroups'] = mlist.linked_newsgroup
    # Note: We need to be sure two messages aren't ever sent to the same list
    # in the same process, since message ids need to be unique.  Further, if
    # messages are crossposted to two Usenet-gated mailing lists, they each
    # need to have unique message ids or the nntpd will only accept one of
    # them.  The solution here is to substitute any existing message-id that
    # isn't ours with one of ours, so we need to parse it to be sure we're not
    # looping.
    #
    # We also add the original Message-ID: to References: to try to help with
    # threading issues and create another header for documentation.
    #
    # Our Message-ID format is <mailman.secs.pid.listname@hostname>
    msgid = msg['message-id']
    hackmsgid = True
    if msgid:
        mo = mcre.search(msgid)
        if mo:
            lname, hname = mo.group('listname', 'hostname')
            if lname == mlist.internal_name() and hname == mlist.host_name:
                hackmsgid = False
    if hackmsgid:
        del msg['message-id']
        msg['Message-ID'] = Utils.unique_message_id(mlist)
        if msgid:
            msg['X-Mailman-Original-Message-ID'] = msgid
            refs = msg['references']
            del msg['references']
            if not refs:
                refs = msg.get('in-reply-to', '')
            else:
                msg['X-Mailman-Original-References'] = refs
            if refs:
                msg['References'] = '\n '.join([refs, msgid])
            else:
                msg['References'] = msgid
    # Lines: is useful
    if msg['Lines'] is None:
        # BAW: is there a better way?
        count = len(list(body_line_iterator(msg)))
        msg['Lines'] = str(count)
    # Massage the message headers by remove some and rewriting others.  This
    # woon't completely sanitize the message, but it will eliminate the bulk
    # of the rejections based on message headers.  The NNTP server may still
    # reject the message because of other problems.
    for header in mm_cfg.NNTP_REMOVE_HEADERS:
        del msg[header]
    for header, rewrite in mm_cfg.NNTP_REWRITE_DUPLICATE_HEADERS:
        values = msg.get_all(header, [])
        if len(values) < 2:
            # We only care about duplicates
            continue
        del msg[header]
        # But keep the first one...
        msg[header] = values[0]
        for v in values[1:]:
            msg[rewrite] = v
    # Mark this message as prepared in case it has to be requeued
    msgdata['prepped'] = True
