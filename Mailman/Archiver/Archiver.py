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


"""Mixin class for putting new messages in the right place for archival.

Public archives are separated from private ones.  An external archival
mechanism (eg, pipermail) should be pointed to the right places, to do the
archival.
"""
from __future__ import absolute_import

from builtins import str
from builtins import object
import os
import errno
import traceback
import re
from io import StringIO

from Mailman import mm_cfg
from Mailman import Mailbox
from Mailman import Utils
from Mailman import Site
from Mailman.SafeDict import SafeDict
from Mailman.Logging.Syslog import syslog
from Mailman.i18n import _


def makelink(old, new):
    try:
        os.symlink(old, new)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

def breaklink(link):
    try:
        os.unlink(link)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise



class Archiver:
    #
    # Interface to Pipermail.  HyperArch.py uses this method to get the
    # archive directory for the mailing list
    #
    def InitVars(self):
        # Configurable
        self.archive = mm_cfg.DEFAULT_ARCHIVE
        # 0=public, 1=private:
        self.archive_private = mm_cfg.DEFAULT_ARCHIVE_PRIVATE
        self.archive_volume_frequency = \
                mm_cfg.DEFAULT_ARCHIVE_VOLUME_FREQUENCY
        # The archive file structure by default is:
        #
        # archives/
        #     private/
        #         listname.mbox/
        #             listname.mbox
        #         listname/
        #             lots-of-pipermail-stuff
        #     public/
        #         listname.mbox@ -> ../private/listname.mbox
        #         listname@ -> ../private/listname
        #
        # IOW, the mbox and pipermail archives are always stored in the
        # private archive for the list.  This is safe because archives/private
        # is always set to o-rx.  Public archives have a symlink to get around
        # the private directory, pointing directly to the private/listname
        # which has o+rx permissions.  Private archives do not have the
        # symbolic links.
        omask = os.umask(0)
        try:
            # Create mbox directory with proper permissions
            mbox_dir = self.archive_dir() + '.mbox'
            os.makedirs(mbox_dir, mode=0o02775, exist_ok=True)
            
            # Create archive directory with proper permissions
            archive_dir = self.archive_dir()
            os.makedirs(archive_dir, mode=0o02775, exist_ok=True)
            
            # See if there's an index.html file there already and if not,
            # write in the empty archive notice.
            indexfile = os.path.join(archive_dir, 'index.html')
            fp = None
            try:
                fp = open(indexfile)
            except IOError as e:
                if e.errno != errno.ENOENT: raise
                omask = os.umask(0o002)
                try:
                    fp = open(indexfile, 'w')
                finally:
                    os.umask(omask)
                fp.write(Utils.maketext(
                    'emptyarchive.html',
                    {'listname': self.real_name,
                     'listinfo': self.GetScriptURL('listinfo', absolute=1),
                     }, mlist=self))
            if fp:
                fp.close()
        finally:
            os.umask(omask)

    def archive_dir(self):
        return Site.get_archpath(self.internal_name())

    def ArchiveFileName(self):
        """The mbox name where messages are left for archive construction."""
        return os.path.join(self.archive_dir() + '.mbox',
                            self.internal_name() + '.mbox')

    def GetBaseArchiveURL(self):
        url = self.GetScriptURL('private', absolute=1) + '/'
        if self.archive_private:
            return url
        else:
            hostname = re.match(r'[^:]*://([^/]*)/.*', url, re.IGNORECASE).group(1)
            url = mm_cfg.PUBLIC_ARCHIVE_URL % {
                'listname': self.internal_name(),
                'hostname': hostname
                }
            if not url.endswith('/'):
                url += '/'
            return url

    def __archive_file(self, afn):
        """Open (creating, if necessary) the named archive file."""
        omask = os.umask(0o002)
        try:
            return Mailbox.Mailbox(open(afn, 'a+'))
        finally:
            os.umask(omask)

    #
    # old ArchiveMail function, retained under a new name
    # for optional archiving to an mbox
    #
    def __archive_to_mbox(self, post):
        """Retain a text copy of the message in an mbox file."""
        try:
            afn = self.ArchiveFileName()
            mbox = self.__archive_file(afn)
            mbox.AppendMessage(post)
            mbox.fp.close()
        except IOError as msg:
            syslog('error', 'Archive file access failure:\n\t%s %s', afn, msg)
            raise

    def ExternalArchive(self, ar, txt):
        d = SafeDict({'listname': self.internal_name(),
                      'hostname': self.host_name,
                      })
        cmd = ar % d
        try:
            with os.popen(cmd, 'w') as extarch:
                extarch.write(txt)
        except OSError as e:
            syslog('error', 'Failed to execute external archiver: %s\nError: %s',
                   cmd, str(e))
            return
        status = extarch.close()
        if status:
            syslog('error', 'External archiver non-zero exit status: %d\nCommand: %s',
                   (status & 0xff00) >> 8, cmd)

    #
    # archiving in real time  this is called from list.post(msg)
    #
    def ArchiveMail(self, msg):
        """Store postings in mbox and/or pipermail archive, depending."""
        # Fork so archival errors won't disrupt normal list delivery
        if mm_cfg.ARCHIVE_TO_MBOX == -1:
            return
        #
        # We don't need an extra archiver lock here because we know the list
        # itself must be locked.
        if mm_cfg.ARCHIVE_TO_MBOX in (1, 2):
            try:
                mbox = self.__archive_file(self.ArchiveFileName())
                mbox.AppendMessage(msg)
                mbox.fp.close()
            except IOError as msg:
                syslog('error', 'Archive file access failure:\n\t%s %s', 
                       self.ArchiveFileName(), msg)
                raise
            if mm_cfg.ARCHIVE_TO_MBOX == 1:
                # Archive to mbox only.
                return
        txt = str(msg)
        # should we use the internal or external archiver?
        private_p = self.archive_private
        if mm_cfg.PUBLIC_EXTERNAL_ARCHIVER and not private_p:
            self.ExternalArchive(mm_cfg.PUBLIC_EXTERNAL_ARCHIVER, txt)
        elif mm_cfg.PRIVATE_EXTERNAL_ARCHIVER and private_p:
            self.ExternalArchive(mm_cfg.PRIVATE_EXTERNAL_ARCHIVER, txt)
        else:
            # use the internal archiver
            with StringIO(txt) as f:
                from . import HyperArch
                h = HyperArch.HyperArchive(self)
                h.processUnixMailbox(f)
                h.close()

    #
    # called from MailList.MailList.Save()
    #
    def CheckHTMLArchiveDir(self):
        # We need to make sure that the archive directory has the right perms
        # for public vs private.  If it doesn't exist, or some weird
        # permissions errors prevent us from stating the directory, it's
        # pointless to try to fix the perms, so we just return -scott
        if mm_cfg.ARCHIVE_TO_MBOX == -1:
            # Archiving is completely disabled, don't require the skeleton.
            return
        pubdir = Site.get_archpath(self.internal_name(), public=True)
        privdir = self.archive_dir()
        pubmbox = pubdir + '.mbox'
        privmbox = privdir + '.mbox'
        if self.archive_private:
            breaklink(pubdir)
            breaklink(pubmbox)
        else:
            # BAW: privdir or privmbox could be nonexistant.  We'd get an
            # OSError, ENOENT which should be caught and reported properly.
            makelink(privdir, pubdir)
            # Only make this link if the site has enabled public mbox files
            if mm_cfg.PUBLIC_MBOX:
                makelink(privmbox, pubmbox)
