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

This module provides a queue switchboard for processing messages. It handles
the storage and retrieval of messages and their metadata in a queue directory.
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
import logging
from typing import Any, Dict, List, Optional, Tuple, Union, Iterator

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
    """Queue switchboard for processing messages.
    
    This class provides a queue switchboard for processing messages. It handles
    the storage and retrieval of messages and their metadata in a queue directory.
    """
    
    def __init__(self, queue: str, slice: Optional[int] = None, 
                 numslices: int = 1, recover: bool = False) -> None:
        """Initialize the switchboard.
        
        Args:
            queue: Name of the queue directory
            slice: Optional slice number for this instance
            numslices: Total number of slices
            recover: Whether to recover backup files
        """
        self.queue = queue
        self.slice = slice
        self.numslices = numslices
        self.queuedir = os.path.join(mm_cfg.QUEUE_DIR, queue)
        self.msgdir = os.path.join(self.queuedir, 'messages')
        self.baddir = os.path.join(self.queuedir, 'bad')
        self.tmpdir = os.path.join(self.queuedir, 'tmp')
        self.logger = logging.getLogger('mailman.switchboard')
        
        # Create necessary directories
        for dir in (self.queuedir, self.msgdir, self.baddir, self.tmpdir):
            if not os.path.exists(dir):
                os.makedirs(dir, mode=0o2775)
                os.chmod(dir, 0o2775)

        # Fast track for no slices
        self.__lower = None
        self.__upper = None
        if numslices != 1:
            self.__lower = ((shamax+1) * slice) // numslices
            self.__upper = (((shamax+1) * (slice+1)) // numslices) - 1
        if recover:
            self.recover_backup_files()

    def whichq(self) -> str:
        """Return the name of this queue.
        
        Returns:
            str: The name of the queue
        """
        return self.queue

    def enqueue(self, msg: Message, msgdata: Dict[str, Any]) -> str:
        """Enqueue a message for processing.
        
        Args:
            msg: The email message
            msgdata: Additional message metadata
            
        Returns:
            str: The message ID
            
        Raises:
            IOError: If the message cannot be enqueued
        """
        # Generate a unique message ID
        msgid = Utils.unique_message_id()
        
        # Create the message file
        msgfile = os.path.join(self.msgdir, msgid)
        try:
            with open(msgfile, 'wb') as fp:
                pickle.dump(msg, fp, protocol=2)
                
            # Create the metadata file
            datafile = os.path.join(self.msgdir, msgid + '.pck')
            with open(datafile, 'wb') as fp:
                pickle.dump(msgdata, fp, protocol=2)
                
            return msgid
        except IOError as e:
            self.logger.error('Failed to enqueue message: %s', e)
            raise

    def dequeue(self, msgid: str) -> Tuple[Message, Dict[str, Any]]:
        """Dequeue a message for processing.
        
        Args:
            msgid: The message ID to dequeue
            
        Returns:
            Tuple[Message, Dict[str, Any]]: The message and its metadata
            
        Raises:
            IOError: If the message cannot be found
        """
        msgfile = os.path.join(self.msgdir, msgid)
        datafile = os.path.join(self.msgdir, msgid + '.pck')
        
        try:
            # Load the message
            with open(msgfile, 'rb') as fp:
                msg = pickle.load(fp)
                
            # Load the metadata
            with open(datafile, 'rb') as fp:
                msgdata = pickle.load(fp)
                
            return msg, msgdata
        except IOError as e:
            self.logger.error('Failed to dequeue message %s: %s', msgid, e)
            raise

    def finish(self, msgid: str, preserve: bool = False) -> None:
        """Finish processing a message.
        
        Args:
            msgid: The message ID to finish
            preserve: Whether to preserve the message in the bad queue
        """
        msgfile = os.path.join(self.msgdir, msgid)
        datafile = os.path.join(self.msgdir, msgid + '.pck')
        
        # Remove the files
        try:
            if preserve:
                psvfile = os.path.join(mm_cfg.BADQUEUE_DIR, msgid + '.psv')
                # Create the directory if it doesn't yet exist.
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
            self.logger.error('Failed to unlink/preserve backup file: %s', e)
            syslog('error', 'Failed to unlink/preserve backup file: %s\n%s',
                   msgfile, e)

    def files(self, extension: str = '.pck') -> Iterator[str]:
        """Get all files in the message directory.
        
        Args:
            extension: File extension to filter by
            
        Yields:
            str: The next file name
        """
        times = {}
        lower = self.__lower
        upper = self.__upper
        for f in os.listdir(self.msgdir):
            # By ignoring anything that doesn't end in .pck, we ignore
            # tempfiles created by this module's _safe_open() function
            if not f.endswith(extension):
                continue
            # The file's base name is the message id of the message.
            msgid = f[:-len(extension)]
            # Calculate which slice this message id belongs to.  If we're
            # not slicing, then we'll process all messages.
            if lower is not None:
                digest = hashlib_new(msgid).hexdigest()
                # Convert the hex digest into a long
                slice = int(digest, 16)
                if slice < lower or slice > upper:
                    continue
            # Calculate the file's modification time
            try:
                mtime = os.path.getmtime(os.path.join(self.msgdir, f))
            except OSError:
                # The file disappeared, so skip it
                continue
            # Get a unique time by adding a small increment to the time in
            # case two entries have the same time.  This prevents skipping
            # one of two entries with the same time until the next pass.
            while mtime in times:
                mtime += DELTA
            times[mtime] = msgid
        # Yield the message ids in chronological order
        for mtime in sorted(times):
            yield times[mtime]

    def recover_backup_files(self) -> None:
        """Recover backup files in the message directory.
        
        This method moves all .bak files in our slice to .pck. It's impossible
        for both to exist at the same time, so the move is enough to ensure
        that our normal dequeuing process will handle them. We keep count in
        _bak_count in the metadata of the number of times we recover this
        file. When the count reaches MAX_BAK_COUNT, we move the .bak file
        to a .psv file in the shunt queue.
        """
        for f in os.listdir(self.msgdir):
            if not f.endswith('.bak'):
                continue
            msgid = f[:-4]
            # Calculate which slice this message id belongs to.  If we're
            # not slicing, then we'll process all messages.
            if self.__lower is not None:
                digest = hashlib_new(msgid).hexdigest()
                # Convert the hex digest into a long
                slice = int(digest, 16)
                if slice < self.__lower or slice > self.__upper:
                    continue
            # Get the metadata file
            datafile = os.path.join(self.msgdir, msgid + '.pck')
            # Get the backup file
            bakfile = os.path.join(self.msgdir, f)
            # Get the count of times this file has been recovered
            try:
                with open(datafile, 'rb') as fp:
                    msgdata = pickle.load(fp)
                count = msgdata.get('_bak_count', 0) + 1
            except (IOError, pickle.UnpicklingError):
                count = 1
            # If we've recovered this file too many times, move it to the
            # shunt queue
            if count >= MAX_BAK_COUNT:
                psvfile = os.path.join(mm_cfg.BADQUEUE_DIR, msgid + '.psv')
                # Create the directory if it doesn't yet exist.
                omask = os.umask(0)                       # rwxrws---
                try:
                    try:
                        os.mkdir(mm_cfg.BADQUEUE_DIR, 0o770)
                    except OSError as e:
                        if e.errno != errno.EEXIST: raise
                finally:
                    os.umask(omask)
                os.rename(bakfile, psvfile)
                os.rename(datafile, psvfile + '.pck')
                self.logger.warning('Moved %s to shunt queue after %d recoveries',
                                  msgid, count)
                continue
            # Update the count
            msgdata['_bak_count'] = count
            with open(datafile, 'wb') as fp:
                pickle.dump(msgdata, fp, protocol=2)
            # Move the backup file to the message file
            os.rename(bakfile, os.path.join(self.msgdir, msgid))

    def reject(self, msgid: str, reason: str) -> None:
        """Reject a message.
        
        Args:
            msgid: The message ID to reject
            reason: The reason for rejection
        """
        self.logger.warning('Rejecting message %s: %s', msgid, reason)
        self.finish(msgid, preserve=True)
