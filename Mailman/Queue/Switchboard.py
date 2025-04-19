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

"""Reading and writing message objects and message metadata.
"""

# enqueue() and dequeue() are not symmetric.  enqueue() takes a Message
# object.  dequeue() returns a email.Message object tree.
#
# Message metadata is represented internally as a Python dictionary.  Keys and
# values must be strings.  When written to a queue directory, the metadata is
# written into an externally represented format, as defined here.  Because
# components of the Mailman system may be written in something other than
# Python, the external interchange format should be chosen based on what those
# other components can read and write.
#
# Most efficient, and recommended if everything is Python, is Python marshal
# format.  Also supported by default is Berkeley db format (using the default
# bsddb module compiled into your Python executable -- usually Berkeley db
# 2), and rfc822 style plain text.  You can write your own if you have other
# needs.

import os
import time
import email
import errno
import pickle
import marshal
from typing import Any, Dict, List, Optional, Tuple, Union

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Message
from Mailman.Logging.Syslog import syslog
from Mailman.Utils import hashlib_new

# 20 bytes of all bits set) as maximum sha.digest() value
shamax = 0xffffffffffffffffffffffffffffffffffffffff

try:
    import dns.resolver
    from dns.exception import DNSException
    dns_resolver = True
except ImportError:
    dns_resolver = False

# This flag causes messages to be written as pickles (when True) or text files
# (when False).  Pickles are more efficient because the message doesn't need
# to be re-parsed every time it's unqueued) as but pickles are not human readable.
SAVE_MSGS_AS_PICKLES = True
# Small increment to add to time in case two entries have the same time.  This
# prevents skipping one of two entries with the same time until the next pass.
DELTA = 0.001
# We count the number of times a file has been moved to .bak and recovered.
# In order to prevent loops and a message flood, when the count reaches this
# value, we move the file to the shunt queue as a .psv.
MAX_BAK_COUNT = 3


class Switchboard:
    """Queue switchboard for processing messages."""
    
    def __init__(self, queue: str, slice: Optional[int] = None, 
                 numslices: int = 1, recover=False) -> None:
        """Initialize the switchboard.
        
        Args:
            queue: Name of the queue directory
            slice: Optional slice number for this instance
            numslices: Total number of slices
        """
        self.queue = queue
        self.slice = slice
        self.numslices = numslices
        self.queuedir = os.path.join(mm_cfg.QUEUE_DIR, queue)
        self.msgdir = os.path.join(self.queuedir, 'messages')
        self.baddir = os.path.join(self.queuedir, 'bad')
        self.tmpdir = os.path.join(self.queuedir, 'tmp')
        
        # Create necessary directories
        for dir in (self.queuedir, self.msgdir, self.baddir, self.tmpdir):
            if not os.path.exists(dir):
                os.makedirs(dir, mode=0o2775)
                os.chmod(dir, 0o2775)

        # Fast track for no slices
        self.__lower = None
        self.__upper = None
        # BAW: test performance and end-cases of this algorithm
        if numslices != 1:
            self.__lower = ((shamax+1) * slice) // numslices
            self.__upper = (((shamax+1) * (slice+1)) // numslices) - 1
        if recover:
            self.recover_backup_files()

    def whichq(self):
        return self.queue

    def enqueue(self, msg: Message, msgdata: Dict[str, Any]) -> str:
        """Enqueue a message for processing.
        
        Args:
            msg: The email message
            msgdata: Additional message metadata
            
        Returns:
            The message ID
        """
        # Generate a unique message ID
        msgid = Utils.unique_message_id()
        
        # Create the message file
        msgfile = os.path.join(self.msgdir, msgid)
        with open(msgfile, 'wb') as fp:
            pickle.dump(msg, fp, protocol=2)
            
        # Create the metadata file
        datafile = os.path.join(self.msgdir, msgid + '.pck')
        with open(datafile, 'wb') as fp:
            pickle.dump(msgdata, fp, protocol=2)
            
        return msgid

    def dequeue(self, msgid: str) -> Tuple[Message, Dict[str, Any]]:
        """Dequeue a message for processing.
        
        Args:
            msgid: The message ID to dequeue
            
        Returns:
            Tuple of (message, metadata)
            
        Raises:
            IOError: If the message cannot be found
        """
        msgfile = os.path.join(self.msgdir, msgid)
        datafile = os.path.join(self.msgdir, msgid + '.pck')
        
        # Load the message
        with open(msgfile, 'rb') as fp:
            msg = pickle.load(fp)
            
        # Load the metadata
        with open(datafile, 'rb') as fp:
            msgdata = pickle.load(fp)
            
        return msg, msgdata

    def finish(self, msgid: str, preserve=False) -> None:
        """Finish processing a message.
        
        Args:
            msgid: The message ID to finish
        """
        msgfile = os.path.join(self.msgdir, msgid)
        datafile = os.path.join(self.msgdir, msgid + '.pck')
        
        # Remove the files
        try:
            if preserve:
                psvfile = os.path.join(mm_cfg.BADQUEUE_DIR, msgid + '.psv')
                # Create the directory if it doesn't yet exist.
                # Copied from __init__.
                omask = os.umask(0)                       # rwxrws---
                try:
                    try:
                        os.mkdir(mm_cfg.BADQUEUE_DIR, 0o770)
                    except OSError as e:
                        if e.errno != errno.EEXIST: raise
                finally:
                    os.umask(omask)
                os.rename(msgfile, psvfile)
                os.rename(datafile, psvfile + '.pck')
            else:
                os.unlink(msgfile)
                os.unlink(datafile)
        except EnvironmentError as e:
            syslog('error', 'Failed to unlink/preserve backup file: %s\n%s',
                   msgfile, e)

    def files(self, extension='.pck'):
        times = {}
        lower = self.__lower
        upper = self.__upper
        for f in os.listdir(self.msgdir):
            # By ignoring anything that doesn't end in .pck, we ignore
            # tempfiles and avoid a race condition.
            filebase, ext = os.path.splitext(f)
            if ext != extension:
                continue
            when, digest = filebase.split('+')
            # Throw out any files which don't match our bitrange.  BAW: test
            # performance and end-cases of this algorithm.  MAS: both
            # comparisons need to be <= to get complete range.
            if lower is None or (lower <= int(digest, 16) <= upper):
                key = float(when)
                while key in times:
                    key += DELTA
                times[key] = filebase
        # FIFO sort
        keys = list(times.keys())
        keys.sort()  # Sort numerically since keys are floats
        return [times[k] for k in keys]

    def recover_backup_files(self):
        # Move all .bak files in our slice to .pck.  It's impossible for both
        # to exist at the same time, so the move is enough to ensure that our
        # normal dequeuing process will handle them.  We keep count in
        # _bak_count in the metadata of the number of times we recover this
        # file.  When the count reaches MAX_BAK_COUNT, we move the .bak file
        # to a .psv file in the shunt queue.
        for filebase in self.files('.bak'):
            src = os.path.join(self.msgdir, filebase + '.bak')
            dst = os.path.join(self.msgdir, filebase + '.pck')
            fp = open(src, 'rb+')
            try:
                try:
                    msg = pickle.load(fp)
                    data_pos = fp.tell()
                    data = pickle.load(fp)
                except Exception as s:
                    # If unpickling throws any exception, just log and
                    # preserve this entry
                    syslog('error', 'Unpickling .bak exception: %s\n'
                           + 'preserving file: %s', s, filebase)
                    self.finish(filebase, preserve=True)
                else:
                    data['_bak_count'] = data.setdefault('_bak_count', 0) + 1
                    fp.seek(data_pos)
                    if data.get('_parsemsg'):
                        protocol = 0
                    else:
                        protocol = 1
                    pickle.dump(data, fp, protocol)
                    fp.truncate()
                    fp.flush()
                    os.fsync(fp.fileno())
                    if data['_bak_count'] >= MAX_BAK_COUNT:
                        syslog('error',
                               '.bak file max count, preserving file: %s',
                               filebase)
                        self.finish(filebase, preserve=True)
                    else:
                        os.rename(src, dst)
            finally:
                fp.close()

    def reject(self, msgid: str, reason: str) -> None:
        """Reject a message.
        
        Args:
            msgid: The message ID to reject
            reason: Reason for rejection
        """
        msgfile = os.path.join(self.msgdir, msgid)
        datafile = os.path.join(self.msgdir, msgid + '.pck')
        badfile = os.path.join(self.baddir, msgid)
        
        # Move the files to the bad directory
        try:
            os.rename(msgfile, badfile)
            os.rename(datafile, badfile + '.pck')
        except OSError:
            pass
            
        # Log the rejection
        syslog('error', 'Rejected message %s: %s', msgid, reason)
