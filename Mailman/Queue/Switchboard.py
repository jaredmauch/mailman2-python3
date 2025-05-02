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
from email.message import Message
import hashlib
import socket
import traceback

from Mailman import mm_cfg
from Mailman import Utils
from Mailman.Message import Message
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
            # Clean up any stale locks during initialization
            self.cleanup_stale_locks()

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
            if isinstance(_msg, email.message.Message) and not isinstance(_msg, Message):
                mailman_msg = Message()
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
        lockfile = filename + '.lock'
        
        # Create lock file with proper cleanup
        max_attempts = 30  # Increased from 10 to 30
        attempt = 0
        while attempt < max_attempts:
            try:
                # Try to create lock file
                lock_fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                # Write process info to lock file for debugging
                with os.fdopen(lock_fd, 'w') as f:
                    f.write('pid=%d\nhost=%s\ntime=%f\n' % (
                        os.getpid(),
                        socket.gethostname(),
                        time.time()
                    ))
                break
            except OSError as e:
                if e.errno == errno.EEXIST:
                    # Check if lock is stale (older than 5 minutes)
                    try:
                        lock_age = time.time() - os.path.getmtime(lockfile)
                        if lock_age > 300:  # 5 minutes
                            # Read lock file contents for debugging
                            try:
                                with open(lockfile, 'r') as f:
                                    lock_info = f.read()
                                mailman_log('warning', 
                                    'Removing stale lock file %s (age: %d seconds)\nLock info: %s',
                                    lockfile, lock_age, lock_info)
                            except Exception:
                                mailman_log('warning', 
                                    'Removing stale lock file %s (age: %d seconds)',
                                    lockfile, lock_age)
                            os.unlink(lockfile)
                            continue
                    except OSError:
                        pass
                    # Wait before retrying with exponential backoff
                    time.sleep(min(1.0 * (2 ** attempt), 10.0))
                    attempt += 1
                    continue
                raise
        else:
            mailman_log('error', 'Failed to acquire lock for %s after %d attempts', filename, max_attempts)
            return None, None
            
        try:
            # Read the message and metadata
            try:
                with open(filename, 'rb') as fp:
                    # Use protocol 2 for Python 2/3 compatibility
                    msg = pickle.load(fp, fix_imports=True, encoding='latin1')
                    metadata = pickle.load(fp, fix_imports=True, encoding='latin1')
            except (pickle.UnpicklingError, EOFError) as e:
                mailman_log('error', 'Error unpickling file %s: %s', filename, str(e))
                return None, None
                
            # Move to backup file
            try:
                os.rename(filename, backfile)
            except OSError as e:
                mailman_log('error', 'Error moving %s to %s: %s', filename, backfile, str(e))
                return None, None
                
            return msg, metadata
        finally:
            # Always clean up the lock file
            try:
                os.unlink(lockfile)
            except OSError:
                pass

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
                        if e.errno != errno.EEXIST:
                            mailman_log('error', 'Failed to create shunt queue directory %s: %s\nTraceback:\n%s',
                                   mm_cfg.BADQUEUE_DIR, str(e), traceback.format_exc())
                            raise
                finally:
                    os.umask(omask)
                
                # Verify source file exists before moving
                if not os.path.exists(bakfile):
                    mailman_log('error', 'Source backup file does not exist: %s', bakfile)
                    return
                    
                # Move the file and verify
                try:
                    os.rename(bakfile, psvfile)
                    if not os.path.exists(psvfile):
                        mailman_log('error', 'Failed to move backup file to shunt queue: %s -> %s',
                               bakfile, psvfile)
                    else:
                        mailman_log('info', 'Successfully moved backup file to shunt queue: %s -> %s',
                               bakfile, psvfile)
                except OSError as e:
                    mailman_log('error', 'Failed to move backup file to shunt queue: %s -> %s: %s\nTraceback:\n%s',
                           bakfile, psvfile, str(e), traceback.format_exc())
                    raise
            else:
                # Verify file exists before unlinking
                if not os.path.exists(bakfile):
                    mailman_log('error', 'Backup file does not exist for unlinking: %s', bakfile)
                    return
                    
                try:
                    os.unlink(bakfile)
                    if os.path.exists(bakfile):
                        mailman_log('error', 'Failed to unlink backup file: %s', bakfile)
                    else:
                        mailman_log('info', 'Successfully unlinked backup file: %s', bakfile)
                except OSError as e:
                    mailman_log('error', 'Failed to unlink backup file %s: %s\nTraceback:\n%s',
                           bakfile, str(e), traceback.format_exc())
                    raise
        except Exception as e:
            mailman_log('error', 'Failed to finish processing backup file %s: %s\nTraceback:\n%s',
                   bakfile, str(e), traceback.format_exc())
            raise

    def files(self, extension='.pck'):
        times = {}
        lower = self.__lower
        upper = self.__upper
        try:
            for f in os.listdir(self.__whichq):
                # By ignoring anything that doesn't end in .pck, we ignore
                # tempfiles and avoid a race condition.
                filebase, ext = os.path.splitext(f)
                if ext != extension:
                    continue
                try:
                    # Validate file name format
                    if '+' not in filebase:
                        full_path = os.path.join(self.__whichq, f)
                        mailman_log('warning', 'Invalid file name format in queue directory (missing +): %s (full path: %s)', f, full_path)
                        # Try to recover by moving to shunt queue
                        try:
                            src = os.path.join(self.__whichq, f)
                            dst = os.path.join(mm_cfg.BADQUEUE_DIR, filebase + '.psv')
                            if not os.path.exists(mm_cfg.BADQUEUE_DIR):
                                os.makedirs(mm_cfg.BADQUEUE_DIR, 0o770)
                            os.rename(src, dst)
                            mailman_log('info', 'Moved invalid file to shunt queue: %s -> %s', f, dst)
                        except Exception as e:
                            mailman_log('error', 'Failed to move invalid file %s to shunt queue: %s\nTraceback:\n%s',
                                   f, str(e), traceback.format_exc())
                        continue

                    parts = filebase.split('+')
                    if len(parts) != 2:
                        full_path = os.path.join(self.__whichq, f)
                        mailman_log('warning', 'Invalid file name format in queue directory (wrong number of parts): %s (full path: %s)', f, full_path)
                        # Try to recover by moving to shunt queue
                        try:
                            src = os.path.join(self.__whichq, f)
                            dst = os.path.join(mm_cfg.BADQUEUE_DIR, filebase + '.psv')
                            if not os.path.exists(mm_cfg.BADQUEUE_DIR):
                                os.makedirs(mm_cfg.BADQUEUE_DIR, 0o770)
                            os.rename(src, dst)
                            mailman_log('info', 'Moved invalid file to shunt queue: %s -> %s', f, dst)
                        except Exception as e:
                            mailman_log('error', 'Failed to move invalid file %s to shunt queue: %s\nTraceback:\n%s',
                                   f, str(e), traceback.format_exc())
                        continue

                    when, digest = parts
                    try:
                        # Validate timestamp format
                        when_float = float(when)
                        # Validate digest format (should be hex)
                        try:
                            digest_int = int(digest, 16)
                        except ValueError as e:
                            mailman_log('error', 'Invalid digest format in queue file %s: %s', f, e)
                            raise
                    except ValueError as e:
                        full_path = os.path.join(self.__whichq, f)
                        mailman_log('warning', 'Invalid file name format in queue directory (invalid timestamp/digest): %s: %s (full path: %s)', f, str(e), full_path)
                        # Try to recover by moving to shunt queue
                        try:
                            src = os.path.join(self.__whichq, f)
                            dst = os.path.join(mm_cfg.BADQUEUE_DIR, filebase + '.psv')
                            if not os.path.exists(mm_cfg.BADQUEUE_DIR):
                                os.makedirs(mm_cfg.BADQUEUE_DIR, 0o770)
                            os.rename(src, dst)
                            mailman_log('info', 'Moved invalid file to shunt queue: %s -> %s', f, dst)
                        except Exception as e:
                            mailman_log('error', 'Failed to move invalid file %s to shunt queue: %s\nTraceback:\n%s',
                                   f, str(e), traceback.format_exc())
                        continue

                    # Throw out any files which don't match our bitrange.  BAW: test
                    # performance and end-cases of this algorithm.  MAS: both
                    # comparisons need to be <= to get complete range.
                    if lower is None or (lower <= digest_int <= upper):
                        key = when_float
                        while key in times:
                            key += DELTA
                        times[key] = filebase
                except Exception as e:
                    mailman_log('error', 'Unexpected error processing file %s: %s\nTraceback:\n%s',
                           f, str(e), traceback.format_exc())
                    continue
        except OSError as e:
            mailman_log('error', 'Failed to list queue directory %s: %s\nTraceback:\n%s',
                   self.__whichq, str(e), traceback.format_exc())
            raise
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
        try:
            for filebase in self.files('.bak'):
                src = os.path.join(self.__whichq, filebase + '.bak')
                dst = os.path.join(self.__whichq, filebase + '.pck')
                
                # First check if the file is too old
                try:
                    file_age = time.time() - os.path.getmtime(src)
                    if file_age > mm_cfg.FORM_LIFETIME:
                        mailman_log('warning',
                            'Backup file %s is too old (%d seconds), moving to shunt queue',
                            filebase, file_age)
                        self.finish(filebase, preserve=True)
                        continue
                except OSError as e:
                    mailman_log('error', 'Error checking file age for %s: %s\nTraceback:\n%s',
                           filebase, str(e), traceback.format_exc())
                    continue
                    
                try:
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
                                   + 'preserving file: %s (full path: %s)', s, tb, filebase, os.path.join(self.__whichq, filebase + '.bak'))
                            self.finish(filebase, preserve=True)
                            continue
                        
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
                                   'last traceback: %s, full path: %s)',
                                   MAX_BAK_COUNT,
                                   filebase,
                                   self.__whichq,
                                   data['_bak_count'],
                                   data.get('_last_error', 'unknown'),
                                   data.get('message-id', 'unknown'),
                                   data.get('listname', 'unknown'),
                                   data.get('recips', 'unknown'),
                                   data.get('_error_history', 'unknown'),
                                   data.get('_traceback', 'none'),
                                   os.path.join(self.__whichq, filebase + '.bak'))
                            self.finish(filebase, preserve=True)
                        else:
                            try:
                                os.rename(src, dst)
                            except OSError as e:
                                mailman_log('error', 'Failed to rename backup file %s (full paths: %s -> %s): %s\nTraceback:\n%s',
                                       filebase, os.path.join(self.__whichq, filebase + '.bak'), os.path.join(self.__whichq, filebase + '.pck'), str(e), traceback.format_exc())
                                self.finish(filebase, preserve=True)
                    finally:
                        fp.close()
                except Exception as e:
                    mailman_log('error', 'Failed to process backup file %s (full path: %s): %s\nTraceback:\n%s',
                           filebase, os.path.join(self.__whichq, filebase + '.bak'), str(e), traceback.format_exc())
                    continue
        except Exception as e:
            mailman_log('error', 'Failed to recover backup files: %s\nTraceback:\n%s',
                   str(e), traceback.format_exc())
            raise

    def _enqueue(self, msg, metadata=None, _recips=None):
        """Enqueue a message for delivery.
        
        This method implements a robust enqueue mechanism with:
        1. Unique temporary filename
        2. Atomic write
        3. Validation of written data
        4. Proper error handling and cleanup
        5. File locking for concurrent access
        """
        # Create a unique filename
        now = time.time()
        hashbase = hashlib.md5(repr(now).encode()).hexdigest()
        msginfo = msg.get('message-id', '')
        if msginfo:
            hashbase = hashlib.md5((hashbase + msginfo).encode()).hexdigest()
        qfile = os.path.join(self.__whichq, hashbase + '.pck')
        tmpfile = qfile + '.tmp.%s.%d' % (socket.gethostname(), os.getpid())
        lockfile = qfile + '.lock'
        
        # Create lock file
        try:
            lock_fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(lock_fd)
        except OSError as e:
            if e.errno == errno.EEXIST:
                mailman_log('warning', 'Lock file exists for %s (full path: %s), waiting...', qfile, lockfile)
                # Wait for lock to be released
                for _ in range(10):  # 10 attempts
                    try:
                        lock_fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        os.close(lock_fd)
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    mailman_log('error', 'Could not acquire lock for %s (full path: %s) after 10 attempts', qfile, lockfile)
                    raise
            else:
                mailman_log('error', 'Failed to create lock file %s (full path: %s): %s\nTraceback:\n%s',
                       qfile, lockfile, str(e), traceback.format_exc())
                raise
        
        try:
            # Ensure directory exists with proper permissions
            dirname = os.path.dirname(tmpfile)
            if not os.path.exists(dirname):
                try:
                    os.makedirs(dirname, 0o755)
                except Exception as e:
                    mailman_log('error', 'Failed to create directory %s (full path: %s): %s\nTraceback:\n%s',
                           dirname, os.path.abspath(dirname), str(e), traceback.format_exc())
                    raise
            
            # Write to temporary file first
            try:
                with open(tmpfile, 'wb') as fp:
                    pickle.dump(data, fp, protocol=2, fix_imports=True)
                    fp.flush()
                    if hasattr(os, 'fsync'):
                        os.fsync(fp.fileno())
            except Exception as e:
                mailman_log('error', 'Failed to write temporary file %s (full path: %s): %s\nTraceback:\n%s',
                       tmpfile, os.path.abspath(tmpfile), str(e), traceback.format_exc())
                raise
            
            # Validate the temporary file
            try:
                with open(tmpfile, 'rb') as fp:
                    test_data = pickle.load(fp, fix_imports=True, encoding='latin1')
                if not isinstance(test_data, tuple) or len(test_data) != 3:
                    raise TypeError('Loaded data is not a valid tuple')
            except Exception as e:
                mailman_log('error', 'Validation of temporary file failed: %s\nTraceback:\n%s', 
                       str(e), traceback.format_exc())
                # Try to clean up
                try:
                    os.unlink(tmpfile)
                except Exception as cleanup_e:
                    mailman_log('error', 'Failed to clean up temporary file %s (full path: %s): %s\nTraceback:\n%s',
                           tmpfile, os.path.abspath(tmpfile), str(cleanup_e), traceback.format_exc())
                raise
            
            # Atomic rename with existence check
            try:
                if os.path.exists(qfile):
                    mailman_log('warning', 'Target file %s (full path: %s) already exists, removing old version', qfile, os.path.abspath(qfile))
                    os.unlink(qfile)
                os.rename(tmpfile, qfile)
            except Exception as e:
                mailman_log('error', 'Failed to rename %s to %s (full paths: %s -> %s): %s\nTraceback:\n%s',
                       tmpfile, qfile, os.path.abspath(tmpfile), os.path.abspath(qfile), str(e), traceback.format_exc())
                # Try to clean up
                try:
                    if os.path.exists(tmpfile):
                        os.unlink(tmpfile)
                except Exception as cleanup_e:
                    mailman_log('error', 'Failed to clean up temporary file %s (full path: %s): %s\nTraceback:\n%s',
                           tmpfile, os.path.abspath(tmpfile), str(cleanup_e), traceback.format_exc())
                raise
            
            # Set proper permissions
            try:
                os.chmod(qfile, 0o660)
            except Exception as e:
                mailman_log('warning', 'Failed to set permissions on %s (full path: %s): %s\nTraceback:\n%s',
                       qfile, os.path.abspath(qfile), str(e), traceback.format_exc())
                # Not critical, continue
                
        finally:
            # Clean up any temporary files and lock
            try:
                if os.path.exists(tmpfile):
                    os.unlink(tmpfile)
                if os.path.exists(lockfile):
                    os.unlink(lockfile)
            except Exception as cleanup_e:
                mailman_log('error', 'Failed to clean up temporary/lock files: %s\nTraceback:\n%s',
                       str(cleanup_e), traceback.format_exc())

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

    def cleanup_stale_locks(self):
        """Clean up any stale lock files in the queue directory."""
        try:
            for f in os.listdir(self.__whichq):
                if f.endswith('.lock'):
                    lockfile = os.path.join(self.__whichq, f)
                    try:
                        lock_age = time.time() - os.path.getmtime(lockfile)
                        if lock_age > 300:  # 5 minutes
                            # Read lock file contents for debugging
                            try:
                                with open(lockfile, 'r') as f:
                                    lock_info = f.read()
                                mailman_log('warning', 
                                    'Cleaning up stale lock file %s (age: %d seconds)\nLock info: %s',
                                    lockfile, lock_age, lock_info)
                            except Exception:
                                mailman_log('warning', 
                                    'Cleaning up stale lock file %s (age: %d seconds)',
                                    lockfile, lock_age)
                            os.unlink(lockfile)
                    except OSError:
                        pass
        except OSError as e:
            mailman_log('error', 'Error cleaning up stale locks: %s', str(e))
