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

# Custom exception class for Switchboard errors
class SwitchboardError(Exception):
    """Exception raised for errors in the Switchboard class."""
    pass

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
            # Clean up any stale backup files
            self.cleanup_stale_backups()
            # Clean up any stale processed files
            self.cleanup_stale_processed()

    def whichq(self):
        return self.__whichq

    def enqueue(self, msg, msgdata=None, listname=None, _plaintext=False, **kwargs):
        """Add a message to the queue.
        
        Args:
            msg: The message to enqueue
            msgdata: Optional message metadata
            listname: Optional list name
            _plaintext: Whether to save as plaintext
            **kwargs: Additional metadata to add
        """
        # Initialize msgdata if not provided
        if msgdata is None:
            msgdata = {}
            
        # Add any additional metadata
        msgdata.update(kwargs)
        
        # Add listname if provided
        if listname:
            msgdata['listname'] = listname
            
        # Then check if we need to set recips
        if 'recips' not in msgdata or not msgdata['recips']:
            # If we have a recipient but no recips, use the recipient
            if msgdata.get('recipient'):
                msgdata['recips'] = [msgdata['recipient']]
                mailman_log('debug', 'Switchboard.enqueue: Set recips from recipient for message: %s',
                           msg.get('message-id', 'n/a'))
            # Otherwise try to get recipients from message headers
            else:
                recips = []
                # First try envelope-to header
                if msg.get('envelope-to'):
                    recips.append(msg.get('envelope-to'))
                # Then try To header
                if msg.get('to'):
                    addrs = email.utils.getaddresses([msg.get('to')])
                    recips.extend([addr[1] for addr in addrs if addr[1]])
                # Then try Cc header
                if msg.get('cc'):
                    addrs = email.utils.getaddresses([msg.get('cc')])
                    recips.extend([addr[1] for addr in addrs if addr[1]])
                # Finally try Bcc header
                if msg.get('bcc'):
                    addrs = email.utils.getaddresses([msg.get('bcc')])
                    recips.extend([addr[1] for addr in addrs if addr[1]])
                
                if recips:
                    msgdata['recips'] = recips
                    mailman_log('debug', 'Switchboard.enqueue: Set recipients from message headers for message: %s',
                               msg.get('message-id', 'n/a'))
                else:
                    mailman_log('error', 'Switchboard: No recipients found in msgdata or message headers for message: %s',
                               msg.get('message-id', 'n/a'))
                    raise ValueError('Switchboard: No recipients found in msgdata or message headers')
        
        # Generate a unique filebase
        filebase = self._make_filebase(msg, msgdata)
        
        # Calculate the filename
        filename = os.path.join(self.__whichq, filebase + '.pck')
        
        # Create a lock file
        lockfile = filename + '.lock'
        try:
            fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
        except OSError as e:
            if e.errno != errno.EEXIST:
                mailman_log('error', 'Switchboard.enqueue: Failed to create lock file for %s: %s', filebase, str(e))
                raise
            return None

        try:
            # Write the message and metadata
            try:
                self._enqueue(filename, msg, msgdata, _plaintext)
            except Exception as e:
                mailman_log('error', 'Switchboard.enqueue: Failed to write message to %s: %s', filebase, str(e))
                raise

            # Add filebase to msgdata for cleanup
            msgdata['filebase'] = filebase
            return filebase
        finally:
            # Always clean up the lock file
            try:
                os.unlink(lockfile)
            except OSError:
                pass

    def dequeue(self, filebase):
        # Calculate the filename from the given filebase.
        filename = os.path.join(self.__whichq, filebase + '.pck')
        bakfile = os.path.join(self.__whichq, filebase + '.bak')
        psvfile = os.path.join(self.__whichq, filebase + '.psv')
        
        # Check if file exists before proceeding
        if not os.path.exists(filename):
            # Check if it's been moved to backup or shunt
            if os.path.exists(bakfile):
                mailman_log('debug', 'Queue file %s has been moved to backup file %s', filename, bakfile)
            elif os.path.exists(psvfile):
                mailman_log('debug', 'Queue file %s has been moved to shunt queue %s', filename, psvfile)
            else:
                mailman_log('warning', 'Queue file does not exist: %s (not found in backup or shunt either)', filename)
            return None, None
            
        # Read the message object and metadata.
        fp = open(filename, 'rb')
        # Move the file to the backup file name for processing.  If this
        # process crashes uncleanly the .bak file will be used to re-instate
        # the .pck file in order to try again.
        os.rename(filename, bakfile)
        try:
            msg = pickle.load(fp, fix_imports=True, encoding='latin1')
            data = pickle.load(fp, fix_imports=True, encoding='latin1')
        finally:
            fp.close()
        if data.get('_parsemsg'):
            msg = email.message_from_string(msg, Message)
        # Add filebase to msgdata for cleanup
        if data is not None:
            data['filebase'] = filebase
        return msg, data

    def finish(self, filebase, preserve=False):
        """Finish processing a file by either removing it or moving it to the shunt queue.
        
        Args:
            filebase: The base name of the file to process
            preserve: If True, move the file to the shunt queue instead of removing it
        """
        if not filebase:
            mailman_log('error', 'Switchboard.finish: No filebase provided')
            return

        bakfile = os.path.join(self.__whichq, filebase + '.bak')
        pckfile = os.path.join(self.__whichq, filebase + '.pck')
        
        # First check if the backup file exists
        if not os.path.exists(bakfile):
            # Only log at debug level if the .pck file still exists (message still being processed)
            if os.path.exists(pckfile):
                mailman_log('debug', 'Switchboard.finish: Backup file does not exist: %s', bakfile)
                # Try to clean up the .pck file if it exists
                try:
                    os.unlink(pckfile)
                    mailman_log('debug', 'Switchboard.finish: Removed stale .pck file: %s', pckfile)
                except OSError as e:
                    mailman_log('error', 'Switchboard.finish: Failed to remove stale .pck file %s: %s',
                              pckfile, str(e))
            return

        try:
            if preserve:
                # Move the file to the shunt queue
                psvfile = os.path.join(mm_cfg.SHUNTQUEUE_DIR, filebase + '.bak')
                
                # Ensure the shunt queue directory exists
                if not os.path.exists(mm_cfg.SHUNTQUEUE_DIR):
                    try:
                        os.makedirs(mm_cfg.SHUNTQUEUE_DIR, 0o775)
                    except OSError as e:
                        mailman_log('error', 'Switchboard.finish: Failed to create shunt queue directory: %s',
                                  str(e))
                        raise
                
                # Move the file and verify
                try:
                    os.rename(bakfile, psvfile)
                    if not os.path.exists(psvfile):
                        mailman_log('error', 'Switchboard.finish: Failed to move backup file to shunt queue: %s -> %s',
                                  bakfile, psvfile)
                    else:
                        mailman_log('debug', 'Switchboard.finish: Successfully moved backup file to shunt queue: %s -> %s',
                                  bakfile, psvfile)
                except OSError as e:
                    mailman_log('error', 'Switchboard.finish: Failed to move backup file to shunt queue: %s -> %s: %s',
                              bakfile, psvfile, str(e))
                    raise
            else:
                # Remove the backup file
                try:
                    os.unlink(bakfile)
                    if os.path.exists(bakfile):
                        mailman_log('error', 'Switchboard.finish: Failed to unlink backup file: %s', bakfile)
                    else:
                        mailman_log('debug', 'Switchboard.finish: Successfully unlinked backup file: %s', bakfile)
                except OSError as e:
                    mailman_log('error', 'Switchboard.finish: Failed to unlink backup file %s: %s',
                              bakfile, str(e))
                    raise
        except Exception as e:
            mailman_log('error', 'Switchboard.finish: Failed to finish processing backup file %s: %s',
                      bakfile, str(e))
            raise

    def files(self, extension='.pck'):
        times = {}
        lower = self.__lower
        upper = self.__upper
        try:
            for f in os.listdir(self.__whichq):
                if not f.endswith(extension):
                    continue
                filebase = f[:-len(extension)]
                try:
                    # Get the file's modification time
                    mtime = os.path.getmtime(os.path.join(self.__whichq, f))
                    # Only apply time bounds if they are set
                    if lower is None or upper is None or (lower <= mtime < upper):
                        times[filebase] = mtime
                except OSError:
                    continue
            # Sort by modification time but return just the filebases
            return [f for f, _ in sorted(times.items(), key=lambda x: x[1])]
        except OSError as e:
            mailman_log('error', 'Error reading queue directory %s: %s', self.__whichq, str(e))
            return []

    def recover_backup_files(self):
        """Move all .bak files in our slice to .pck.
        
        This method implements a robust recovery mechanism with:
        1. Proper error handling for corrupted files
        2. Validation of backup file contents
        3. Detailed logging of recovery attempts
        4. Safe file operations with atomic moves
        """
        try:
            for filebase in self.files('.bak'):
                src = os.path.join(self.__whichq, filebase + '.bak')
                dst = os.path.join(self.__whichq, filebase + '.pck')
                
                try:
                    # First try to validate the backup file
                    with open(src, 'rb') as fp:
                        try:
                            # Try to read the entire file first to check for EOF
                            content = fp.read()
                            if not content:
                                mailman_log('error', 'Empty backup file found: %s', filebase)
                                raise EOFError('Empty backup file')
                            
                            # Create a BytesIO object to read from the content
                            from io import BytesIO
                            fp = BytesIO(content)
                            
                            try:
                                msg = pickle.load(fp, fix_imports=True, encoding='latin1')
                                data_pos = fp.tell()
                                data = pickle.load(fp, fix_imports=True, encoding='latin1')
                            except (EOFError, pickle.UnpicklingError) as e:
                                mailman_log('error', 'Corrupted backup file %s: %s\nTraceback:\n%s',
                                       filebase, str(e), traceback.format_exc())
                                self.finish(filebase, preserve=True)
                                return
                            
                            # Validate the unpickled data
                            if not isinstance(data, dict):
                                mailman_log('error', 'Invalid data format in backup file %s: expected dict, got %s', filebase, type(data))
                                raise TypeError('Invalid data format in backup file')
                                
                            try:
                                os.rename(src, dst)
                            except Exception as e:
                                mailman_log('error', 'Failed to rename backup file %s (full paths: %s -> %s): %s\nTraceback:\n%s',
                                       filebase, os.path.join(self.__whichq, filebase + '.bak'), os.path.join(self.__whichq, filebase + '.pck'), str(e), traceback.format_exc())
                                self.finish(filebase, preserve=True)
                                return
                        except Exception as e:
                            mailman_log('error', 'Failed to process backup file %s (full path: %s): %s\nTraceback:\n%s',
                                   filebase, os.path.join(self.__whichq, filebase + '.bak'), str(e), traceback.format_exc())
                            self.finish(filebase, preserve=True)
                            return
                            
                except Exception as e:
                    mailman_log('error', 'Failed to process backup file %s (full path: %s): %s\nTraceback:\n%s',
                           filebase, os.path.join(self.__whichq, filebase + '.bak'), str(e), traceback.format_exc())
                    return None, None
        except Exception as e:
            mailman_log('error', 'Failed to recover backup files: %s\nTraceback:\n%s',
                   str(e), traceback.format_exc())
            raise

    def _enqueue(self, filename, msg, msgdata, _plaintext):
        """Enqueue a message for delivery.
        
        This method implements a robust enqueue mechanism with:
        1. Unique temporary filename
        2. Atomic write
        3. Validation of written data
        4. Proper error handling and cleanup
        5. File locking for concurrent access
        """
        # Create a unique filename using the standard format
        now = time.time()
        msgid = msg.get('message-id', '')
        listname = msgdata.get('listname', '--nolist--')
        hash_input = (str(msgid) + str(listname) + str(now)).encode('utf-8')
        digest = hashlib.sha1(hash_input).hexdigest()
        filebase = "%d+%s" % (int(now), digest)
        qfile = os.path.join(self.__whichq, filebase + '.pck')
        tmpfile = qfile + '.tmp.%s.%d' % (socket.gethostname(), os.getpid())
        lockfile = qfile + '.lock'
        
        # Create lock file
        try:
            lock_fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(lock_fd)
        except OSError as e:
            if e.errno == errno.EEXIST:
                mailman_log('warning', 'Lock file exists for %s (full path: %s)', qfile, lockfile)
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
            
            # Convert message to Mailman.Message if needed
            if isinstance(msg, email.message.Message) and not isinstance(msg, Message):
                mailman_msg = Message()
                # Copy all attributes from the original message
                for key, value in msg.items():
                    mailman_msg[key] = value
                # Copy the payload with proper MIME handling
                if msg.is_multipart():
                    for part in msg.get_payload():
                        if isinstance(part, email.message.Message):
                            mailman_msg.attach(part)
                        else:
                            newpart = Message()
                            newpart.set_payload(part)
                            mailman_msg.attach(newpart)
                else:
                    mailman_msg.set_payload(msg.get_payload())
                msg = mailman_msg
            
            # Write to temporary file first
            try:
                with open(tmpfile, 'wb') as fp:
                    pickle.dump((msg, msgdata), fp, protocol=4, fix_imports=True)
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
                if not isinstance(test_data, tuple) or len(test_data) != 2:
                    raise TypeError('Loaded data is not a valid tuple')
                # Verify message type
                if not isinstance(test_data[0], Message):
                    raise TypeError('Message is not a Mailman.Message instance')
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
                    # Try UTF-8 first for newer files
                    data = pickle.load(fp, fix_imports=True, encoding='utf-8')
                    if not isinstance(data, tuple) or len(data) != 2:
                        raise TypeError('Invalid data format in queue file')
                    msgsave, metadata = data
                    
                    # Ensure we have a Mailman.Message
                    if isinstance(msgsave, email.message.Message) and not isinstance(msgsave, Message):
                        mailman_msg = Message()
                        # Copy all attributes from the original message
                        for key, value in msgsave.items():
                            mailman_msg[key] = value
                        # Copy the payload with proper MIME handling
                        if msgsave.is_multipart():
                            for part in msgsave.get_payload():
                                if isinstance(part, email.message.Message):
                                    mailman_msg.attach(part)
                                else:
                                    newpart = Message()
                                    newpart.set_payload(part)
                                    mailman_msg.attach(newpart)
                        else:
                            mailman_msg.set_payload(msgsave.get_payload())
                        msgsave = mailman_msg
                    
                    return msgsave, metadata
                except (UnicodeDecodeError, pickle.UnpicklingError):
                    # Fall back to latin1 for older files
                    fp.seek(0)
                    data = pickle.load(fp, fix_imports=True, encoding='latin1')
                    if not isinstance(data, tuple) or len(data) != 2:
                        raise TypeError('Invalid data format in queue file')
                    msgsave, metadata = data
                    
                    # Ensure we have a Mailman.Message
                    if isinstance(msgsave, email.message.Message) and not isinstance(msgsave, Message):
                        mailman_msg = Message()
                        # Copy all attributes from the original message
                        for key, value in msgsave.items():
                            mailman_msg[key] = value
                        # Copy the payload with proper MIME handling
                        if msgsave.is_multipart():
                            for part in msgsave.get_payload():
                                if isinstance(part, email.message.Message):
                                    mailman_msg.attach(part)
                                else:
                                    newpart = Message()
                                    newpart.set_payload(part)
                                    mailman_msg.attach(newpart)
                        else:
                            mailman_msg.set_payload(msgsave.get_payload())
                        msgsave = mailman_msg
                    
                    return msgsave, metadata
        except (IOError, OSError) as e:
            mailman_log('error', 'Error dequeuing message from %s: %s', filename, str(e))
            return None, None

    def _dequeue_metadata(self, filename):
        """Dequeue just the metadata from the queue."""
        try:
            with open(filename, 'rb') as fp:
                try:
                    # Try UTF-8 first, then fall back to latin-1
                    try:
                        # Skip the message
                        pickle.load(fp, fix_imports=True, encoding='utf-8')
                        # Get the metadata
                        metadata = pickle.load(fp, fix_imports=True, encoding='utf-8')
                    except (pickle.UnpicklingError, EOFError) as e:
                        # Reset file pointer to beginning
                        fp.seek(0)
                        # Try latin-1 as fallback
                        pickle.load(fp, fix_imports=True, encoding='latin1')
                        metadata = pickle.load(fp, fix_imports=True, encoding='latin1')
                except (pickle.UnpicklingError, EOFError) as e:
                    raise IOError('Could not unpickle %s: %s' % (filename, e))
            return metadata
        except (IOError, OSError) as e:
            raise IOError('Could not read %s: %s' % (filename, e))

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

    def cleanup_stale_backups(self):
        """Clean up any stale backup files in the queue directory.
        
        This method removes backup files that are older than 24 hours
        to prevent accumulation of stale files.
        """
        try:
            now = time.time()
            stale_age = 24 * 3600  # 24 hours in seconds
            
            for f in os.listdir(self.__whichq):
                if f.endswith('.bak'):
                    bakfile = os.path.join(self.__whichq, f)
                    try:
                        # Check file age
                        file_age = now - os.path.getmtime(bakfile)
                        if file_age > stale_age:
                            mailman_log('warning', 
                                'Cleaning up stale backup file %s (age: %d seconds)',
                                bakfile, file_age)
                            os.unlink(bakfile)
                    except OSError as e:
                        mailman_log('error', 
                            'Failed to clean up stale backup file %s: %s',
                            bakfile, str(e))
        except OSError as e:
            mailman_log('error', 'Error cleaning up stale backup files: %s', str(e))

    def cleanup_stale_processed(self):
        """Clean up any stale processed files in the queue directory.
        
        This method removes processed files that are older than 7 days
        to prevent accumulation of stale files.
        """
        try:
            now = time.time()
            stale_age = 7 * 24 * 3600  # 7 days in seconds
            
            for f in os.listdir(self.__whichq):
                if f.endswith('.pck'):
                    pckfile = os.path.join(self.__whichq, f)
                    try:
                        # Check file age
                        file_age = now - os.path.getmtime(pckfile)
                        if file_age > stale_age:
                            mailman_log('warning', 
                                'Cleaning up stale processed file %s (age: %d seconds)',
                                pckfile, file_age)
                            os.unlink(pckfile)
                    except OSError as e:
                        mailman_log('error', 
                            'Failed to clean up stale processed file %s: %s',
                            pckfile, str(e))
        except OSError as e:
            mailman_log('error', 'Error cleaning up stale processed files: %s', str(e))

    def _make_filebase(self, msg, msgdata):
        import hashlib
        import time
        msgid = msg.get('message-id', '')
        listname = msgdata.get('listname', '--nolist--')
        now = time.time()
        hash_input = (str(msgid) + str(listname) + str(now)).encode('utf-8')
        digest = hashlib.sha1(hash_input).hexdigest()
        return "%d+%s" % (int(now), digest)
