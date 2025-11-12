# Copyright (C) 2000-2018 by the Free Software Foundation, Inc.
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

"""Archive queue runner."""

import time
from email.utils import parsedate_tz, mktime_tz, formatdate

from Mailman import i18n
from Mailman import mm_cfg
from Mailman import LockFile
from Mailman.Queue.Runner import Runner



class ArchRunner(Runner):
    QDIR = mm_cfg.ARCHQUEUE_DIR

    def _dispose(self, mlist, msg, msgdata):
        from Mailman.Logging.Syslog import syslog
        syslog('debug', 'ArchRunner: Starting archive processing for list %s', mlist.internal_name())
        
        # Support clobber_date, i.e. setting the date in the archive to the
        # received date, not the (potentially bogus) Date: header of the
        # original message.
        clobber = 0
        originaldate = msg.get('date')
        
        # Handle potential bytes/string issues with header values
        if isinstance(originaldate, bytes):
            try:
                originaldate = originaldate.decode('utf-8', 'replace')
            except (UnicodeDecodeError, AttributeError):
                originaldate = None
        
        receivedtime = formatdate(msgdata['received_time'])
        syslog('debug', 'ArchRunner: Original date: %s, Received time: %s', originaldate, receivedtime)
        
        if not originaldate:
            clobber = 1
            syslog('debug', 'ArchRunner: No original date, will clobber')
        elif mm_cfg.ARCHIVER_CLOBBER_DATE_POLICY == 1:
            clobber = 1
            syslog('debug', 'ArchRunner: ARCHIVER_CLOBBER_DATE_POLICY = 1, will clobber')
        elif mm_cfg.ARCHIVER_CLOBBER_DATE_POLICY == 2:
            # what's the timestamp on the original message?
            try:
                tup = parsedate_tz(originaldate)
                now = time.time()
                if not tup:
                    clobber = 1
                    syslog('debug', 'ArchRunner: Could not parse original date, will clobber')
                elif abs(now - mktime_tz(tup)) > \
                         mm_cfg.ARCHIVER_ALLOWABLE_SANE_DATE_SKEW:
                    clobber = 1
                    syslog('debug', 'ArchRunner: Date skew too large, will clobber')
            except (ValueError, OverflowError, TypeError):
                # The likely cause of this is that the year in the Date: field
                # is horribly incorrect, e.g. (from SF bug # 571634):
                # Date: Tue, 18 Jun 0102 05:12:09 +0500
                # Obviously clobber such dates.
                clobber = 1
                syslog('debug', 'ArchRunner: Date parsing exception, will clobber')
        
        if clobber:
            # Use proper header manipulation methods
            if 'date' in msg:
                del msg['date']
            if 'x-original-date' in msg:
                del msg['x-original-date']
            msg['Date'] = receivedtime
            if originaldate:
                msg['X-Original-Date'] = originaldate
            syslog('debug', 'ArchRunner: Clobbered date headers')
        
        # Always put an indication of when we received the message.
        msg['X-List-Received-Date'] = receivedtime
        
        # Now try to get the list lock
        syslog('debug', 'ArchRunner: Attempting to lock list %s', mlist.internal_name())
        try:
            mlist.Lock(timeout=mm_cfg.LIST_LOCK_TIMEOUT)
            syslog('debug', 'ArchRunner: Successfully locked list %s', mlist.internal_name())
        except LockFile.TimeOutError:
            # oh well, try again later
            syslog('debug', 'ArchRunner: Failed to lock list %s, will retry later', mlist.internal_name())
            return 1
        
        try:
            # Archiving should be done in the list's preferred language, not
            # the sender's language.
            i18n.set_language(mlist.preferred_language)
            syslog('debug', 'ArchRunner: Calling ArchiveMail for list %s', mlist.internal_name())
            mlist.ArchiveMail(msg)
            syslog('debug', 'ArchRunner: ArchiveMail completed, saving list %s', mlist.internal_name())
            mlist.Save()
            syslog('debug', 'ArchRunner: Successfully completed archive processing for list %s', mlist.internal_name())
        except Exception as e:
            syslog('error', 'ArchRunner: Exception during archive processing for list %s: %s', mlist.internal_name(), e)
            raise
        finally:
            mlist.Unlock()
            syslog('debug', 'ArchRunner: Unlocked list %s', mlist.internal_name())
