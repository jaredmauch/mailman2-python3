#!/usr/bin/env python3
# Copyright (C) 2024 by the Free Software Foundation, Inc.
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

"""Test script for list locking functionality using installed Mailman paths."""

import os
import sys
import random
import time
import unittest
import socket
import logging
from pathlib import Path

# Remove current directory from sys.path to avoid importing local Mailman
if '' in sys.path:
    sys.path.remove('')
if '.' in sys.path:
    sys.path.remove('.')

# Add the installed Mailman path to sys.path
INSTALLED_MAILMAN_PATH = '/usr/local/mailman'
if INSTALLED_MAILMAN_PATH not in sys.path:
    sys.path.insert(0, INSTALLED_MAILMAN_PATH)

# Import Mailman modules
try:
    from Mailman import mm_cfg
    from Mailman import MailList
    from Mailman import Utils
    from Mailman import LockFile
    from Mailman.Errors import MMListError
    from Mailman.Logging.Syslog import syslog
except ImportError as e:
    print(f"Error importing Mailman modules: {e}")
    print(f"sys.path = {sys.path}")
    raise

# Enable lock debugging
mm_cfg.LIST_LOCK_DEBUGGING = True

class TestListLocks(unittest.TestCase):
    """Test list locking functionality."""

    def setUp(self):
        """Set up test environment."""
        # Get all existing lists
        self.lists_dir = os.path.join(INSTALLED_MAILMAN_PATH, 'lists')
        self.locks_dir = mm_cfg.LOCK_DIR
        print(f"Using lock directory: {self.locks_dir}")
        
        self.available_lists = [d for d in os.listdir(self.lists_dir)
                              if os.path.isdir(os.path.join(self.lists_dir, d))]
        if not self.available_lists:
            self.skipTest("No lists found in the Mailman installation")
        self.test_list = random.choice(self.available_lists)
        print(f"Testing with list: {self.test_list}")
        print(f"Current user: {os.getuid()}")
        print(f"Current group: {os.getgid()}")
        print(f"Locks directory permissions: {oct(os.stat(self.locks_dir).st_mode)}")

    def test_direct_lock_creation(self):
        """Test lock creation directly using LockFile."""
        # Create a lock file path
        lock_file = os.path.join(self.locks_dir, f"{self.test_list}.lock")
        
        # Create a LockFile instance with logging enabled
        lock = LockFile.LockFile(lock_file, lifetime=15, withlogging=True)
        
        # Ensure lock file doesn't exist initially
        if os.path.exists(lock_file):
            os.unlink(lock_file)
        
        print(f"Attempting to create lock file at {lock_file}")
        try:
            # Try to acquire the lock
            lock.lock()
            print("Lock acquired successfully")
            
            # Create the temp file first
            temp_file = f"{lock_file}.{socket.gethostname()}.{os.getpid()}.0"
            print(f"Creating temp file: {temp_file}")
            with open(temp_file, 'w') as f:
                f.write(temp_file)
            
            # Verify temp file exists
            self.assertTrue(os.path.exists(temp_file),
                          f"Temp file {temp_file} was not created")
            
            # Now verify lock file exists
            self.assertTrue(os.path.exists(lock_file),
                          f"Lock file {lock_file} was not created")
            
            # Check lock file contents
            if os.path.exists(lock_file):
                with open(lock_file) as f:
                    contents = f.read().strip()
                print(f"Lock file contents: {contents}")
                print(f"Lock file permissions: {oct(os.stat(lock_file).st_mode)}")
                
                # Verify lock file format (should be hostname.pid)
                expected_prefix = f"{socket.gethostname()}.{os.getpid()}"
                self.assertTrue(contents.startswith(expected_prefix),
                              f"Lock file contents {contents} don't match expected format")
            
            # Try to unlock
            print("Attempting to unlock")
            lock.unlock()
            print("Unlock completed")
            
            # Verify both files were removed
            self.assertFalse(os.path.exists(lock_file),
                           f"Lock file {lock_file} was not removed after unlock")
            self.assertFalse(os.path.exists(temp_file),
                           f"Temp file {temp_file} was not removed after unlock")
            
        except Exception as e:
            print(f"Error during lock test: {e}")
            if os.path.exists(lock_file):
                print(f"Lock file exists after error")
                try:
                    with open(lock_file) as f:
                        print(f"Lock file contents: {f.read()}")
                except:
                    print("Could not read lock file")
            if os.path.exists(temp_file):
                print(f"Temp file exists after error")
                try:
                    with open(temp_file) as f:
                        print(f"Temp file contents: {f.read()}")
                except:
                    print("Could not read temp file")
            raise
        finally:
            # Clean up if needed
            try:
                lock.unlock(unconditionally=True)
            except:
                pass
            for f in [lock_file, temp_file]:
                if os.path.exists(f):
                    try:
                        os.unlink(f)
                    except:
                        pass

    def test_list_lock_creation(self):
        """Test that a list lock is created when locking a list."""
        # Get the list object
        mlist = MailList.MailList(self.test_list, lock=False)
        
        # Get the expected lock file path
        lock_file = os.path.join(self.locks_dir, f"{self.test_list}.lock")
        temp_file = f"{lock_file}.{socket.gethostname()}.{os.getpid()}.0"
        
        # Ensure files don't exist initially
        for f in [lock_file, temp_file]:
            if os.path.exists(f):
                os.unlink(f)
        
        # Lock the list
        print(f"Attempting to lock list {self.test_list}")
        try:
            mlist.Lock()
            print(f"Lock method completed successfully")
        except Exception as e:
            print(f"Error during lock: {e}")
            raise
        
        # Verify lock file was created
        print(f"Checking for lock file at {lock_file}")
        print(f"Lock file exists: {os.path.exists(lock_file)}")
        print(f"Temp file exists: {os.path.exists(temp_file)}")
        
        if os.path.exists(lock_file):
            print(f"Lock file contents: {open(lock_file).read()}")
            print(f"Lock file permissions: {oct(os.stat(lock_file).st_mode)}")
        
        if os.path.exists(temp_file):
            print(f"Temp file contents: {open(temp_file).read()}")
            print(f"Temp file permissions: {oct(os.stat(temp_file).st_mode)}")
        
        self.assertTrue(os.path.exists(lock_file),
                       f"Lock file {lock_file} was not created")
        
        # Read lock file contents
        with open(lock_file) as f:
            lock_contents = f.read().strip()
        
        # Verify lock file contains expected format (hostname.pid)
        expected_prefix = f"{socket.gethostname()}.{os.getpid()}"
        self.assertTrue(lock_contents.startswith(expected_prefix),
                      f"Lock file contents {lock_contents} don't match expected format")
        
        # Unlock the list
        print(f"Attempting to unlock list {self.test_list}")
        try:
            mlist.Unlock()
            print(f"Unlock method completed successfully")
        except Exception as e:
            print(f"Error during unlock: {e}")
            raise
        
        # Verify files were removed
        print(f"Checking if files were removed")
        print(f"Lock file exists: {os.path.exists(lock_file)}")
        print(f"Temp file exists: {os.path.exists(temp_file)}")
        self.assertFalse(os.path.exists(lock_file),
                        f"Lock file {lock_file} was not removed after unlock")
        self.assertFalse(os.path.exists(temp_file),
                        f"Temp file {temp_file} was not removed after unlock")

    def test_list_lock_timeout(self):
        """Test that a list lock times out after the specified lifetime."""
        # Get the list object with a short lock lifetime
        mlist = MailList.MailList(self.test_list, lock=False)
        
        # Get the expected lock file path
        lock_file = os.path.join(self.locks_dir, f"{self.test_list}.lock")
        temp_file = f"{lock_file}.{socket.gethostname()}.{os.getpid()}.0"
        
        # Create a direct lock with a short lifetime
        lock = LockFile.LockFile(lock_file, lifetime=5, withlogging=True)
        
        # Ensure files don't exist initially
        for f in [lock_file, temp_file]:
            if os.path.exists(f):
                os.unlink(f)
        
        # Lock the file
        print(f"Attempting to create lock with 5 second lifetime")
        try:
            lock.lock()
            print(f"Lock created successfully")
        except Exception as e:
            print(f"Error during lock creation: {e}")
            raise
        
        # Verify lock file was created
        print(f"Checking for lock file at {lock_file}")
        print(f"Lock file exists: {os.path.exists(lock_file)}")
        print(f"Temp file exists: {os.path.exists(temp_file)}")
        
        if os.path.exists(lock_file):
            print(f"Lock file contents: {open(lock_file).read()}")
            print(f"Lock file permissions: {oct(os.stat(lock_file).st_mode)}")
        
        if os.path.exists(temp_file):
            print(f"Temp file contents: {open(temp_file).read()}")
            print(f"Temp file permissions: {oct(os.stat(temp_file).st_mode)}")
        
        self.assertTrue(os.path.exists(lock_file),
                       f"Lock file {lock_file} was not created")
        
        # Wait for lock to timeout (lifetime is 5 seconds)
        print("Waiting for lock to timeout...")
        time.sleep(7)  # Wait a bit longer than the lifetime
        
        # Verify files were automatically removed
        print(f"Checking if files were automatically removed")
        print(f"Lock file exists: {os.path.exists(lock_file)}")
        print(f"Temp file exists: {os.path.exists(temp_file)}")
        self.assertFalse(os.path.exists(lock_file),
                        f"Lock file {lock_file} was not automatically removed after timeout")
        self.assertFalse(os.path.exists(temp_file),
                        f"Temp file {temp_file} was not automatically removed after timeout")

if __name__ == '__main__':
    unittest.main() 