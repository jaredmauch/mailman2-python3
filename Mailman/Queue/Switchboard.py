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
import email.message
from email.message import Message as EmailMessage
import hashlib
import socket
import traceback

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Message
from Mailman.Logging.Syslog import mailman_log
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
    def __init__(self, whichq, slice=None, numslices=1, recover=False):
        self.__whichq = whichq
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
        # BAW: test performance and end-cases of this algorithm
        if numslices != 1:
            self.__lower = (((shamax+1) * slice) / numslices)
            self.__upper = ((((shamax+1) * (slice+1)) / numslices)) - 1
        if recover:
            self.recover_backup_files()

    def whichq(self):
        return self.__whichq

    def enqueue(self, _msg, _metadata={}, **_kws):
        # Calculate the SHA hexdigest of the message to get a unique base
        # filename.  We're also going to use the digest as a hash into the set
        # of parallel qrunner processes.
        data = _metadata.copy()
        data.update(_kws)
        
        # Ensure metadata values are properly encoded
        for key, value in list(data.items()):
            if isinstance(key, bytes):
                del data[key]
                key = key.decode('utf-8', 'replace')
            if isinstance(value, bytes):
                value = value.decode('utf-8', 'replace')
            elif isinstance(value, list):
                value = [
                    v.decode('utf-8', 'replace') if isinstance(v, bytes) else v
                    for v in value
                ]
            elif isinstance(value, dict):
                new_dict = {}
                for k, v in value.items():
                    if isinstance(k, bytes):
                        k = k.decode('utf-8', 'replace')
                    if isinstance(v, bytes):
                        v = v.decode('utf-8', 'replace')
                    new_dict[k] = v
                value = new_dict
            data[key] = value
        
        listname = data.get('listname', '--nolist--')
        # Get some data for the input to the sha hash
        now = time.time()
        if SAVE_MSGS_AS_PICKLES and not data.get('_plaintext'):
            # Convert email.message.Message to Mailman.Message if needed
            if isinstance(_msg, email.message.Message) and not isinstance(_msg, Message.Message):
                mailman_msg = Message.Message()
                # Copy all attributes from the original message
                for key, value in _msg.items():
                    mailman_msg[key] = value
                # Copy the payload
                if _msg.is_multipart():
                    for part in _msg.get_payload():
                        mailman_msg.attach(part)
                else:
                    mailman_msg.set_payload(_msg.get_payload())
                _msg = mailman_msg
            # Use protocol 2 for Python 2/3 compatibility
            msgsave = pickle.dumps(_msg, protocol=2, fix_imports=True)
        else:
            # Use protocol 2 for Python 2/3 compatibility
            msgsave = pickle.dumps(str(_msg), protocol=2, fix_imports=True)
        hashfood = msgsave + listname.encode('utf-8') + repr(now).encode('utf-8')
        # Encode the current time into the file name for FIFO sorting in
        # files().  The file name consists of two parts separated by a `+':
        # the received time for this message (i.e. when it first showed up on
        # this system) and the sha hex digest.
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
        protocol = 2  # Use protocol 2 for Python 2/3 compatibility
        data['_parsemsg'] = (protocol == 0)
        # Write to the pickle file the message object and metadata.
        omask = os.umask(0o007)                     # -rw-rw----
        try:
            fp = open(tmpfile, 'wb')
            try:
                fp.write(msgsave)
                pickle.dump(data, fp, protocol=2, fix_imports=True)
                fp.flush()
                os.fsync(fp.fileno())
            finally:
                fp.close()
        finally:
            os.umask(omask)
        os.rename(tmpfile, filename)
        return filebase

    def dequeue(self, filebase):
        # Calculate the filename from the given filebase.
        filename = os.path.join(self.__whichq, filebase + '.pck')
        backfile = os.path.join(self.__whichq, filebase + '.bak')
        
        try:
            # Move the file to the backup file name for processing.  If this
            # process crashes uncleanly the .bak file will be used to re-instate
            # the .pck file in order to try again.
            os.rename(filename, backfile)
            
            # Read the message object and metadata.
            with open(backfile, 'rb') as fp:
                # Try loading with Python 3 protocol first
                try:
                    msg = pickle.load(fp, fix_imports=True)
                    data = pickle.load(fp, fix_imports=True)
                except (pickle.UnpicklingError, EOFError) as e:
                    # If that fails, try with Python 2 protocol
                    fp.seek(0)
                    try:
                        msg = pickle.load(fp, fix_imports=True, encoding='latin1')
                        data = pickle.load(fp, fix_imports=True, encoding='latin1')
                    except (pickle.UnpicklingError, EOFError) as e2:
                        # The file is corrupted. Log the error and try to recover it.
                        mailman_log('error', 'Failed to unpickle file %s: Python 3 error: %s, Python 2 error: %s',
                               backfile, str(e), str(e2))
                        self.recover_backup_files()
                        return None, None
                
                # Convert any bytes in the loaded data to strings
                if isinstance(data, dict):
                    for key, value in list(data.items()):
                        if isinstance(key, bytes):
                            del data[key]
                            key = key.decode('utf-8', 'replace')
                            data[key] = value
                        if isinstance(value, bytes):
                            data[key] = value.decode('utf-8', 'replace')
                        elif isinstance(value, list):
                            data[key] = [
                                v.decode('utf-8', 'replace') if isinstance(v, bytes) else v
                                for v in value
                            ]
                        elif isinstance(value, dict):
                            new_dict = {}
                            for k, v in value.items():
                                if isinstance(k, bytes):
                                    k = k.decode('utf-8', 'replace')
                                if isinstance(v, bytes):
                                    v = v.decode('utf-8', 'replace')
                                new_dict[k] = v
                            data[key] = new_dict
                    
            # Always try to parse the message if it's a string
            if isinstance(msg, str) or data.get('_parsemsg'):
                try:
                    msg = email.message_from_string(msg, EmailMessage)
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
                except Exception as e:
                    mailman_log('error', 'Failed to parse message from file %s: %s', backfile, str(e))
                    return None, None
                
            return msg, data
            
        except EnvironmentError as e:
            if e.errno != errno.ENOENT:
                mailman_log('error', 'Environment error processing file %s: %s', backfile, str(e))
                raise
            return None, None
        except Exception as e:
            mailman_log('error', 'Unexpected error processing file %s: %s', backfile, str(e))
            return None, None

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
                
                # Verify source file exists before moving
                if not os.path.exists(bakfile):
                    mailman_log('error', 'Source backup file does not exist: %s', bakfile)
                    return
                    
                # Move the file and verify
                os.rename(bakfile, psvfile)
                if not os.path.exists(psvfile):
                    mailman_log('error', 'Failed to move backup file to shunt queue: %s -> %s',
                           bakfile, psvfile)
                else:
                    mailman_log('info', 'Successfully moved backup file to shunt queue: %s -> %s',
                           bakfile, psvfile)
            else:
                # Verify file exists before unlinking
                if not os.path.exists(bakfile):
                    mailman_log('error', 'Backup file does not exist for unlinking: %s', bakfile)
                    return
                    
                os.unlink(bakfile)
                if os.path.exists(bakfile):
                    mailman_log('error', 'Failed to unlink backup file: %s', bakfile)
                else:
                    mailman_log('info', 'Successfully unlinked backup file: %s', bakfile)
        except EnvironmentError as e:
            mailman_log('error', 'Failed to unlink/preserve backup file: %s\n%s',
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
                    tb = traceback.format_exc()
                    mailman_log('error', 'Unpickling .bak exception: %s\n'
                           + 'Traceback:\n%s\n'
                           + 'preserving file: %s', s, tb, filebase)
                    self.finish(filebase, preserve=True)
                else:
                    data['_bak_count'] = data.setdefault('_bak_count', 0) + 1
                    data['_last_attempt'] = time.time()
                    if '_error_history' not in data:
                        data['_error_history'] = []
                    if '_traceback' in data:
                        data['_error_history'].append({
                            'error': data.get('_last_error', 'unknown'),
                            'traceback': data.get('_traceback', 'none'),
                            'time': data.get('_last_attempt', 0)
                        })
                    fp.seek(data_pos)
                    if data.get('_parsemsg'):
                        protocol = 0
                    else:
                        protocol = 1
                    pickle.dump(data, fp, protocol=2, fix_imports=True)
                    fp.truncate()
                    fp.flush()
                    os.fsync(fp.fileno())
                    
                    # Log detailed information about the retry
                    mailman_log('warning',
                           'Message retry attempt %d/%d: %s (queue: %s, '
                           'message-id: %s, listname: %s, recipients: %s, '
                           'error: %s, last attempt: %s, traceback: %s)',
                           data['_bak_count'],
                           MAX_BAK_COUNT,
                           filebase,
                           self.__whichq,
                           data.get('message-id', 'unknown'),
                           data.get('listname', 'unknown'),
                           data.get('recips', 'unknown'),
                           data.get('_last_error', 'unknown'),
                           time.ctime(data.get('_last_attempt', 0)),
                           data.get('_traceback', 'none'))
                    
                    if data['_bak_count'] >= MAX_BAK_COUNT:
                        mailman_log('error',
                               'Backup file exceeded maximum retry count (%d). '
                               'Moving to shunt queue: %s (original queue: %s, '
                               'retry count: %d, last error: %s, '
                               'message-id: %s, listname: %s, '
                               'recipients: %s, error history: %s, '
                               'last traceback: %s)',
                               MAX_BAK_COUNT,
                               filebase,
                               self.__whichq,
                               data['_bak_count'],
                               data.get('_last_error', 'unknown'),
                               data.get('message-id', 'unknown'),
                               data.get('listname', 'unknown'),
                               data.get('recips', 'unknown'),
                               data.get('_error_history', 'unknown'),
                               data.get('_traceback', 'none'))
                        self.finish(filebase, preserve=True)
                    else:
                        os.rename(src, dst)
            finally:
                fp.close()

    def _enqueue(self, msg, metadata=None, _recips=None):
        """Enqueue a message for delivery.
        
        This method implements a robust enqueue mechanism with:
        1. Unique temporary filename
        2. Atomic write
        3. Validation of written data
        4. Proper error handling and cleanup
        """
        # Create a unique filename
        now = time.time()
        hashbase = hashlib.md5(repr(now).encode()).hexdigest()
        msginfo = msg.get('message-id', '')
        if msginfo:
            hashbase = hashlib.md5((hashbase + msginfo).encode()).hexdigest()
        qfile = os.path.join(self.__whichq, hashbase + '.pck')
        tmpfile = qfile + '.tmp.%s.%d' % (socket.gethostname(), os.getpid())
        
        # Prepare the data to be pickled
        data = (msg, metadata or {}, _recips)
        
        try:
            # Ensure directory exists with proper permissions
            dirname = os.path.dirname(tmpfile)
            if not os.path.exists(dirname):
                try:
                    os.makedirs(dirname, 0o755)
                except Exception as e:
                    mailman_log('error', 'Failed to create directory %s: %s', dirname, e)
                    raise
            
            # Write to temporary file first
            with open(tmpfile, 'wb') as fp:
                pickle.dump(data, fp, protocol=2, fix_imports=True)
                fp.flush()
                if hasattr(os, 'fsync'):
                    os.fsync(fp.fileno())
            
            # Validate the temporary file
            try:
                with open(tmpfile, 'rb') as fp:
                    test_data = pickle.load(fp, fix_imports=True, encoding='latin1')
                if not isinstance(test_data, tuple) or len(test_data) != 3:
                    raise TypeError('Loaded data is not a valid tuple')
            except Exception as e:
                mailman_log('error', 'Validation of temporary file failed: %s', e)
                # Try to clean up
                try:
                    os.unlink(tmpfile)
                except Exception:
                    pass
                raise
            
            # Atomic rename
            try:
                os.rename(tmpfile, qfile)
            except Exception as e:
                mailman_log('error', 'Failed to rename %s to %s: %s', tmpfile, qfile, e)
                # Try to clean up
                try:
                    os.unlink(tmpfile)
                except Exception:
                    pass
                raise
            
            # Set proper permissions
            try:
                os.chmod(qfile, 0o660)
            except Exception as e:
                mailman_log('warning', 'Failed to set permissions on %s: %s', qfile, e)
                # Not critical, continue
                
        except Exception:
            # Clean up any temporary files
            try:
                if os.path.exists(tmpfile):
                    os.unlink(tmpfile)
            except Exception:
                pass
            raise

    def _dequeue(self, filename):
        """Dequeue a message from the queue."""
        try:
            with open(filename, 'rb') as fp:
                try:
                    msgsave = pickle.load(fp, fix_imports=True, encoding='latin1')
                    metadata = pickle.load(fp, fix_imports=True, encoding='latin1')
                except (pickle.UnpicklingError, EOFError) as e:
                    raise SwitchboardError('Could not unpickle %s: %s' %
                                         (filename, e))
            # Try to unpickle the message
            try:
                msg = pickle.loads(msgsave, fix_imports=True, encoding='latin1')
            except (pickle.UnpicklingError, EOFError) as e:
                raise SwitchboardError('Could not unpickle message from %s: %s' %
                                     (filename, e))
            return msg, metadata
        except (IOError, OSError) as e:
            raise SwitchboardError('Could not read %s: %s' % (filename, e))

    def _dequeue_metadata(self, filename):
        """Dequeue just the metadata from the queue."""
        try:
            with open(filename, 'rb') as fp:
                try:
                    # Skip the message
                    pickle.load(fp, fix_imports=True, encoding='latin1')
                    # Get the metadata
                    metadata = pickle.load(fp, fix_imports=True, encoding='latin1')
                except (pickle.UnpicklingError, EOFError) as e:
                    raise SwitchboardError('Could not unpickle %s: %s' %
                                         (filename, e))
            return metadata
        except (IOError, OSError) as e:
            raise SwitchboardError('Could not read %s: %s' % (filename, e))
