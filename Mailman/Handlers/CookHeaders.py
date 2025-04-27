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

"""Cook a message's Subject header.
Also do other manipulations of From:, Reply-To: and Cc: depending on
list configuration.
"""

from __future__ import absolute_import, print_function, unicode_literals

import re
from email.charset import Charset
from email.header import Header, decode_header, make_header
from email.utils import parseaddr, formataddr, getaddresses
from email.errors import HeaderParseError
from email.iterators import body_line_iterator

from Mailman import i18n
from Mailman import mm_cfg
from Mailman import Utils
from Mailman.i18n import _
from Mailman.Logging.Syslog import mailman_log

CONTINUATION = ',\n '
COMMASPACE = ', '
MAXLINELEN = 78

def _isunicode(s):
    return isinstance(s, str)

nonascii = re.compile(r'[^\s!-~]')

def uheader(mlist, s, header_name=None, continuation_ws=' ', maxlinelen=None):
    """Create a Header object from a string with proper charset handling.
    
    This function ensures proper handling of both str and bytes input,
    and uses the list's preferred charset for encoding.
    """
    # Get the charset to encode the string in
    charset = Utils.GetCharSet(mlist.preferred_language)
    
    # Convert input to str if it's bytes
    if isinstance(s, bytes):
        try:
            s = s.decode('ascii')
        except UnicodeDecodeError:
            try:
                s = s.decode(charset)
            except UnicodeDecodeError:
                s = s.decode('utf-8', 'replace')
    
    # If there are non-ASCII characters, use the list's charset
    if nonascii.search(s):
        if charset == 'us-ascii':
            charset = 'utf-8'
    else:
        charset = 'us-ascii'
        
    try:
        return Header(s, charset, maxlinelen, header_name, continuation_ws)
    except UnicodeError:
        mailman_log('error', 'list: %s: cannot encode "%s" as %s',
                   mlist.internal_name(), s, charset)
        # Fall back to ASCII with replacement characters
        return Header(s.encode('ascii', 'replace').decode('ascii'),
                     'us-ascii', maxlinelen, header_name, continuation_ws)

def change_header(name, value, mlist, msg, msgdata, delete=True, repl=True):
    """Change or add a message header.
    
    This function handles header changes in a Python 3 compatible way,
    properly dealing with encodings and header values.
    """
    if ((msgdata.get('from_is_list') == 2 or
        (msgdata.get('from_is_list') == 0 and mlist.from_is_list == 2)) and 
        not msgdata.get('_fasttrack')
       ) or name.lower() in ('from', 'reply-to', 'cc'):
        msgdata.setdefault('add_header', {})[name] = value
    elif repl or name not in msg:
        if delete:
            del msg[name]
        if isinstance(value, Header):
            msg[name] = value
        else:
            try:
                msg[name] = str(value)
            except UnicodeEncodeError:
                msg[name] = Header(value,
                                 Utils.GetCharSet(mlist.preferred_language))

def process(mlist, msg, msgdata):
    # Set the "X-Ack: no" header if noack flag is set
    if msgdata.get('noack'):
        change_header('X-Ack', 'no', mlist, msg, msgdata)
    
    # Save original sender for later
    if 'original_sender' not in msgdata:
        msgdata['original_sender'] = msg.get_sender()
    
    # Handle subject prefix and other headers
    fasttrack = msgdata.get('_fasttrack')
    if not msgdata.get('isdigest') and not fasttrack:
        try:
            prefix_subject(mlist, msg, msgdata)
        except (UnicodeError, ValueError):
            pass
    
    # Mark message as processed
    change_header('X-BeenThere', mlist.GetListEmail(),
                 mlist, msg, msgdata, delete=False)
    
    # Add standard headers
    change_header('X-Mailman-Version', mm_cfg.VERSION,
                 mlist, msg, msgdata, repl=False)
    change_header('Precedence', 'list',
                 mlist, msg, msgdata, repl=False)
    
    # Handle From: header munging if needed
    if (msgdata.get('from_is_list') or mlist.from_is_list) and not fasttrack:
        munge_from_header(mlist, msg, msgdata)

def munge_from_header(mlist, msg, msgdata):
    """Munge the From: header for the list.
    
    This is separated into its own function to make the logic clearer
    and handle all the encoding issues in one place.
    """
    # Get the original From: addresses
    faddrs = getaddresses(msg.get_all('from', []))
    faddrs = [x for x in faddrs if x[1].find('@') > 0]
    
    if len(faddrs) == 1:
        realname, email = faddrs[0]
    else:
        realname = ''
        email = msgdata['original_sender']
    
    # Get or create realname
    if not realname:
        if mlist.isMember(email):
            realname = mlist.getMemberName(email) or email
        else:
            realname = email
    
    # Remove domain from realname if it looks like an email
    realname = re.sub(r'@([^ .]+\.)+[^ .]+$', '---', realname)
    
    # Convert realname to unicode
    charset = Utils.GetCharSet(mlist.preferred_language)
    if isinstance(realname, bytes):
        try:
            realname = realname.decode(charset)
        except UnicodeDecodeError:
            realname = realname.decode('utf-8', 'replace')
    
    # Format the new From: header
    via = _('%(realname)s via %(listname)s')
    listname = mlist.real_name
    if isinstance(listname, bytes):
        listname = listname.decode(charset, 'replace')
    
    display_name = via % {'realname': realname, 'listname': listname}
    
    # Create the new From: header value
    new_from = formataddr((display_name, mlist.GetListEmail()))
    change_header('From', new_from, mlist, msg, msgdata)

def prefix_subject(mlist, msg, msgdata):
    # Add the subject prefix unless the message is a digest or is being fast
    # tracked (e.g. internally crafted, delivered to a single user such as the
    # list admin).
    prefix = mlist.subject_prefix.strip()
    if not prefix:
        return
    subject = msg.get('subject', '')
    # Try to figure out what the continuation_ws is for the header
    if isinstance(subject, Header):
        lines = str(subject).splitlines()
    else:
        lines = subject.splitlines()
    ws = ' '
    if len(lines) > 1 and lines[1] and lines[1][0] in ' \t':
        ws = lines[1][0]
    msgdata['origsubj'] = subject
    # The subject may be multilingual but we take the first charset as major
    # one and try to decode.  If it is decodable, returned subject is in one
    # line and cset is properly set.  If fail, subject is mime-encoded and
    # cset is set as us-ascii.  See detail for ch_oneline() (CookHeaders one
    # line function).
    subject, cset = ch_oneline(subject)
    # TK: Python interpreter has evolved to be strict on ascii charset code
    # range.  It is safe to use unicode string when manupilating header
    # contents with re module.  It would be best to return unicode in
    # ch_oneline() but here is temporary solution.
    subject = str(subject, cset)
    # If the subject_prefix contains '%d', it is replaced with the
    # mailing list sequential number.  Sequential number format allows
    # '%d' or '%05d' like pattern.
    prefix_pattern = re.escape(prefix)
    # unescape '%' :-<
    prefix_pattern = '%'.join(prefix_pattern.split(r'\%'))
    p = re.compile(r'%\d*d')
    if p.search(prefix, 1):
        # prefix have number, so we should search prefix w/number in subject.
        # Also, force new style.
        prefix_pattern = p.sub(r'\s*\d+\s*', prefix_pattern)
        old_style = False
    else:
        old_style = mm_cfg.OLD_STYLE_PREFIXING
    subject = re.sub(prefix_pattern, '', subject)
    # Previously the following re didn't have the first \s*. It would fail
    # if the incoming Subject: was like '[prefix] Re: Re: Re:' because of the
    # leading space after stripping the prefix. It is not known what MUA would
    # create such a Subject:, but the issue was reported.
    rematch = re.match(
                       r'(\s*(RE|AW|SV|VS)\s*(\[\d+\])?\s*:\s*)+',
                        subject, re.I)
    if rematch:
        subject = subject[rematch.end():]
        recolon = 'Re:'
    else:
        recolon = ''
    # Strip leading and trailing whitespace from subject.
    subject = subject.strip()
    # At this point, subject may become null if someone post mail with
    # Subject: [subject prefix]
    if subject == '':
        # We want the i18n context to be the list's preferred_language.  It
        # could be the poster's.
        otrans = i18n.get_translation()
        i18n.set_language(mlist.preferred_language)
        subject = _('(no subject)')
        i18n.set_translation(otrans)
        cset = Utils.GetCharSet(mlist.preferred_language)
        subject = str(subject, cset)
    # and substitute %d in prefix with post_id
    try:
        prefix = prefix % mlist.post_id
    except TypeError:
        pass
    # If charset is 'us-ascii', try to concatnate as string because there
    # is some weirdness in Header module (TK)
    if cset == 'us-ascii':
        try:
            if old_style:
                h = u' '.join([recolon, prefix, subject])
            else:
                if recolon:
                    h = u' '.join([prefix, recolon, subject])
                else:
                    h = u' '.join([prefix, subject])
            h = h.encode('us-ascii')
            h = uheader(mlist, h, 'Subject', continuation_ws=ws)
            change_header('Subject', h, mlist, msg, msgdata)
            ss = u' '.join([recolon, subject])
            ss = ss.encode('us-ascii')
            ss = uheader(mlist, ss, 'Subject', continuation_ws=ws)
            msgdata['stripped_subject'] = ss
            return
        except UnicodeError:
            pass
    # Get the header as a Header instance, with proper unicode conversion
    # Because of rfc2047 encoding, spaces between encoded words can be
    # insignificant, so we need to append spaces to our encoded stuff.
    prefix += ' '
    if recolon:
        recolon += ' '
    if old_style:
        h = uheader(mlist, recolon, 'Subject', continuation_ws=ws)
        h.append(prefix)
    else:
        h = uheader(mlist, prefix, 'Subject', continuation_ws=ws)
        h.append(recolon)
    # TK: Subject is concatenated and unicode string.
    subject = subject.encode(cset, 'replace')
    h.append(subject, cset)
    change_header('Subject', h, mlist, msg, msgdata)
    ss = uheader(mlist, recolon, 'Subject', continuation_ws=ws)
    ss.append(subject, cset)
    msgdata['stripped_subject'] = ss


def ch_oneline(headerstr):
    # Decode header string in one line and convert into single charset
    # copied and modified from ToDigest.py and Utils.py
    # return (string, cset) tuple as check for failure
    try:
        d = decode_header(headerstr)
        # at this point, we should rstrip() every string because some
        # MUA deliberately add trailing spaces when composing return
        # message.
        d = [(s.rstrip(), c) for (s,c) in d]
        cset = 'us-ascii'
        for x in d:
            # search for no-None charset
            if x[1]:
                cset = x[1]
                break
        h = make_header(d)
        ustr = str(h)
        oneline = u''.join(ustr.splitlines())
        return oneline.encode(cset, 'replace'), cset
    except (LookupError, UnicodeError, ValueError, HeaderParseError):
        # possibly charset problem. return with undecoded string in one line.
        return ''.join(headerstr.splitlines()), 'us-ascii'
