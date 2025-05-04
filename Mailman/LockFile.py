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

"""Portable, NFS-safe file locking with timeouts.

This code implements an NFS-safe file-based locking algorithm influenced by
the GNU/Linux open(2) manpage, under the description of the O_EXCL option.
From RH6.1:

        [...] O_EXCL is broken on NFS file systems, programs which rely on it
        for performing locking tasks will contain a race condition.  The
        solution for performing atomic file locking using a lockfile is to
        create a unique file on the same fs (e.g., incorporating hostname and
        pid), use link(2) to make a link to the lockfile.  If link() returns
        0, the lock is successful.  Otherwise, use stat(2) on the unique file
        to check if its link count has increased to 2, in which case the lock
        is also successful.

The assumption made here is that there will be no `outside interference',
e.g. no agent external to this code will have access to link() to the affected
lock files.

LockFile objects support lock-breaking so that you can't wedge a process
forever.  This is especially helpful in a web environment, but may not be
appropriate for all applications.

Locks have a `lifetime', which is the maximum length of time the process
expects to retain the lock.  It is important to pick a good number here
because other processes will not break an existing lock until the expected
lifetime has expired.  Too long and other processes will hang; too short and
you'll end up trampling on existing process locks -- and possibly corrupting
data.  In a distributed (NFS) environment, you also need to make sure that
your clocks are properly synchronized.

Locks can also log their state to a log file.  When running under Mailman, the
log file is placed in a Mailman-specific location, otherwise, the log file is
called `LockFile.log' and placed in the temp directory (calculated from
tempfile.mktemp()).

"""
from __future__ import print_function

# This code has undergone several revisions, with contributions from Barry
# Warsaw, Thomas Wouters, Harald Meland, and John Viega.  It should also work
# well outside of Mailman so it could be used for other Python projects
# requiring file locking.  See the __main__ section at the bottom of the file
# for unit testing.

from builtins import str
from builtins import range
from builtins import object
import os
import socket
import time
import errno
import random
import traceback
from stat import ST_NLINK, ST_MTIME
from Mailman.Logging.Syslog import mailman_log

# Units are floating-point seconds.
DEFAULT_LOCK_LIFETIME  = 15
# Allowable a bit of clock skew
CLOCK_SLOP = 10

# Exceptions that can be raised by this module
class LockError(Exception):
    """Base class for all exceptions in this module."""

class AlreadyLockedError(LockError):
    """An attempt is made to lock an already locked object."""

class NotLockedError(LockError):
    """An attempt is made to unlock an object that isn't locked."""

class TimeOutError(LockError):
    """The timeout interval elapsed before the lock succeeded."""


class LockFile:
    """A portable way to lock resources by way of the file system.

    This class supports the following methods:

    __init__(lockfile[, lifetime[, withlogging]]):
        Create the resource lock using lockfile as the global lock file.  Each
        process laying claim to this resource lock will create their own
        temporary lock files based on the path specified by lockfile.
        Optional lifetime is the number of seconds the process expects to hold
        the lock.  Optional withlogging, when true, turns on lockfile logging
        (see the module docstring for details).

    set_lifetime(lifetime):
        Set a new lock lifetime.  This takes affect the next time the file is
        locked, but does not refresh a locked file.

    get_lifetime():
        Return the lock's lifetime.

    refresh([newlifetime[, unconditionally]]):
        Refreshes the lifetime of a locked file.

        Use this if you realize that you need to keep a resource locked longer
        than you thought. With optional newlifetime, set the lock's lifetime.
        Raises NotLockedError if the lock is not set, unless optional
        unconditionally flag is set to true.

    lock([timeout]):
        Acquire the lock.

        This blocks until the lock is acquired unless optional timeout is
        greater than 0, in which case, a TimeOutError is raised when timeout
        number of seconds (or possibly more) expires without lock acquisition.
        Raises AlreadyLockedError if the lock is already set.

    unlock([unconditionally]):
        Relinquishes the lock.

        Raises a NotLockedError if the lock is not set, unless optional
        unconditionally is true.

    locked():
        Return true if the lock is set, otherwise false.

        To avoid race conditions, this refreshes the lock (on set locks).
        """
    # BAW: We need to watch out for two lock objects in the same process
    # pointing to the same lock file.  Without this, if you lock lf1 and do
    # not lock lf2, lf2.locked() will still return true.  NOTE: this gimmick
    # probably does /not/ work in a multithreaded world, but we don't have to
    # worry about that, do we? <1 wink>.
    COUNTER = 0

    def __init__(self, lockfile,
                 lifetime=DEFAULT_LOCK_LIFETIME,
                 withlogging=False):
        """Create the resource lock using lockfile as the global lock file.

        Each process laying claim to this resource lock will create their own
        temporary lock files based on the path specified by lockfile.
        Optional lifetime is the number of seconds the process expects to hold
        the lock.  Optional withlogging, when true, turns on lockfile logging
        (see the module docstring for details).

        """
        self.__lockfile = lockfile
        self.__lifetime = lifetime
        # This works because we know we're single threaded
        self.__counter = LockFile.COUNTER
        LockFile.COUNTER += 1
        self.__tmpfname = '%s.%s.%d.%d' % (
            lockfile, socket.gethostname(), os.getpid(), self.__counter)
        self.__withlogging = withlogging
        self.__logprefix = os.path.split(self.__lockfile)[1]
        # For transferring ownership across a fork.
        self.__owned = True

    def locked(self):
        """Return true if the lock is set, otherwise false.

        To avoid race conditions, this refreshes the lock (on set locks).
        """
        try:
            # Get the link count of our temp file
            nlinks = self.__linkcount()
            if nlinks == 2:
                # We have the lock, refresh it
                self.__touch()
                return True
            return False
        except OSError as e:
            if e.errno != errno.ENOENT:
                mailman_log('error', 'stat failed: %s', str(e))
                raise
            return False

    def finalize(self):
        """Clean up the lock file."""
        try:
            if self.locked():
                self.unlock(unconditionally=True)
        except Exception as e:
            mailman_log('error', 'Error during finalize: %s', str(e))
            raise

    def __del__(self):
        """Clean up when the object is garbage collected."""
        if self.__owned:
            try:
                self.finalize()
            except Exception as e:
                # Don't raise exceptions during garbage collection
                # Just log if we can
                try:
                    mailman_log('error', 'Error during cleanup: %s', str(e))
                except:
                    pass

    # Use these only if you're transfering ownership to a child process across
    # a fork.  Use at your own risk, but it should be race-condition safe.
    # _transfer_to() is called in the parent, passing in the pid of the child.
    # _take_possession() is called in the child, and blocks until the parent
    # has transferred possession to the child.  _disown() is used to set the
    # __owned flag to false, and it is a disgusting wart necessary to make
    # forced lock acquisition work in mailmanctl. :(
    def _transfer_to(self, pid):
        # First touch it so it won't get broken while we're fiddling about.
        self.__touch()
        # Find out current claim's temp filename
        winner = self.__read()
        # Now twiddle ours to the given pid
        self.__tmpfname = '%s.%s.%d' % (
            self.__lockfile, socket.gethostname(), pid)
        # Create a hard link from the global lock file to the temp file.  This
        # actually does things in reverse order of normal operation because we
        # know that lockfile exists, and tmpfname better not!
        mailman_log('debug', 'Attempting to transfer lock from %s to %s', winner, self.__tmpfname)
        os.link(self.__lockfile, self.__tmpfname)
        # Now update the lock file to contain a reference to the new owner
        self.__write()
        # Toggle off our ownership of the file so we don't try to finalize it
        # in our __del__()
        self.__owned = False
        # Unlink the old winner, completing the transfer
        os.unlink(winner)
        # And do some sanity checks
        link_count = self.__linkcount()
        if link_count != 2:
            mailman_log('error', 'Lock transfer failed: link count is %d, expected 2 for lockfile %s (temp file: %s)', 
                       link_count, self.__lockfile, self.__tmpfname)
            raise LockError('Lock transfer failed: link count is %d, expected 2' % link_count)
        if not self.locked():
            mailman_log('error', 'Lock transfer failed: lock not acquired for lockfile %s (temp file: %s)', 
                       self.__lockfile, self.__tmpfname)
            raise LockError('Lock transfer failed: lock not acquired')
        mailman_log('debug', 'Successfully transferred lock from %s to %s', winner, self.__tmpfname)

    def _take_possession(self):
        """Try to take possession of the lock file.

        Returns 0 if we successfully took possession of the lock file, -1 if we
        did not, and -2 if something very bad happened.
        """
        mailman_log('debug', 'attempting to take possession of lock')
        
        # First, clean up any stale temp files for all processes
        self.clean_stale_locks()
        
        # Create a temp file with our PID and hostname
        lockfile_dir = os.path.dirname(self.__lockfile)
        hostname = socket.gethostname()
        suffix = '.%s.%d' % (hostname, os.getpid())
        tempfile = self.__lockfile + suffix
        
        try:
            # Write our PID and hostname to help with debugging
            with open(tempfile, 'w') as fp:
                fp.write('%d %s\n' % (os.getpid(), hostname))
            # Set group read-write permissions (660)
            os.chmod(tempfile, 0o660)
        except (IOError, OSError) as e:
            mailman_log('error', 'failed to create temp file: %s', str(e))
            return -2

        # Try to create a hard link from the global lock file to our temp file
        try:
            os.link(tempfile, self.__lockfile)
        except OSError as e:
            if e.errno == errno.EEXIST:
                # Lock file exists, check if it's stale
                try:
                    with open(self.__lockfile, 'r') as fp:
                        pid_host = fp.read().strip().split()
                        if len(pid_host) == 2:
                            pid = int(pid_host[0])
                            if not self._is_pid_valid(pid):
                                # Stale lock, try to break it
                                mailman_log('debug', 'stale lock detected (pid=%d)', pid)
                                self._break()
                                # Try to create the link again
                                try:
                                    os.link(tempfile, self.__lockfile)
                                except OSError as e2:
                                    if e2.errno == errno.EEXIST:
                                        return -1
                                    raise
                            else:
                                return -1
                except (IOError, OSError, ValueError):
                    # Error reading lock file or invalid PID, try to break it
                    mailman_log('error', 'error reading lock file, attempting to break')
                    self._break()
                    try:
                        os.link(tempfile, self.__lockfile)
                    except OSError as e2:
                        if e2.errno == errno.EEXIST:
                            return -1
                        raise
            else:
                raise

        # Success! Set group read-write permissions on the lock file
        try:
            os.chmod(self.__lockfile, 0o660)
        except (IOError, OSError):
            pass  # Don't fail if we can't set permissions

        mailman_log('debug', 'successfully acquired lock')
        return 0

    def _is_pid_valid(self, pid):
        """Check if a PID is still valid (process exists).
        
        Returns True if the process exists, False otherwise.
        """
        try:
            # First check if process exists
            os.kill(pid, 0)
            
            # On Linux, check if it's a zombie
            try:
                with open(f'/proc/{pid}/status') as f:
                    status = f.read()
                    if 'State:' in status and 'Z (zombie)' in status:
                        mailman_log('debug', 'found zombie process (pid %d)', pid)
                        return False
            except (IOError, OSError):
                pass
                
            return True
        except OSError:
            return False

    def _break(self):
        """Break the lock.

        Returns 0 if we successfully broke the lock, -1 if we didn't, and -2 if
        something very bad happened.
        """
        mailman_log('debug', 'breaking the lock')
        try:
            if not os.path.exists(self.__lockfile):
                mailman_log('debug', 'nothing to break -- lock file does not exist')
                return -1
            # Read the lock file to get the old PID
            try:
                with open(self.__lockfile) as fp:
                    content = fp.read().strip()
                    if not content:
                        mailman_log('debug', 'lock file is empty')
                        os.unlink(self.__lockfile)
                        return 0
                        
                    # Parse PID and hostname from lock file
                    try:
                        parts = content.split()
                        if len(parts) >= 2:
                            pid = int(parts[0])
                            lock_hostname = ' '.join(parts[1:])  # Handle hostnames with spaces
                            if lock_hostname != socket.gethostname():
                                mailman_log('debug', 'lock owned by different host: %s', lock_hostname)
                                return -1
                        else:
                            # Try old format
                            try:
                                pid = int(content)
                            except ValueError:
                                mailman_log('debug', 'invalid lock file format: %s', content)
                                os.unlink(self.__lockfile)
                                return 0
                            
                        if not self._is_pid_valid(pid):
                            mailman_log('debug', 'breaking stale lock owned by pid %d', pid)
                            # Add random delay between 1-10 seconds before breaking lock
                            delay = random.uniform(1, 10)
                            mailman_log('debug', 'waiting %.2f seconds before breaking lock', delay)
                            time.sleep(delay)
                            os.unlink(self.__lockfile)
                            return 0
                        mailman_log('debug', 'lock is valid (pid %d)', pid)
                        return -1
                    except (ValueError, IndexError) as e:
                        mailman_log('error', 'error parsing lock content: %s', str(e))
                        os.unlink(self.__lockfile)
                        return 0
            except (ValueError, OSError) as e:
                mailman_log('error', 'error reading lock: %s', e)
                try:
                    os.unlink(self.__lockfile)
                    return 0
                except OSError:
                    return -2
        except OSError as e:
            mailman_log('error', 'error breaking lock: %s', e)
            return -2

    def clean_stale_locks(self):
        """Clean up any stale lock files for this lock.
        
        This is a safe method that can be called to clean up stale lock files
        without attempting to acquire the lock.
        """
        mailman_log('debug', 'cleaning stale locks')
        try:
            # Check for the main lock file
            if os.path.exists(self.__lockfile):
                try:
                    with open(self.__lockfile) as fp:
                        content = fp.read().strip().split()
                        if not content:
                            mailman_log('debug', 'lock file is empty')
                            os.unlink(self.__lockfile)
                            return
                            
                        # Parse PID and hostname from lock file
                        if len(content) >= 2:
                            pid = int(content[0])
                            lock_hostname = content[1]
                            
                            # Only clean locks from our host
                            if lock_hostname == socket.gethostname():
                                if not self._is_pid_valid(pid):
                                    mailman_log('debug', 'removing stale lock (pid %d)', pid)
                                    try:
                                        os.unlink(self.__lockfile)
                                    except OSError:
                                        pass
                        else:
                            # Try old format
                            try:
                                pid = int(content[0])
                                if not self._is_pid_valid(pid):
                                    mailman_log('debug', 'removing stale lock (pid %d)', pid)
                                    try:
                                        os.unlink(self.__lockfile)
                                    except OSError:
                                        pass
                            except (ValueError, IndexError):
                                mailman_log('debug', 'invalid lock file format')
                                try:
                                    os.unlink(self.__lockfile)
                                except OSError:
                                    pass
                except (ValueError, OSError) as e:
                    mailman_log('error', 'error reading lock: %s', e)
                    try:
                        os.unlink(self.__lockfile)
                    except OSError:
                        pass
            
            # Clean up any temp files
            lockfile_dir = os.path.dirname(self.__lockfile)
            base = os.path.basename(self.__lockfile)
            try:
                for filename in os.listdir(lockfile_dir):
                    if filename.startswith(base + '.'):
                        filepath = os.path.join(lockfile_dir, filename)
                        try:
                            # Check if temp file is old (> 1 hour)
                            if time.time() - os.path.getmtime(filepath) > 3600:
                                os.unlink(filepath)
                                mailman_log('debug', 'removed old temp file: %s', filepath)
                        except OSError as e:
                            mailman_log('error', 'error removing temp file %s: %s', filepath, e)
            except OSError as e:
                mailman_log('error', 'error listing directory: %s', e)
        except OSError as e:
            mailman_log('error', 'error cleaning locks: %s', e)

    #
    # Private interface
    #

    def __atomic_write(self, filename, content):
        """Atomically write content to a file using a temporary file."""
        tempname = filename + '.tmp'
        try:
            # Write to temporary file first
            with open(tempname, 'w') as f:
                f.write(content)
            # Atomic rename
            os.rename(tempname, filename)
        except Exception as e:
            # Clean up temp file if it exists
            try:
                os.unlink(tempname)
            except OSError:
                pass
            raise e

    def __write(self):
        """Write the lock file contents."""
        # Make sure it's group writable
        try:
            os.chmod(self.__tmpfname, 0o664)
        except OSError:
            pass
        self.__atomic_write(self.__tmpfname, self.__tmpfname)

    def __read(self):
        """Read the lock file contents."""
        try:
            with open(self.__lockfile, 'r') as fp:
                return fp.read().strip()
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise
            return ''

    def __touch(self, filename=None):
        """Touch the file to update its mtime."""
        if filename is None:
            filename = self.__tmpfname
        try:
            os.utime(filename, None)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise

    def __releasetime(self):
        """Return the time when the lock should be released."""
        try:
            mtime = os.stat(self.__lockfile)[ST_MTIME]
            return mtime + self.__lifetime + CLOCK_SLOP
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise
            return 0

    def __linkcount(self):
        """Return the link count of our temp file."""
        return os.stat(self.__tmpfname)[ST_NLINK]

    def __sleep(self):
        """Sleep for a random amount of time."""
        time.sleep(random.random() * 0.1)

    def __cleanup(self):
        """Clean up any temporary files."""
        try:
            if os.path.exists(self.__tmpfname):
                os.unlink(self.__tmpfname)
        except Exception as e:
            mailman_log('error', 'error during cleanup: %s', str(e))

    def __nfs_safe_stat(self, filename):
        """Perform NFS-safe stat operation with retries."""
        for i in range(self.__nfs_max_retries):
            try:
                return os.stat(filename)
            except OSError as e:
                if e.errno == errno.ESTALE:
                    # NFS stale file handle
                    time.sleep(self.__nfs_retry_delay)
                    continue
                raise
        raise OSError(errno.ESTALE, "NFS stale file handle after retries")

    def __break(self):
        """Break a stale lock.

        First, touch the global lock file.  This reduces but does not
        eliminate the chance for a race condition during breaking.  Two
        processes could both pass the test for lock expiry in lock() before
        one of them gets to touch the global lockfile.  This shouldn't be
        too bad because all they'll do in this function is wax the lock
        files, not claim the lock, and we can be defensive for ENOENTs
        here.

        Touching the lock could fail if the process breaking the lock and
        the process that claimed the lock have different owners.  We could
        solve this by set-uid'ing the CGI and mail wrappers, but I don't
        think it's that big a problem.
        """
        mailman_log('debug', 'breaking lock')
        try:
            self.__touch(self.__lockfile)
        except OSError as e:
            if e.errno != errno.ENOENT:
                mailman_log('error', 'touch failed: %s', str(e))
                raise
        try:
            os.unlink(self.__lockfile)
        except OSError as e:
            if e.errno != errno.ENOENT:
                mailman_log('error', 'unlink failed: %s', str(e))
                raise
        mailman_log('debug', 'lock broken')

    def lock(self, timeout=0):
        """Acquire the lock.

        This blocks until the lock is acquired unless optional timeout is
        greater than 0, in which case, a TimeOutError is raised when timeout
        number of seconds (or possibly more) expires without lock acquisition.
        Raises AlreadyLockedError if the lock is already set.
        """
        if self.locked():
            raise AlreadyLockedError('Lock already set')

        start = time.time()
        while True:
            try:
                # Create our temp file
                with open(self.__tmpfname, 'w') as fp:
                    fp.write(self.__tmpfname)
                # Set group read-write permissions
                os.chmod(self.__tmpfname, 0o660)
                # Try to create a hard link
                try:
                    os.link(self.__tmpfname, self.__lockfile)
                    # Success! We got the lock
                    self.__touch()
                    return
                except OSError as e:
                    if e.errno != errno.EEXIST:
                        raise
                    # Lock exists, check if it's stale
                    try:
                        releasetime = self.__releasetime()
                        if time.time() > releasetime:
                            # Lock is stale, try to break it
                            self.__break()
                            continue
                    except OSError:
                        # Lock file doesn't exist, try again
                        continue
            except OSError as e:
                mailman_log('error', 'Error creating lock: %s', str(e))
                raise

            # Check timeout
            if timeout > 0 and time.time() - start > timeout:
                raise TimeOutError('Timeout waiting for lock')

            # Sleep a bit before trying again
            self.__sleep()

    def unlock(self, unconditionally=False):
        """Relinquishes the lock.

        Raises a NotLockedError if the lock is not set, unless optional
        unconditionally is true.
        """
        if not unconditionally and not self.locked():
            raise NotLockedError('Lock not set')
        try:
            # Remove the lock file
            os.unlink(self.__lockfile)
            # Clean up our temp file
            self.__cleanup()
        except OSError as e:
            if e.errno != errno.ENOENT:
                mailman_log('error', 'Error removing lock: %s', str(e))
                raise

    def refresh(self, newlifetime=None, unconditionally=False):
        """Refreshes the lifetime of a locked file.
        
        Use this if you realize that you need to keep a resource locked longer
        than you thought. With optional newlifetime, set the lock's lifetime.
        Raises NotLockedError if the lock is not set, unless optional
        unconditionally flag is set to true.
        """
        if not unconditionally and not self.locked():
            raise NotLockedError('Lock not set')
        if newlifetime is not None:
            self.__lifetime = newlifetime
        self.__touch()


# Unit test framework
def _dochild():
    prefix = '[%d]' % os.getpid()
    # Create somewhere between 1 and 1000 locks
    lockfile = LockFile('/tmp/LockTest', withlogging=True, lifetime=120)
    # Use a lock lifetime of between 1 and 15 seconds.  Under normal
    # situations, Mailman's usage patterns (untested) shouldn't be much longer
    # than this.
    workinterval = 5 * random.random()
    hitwait = 20 * random.random()
    print((prefix, 'workinterval:', workinterval))
    islocked = False
    t0 = 0
    t1 = 0
    t2 = 0
    try:
        try:
            t0 = time.time()
            print((prefix, 'acquiring...'))
            lockfile.lock()
            print(( prefix, 'acquired...'))
            islocked = True
        except TimeOutError:
            print((prefix, 'timed out'))
        else:
            t1 = time.time()
            print((prefix, 'acquisition time:', t1-t0, 'seconds'))
            time.sleep(workinterval)
    finally:
        if islocked:
            try:
                lockfile.unlock()
                t2 = time.time()
                print((prefix, 'lock hold time:', t2-t1, 'seconds'))
            except NotLockedError:
                print((prefix, 'lock was broken'))
    # wait for next web hit
    print((prefix, 'webhit sleep:', hitwait))
    time.sleep(hitwait)


def _seed():
    try:
        fp = open('/dev/random')
        d = fp.read(40)
        fp.close()
    except EnvironmentError as e:
        if e.errno != errno.ENOENT:
            raise
        from Mailman.Utils import sha_new
        d = sha_new(str(os.getpid())+str(time.time())).hexdigest()
    random.seed(d)


def _onetest():
    loopcount = random.randint(1, 100)
    for i in range(loopcount):
        print('Loop %d of %d' % (i+1, loopcount))
        pid = os.fork()
        if pid:
            # parent, wait for child to exit
            pid, status = os.waitpid(pid, 0)
        else:
            # child
            _seed()
            try:
                _dochild()
            except KeyboardInterrupt:
                pass
            os._exit(0)


def _reap(kids):
    if not kids:
        return
    pid, status = os.waitpid(-1, os.WNOHANG)
    if pid != 0:
        del kids[pid]


def _test(numtests):
    kids = {}
    for i in range(numtests):
        pid = os.fork()
        if pid:
            # parent
            kids[pid] = pid
        else:
            # child
            _seed()
            try:
                _onetest()
            except KeyboardInterrupt:
                pass
            os._exit(0)
        # slightly randomize each kid's seed
    while kids:
        _reap(kids)


if __name__ == '__main__':
    import sys
    import random
    _test(int(sys.argv[1]))
