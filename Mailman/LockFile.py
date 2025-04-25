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

# Units are floating-point seconds.
DEFAULT_LOCK_LIFETIME  = 15
# Allowable a bit of clock skew
CLOCK_SLOP = 10


# Figure out what logfile to use.  This is different depending on whether
# we're running in a Mailman context or not.
_logfile = None

def _get_logfile():
    global _logfile
    if _logfile is None:
        try:
            from Mailman.Logging.StampedLogger import StampedLogger
            _logfile = StampedLogger('locks')
        except ImportError:
            # not running inside Mailman
            import tempfile
            dir = os.path.split(tempfile.mktemp())[0]
            path = os.path.join(dir, 'LockFile.log')
            # open in line-buffered mode
            class SimpleUserFile(object):
                def __init__(self, path):
                    self.__fp = open(path, 'a', 1)
                    self.__prefix = '(%d) ' % os.getpid()
                def write(self, msg):
                    now = '%.3f' % time.time()
                    self.__fp.write(self.__prefix + now + ' ' + msg)
            _logfile = SimpleUserFile(path)
    return _logfile



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
        Refreshes the lifetime of a locked file.  Use this if you realize that
        you need to keep a resource locked longer than you thought.  With
        optional newlifetime, set the lock's lifetime.   Raises NotLockedError
        if the lock is not set, unless optional unconditionally flag is set to
        true.

    lock([timeout]):
        Acquire the lock.  This blocks until the lock is acquired unless
        optional timeout is greater than 0, in which case, a TimeOutError is
        raised when timeout number of seconds (or possibly more) expires
        without lock acquisition.  Raises AlreadyLockedError if the lock is
        already set.

    unlock([unconditionally]):
        Relinquishes the lock.  Raises a NotLockedError if the lock is not
        set, unless optional unconditionally is true.

    locked():
        Return true if the lock is set, otherwise false.  To avoid race
        conditions, this refreshes the lock (on set locks).

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
        # Maximum number of retries for lock operations
        self.__max_retries = 100
        # NFS-specific settings
        self.__nfs_retry_delay = 0.1
        self.__nfs_max_retries = 5

    def __repr__(self):
        return '<LockFile %s: %s [%s: %ssec] pid=%s>' % (
            id(self), self.__lockfile,
            self.locked() and 'locked' or 'unlocked',
            self.__lifetime, os.getpid())

    def set_lifetime(self, lifetime):
        """Set a new lock lifetime.

        This takes affect the next time the file is locked, but does not
        refresh a locked file.
        """
        self.__lifetime = lifetime

    def get_lifetime(self):
        """Return the lock's lifetime."""
        return self.__lifetime

    def refresh(self, newlifetime=None, unconditionally=False):
        """Refreshes the lifetime of a locked file.

        Use this if you realize that you need to keep a resource locked longer
        than you thought.  With optional newlifetime, set the lock's lifetime.
        Raises NotLockedError if the lock is not set, unless optional
        unconditionally flag is set to true.
        """
        if newlifetime is not None:
            self.set_lifetime(newlifetime)
        # Do we have the lock?  As a side effect, this refreshes the lock!
        if not self.locked() and not unconditionally:
            raise NotLockedError('%s: %s' % (repr(self), self.__read()))

    def lock(self, timeout=0):
        """Acquire the lock with improved timeout and interrupt handling."""
        if self.locked():
            raise AlreadyLockedError
        # Set up the timeout
        if timeout > 0:
            endtime = time.time() + timeout
        else:
            endtime = None
        # Try to acquire the lock
        loopcount = 0
        retry_count = 0
        while True:
            try:
                # Check for timeout
                if endtime and time.time() > endtime:
                    raise TimeOutError
                # Check for max retries
                if retry_count >= self.__max_retries:
                    self.__writelog('max retries exceeded', important=True)
                    raise TimeOutError
                # Try to acquire the lock
                if self._take_possession():
                    self.__writelog('locked')
                    return
                # Check if the lock is stale
                if self.__releasetime() < time.time():
                    # Yes, so break the lock.
                    self.__break()
                    self.__writelog('lifetime has expired, breaking',
                                    important=True)
                # Wait a while for the owner of the lock to give it up
                elif not loopcount % 100:
                    self.__writelog('waiting for claim')
                self.__sleep()
                loopcount += 1
                retry_count += 1
            except KeyboardInterrupt:
                # If we get a keyboard interrupt, clean up and re-raise
                self.__writelog('interrupted while waiting for lock')
                self.__cleanup()
                raise
            except Exception as e:
                # Log any other exceptions but continue trying
                self.__writelog(f'error while waiting for lock: {str(e)}')
                self.__sleep()
                loopcount += 1
                retry_count += 1

    def unlock(self, unconditionally=False):
        """Unlock the lock.

        If we don't already own the lock (either because of unbalanced unlock
        calls, or because the lock was stolen out from under us), raise a
        NotLockedError, unless optional `unconditionally' is true.
        """
        try:
            islocked = self.locked()
            if not islocked and not unconditionally:
                raise NotLockedError
            # If we owned the lock, remove the global file, relinquishing it.
            if islocked:
                try:
                    os.unlink(self.__lockfile)
                except OSError as e:
                    if e.errno != errno.ENOENT:
                        raise
            # Remove our tempfile
            try:
                os.unlink(self.__tmpfname)
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise
            self.__writelog('unlocked')
        except Exception as e:
            self.__writelog(f'Error during unlock: {str(e)}', important=True)
            raise

    def locked(self):
        """Return true if we own the lock, false if we do not.

        Checking the status of the lock resets the lock's lifetime, which
        helps avoid race conditions during the lock status test.
        """
        try:
            # Discourage breaking the lock for a while.
            self.__touch()
        except OSError as e:
            if e.errno == errno.EPERM:
                # We can't touch the file because we're not the owner
                return False
            else:
                raise
        # TBD: can the link count ever be > 2?
        if self.__linkcount() != 2:
            return False
        return self.__read() == self.__tmpfname

    def finalize(self):
        self.unlock(unconditionally=True)

    def __del__(self):
        """Clean up when the object is garbage collected."""
        if self.__owned:
            try:
                self.finalize()
            except Exception as e:
                # Don't raise exceptions during garbage collection
                # Just log if we can
                try:
                    self.__writelog(f'Error during cleanup: {str(e)}', important=True)
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
        os.link(self.__lockfile, self.__tmpfname)
        # Now update the lock file to contain a reference to the new owner
        self.__write()
        # Toggle off our ownership of the file so we don't try to finalize it
        # in our __del__()
        self.__owned = False
        # Unlink the old winner, completing the transfer
        os.unlink(winner)
        # And do some sanity checks
        assert self.__linkcount() == 2
        assert self.locked()
        self.__writelog('transferred the lock')

    def _take_possession(self):
        self.__tmpfname = tmpfname = '%s.%s.%d' % (
            self.__lockfile, socket.gethostname(), os.getpid())
        # Wait until the linkcount is 2, indicating the parent has completed
        # the transfer.
        while self.__linkcount() != 2 or self.__read() != tmpfname:
            time.sleep(0.25)
        self.__writelog('took possession of the lock')

    def _disown(self):
        self.__owned = False

    #
    # Private interface
    #

    def __writelog(self, msg, important=False):
        """Write a message to the log file."""
        if not self.__withlogging and not important:
            return
        try:
            logf = _get_logfile()
            logf.write('%s %s\n' % (self.__logprefix, msg))
            if important:
                traceback.print_stack(file=logf)
        except Exception:
            # Don't raise exceptions during logging
            pass

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
        """Write the lock file atomically."""
        try:
            self.__atomic_write(self.__tmpfname, self.__tmpfname)
        except OSError as e:
            self.__writelog(f'error writing temp file: {str(e)}')
            raise

    def __read(self):
        """Read the lock file contents."""
        try:
            with open(self.__lockfile) as fp:
                filename = fp.read()
                return filename
        except EnvironmentError as e:
            if e.errno != errno.ENOENT:
                raise
            return None

    def __touch(self, filename=None):
        t = time.time() + self.__lifetime
        try:
            # TBD: We probably don't need to modify atime, but this is easier.
            os.utime(filename or self.__tmpfname, (t, t))
        except OSError as e:
            if e.errno != errno.ENOENT: raise

    def __releasetime(self):
        """Get release time with NFS safety."""
        try:
            return self.__nfs_safe_stat(self.__lockfile)[ST_MTIME]
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise
            return -1

    def __linkcount(self):
        """Get link count with NFS safety."""
        try:
            return self.__nfs_safe_stat(self.__lockfile)[ST_NLINK]
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise
            return -1

    def __break(self):
        """Break a stale lock with improved error handling."""
        try:
            # First, touch the global lock file to reduce race conditions
            self.__touch()
        except OSError as e:
            if e.errno != errno.EPERM:
                self.__writelog(f'error touching lock file: {str(e)}')
                raise
        # Remove the lock files
        try:
            os.unlink(self.__lockfile)
        except OSError as e:
            if e.errno != errno.ENOENT:
                self.__writelog(f'error removing lock file: {str(e)}')
                raise
        try:
            os.unlink(self.__tmpfname)
        except OSError as e:
            if e.errno != errno.ENOENT:
                self.__writelog(f'error removing temp file: {str(e)}')
                raise
        self.__writelog('lock broken', important=True)

    def __sleep(self):
        """Sleep for a short interval, handling keyboard interrupts gracefully."""
        try:
            # Use a fixed interval with small jitter for more predictable behavior
            interval = 0.1 + (random.random() * 0.1)  # 100-200ms
            time.sleep(interval)
        except KeyboardInterrupt:
            # If we get a keyboard interrupt during sleep, raise it
            raise
        except Exception:
            # For any other exception during sleep, just continue
            pass

    def __cleanup(self):
        """Clean up any temporary files in case of error."""
        try:
            if os.path.exists(self.__tmpfname):
                os.unlink(self.__tmpfname)
        except OSError as e:
            self.__writelog(f'error cleaning up temp file: {str(e)}')

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
