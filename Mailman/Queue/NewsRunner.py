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
from email.utils import getaddresses
from email.iterators import body_line_iterator

COMMASPACE = ', '

from Mailman import mm_cfg
from Mailman import Utils
from Mailman.Queue.Runner import Runner
from Mailman.Logging.Syslog import mailman_log

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
        # Always initialize the parent class first
        Runner.__init__(self, slice, numslices)
        
        # Check if NNTP support is available and configured
        self._nntp_enabled = False
        if not HAVE_NNTP:
            # Only log if we're actually trying to start the runner
            if slice is not None:
                mailman_log('warning', 'NNTP support is not enabled. NewsRunner will not process messages.')
            return
        if not mm_cfg.DEFAULT_NNTP_HOST:
            if slice is not None:
                mailman_log('info', 'NewsRunner not processing messages due to DEFAULT_NNTP_HOST not being set')
            return
            
        # Check if any lists actually need NNTP support
        from Mailman import Utils
        from Mailman.MailList import MailList
        from Mailman import Errors
        
        has_nntp_lists = False
        for listname in Utils.list_names():
            try:
                mlist = MailList(listname, lock=False)
                if mlist.nntp_host:
                    has_nntp_lists = True
                    break
            except Errors.MMUnknownListError:
                continue
            finally:
                if 'mlist' in locals():
                    mlist.Unlock()
                    
        if not has_nntp_lists:
            if slice is not None:
                mailman_log('info', 'No lists require NNTP support. NewsRunner will not be started.')
            return
            
        # NNTP is available, configured, and needed by at least one list
        self._nntp_enabled = True
        from Mailman.Queue.Switchboard import Switchboard
        self._switchboard = Switchboard(self.QDIR, slice, numslices, True)
        # Initialize _kids if not already done by parent
        if not hasattr(self, '_kids'):
            self._kids = {}

    def _oneloop(self):
        # If NNTP is not enabled, sleep for a while before checking again
        if not self._nntp_enabled:
            # Check the stop flag every second during sleep
            for _ in range(60):
                if self._stop:
                    return 0
                time.sleep(1)
            return 0
            
        # Get one message from the queue
        msg = self._switchboard.dequeue()
        if msg is None:
            return 0
            
        # Process the message
        try:
            self._dopost(msg)
        except Exception as e:
            mailman_log('error', 'NewsRunner error: %s', str(e))
            # Put the message back in the queue
            self._switchboard.enqueue(msg)
        return 1

    def _dispose(self, mlist, msg, msgdata):
        """Post the message to the newsgroup."""
        try:
            # Validate message type first
            msg, success = self._validate_message(msg, msgdata)
            if not success:
                mailman_log('error', 'Message validation failed for news message')
                return False

            # Post the message to the newsgroup
            mlist.post_to_news(msg)
            return True
        except Exception as e:
            mailman_log('error', 'Error posting message to newsgroup for list %s: %s',
                   mlist.internal_name(), str(e))
            return False

    def _queue_news(self, listname, msg, msgdata):
        """Queue a news message for processing."""
        # Create a unique filename
        now = time.time()
        filename = os.path.join(mm_cfg.NEWSQUEUE_DIR,
                               '%d.%d.pck' % (os.getpid(), now))
        
        # Write the message and metadata to the pickle file
        try:
            # Use protocol 2 for Python 2/3 compatibility
            protocol = 2
            with open(filename, 'wb') as fp:
                pickle.dump(listname, fp, protocol=2, fix_imports=True)
                pickle.dump(msg, fp, protocol=2, fix_imports=True)
                pickle.dump(msgdata, fp, protocol=2, fix_imports=True)
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
        if hasattr(self, '_nntp_conn') and self._nntp_conn:
            try:
                self._nntp_conn.quit()
            except Exception:
                pass
            self._nntp_conn = None
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
