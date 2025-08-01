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

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Message
from Mailman.Logging.Syslog import syslog
from Mailman.Utils import sha_new

# 20 bytes of all bits set, maximum sha.digest() value
shamax = 0xffffffffffffffffffffffffffffffffffffffff

# This flag causes messages to be written as pickles (when True) or text files
# (when False).  Pickles are more efficient because the message doesn't need
# to be re-parsed every time it's unqueued, but pickles are not human readable.
SAVE_MSGS_AS_PICKLES = True
# Small increment to add to time in case two entries have the same time.  This
# prevents skipping one of two entries with the same time until the next pass.
DELTA = .0001
# We count the number of times a file has been moved to .bak and recovered.
# In order to prevent loops and a message flood, when the count reaches this
# value, we move the file to the shunt queue as a .psv.
MAX_BAK_COUNT = 3



class Switchboard:
    def __init__(self, whichq, slice=None, numslices=1, recover=False, distribution='hash'):
        self.__whichq = whichq
        self.__distribution = distribution
        # Create the directory if it doesn't yet exist.
        # FIXME
        omask = os.umask(0)                       # rwxrws---
        try:
            try:
                os.mkdir(self.__whichq, 0o0770)
            except OSError as e:
                if e.errno != errno.EEXIST: raise
        finally:
            os.umask(omask)
        # Fast track for no slices
        self.__lower = None
        self.__upper = None
        # Always set slice and numslices for compatibility
        self.__slice = slice
        self.__numslices = numslices
        # BAW: test performance and end-cases of this algorithm
        if numslices != 1:
            if distribution == 'hash':
                self.__lower = (((shamax+1) * slice) / numslices)
                self.__upper = ((((shamax+1) * (slice+1)) / numslices)) - 1
            elif distribution == 'round_robin':
                # __slice and __numslices already set above
                pass
            # Add more distribution methods here as needed
        if recover:
            self.recover_backup_files()

    def whichq(self):
        return self.__whichq

    def enqueue(self, _msg, _metadata={}, **_kws):
        from Mailman.Logging.Syslog import syslog
        # Calculate the SHA hexdigest of the message to get a unique base
        # filename.  We're also going to use the digest as a hash into the set
        # of parallel qrunner processes.
        data = _metadata.copy()
        data.update(_kws)
        listname = data.get('listname', '--nolist--')
        
        # DEBUG: Log archive queue enqueue
        if self.__whichq == mm_cfg.ARCHQUEUE_DIR:
            syslog('debug', 'Switchboard: Enqueuing message to archive queue for list %s', listname)
        
        # Get some data for the input to the sha hash
        now = time.time()
        if SAVE_MSGS_AS_PICKLES and not data.get('_plaintext'):
            protocol = 1
            msgsave = pickle.dumps(_msg, protocol, fix_imports=True)
        else:
            protocol = 0
            msgsave = pickle.dumps(str(_msg), protocol, fix_imports=True)
        
        # Choose distribution method
        if self.__distribution == 'round_robin':
            # Use a simple counter for round-robin distribution
            import threading
            if not hasattr(self, '_counter'):
                self._counter = 0
                self._counter_lock = threading.Lock()
            
            with self._counter_lock:
                self._counter = (self._counter + 1) % self.__numslices
                current_slice = self._counter
            hashfood = msgsave + listname.encode() + repr(now).encode() + str(current_slice).encode()
        else:
            # Default hash-based distribution
            hashfood = msgsave + listname.encode() + repr(now).encode()
        
        # Encode the current time into the file name for FIFO sorting in
        # files().  The file name consists of two parts separated by a `+':
        # the received time for this message (i.e. when it first showed up on
        # this system) and the sha hex digest.
        #rcvtime = data.setdefault('received_time', now)
        rcvtime = data.setdefault('received_time', now)
        filebase = repr(rcvtime) + '+' + sha_new(hashfood).hexdigest()
        filename = os.path.join(self.__whichq, filebase + '.pck')
        tmpfile = filename + '.tmp'
        # Always add the metadata schema version number
        data['version'] = mm_cfg.QFILE_SCHEMA_VERSION
        # Filter out volatile entries
        for k in list(data.keys()):
            if k.startswith('_'):
                del data[k]
        # We have to tell the dequeue() method whether to parse the message
        # object or not.
        data['_parsemsg'] = (protocol == 0)
        # Write to the pickle file the message object and metadata.
        omask = os.umask(0o007)                     # -rw-rw----
        try:
            fp = open(tmpfile, 'wb')
            try:
                fp.write(msgsave)
                pickle.dump(data, fp, protocol)
                fp.flush()
                os.fsync(fp.fileno())
            finally:
                fp.close()
        finally:
            os.umask(omask)
        os.rename(tmpfile, filename)
        
        # DEBUG: Log successful enqueue
        if self.__whichq == mm_cfg.ARCHQUEUE_DIR:
            syslog('debug', 'Switchboard: Successfully enqueued message to archive queue: %s', filebase)
        
        return filebase

    def dequeue(self, filebase):
        # Calculate the filename from the given filebase.
        filename = os.path.join(self.__whichq, filebase + '.pck')
        backfile = os.path.join(self.__whichq, filebase + '.bak')
        # Read the message object and metadata.
        fp = open(filename, 'rb')
        # Move the file to the backup file name for processing.  If this
        # process crashes uncleanly the .bak file will be used to re-instate
        # the .pck file in order to try again.
        os.rename(filename, backfile)
        try:
            msg = pickle.load(fp, fix_imports=True, encoding='latin1')
            data = pickle.load(fp, fix_imports=True, encoding='latin1')
        finally:
            fp.close()
        if data.get('_parsemsg'):
            msg = email.message_from_string(msg, Message.Message)
        return msg, data

    def finish(self, filebase, preserve=False):
        bakfile = os.path.join(self.__whichq, filebase + '.bak')
        try:
            if preserve:
                psvfile = os.path.join(mm_cfg.BADQUEUE_DIR, filebase + '.psv')
                # Create the directory if it doesn't yet exist.
                # Copied from __init__.
                omask = os.umask(0)                       # rwxrws---
                try:
                    try:
                        os.mkdir(mm_cfg.BADQUEUE_DIR, 0o0770)
                    except OSError as e:
                        if e.errno != errno.EEXIST: raise
                finally:
                    os.umask(omask)
                os.rename(bakfile, psvfile)
            else:
                os.unlink(bakfile)
        except EnvironmentError as e:
            syslog('error', 'Failed to unlink/preserve backup file: %s\n%s',
                   bakfile, e)

    def files(self, extension='.pck'):
        times = {}
        lower = self.__lower
        upper = self.__upper
        for f in os.listdir(self.__whichq):
            # By ignoring anything that doesn't end in .pck, we ignore
            # tempfiles and avoid a race condition.
            filebase, ext = os.path.splitext(f)
            if ext != extension:
                continue
            when, digest = filebase.split('+')
            
            # Choose distribution method for file filtering
            if self.__distribution == 'round_robin':
                # For round-robin, use modulo of digest to determine slice
                slice_num = int(digest, 16) % self.__numslices
                if slice_num == self.__slice:
                    key = float(when)
                    while key in times:
                        key += DELTA
                    times[key] = filebase
            else:
                # Default hash-based distribution
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
        keys.sort()
        return [times[k] for k in keys]

    def recover_backup_files(self):
        # Move all .bak files in our slice to .pck.  It's impossible for both
        # to exist at the same time, so the move is enough to ensure that our
        # normal dequeuing process will handle them.  We keep count in
        # _bak_count in the metadata of the number of times we recover this
        # file.  When the count reaches MAX_BAK_COUNT, we move the .bak file
        # to a .psv file in the shunt queue.
        for filebase in self.files('.bak'):
            src = os.path.join(self.__whichq, filebase + '.bak')
            dst = os.path.join(self.__whichq, filebase + '.pck')
            fp = open(src, 'rb+')
            try:
                try:
                    msg = pickle.load(fp, fix_imports=True, encoding='latin1')
                    data_pos = fp.tell()
                    data = pickle.load(fp, fix_imports=True, encoding='latin1')
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
