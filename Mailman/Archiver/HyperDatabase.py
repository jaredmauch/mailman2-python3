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

#
# site modules
#
import os
import marshal
import time
import errno

#
# package/project modules
#
import pipermail
from Mailman import LockFile

CACHESIZE = pipermail.CACHESIZE

import pickle

#
# we're using a python dict in place of
# of bsddb.btree database.  only defining
# the parts of the interface used by class HyperDatabase
# only one thing can access this at a time.
#
class DumbBTree:
    """Stores pickles of Article objects.
    
    A simple implementation of a BTree database using a dictionary and
    maintaining a sorted list of keys.
    """
    
    def __init__(self, path: str) -> None:
        """Initialize the BTree.
        
        Args:
            path: Path to the database file
        """
        self.path = path
        self.dict: Dict[str, Any] = {}
        self.sorted: List[str] = []
        self.current_index = 0
        self.load()

    def __sort(self) -> None:
        """Sort the keys list."""
        self.sorted = sorted(self.dict.keys())

    def first(self) -> Tuple[str, Any]:
        """Get the first item.
        
        Returns:
            A tuple of (key, value) for the first item
            
        Raises:
            KeyError: If the database is empty
        """
        if not self.sorted:
            self.__sort()
        if not self.sorted:
            raise KeyError
        self.current_index = 0
        key = self.sorted[0]
        return key, self.dict[key]

    def last(self) -> Tuple[str, Any]:
        """Get the last item.
        
        Returns:
            A tuple of (key, value) for the last item
            
        Raises:
            KeyError: If the database is empty
        """
        if not self.sorted:
            self.__sort()
        if not self.sorted:
            raise KeyError
        self.current_index = len(self.sorted) - 1
        key = self.sorted[-1]
        return key, self.dict[key]

    def next(self) -> Tuple[str, Any]:
        """Get the next item.
        
        Returns:
            A tuple of (key, value) for the next item
            
        Raises:
            KeyError: If at the end of the database
        """
        if self.current_index >= len(self.sorted):
            raise KeyError
        self.current_index += 1
        key = self.sorted[self.current_index]
        return key, self.dict[key]

    def __contains__(self, key: str) -> bool:
        """Check if a key exists in the database.
        
        Args:
            key: The key to check
            
        Returns:
            True if the key exists, False otherwise
        """
        return key in self.dict

    def set_location(self, loc: str) -> None:
        """Set the current location in the database.
        
        Args:
            loc: The key to set as current location
        """
        self.current_index = self.sorted.index(loc)

    def __getitem__(self, item: str) -> Any:
        """Get an item from the database.
        
        Args:
            item: The key to look up
            
        Returns:
            The value associated with the key
        """
        return self.dict[item]

    def __setitem__(self, item: str, val: Any) -> None:
        """Set an item in the database.
        
        Args:
            item: The key to set
            val: The value to set
        """
        if self.current_index == 0:
            self.dict[item] = val
            return
        self.__sort()
        self.dict[item] = val

    def __len__(self) -> int:
        """Get the number of items in the database.
        
        Returns:
            The number of items
        """
        return len(self.dict)

    def load(self) -> None:
        """Load the database from disk."""
        try:
            with open(self.path, 'rb') as f:
                self.dict = pickle.load(f)
        except (IOError, EOFError):
            self.dict = {}

    def close(self) -> None:
        """Save the database to disk."""
        try:
            with open(self.path, 'wb') as f:
                pickle.dump(self.dict, f)
        except IOError:
            pass

# this is lifted straight out of pipermail with
# the bsddb.btree replaced with above class.
# didn't use inheritance because of all the
# __internal stuff that needs to be here -scott
#
class HyperDatabase(pipermail.Database):
    __super_addArticle = pipermail.Database.addArticle

    def __init__(self, basedir, mlist):
        self.__cache = {}
        self.__currentOpenArchive = None   # The currently open indices
        self.basedir = basedir
        self.mlist = mlist
        # Recently added articles, indexed only by message ID
        self.changed={}
        self.dateIndex = None
        self.subjectIndex = None
        self.authorIndex = None
        self.threadIndex = None

    def firstdate(self, archive):
        self.__openIndices(archive)
        date = 'None'
        try:
            date = self.dateIndex.first()[0]
        except KeyError:
            pass
        return date

    def lastdate(self, archive):
        self.__openIndices(archive)
        date = 'None'
        try:
            date = self.dateIndex.last()[0]
        except KeyError:
            pass
        return date

    def numArticles(self, archive):
        self.__openIndices(archive)
        return len(self.dateIndex)

    def addArticle(self, archive, article, subject=None, author=None,
                   date=None):
        self.__openIndices(archive)
        if subject is None:
            subject = article.subject
        if author is None:
            author = article.author
        if date is None:
            date = article.date
        self.dateIndex[date] = article.msgid
        self.subjectIndex[subject.lower()] = article.msgid
        self.authorIndex[author.lower()] = article.msgid
        if article.threadKey:
            self.threadIndex[article.threadKey] = article.msgid

    def __openIndices(self, archive):
        if self.__currentOpenArchive == archive:
            return
        self.__closeIndices()
        arcdir = os.path.join(self.basedir, 'database')
        omask = os.umask(0o007)
        try:
            try:
                os.mkdir(arcdir, 0o2770)
            except OSError as e:
                if e.errno != errno.EEXIST: raise
            for i in ('date', 'author', 'subject', 'article', 'thread'):
                t = DumbBTree(os.path.join(arcdir, archive + '-' + i))
                setattr(self, i + 'Index', t)
        finally:
            os.umask(omask)
        self.__currentOpenArchive = archive

    def __closeIndices(self):
        for i in ('date', 'author', 'subject', 'thread', 'article'):
            attr = i + 'Index'
            if hasattr(self, attr):
                index = getattr(self, attr)
                if i == 'article':
                    if not hasattr(self, 'archive_length'):
                        self.archive_length = {}
                    l = len(index)
                    self.archive_length[self.__currentOpenArchive] = l
                index.close()
                delattr(self, attr)
        self.__currentOpenArchive = None

    def close(self):
        self.__closeIndices()

    def hasArticle(self, archive, msgid):
        self.__openIndices(archive)
        return msgid in self.dateIndex

    def setThreadKey(self, archive, key, msgid):
        self.__openIndices(archive)
        self.threadIndex[key] = msgid

    def getArticle(self, archive, msgid):
        self.__openIndices(archive)
        if not self.__cache.has_key(msgid):
            # get the pickled object out of the DumbBTree
            buf = self.dateIndex[msgid]
            article = self.__cache[msgid] = pickle.loads(buf)
            # For upgrading older archives
            article.setListIfUnset(self.mlist)
        else:
            article = self.__cache[msgid]
        return article

    def first(self, archive, index):
        self.__openIndices(archive)
        index = getattr(self, index + 'Index')
        try:
            return index.first()
        except KeyError:
            return None

    def next(self, archive, index):
        self.__openIndices(archive)
        index = getattr(self, index + 'Index')
        try:
            return index.next()
        except KeyError:
            return None

    def getOldestArticle(self, archive, subject):
        self.__openIndices(archive)
        subject = subject.lower()
        try:
            msgid = self.subjectIndex[subject]
            return self.dateIndex[msgid]
        except KeyError:
            return None

    def newArchive(self, archive):
        pass

    def clearIndex(self, archive, index):
        self.__openIndices(archive)
        index = getattr(self, index + 'Index')
        index.clear()
