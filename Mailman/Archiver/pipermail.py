#! /usr/bin/python3

import errno
import mailbox
import os
import re
import sys
import time
from email.utils import parseaddr, parsedate_tz, mktime_tz, formatdate
import pickle
from io import StringIO
from string import ascii_lowercase as lowercase

__version__ = '0.09 (Mailman edition)'
VERSION = __version__
CACHESIZE = 100    # Number of slots in the cache

from Mailman import mm_cfg
from Mailman import Errors
from Mailman import Utils
from Mailman.Mailbox import ArchiverMailbox
from Mailman.Logging.Syslog import syslog
from Mailman.i18n import _, C_

SPACE = ' '



msgid_pat = re.compile(r'(<.*>)')
def strip_separators(s):
    "Remove quotes or parenthesization from a Message-ID string"
    if not s:
        return ""
    if s[0] in '"<([' and s[-1] in '">)]':
        s = s[1:-1]
    return s

smallNameParts = ['van', 'von', 'der', 'de']

def fixAuthor(author):
    "Canonicalize a name into Last, First format"
    # If there's a comma, guess that it's already in "Last, First" format
    if ',' in author:
        return author
    L = author.split()
    i = len(L) - 1
    if i == 0:
        return author # The string's one word--forget it
    if author.upper() == author or author.lower() == author:
        # Damn, the name is all upper- or lower-case.
        while i > 0 and L[i-1].lower() in smallNameParts:
            i = i - 1
    else:
        # Mixed case; assume that small parts of the last name will be
        # in lowercase, and check them against the list.
        while i>0 and (L[i-1][0] in lowercase or
                       L[i-1].lower() in smallNameParts):
            i = i - 1
    author = SPACE.join(L[-1:] + L[i:-1]) + ', ' + SPACE.join(L[:i])
    return author

# Abstract class for databases

class DatabaseInterface(object):
    def __init__(self): pass
    def close(self): pass
    def getArticle(self, archive, msgid): pass
    def hasArticle(self, archive, msgid): pass
    def addArticle(self, archive, article, subject=None, author=None,
                   date=None): pass
    def firstdate(self, archive): pass
    def lastdate(self, archive): pass
    def first(self, archive, index): pass
    def next(self, archive, index): pass
    def numArticles(self, archive): pass
    def newArchive(self, archive): pass
    def setThreadKey(self, archive, key, msgid): pass
    def getOldestArticle(self, subject): pass

class Database(DatabaseInterface):
    """Define the basic sorting logic for a database

    Assumes that the database internally uses dateIndex, authorIndex,
    etc.
    """

    # TBD Factor out more of the logic shared between BSDDBDatabase
    # and HyperDatabase and place it in this class.

    def __init__(self):
        # This method need not be called by subclasses that do their
        # own initialization.
        self.dateIndex = {}
        self.authorIndex = {}
        self.subjectIndex = {}
        self.articleIndex = {}
        self.changed = {}

    def addArticle(self, archive, article, subject=None, author=None,
                   date=None):
        # create the keys; always end w/ msgid which will be unique
        authorkey = (author or article.author, article.date,
                     article.msgid)
        subjectkey = (subject or article.subject, article.date,
                      article.msgid)
        datekey = date or article.date, article.msgid

        # Add the new article
        self.dateIndex[datekey] = article.msgid
        self.authorIndex[authorkey] = article.msgid
        self.subjectIndex[subjectkey] = article.msgid

        self.store_article(article)
        self.changed[archive, article.msgid] = None

        parentID = article.parentID
        if parentID is not None and parentID in self.articleIndex:
            parent = self.getArticle(archive, parentID)
            myThreadKey = (parent.threadKey + article.date + '.'
                           + str(article.sequence) + '-')
        else:
            myThreadKey = article.date + '.' + str(article.sequence) + '-'
        article.threadKey = myThreadKey
        key = myThreadKey, article.msgid
        self.setThreadKey(archive, key, article.msgid)

    def store_article(self, article):
        """Store article without message body to save space"""
        # TBD this is not thread safe!
        temp = article.body
        temp2 = article.html_body
        article.body = []
        del article.html_body
        self.articleIndex[article.msgid] = pickle.dumps(article)
        article.body = temp
        article.html_body = temp2


# The Article class encapsulates a single posting.  The attributes
# are:
#
# sequence   : Sequence number, unique for each article in a set of archives
# subject    : Subject
# datestr    : The posting date, in human-readable format
# date       : The posting date, in purely numeric format
# headers    : Any other headers of interest
# author     : The author's name (and possibly organization)
# email      : The author's e-mail address
# msgid      : A unique message ID
# in_reply_to: If != "", this is the msgid of the article being replied to
# references : A (possibly empty) list of msgid's of earlier articles
#              in the thread
# body       : A list of strings making up the message body

class Article(object):
    _last_article_time = time.time()

    def __init__(self, message = None, sequence = 0, keepHeaders = []):
        if message is None:
            return
        self.sequence = sequence

        self.parentID = None
        self.threadKey = None
        # otherwise the current sequence number is used.
        id = strip_separators(message['Message-Id'])
        if id == "":
            self.msgid = str(self.sequence)
        else: self.msgid = id

        if 'Subject' in message:
            self.subject = str(message['Subject'])
        else:
            self.subject = _('No subject')
        if self.subject == "": self.subject = _('No subject')

        self._set_date(message)

        # Figure out the e-mail address and poster's name.  Use the From:
        # field first, followed by Reply-To:
        self.author, self.email = parseaddr(message.get('From', ''))
        e = message['Reply-To']
        if not self.email and e is not None:
            ignoreauthor, self.email = parseaddr(e)
        self.email = strip_separators(self.email)
        self.author = strip_separators(self.author)

        if self.author == "":
            self.author = self.email

        # Save the In-Reply-To:, References:, and Message-ID: lines
        #
        # TBD: The original code does some munging on these fields, which
        # shouldn't be necessary, but changing this may break code.  For
        # safety, I save the original headers on different attributes for use
        # in writing the plain text periodic flat files.
        self._in_reply_to = message['in-reply-to']
        self._references = message['references']
        self._message_id = message['message-id']

        i_r_t = message['In-Reply-To']
        if i_r_t is None:
            self.in_reply_to = ''
        else:
            match = msgid_pat.search(i_r_t)
            if match is None: self.in_reply_to = ''
            else: self.in_reply_to = strip_separators(match.group(1))

        references = message['References']
        if references is None:
            self.references = []
        else:
            self.references = list(map(strip_separators, references.split()))

        # Save any other interesting headers
        self.headers = {}
        for i in keepHeaders:
            if i in message:
                self.headers[i] = message[i]

        # Read the message body
        msg = message.get_payload()\
                     or message.as_string().split('\n\n',1)[1]
        s = StringIO(msg)
        self.body = s.readlines()

    def _set_date(self, message):
        def floatdate(datestr):
            if not datestr:
                return None
            date = parsedate_tz(datestr)
            try:
                date = mktime_tz(date)
                if (date < 0 or
                    date - time.time() >
                        mm_cfg.ARCHIVER_ALLOWABLE_SANE_DATE_SKEW
                   ):
                    return None
                return date
            except (TypeError, ValueError, OverflowError):
                return None
        date = floatdate(message.get('date'))
        if date is None:
            date = floatdate(message.get('x-list-received-date'))
        if date is None:
            rec_re = re.compile(r'^.*;\s*', re.DOTALL)
            date = floatdate(rec_re.sub('', message.get('received', '')))
        if date is None:
            date = floatdate(re.sub(r'From \s*\S+\s+', '',
                                    message.get_unixfrom() or '' ))
        if date is None:
            date = self._last_article_time + 1
        self._last_article_time = date
        self.date = '%011i' % date
        self.datestr = message.get('date') \
                       or message.get('x-list-received-date') \
                       or formatdate(date)

    def __repr__(self):
        return '<Article ID = '+repr(self.msgid)+'>'

    def finished_update_article(self):
        pass    

# Pipermail formatter class

class T(object):
    DIRMODE = 0o0755      # Mode to give to created directories
    FILEMODE = 0o0644     # Mode to give to created files
    INDEX_EXT = ".html" # Extension for indexes

    def __init__(self, basedir = None, reload = 1, database = None):
        # If basedir isn't provided, assume the current directory
        if basedir is None:
            self.basedir = os.getcwd()
        else:
            basedir = os.path.expanduser(basedir)
            self.basedir = basedir
        self.database = database

        # If the directory doesn't exist, create it.  This code shouldn't get
        # run anymore, we create the directory in Archiver.py.  It should only
        # get used by legacy lists created that are only receiving their first
        # message in the HTML archive now -- Marc
        try:
            os.stat(self.basedir)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise
            else:
                self.message(C_('Creating archive directory ') + self.basedir)
                omask = os.umask(0)
                try:
                    os.mkdir(self.basedir, self.DIRMODE)
                finally:
                    os.umask(omask)

        # Try to load previously pickled state
        try:
            if not reload:
                raise IOError
            d = Utils.load_pickle(os.path.join(self.basedir, 'pipermail.pck'))
            if not d:
                raise IOError("Pickled data is empty or None")
            self.message(C_('Reloading pickled archive state'))
            for key, value in list(d.items()):
                setattr(self, key, value)
        except (IOError, EOFError):
            # No pickled version, so initialize various attributes
            self.archives = []        # Archives
            self._dirty_archives = [] # Archives that will have to be updated
            self.sequence = 0         # Sequence variable used for
                                      #   numbering articles
            self.update_TOC = 0       # Does the TOC need updating?
        #
        # make the basedir variable work when passed in as an __init__ arg
        # and different from the one in the pickle.  Let the one passed in
        # as an __init__ arg take precedence if it's stated.  This way, an
        # archive can be moved from one place to another and still work.
        #
        if basedir != self.basedir:
            self.basedir = basedir

    def close(self):
        "Close an archive, save its state, and update any changed archives."
        self.update_dirty_archives()
        self.update_TOC = 0
        self.write_TOC()
        # Save the collective state
        self.message(C_('Pickling archive state into ')
                     + os.path.join(self.basedir, 'pipermail.pck'))
        self.database.close()
        del self.database

        omask = os.umask(0o007)
        try:
            f = open(os.path.join(self.basedir, 'pipermail.pck'), 'wb')
        finally:
            os.umask(omask)
        pickle.dump(self.getstate(), f)
        f.close()

    def getstate(self):
        # can override this in subclass
        return self.__dict__

    #
    # Private methods
    #
    # These will be neither overridden nor called by custom archivers.
    #


    # Create a dictionary of various parameters that will be passed
    # to the write_index_{header,footer} functions
    def __set_parameters(self, archive):
        # Determine the earliest and latest date in the archive
        firstdate = self.database.firstdate(archive)
        lastdate = self.database.lastdate(archive)

        # Get the current time
        now = time.asctime(time.localtime(time.time()))
        self.firstdate = firstdate
        self.lastdate = lastdate
        self.archivedate = now
        self.size = self.database.numArticles(archive)
        self.archive = archive
        self.version = __version__

    # Find the message ID of an article's parent, or return None
    # if no parent can be found.

    def __findParent(self, article, children = []):
            parentID = None
            if article.in_reply_to:
                parentID = article.in_reply_to
            elif article.references:
                # Remove article IDs that aren't in the archive
                refs = list(filter(lambda x: x in self.articleIndex, article.references))
                if not refs:
                    return None
                maxdate = self.database.getArticle(self.archive,
                                                   refs[0])
                for ref in refs[1:]:
                    a = self.database.getArticle(self.archive, ref)
                    if a.date > maxdate.date:
                        maxdate = a
                parentID = maxdate.msgid
            else:
                # Look for the oldest matching subject
                try:
                    key, tempid = \
                         self.subjectIndex.set_location(article.subject)
                    print((key, tempid))
                    next(self.subjectIndex)
                    [subject, date] = key.split('\0')
                    print((article.subject, subject, date))
                    if subject == article.subject and tempid not in children:
                        parentID = tempid
                except KeyError:
                    pass
            return parentID

    # Update the threaded index completely
    def updateThreadedIndex(self):
        # Erase the threaded index
        self.database.clearIndex(self.archive, 'thread')

        # Loop over all the articles
        msgid = self.database.first(self.archive, 'date')
        while msgid is not None:
            try:
                article = self.database.getArticle(self.archive, msgid)
            except KeyError:
                pass
            else:
                if article.parentID is None or \
                   not self.database.hasArticle(self.archive,
                                                article.parentID):
                    # then
                    pass
                else:
                    parent = self.database.getArticle(self.archive,
                                                    article.parentID)
                    article.threadKey = (parent.threadKey + article.date + '.'
                                         + str(article.sequence) + '-')
                self.database.setThreadKey(self.archive,
                    (article.threadKey, article.msgid),
                    msgid)
            msgid = self.database.next(self.archive, 'date')

    #
    # Public methods:
    #
    # These are part of the public interface of the T class, but will
    # never be overridden (unless you're trying to do something very new).

    # Update a single archive's indices, whether the archive's been
    # dirtied or not.
    def update_archive(self, archive):
        self.archive = archive
        self.message(C_("Updating index files for archive [%(archive)s]"))
        arcdir = os.path.join(self.basedir, archive)
        self.__set_parameters(archive)

        for hdr in ('Date', 'Subject', 'Author'):
            self._update_simple_index(hdr, archive, arcdir)

        self._update_thread_index(archive, arcdir)

    def _update_simple_index(self, hdr, archive, arcdir):
        self.message("  " + hdr)
        self.type = hdr
        hdr = hdr.lower()

        self._open_index_file_as_stdout(arcdir, hdr)
        self.write_index_header()
        count = 0
        # Loop over the index entries
        msgid = self.database.first(archive, hdr)
        while msgid is not None:
            try:
                article = self.database.getArticle(self.archive, msgid)
            except KeyError:
                pass
            else:
                count = count + 1
                self.write_index_entry(article)
            msgid = self.database.next(archive, hdr)
        # Finish up this index
        self.write_index_footer()
        self._restore_stdout()

    def _update_thread_index(self, archive, arcdir):
        self.message(C_("  Thread"))
        self._open_index_file_as_stdout(arcdir, "thread")
        self.type = 'Thread'
        self.write_index_header()

        # To handle the prev./next in thread pointers, we need to
        # track articles 5 at a time.

        # Get the first 5 articles
        L = [None] * 5
        i = 2
        msgid = self.database.first(self.archive, 'thread')

        while msgid is not None and i < 5:
            L[i] = self.database.getArticle(self.archive, msgid)
            i = i + 1
            msgid = self.database.next(self.archive, 'thread')

        while L[2] is not None:
            article = L[2]
            artkey = None
            if article is not None:
                artkey = article.threadKey
            if artkey is not None:
                self.write_threadindex_entry(article, artkey.count('-') - 1)
                if (archive,article.msgid) in self.database.changed:
                    a1 = L[1]
                    a3 = L[3]
                    self.update_article(arcdir, article, a1, a3)
                    if a3 is not None:
                        self.database.changed[(archive, a3.msgid)] = None
                    if a1 is not None:
                        key = archive, a1.msgid
                        if key not in self.database.changed:
                            self.update_article(arcdir, a1, L[0], L[2])
                        else:
                            del self.database.changed[key]
            if L[0]:
                L[0].finished_update_article()
            L = L[1:]                   # Rotate the list
            if msgid is None:
                L.append(msgid)
            else:
                L.append(self.database.getArticle(self.archive, msgid))
            msgid = self.database.next(self.archive, 'thread')

        self.write_index_footer()
        self._restore_stdout()

    def _open_index_file_as_stdout(self, arcdir, index_name):
        path = os.path.join(arcdir, index_name + self.INDEX_EXT)
        omask = os.umask(0o002)
        try:
            self.__f = open(path, 'w', encoding='utf-8')
        finally:
            os.umask(omask)
        self.__stdout = sys.stdout
        sys.stdout = self.__f

    def _restore_stdout(self):
        sys.stdout = self.__stdout
        self.__f.close()
        del self.__f
        del self.__stdout

    # Update only archives that have been marked as "changed".
    def update_dirty_archives(self):
        for i in self._dirty_archives:
            self.update_archive(i)
        self._dirty_archives = []

    # Read a Unix mailbox file from the file object <input>,
    # and create a series of Article objects.  Each article
    # object will then be archived.

    def _makeArticle(self, msg, sequence):
        return Article(msg, sequence)

    def processUnixMailbox(self, input, start=None, end=None):
        mbox = ArchiverMailbox(input.name, self.maillist)
        mbox_iterator = iter(mbox.values())
        if start is None:
            start = 0
        counter = 0
        if start:
            mbox.skipping(True)
        while counter < start:
            try:
                m = next(mbox_iterator, None)
            except Errors.DiscardMessage:
                continue
            if m is None:
                return
            counter += 1
        if start:
            mbox.skipping(False)
        while 1:
            try:
                pos = input.tell()
                m = next(mbox_iterator, None)
            except Errors.DiscardMessage:
                continue
            except Exception:
                syslog('error', 'uncaught archiver exception at filepos: %s',
                       pos)
                raise
            if m is None:
                break
            if m == '':
                # It was an unparseable message
                continue
            msgid = m.get('message-id', 'n/a')
            self.message(C_('#%(counter)05d %(msgid)s'))
            a = self._makeArticle(m, self.sequence)
            self.sequence += 1
            self.add_article(a)
            if end is not None and counter >= end:
               break
            counter += 1

    def new_archive(self, archive, archivedir):
        self.archives.append(archive)
        self.update_TOC = 1
        self.database.newArchive(archive)
        # If the archive directory doesn't exist, create it
        try:
            os.stat(archivedir)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise
            else:
                omask = os.umask(0)
                try:
                    os.mkdir(archivedir, self.DIRMODE)
                finally:
                    os.umask(omask)
        self.open_new_archive(archive, archivedir)

    def add_article(self, article):
        archives = self.get_archives(article)
        if not archives:
            return
        if type(archives) == type(''):
            archives = [archives]

        article.filename = filename = self.get_filename(article)
        temp = self.format_article(article)
        for arch in archives:
            self.archive = arch # why do this???
            archivedir = os.path.join(self.basedir, arch)
            if arch not in self.archives:
                self.new_archive(arch, archivedir)

            # Write the HTML-ized article
            self.write_article(arch, temp, os.path.join(archivedir,
                                                        filename))

            if 'author' in article.decoded:
                author = fixAuthor(article.decoded['author'])
            else:
                author = fixAuthor(article.author)
            if 'stripped' in article.decoded:
                subject = article.decoded['stripped'].lower()
            else:
                subject = article.subject.lower()

            article.parentID = parentID = self.get_parent_info(arch, article)
            if parentID:
                parent = self.database.getArticle(arch, parentID)
                article.threadKey = (parent.threadKey + article.date + '.'
                                     + str(article.sequence) + '-')
            else:
                article.threadKey = (article.date + '.'
                                     + str(article.sequence) + '-')
            key = article.threadKey, article.msgid

            self.database.setThreadKey(arch, key, article.msgid)
            self.database.addArticle(arch, temp, author=author,
                                     subject=subject)

            if arch not in self._dirty_archives:
                self._dirty_archives.append(arch)

    def get_parent_info(self, archive, article):
        parentID = None
        if article.in_reply_to:
            if self.database.hasArticle(archive, article.in_reply_to):
                # Only use In-Reply-To if it's in the archive.
                parentID = article.in_reply_to
        if not parentID and article.references:
            refs = self._remove_external_references(article.references)
            if refs:
                maxdate = self.database.getArticle(archive, refs[0])
                for ref in refs[1:]:
                    a = self.database.getArticle(archive, ref)
                    if a.date > maxdate.date:
                        maxdate = a
                parentID = maxdate.msgid
        if not parentID:
            # Get the oldest article with a matching subject, and
            # assume this is a follow-up to that article
            # But, use the subject that's in the database
            if 'stripped' in article.decoded:
                subject = article.decoded['stripped'].lower()
            else:
                subject = article.subject.lower()
            parentID = self.database.getOldestArticle(archive, subject)

        if parentID and not self.database.hasArticle(archive, parentID):
            parentID = None
        return parentID

    def write_article(self, index, article, path):
        omask = os.umask(0o002)
        try:
            f = open(path, 'w', encoding='utf-8')
        finally:
            os.umask(omask)
        temp_stdout, sys.stdout = sys.stdout, f
        self.write_article_header(article)
        sys.stdout.writelines(article.body)
        self.write_article_footer(article)
        sys.stdout = temp_stdout
        f.close()

    def _remove_external_references(self, refs):
        keep = []
        for ref in refs:
            if self.database.hasArticle(self.archive, ref):
                keep.append(ref)
        return keep

    # Abstract methods: these will need to be overridden by subclasses
    # before anything useful can be done.

    def get_filename(self, article):
        pass
    def get_archives(self, article):
        """Return a list of indexes where the article should be filed.
        A string can be returned if the list only contains one entry,
        and the empty list is legal."""
        pass
    def format_article(self, article):
        pass
    def write_index_header(self):
        pass
    def write_index_footer(self):
        pass
    def write_index_entry(self, article):
        pass
    def write_threadindex_entry(self, article, depth):
        pass
    def write_article_header(self, article):
        pass
    def write_article_footer(self, article):
        pass
    def write_article_entry(self, article):
        pass
    def update_article(self, archivedir, article, prev, next):
        pass
    def write_TOC(self):
        pass
    def open_new_archive(self, archive, dir):
        pass
    def message(self, msg):
        pass


class BSDDBdatabase(Database):
    __super_addArticle = Database.addArticle

    def __init__(self, basedir):
        self.__cachekeys = []
        self.__cachedict = {}
        self.__currentOpenArchive = None # The currently open indices
        self.basedir = os.path.expanduser(basedir)
        self.changed = {} # Recently added articles, indexed only by
                          # message ID

    def firstdate(self, archive):
        self.__openIndices(archive)
        date = 'None'
        try:
            date, msgid = self.dateIndex.first()
            date = time.asctime(time.localtime(float(date)))
        except KeyError:
            pass
        return date

    def lastdate(self, archive):
        self.__openIndices(archive)
        date = 'None'
        try:
            date, msgid = self.dateIndex.last()
            date = time.asctime(time.localtime(float(date)))
        except KeyError:
            pass
        return date

    def numArticles(self, archive):
        self.__openIndices(archive)
        return len(self.dateIndex)

    def addArticle(self, archive, article, subject=None, author=None,
                   date=None):
        self.__openIndices(archive)
        self.__super_addArticle(archive, article, subject, author, date)

    # Open the BSDDB files that are being used as indices
    # (dateIndex, authorIndex, subjectIndex, articleIndex)
    def __openIndices(self, archive):
        if self.__currentOpenArchive == archive:
            return

        import bsddb
        self.__closeIndices()
        arcdir = os.path.join(self.basedir, 'database')
        omask = os.umask(0)
        try:
            try:
                os.mkdir(arcdir, 0o02775)
            except OSError:
                # BAW: Hmm...
                pass
        finally:
            os.umask(omask)
        for hdr in ('date', 'author', 'subject', 'article', 'thread'):
            path = os.path.join(arcdir, archive + '-' + hdr)
            t = bsddb.btopen(path, 'c')
            setattr(self, hdr + 'Index', t)
        self.__currentOpenArchive = archive

    # Close the BSDDB files that are being used as indices (if they're
    # open--this is safe to call if they're already closed)
    def __closeIndices(self):
        if self.__currentOpenArchive is not None:
            pass
        for hdr in ('date', 'author', 'subject', 'thread', 'article'):
            attr = hdr + 'Index'
            if hasattr(self, attr):
                index = getattr(self, attr)
                if hdr == 'article':
                    if not hasattr(self, 'archive_length'):
                        self.archive_length = {}
                    self.archive_length[self.__currentOpenArchive] = len(index)
                index.close()
                delattr(self,attr)
        self.__currentOpenArchive = None

    def close(self):
        self.__closeIndices()
    def hasArticle(self, archive, msgid):
        self.__openIndices(archive)
        return msgid in self.articleIndex
    def setThreadKey(self, archive, key, msgid):
        self.__openIndices(archive)
        self.threadIndex[key] = msgid
    def getArticle(self, archive, msgid):
        self.__openIndices(archive)
        if msgid in self.__cachedict:
            self.__cachekeys.remove(msgid)
            self.__cachekeys.append(msgid)
            return self.__cachedict[msgid]
        if len(self.__cachekeys) == CACHESIZE:
            delkey, self.__cachekeys = (self.__cachekeys[0],
                                        self.__cachekeys[1:])
            del self.__cachedict[delkey]
        s = self.articleIndex[msgid]
        article = pickle.loads(s, fix_imports=True, encoding='latin1')
        self.__cachekeys.append(msgid)
        self.__cachedict[msgid] = article
        return article

    def first(self, archive, index):
        self.__openIndices(archive)
        index = getattr(self, index+'Index')
        try:
            key, msgid = index.first()
            return msgid
        except KeyError:
            return None
    def next(self, archive, index):
        self.__openIndices(archive)
        index = getattr(self, index+'Index')
        try:
            key, msgid = next(index)
        except KeyError:
            return None
        else:
            return msgid

    def getOldestArticle(self, archive, subject):
        self.__openIndices(archive)
        subject = subject.lower()
        try:
            key, tempid = self.subjectIndex.set_location(subject)
            next(self.subjectIndex)
            [subject2, date] = key.split('\0')
            if subject != subject2:
                return None
            return tempid
        except KeyError: # XXX what line raises the KeyError?
            return None

    def newArchive(self, archive):
        pass

    def clearIndex(self, archive, index):
        self.__openIndices(archive)
        index = getattr(self, index+'Index')
        finished = 0
        try:
            key, msgid = self.threadIndex.first()
        except KeyError:
            finished = 1
        while not finished:
            del self.threadIndex[key]
            try:
                key, msgid = next(self.threadIndex)
            except KeyError:
                finished = 1


