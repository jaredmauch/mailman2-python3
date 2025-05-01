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
import socket
import traceback

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
        
        # Ensure content is properly encoded and convert to tuple
        content = tuple(
            c.decode('utf-8', 'replace') if isinstance(c, bytes) else c
            for c in content
        )
        
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
        """Load the pending database with improved error handling."""
        filename = os.path.join(mm_cfg.DATA_DIR, 'pending.pck')
        filename_backup = filename + '.bak'

        # Try loading the main file first
        try:
            with open(filename, 'rb') as fp:
                try:
                    data = fp.read()
                    if not data:
                        return {}
                    return pickle.loads(data, fix_imports=True, encoding='latin1')
                except (EOFError, ValueError, TypeError, pickle.UnpicklingError) as e:
                    syslog('error', 'Error loading pending.pck: %s\nTraceback:\n%s', 
                           str(e), traceback.format_exc())

            # If we get here, the main file failed to load properly
            if os.path.exists(filename_backup):
                syslog('info', 'Attempting to load from backup file')
                with open(filename_backup, 'rb') as fp:
                    try:
                        data = fp.read()
                        if not data:
                            return {}
                        db = pickle.loads(data, fix_imports=True, encoding='latin1')
                        # Successfully loaded backup, restore it as main
                        import shutil
                        shutil.copy2(filename_backup, filename)
                        return db
                    except (EOFError, ValueError, TypeError, pickle.UnpicklingError) as e:
                        syslog('error', 'Error loading backup pending.pck: %s\nTraceback:\n%s', 
                               str(e), traceback.format_exc())

        except IOError as e:
            if e.errno != errno.ENOENT:
                syslog('error', 'IOError loading pending.pck: %s\nTraceback:\n%s', 
                       str(e), traceback.format_exc())

        # If we get here, both main and backup files failed or don't exist
        return {}

    def __save(self, db):
        """Save the pending database with atomic operations and backup."""
        if not db:
            return

        filename = os.path.join(mm_cfg.DATA_DIR, 'pending.pck')
        filename_tmp = filename + '.tmp.%s.%d' % (socket.gethostname(), os.getpid())
        filename_backup = filename + '.bak'

        # First create a backup of the current file if it exists
        if os.path.exists(filename):
            try:
                import shutil
                shutil.copy2(filename, filename_backup)
            except IOError as e:
                syslog('error', 'Error creating backup: %s', str(e))

        # Save to temporary file first
        try:
            # Ensure directory exists
            dirname = os.path.dirname(filename)
            if not os.path.exists(dirname):
                os.makedirs(dirname, 0o755)

            with open(filename_tmp, 'wb') as fp:
                # Use protocol 2 for better compatibility
                pickle.dump(db, fp, protocol=2, fix_imports=True)
                fp.flush()
                if hasattr(os, 'fsync'):
                    os.fsync(fp.fileno())

            # Atomic rename
            os.rename(filename_tmp, filename)

        except (IOError, OSError) as e:
            syslog('error', 'Error saving pending.pck: %s', str(e))
            # Try to clean up
            try:
                os.unlink(filename_tmp)
            except OSError:
                pass
            raise

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
