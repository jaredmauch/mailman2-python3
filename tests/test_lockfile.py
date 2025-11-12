# Copyright (C) 2002-2018 by the Free Software Foundation, Inc.
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

"""Unit tests for the LockFile class.
"""

import unittest
import os
import time
import errno
try:
    from Mailman import __init__
except ImportError:
    import paths

from Mailman.LockFile import LockFile, AlreadyLockedError, NotLockedError
from Mailman.MailList import MailList
from Mailman import Utils
from Mailman import mm_cfg

LOCKFILE_NAME = '/tmp/.mm-test-lock'
TEST_LIST_NAME = 'test-list'


class TestLockFile(unittest.TestCase):
    def setUp(self):
        # Clean up any existing lock file
        try:
            os.unlink(LOCKFILE_NAME)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise

    def tearDown(self):
        # Clean up after each test
        try:
            os.unlink(LOCKFILE_NAME)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise

    def test_two_lockfiles_same_proc(self):
        lf1 = LockFile(LOCKFILE_NAME)
        lf2 = LockFile(LOCKFILE_NAME)
        lf1.lock()
        self.failIf(lf2.locked())

    def test_normal_lock_release(self):
        lf = LockFile(LOCKFILE_NAME)
        lf.lock()
        self.assertTrue(lf.locked())
        lf.unlock()
        self.assertFalse(lf.locked())

    def test_unconditional_unlock(self):
        lf = LockFile(LOCKFILE_NAME)
        # Should not raise an error even when not locked
        lf.unlock(unconditionally=True)
        lf.lock()
        lf.unlock(unconditionally=True)
        self.assertFalse(lf.locked())

    def test_lock_timeout(self):
        lf1 = LockFile(LOCKFILE_NAME)
        lf2 = LockFile(LOCKFILE_NAME)
        lf1.lock()
        # Try to acquire lock with short timeout
        start_time = time.time()
        try:
            lf2.lock(timeout=0.1)
            self.fail("Expected timeout")
        except AlreadyLockedError:
            pass
        elapsed = time.time() - start_time
        self.assertTrue(0.1 <= elapsed < 0.2)  # Should timeout after ~0.1s
        lf1.unlock()

    def test_lock_refresh(self):
        lf = LockFile(LOCKFILE_NAME, lifetime=1)
        lf.lock()
        self.assertTrue(lf.locked())
        time.sleep(0.5)  # Wait half the lifetime
        lf.refresh()  # Refresh the lock
        time.sleep(0.6)  # Wait more than original lifetime
        self.assertTrue(lf.locked())  # Should still be locked due to refresh
        lf.unlock()

    def test_lock_lifetime(self):
        lf = LockFile(LOCKFILE_NAME, lifetime=1)
        lf.lock()
        self.assertTrue(lf.locked())
        time.sleep(1.1)  # Wait longer than lifetime
        self.assertFalse(lf.locked())  # Should have expired

    def test_error_handling(self):
        lf = LockFile(LOCKFILE_NAME)
        # Test that lock is released after exception
        try:
            with self.assertRaises(ValueError):
                lf.lock()
                raise ValueError("Test error")
        finally:
            self.assertFalse(lf.locked())

    def test_concurrent_locks(self):
        lf1 = LockFile(LOCKFILE_NAME)
        lf2 = LockFile(LOCKFILE_NAME)
        lf1.lock()
        self.assertTrue(lf1.locked())
        self.assertFalse(lf2.locked())
        lf1.unlock()
        lf2.lock()
        self.assertTrue(lf2.locked())
        self.assertFalse(lf1.locked())
        lf2.unlock()

    def test_mailing_list_lock_release(self):
        """Test that mailing list locks are properly released on disk."""
        # Create a test list if it doesn't exist
        if not Utils.list_exists(TEST_LIST_NAME):
            mlist = MailList.MailList(TEST_LIST_NAME, lock=0)
            mlist.Create(TEST_LIST_NAME, 'test@example.com', 'testpass')
            mlist.Save()
            mlist.Unlock()

        # Get the list's lock file path
        lock_path = os.path.join(mm_cfg.LOCK_DIR, TEST_LIST_NAME + '.lock')
        
        # Clean up any existing lock file
        try:
            os.unlink(lock_path)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise

        # Create and lock the list
        mlist = MailList.MailList(TEST_LIST_NAME, lock=1)
        try:
            # Verify lock file exists
            self.assertTrue(os.path.exists(lock_path), 
                          "Lock file should exist after locking")
            
            # Verify we can't create another instance with lock=1
            with self.assertRaises(AlreadyLockedError):
                mlist2 = MailList.MailList(TEST_LIST_NAME, lock=1)
        finally:
            # Release the lock
            mlist.Unlock()
            
            # Verify lock file is removed
            self.assertFalse(os.path.exists(lock_path),
                           "Lock file should be removed after unlock")
            
            # Verify we can create another instance with lock=1
            mlist2 = MailList.MailList(TEST_LIST_NAME, lock=1)
            mlist2.Unlock()


def suite():
    suite = unittest.TestSuite()
    suite.addTest(unittest.makeSuite(TestLockFile))
    return suite


if __name__ == '__main__':
    unittest.main(defaultTest='suite')
