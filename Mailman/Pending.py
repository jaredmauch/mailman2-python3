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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301,
# USA.

"""Track pending actions which require confirmation."""

from builtins import str
from builtins import object
import os
import time
import errno
import random
import pickle

from Mailman import mm_cfg
from Mailman import UserDesc
from Mailman.Utils import sha_new

# Types of pending records
SUBSCRIPTION = 'S'
UNSUBSCRIPTION = 'U'
CHANGE_OF_ADDRESS = 'C'
HELD_MESSAGE = 'H'
RE_ENABLE = 'E'
PROBE_BOUNCE = 'P'

_ALLKEYS = (SUBSCRIPTION, UNSUBSCRIPTION,
            CHANGE_OF_ADDRESS, HELD_MESSAGE,
            RE_ENABLE, PROBE_BOUNCE,
            )

_missing = []



class Pending(object):
    def InitTempVars(self):
        self.__pendfile = os.path.join(self.fullpath(), 'pending.pck')

    def pend_new(self, op, *content, **kws):
        """Create a new entry in the pending database, returning cookie for it.
        """
        assert op in _ALLKEYS, 'op: %s' % op
        lifetime = kws.get('lifetime', mm_cfg.PENDING_REQUEST_LIFE)
        # We try the main loop several times. If we get a lock error somewhere
        # (for instance because someone broke the lock) we simply try again.
        assert self.Locked()
        # Load the database
        db = self.__load()
        
        # Ensure content is properly encoded
        content = [
            c.decode('utf-8', 'replace') if isinstance(c, bytes) else c
            for c in content
        ]
        
        # Calculate a unique cookie.  Algorithm vetted by the Timbot.  time()
        # has high resolution on Linux, clock() on Windows.  random gives us
        # about 45 bits in Python 2.2, 53 bits on Python 2.3.  The time and
        # clock values basically help obscure the random number generator, as
        # does the hash calculation.  The integral parts of the time values
        # are discarded because they're the most predictable bits.
        while True:
            now = time.time()
            x = random.random() + now % 1.0
            cookie = sha_new(repr(x).encode()).hexdigest()
            # We'll never get a duplicate, but we'll be anal about checking
            # anyway.
            if cookie not in db:
                break
        # Store the content, plus the time in the future when this entry will
        # be evicted from the database, due to staleness.
        db[cookie] = (op,) + content
        evictions = db.setdefault('evictions', {})
        evictions[cookie] = now + lifetime
        self.__save(db)
        return cookie

    def __load(self):
        """Load the pending database with improved error handling and validation."""
        backup_file = self.__pendfile + '.bak'
        try:
            # Try loading the main file first
            return self.__load_file(self.__pendfile)
        except (pickle.UnpicklingError, EOFError, ValueError) as e:
            # If main file is corrupt, try the backup
            try:
                if os.path.exists(backup_file):
                    return self.__load_file(backup_file)
            except Exception:
                pass
            # If both files are corrupt, start fresh
            return {'evictions': {}, 'version': mm_cfg.PENDING_FILE_SCHEMA_VERSION}

    def __load_file(self, filename):
        """Load and validate a specific pending database file."""
        try:
            with open(filename, 'rb') as fp:
                db = pickle.load(fp, fix_imports=True, encoding='latin1')
                
                # Validate the loaded data
                if not isinstance(db, dict):
                    raise ValueError("Loaded data is not a dictionary")
                
                # Check version
                if 'version' not in db:
                    db['version'] = mm_cfg.PENDING_FILE_SCHEMA_VERSION
                elif db['version'] != mm_cfg.PENDING_FILE_SCHEMA_VERSION:
                    # Handle version mismatch - could add migration logic here
                    db['version'] = mm_cfg.PENDING_FILE_SCHEMA_VERSION
                
                # Ensure evictions dict exists
                if 'evictions' not in db:
                    db['evictions'] = {}
                
                # Convert any bytes to strings
                new_db = {}
                for key, value in db.items():
                    if isinstance(key, bytes):
                        key = key.decode('utf-8', 'replace')
                    if isinstance(value, bytes):
                        value = value.decode('utf-8', 'replace')
                    elif isinstance(value, (list, tuple)):
                        value = list(value)  # Convert tuple to list for modification
                        for i, v in enumerate(value):
                            if isinstance(v, bytes):
                                value[i] = v.decode('utf-8', 'replace')
                    new_db[key] = value
                
                # Validate all entries have corresponding eviction times
                for key in list(new_db.keys()):
                    if key not in ('evictions', 'version'):
                        if key not in new_db['evictions']:
                            new_db['evictions'][key] = time.time() + mm_cfg.PENDING_REQUEST_LIFE
                
                return new_db
        except IOError as e:
            if e.errno != errno.ENOENT:
                raise
            return {'evictions': {}, 'version': mm_cfg.PENDING_FILE_SCHEMA_VERSION}

    def __save(self, db):
        """Save the pending database with atomic operations and backup."""
        # Clean up stale entries first
        self.__cleanup_stale_entries(db)
        
        # Create backup of current file if it exists
        if os.path.exists(self.__pendfile):
            try:
                import shutil
                shutil.copy2(self.__pendfile, self.__pendfile + '.bak')
            except IOError:
                pass  # Best effort backup
        
        # Save to temporary file first
        tmpfile = '%s.tmp.%d.%d' % (self.__pendfile, os.getpid(), int(time.time()))
        omask = os.umask(0o007)
        try:
            # Ensure the directory exists
            dirname = os.path.dirname(self.__pendfile)
            if not os.path.exists(dirname):
                os.makedirs(dirname, 0o755)
            
            with open(tmpfile, 'wb') as fp:
                pickle.dump(db, fp, protocol=2)  # Use protocol 2 for better compatibility
                fp.flush()
                os.fsync(fp.fileno())
            
            # Atomic rename
            os.rename(tmpfile, self.__pendfile)
            
        except Exception as e:
            # Clean up temp file if something went wrong
            try:
                os.unlink(tmpfile)
            except OSError:
                pass
            raise e
        finally:
            os.umask(omask)

    def __cleanup_stale_entries(self, db):
        """Clean up stale entries from the database."""
        evictions = db['evictions']
        now = time.time()
        
        # Remove stale entries
        for cookie, data in list(db.items()):
            if cookie in ('evictions', 'version'):
                continue
            timestamp = evictions.get(cookie)
            if timestamp is None or now > timestamp:
                del db[cookie]
                if cookie in evictions:
                    del evictions[cookie]
        
        # Clean up orphaned eviction entries
        for cookie in list(evictions.keys()):
            if cookie not in db:
                del evictions[cookie]
        
        # Ensure version is set
        db['version'] = mm_cfg.PENDING_FILE_SCHEMA_VERSION

    def pend_confirm(self, cookie, expunge=True):
        """Return data for cookie, or None if not found.

        If optional expunge is True (the default), the record is also removed
        from the database.
        """
        db = self.__load()
        # If we're not expunging, the database is read-only.
        if not expunge:
            return db.get(cookie)
        # Since we're going to modify the database, we must make sure the list
        # is locked, since it's the list lock that protects pending.pck.
        assert self.Locked()
        content = db.get(cookie, _missing)
        if content is _missing:
            return None
        # Do the expunge
        del db[cookie]
        del db['evictions'][cookie]
        self.__save(db)
        return content

    def pend_repend(self, cookie, data, lifetime=mm_cfg.PENDING_REQUEST_LIFE):
        assert self.Locked()
        db = self.__load()
        db[cookie] = data
        db['evictions'][cookie] = time.time() + lifetime
        self.__save(db)



def _update(olddb):
    db = {}
    # We don't need this entry anymore
    if 'lastculltime' in olddb:
        del olddb['lastculltime']
    evictions = db.setdefault('evictions', {})
    for cookie, data in list(olddb.items()):
        # The cookies used to be kept as a 6 digit integer.  We now keep the
        # cookies as a string (sha in our case, but it doesn't matter for
        # cookie matching).
        cookie = str(cookie)
        # The old format kept the content as a tuple and tacked the timestamp
        # on as the last element of the tuple.  We keep the timestamps
        # separate, but require the prepending of a record type indicator.  We
        # know that the only things that were kept in the old format were
        # subscription requests.  Also, the old request format didn't have the
        # subscription language.  Best we can do here is use the server
        # default.  We also need a fullname because confirmation processing
        # references all those UserDesc attributes.
        ud = UserDesc.UserDesc(address=data[0],
                               fullname='',
                               password=data[1],
                               digest=data[2],
                               lang=mm_cfg.DEFAULT_SERVER_LANGUAGE,
                               )
        db[cookie] = (SUBSCRIPTION, ud)
        # The old database format kept the timestamp as the time the request
        # was made.  The new format keeps it as the time the request should be
        # evicted.
        evictions[cookie] = data[-1] + mm_cfg.PENDING_REQUEST_LIFE
    return db
