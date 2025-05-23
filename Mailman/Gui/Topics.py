# Copyright (C) 2001-2018 by the Free Software Foundation, Inc.
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

import re

from Mailman import mm_cfg
from Mailman import Utils
from Mailman.i18n import _
from Mailman.Logging.Syslog import syslog
from Mailman.Gui.GUIBase import GUIBase

OR = '|'



class Topics(GUIBase):
    def GetConfigCategory(self):
        return 'topics', _('Topics')

    def GetConfigInfo(self, mlist, category, subcat=None):
        if category != 'topics':
            return None
        WIDTH = mm_cfg.TEXTFIELDWIDTH

        return [
            _('List topic keywords'),

            ('topics_enabled', mm_cfg.Radio, (_('Disabled'), _('Enabled')), 0,
             _('''Should the topic filter be enabled or disabled?'''),

             _("""The topic filter categorizes each incoming email message
             according to <a
            href="https://docs.python.org/3/library/re.html">regular
             expression filters</a> you specify below.  If the message's
             <code>Subject:</code> or <code>Keywords:</code> header contains a
             match against a topic filter, the message is logically placed
             into a topic <em>bucket</em>.  Each user can then choose to only
             receive messages from the mailing list for a particular topic
             bucket (or buckets).  Any message not categorized in a topic
             bucket registered with the user is not delivered to the list.

             <p>Note that this feature only works with regular delivery, not
             digest delivery.

             <p>The body of the message can also be optionally scanned for
             <code>Subject:</code> and <code>Keywords:</code> headers, as
             specified by the <a
       href="?VARHELP=topics/topics_bodylines_limit">topics_bodylines_limit</a>
             configuration variable.""")),

            ('topics_bodylines_limit', mm_cfg.Number, 5, 0,
             _('How many body lines should the topic matcher scan?'),

             _("""The topic matcher will scan this many lines of the message
             body looking for topic keyword matches.  Body scanning stops when
             either this many lines have been looked at, or a non-header-like
             body line is encountered.  By setting this value to zero, no body
             lines will be scanned (i.e. only the <code>Keywords:</code> and
             <code>Subject:</code> headers will be scanned).  By setting this
             value to a negative number, then all body lines will be scanned
             until a non-header-like line is encountered.
             """)),

            ('topics', mm_cfg.Topics, 0, 0,
             _('Topic keywords, one per line, to match against each message.'),

             _("""Each topic keyword is actually a regular expression, which is
             matched against certain parts of a mail message, specifically the
             <code>Keywords:</code> and <code>Subject:</code> message headers.
             Note that the first few lines of the body of the message can also
             contain a <code>Keywords:</code> and <code>Subject:</code>
             "header" on which matching is also performed.""")),

            ]

    def handleForm(self, mlist, category, subcat, cgidata, doc):
        # MAS: Did we come from the authentication page?
        if 'topic_box_01' not in cgidata:
            return
        topics = []
        # We start i at 1 and keep going until we no longer find items keyed
        # with the marked tags.
        i = 1
        while True:
            deltag   = 'topic_delete_%02d' % i
            boxtag   = 'topic_box_%02d' % i
            reboxtag = 'topic_rebox_%02d' % i
            desctag  = 'topic_desc_%02d' % i
            wheretag = 'topic_where_%02d' % i
            addtag   = 'topic_add_%02d' % i
            newtag   = 'topic_new_%02d' % i
            i += 1
            # Was this a delete?  If so, we can just ignore this entry
            if deltag in cgidata:
                continue
            # Get the data for the current box
            name = cgidata.get(boxtag, [''])[0]
            pattern = cgidata.get(reboxtag, [''])[0]
            desc = cgidata.get(desctag, [''])[0]
            if name is None:
                # We came to the end of the boxes
                break
            if newtag in cgidata and (not name or not pattern):
                # This new entry is incomplete.
                doc.addError(_("""Topic specifications require both a name and
                a pattern.  Incomplete topics will be ignored."""))
                continue
            # Make sure the pattern was a legal regular expression
            name = Utils.websafe(name)
            try:
                orpattern = OR.join(pattern.splitlines())
                re.compile(orpattern)
            except (re.error, TypeError):
                safepattern = Utils.websafe(orpattern)
                doc.addError(_("""The topic pattern '%(safepattern)s' is not a
                legal regular expression.  It will be discarded."""))
                continue
            # Was this an add item?
            if addtag in cgidata:
                # Where should the new one be added?
                where = cgidata.get(wheretag, ['after'])[0]
                if where == 'before':
                    # Add a new empty topics box before the current one
                    topics.append(('', '', '', True))
                    topics.append((name, pattern, desc, False))
                    # Default is to add it after...
                else:
                    topics.append((name, pattern, desc, False))
                    topics.append(('', '', '', True))
            # Otherwise, just retain this one in the list
            else:
                topics.append((name, pattern, desc, False))
        # Add these topics to the mailing list object, and deal with other
        # options.
        mlist.topics = topics
        try:
            mlist.topics_enabled = int(cgidata.get('topics_enabled', [mlist.topics_enabled])[0])
        except ValueError:
            # BAW: should really print a warning
            pass
        try:
            mlist.topics_bodylines_limit = int(cgidata.get('topics_bodylines_limit', [mlist.topics_bodylines_limit])[0])
        except ValueError:
            # BAW: should really print a warning
            pass

    def process_form(self, mlist, cgidata):
        # Get the topic information from the form
        name = cgidata.get(boxtag, [''])[0]
        pattern = cgidata.get(reboxtag, [''])[0]
        desc = cgidata.get(desctag, [''])[0]
        where = cgidata.get(wheretag, [''])[0]
        
        # Update list settings
        mlist.topics_enabled = int(cgidata.get('topics_enabled', ['0'])[0])
        mlist.topics_bodylines_limit = int(cgidata.get('topics_bodylines_limit', ['0'])[0])
        
        # Process the topic
        if name and pattern:
            if where == 'add':
                mlist.AddTopic(name, pattern, desc)
            elif where == 'change':
                mlist.ChangeTopic(name, pattern, desc)
            elif where == 'delete':
                mlist.DeleteTopic(name)
        
        mlist.Save()
