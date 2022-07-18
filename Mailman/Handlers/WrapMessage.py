# Copyright (C) 2013-2018 by the Free Software Foundation, Inc.
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

"""Wrap the message in an outer message/rfc822 part and transfer/add
some headers from the original.

Also, in the case of Munge From, replace the From:, Reply-To: and Cc: in the
original message.
"""

import copy

from email.MIMEMessage import MIMEMessage
from email.MIMEText import MIMEText

from Mailman import Utils

# Headers from the original that we want to keep in the wrapper.
KEEPERS = ('to',
           'in-reply-to',
           'references',
           'x-mailman-approved-at',
           'date',
          )



def process(mlist, msg, msgdata):
    # This is the negation of we're wrapping because dmarc_moderation_action
    # is wrap this message or from_is_list applies and is wrap.
    if not (msgdata.get('from_is_list') == 2 or
            (mlist.from_is_list == 2 and msgdata.get('from_is_list') == 0)):
        # Now see if we need to add a From:, Reply-To: or Cc: without wrapping.
        # See comments in CookHeaders.change_header for why we do this here.
        a_h = msgdata.get('add_header')
        if a_h:
            if a_h.get('From'):
                del msg['from']
                msg['From'] = a_h.get('From')
            if a_h.get('Reply-To'):
                del msg['reply-to']
                msg['Reply-To'] = a_h.get('Reply-To')
            if a_h.get('Cc'):
                del msg['cc']
                msg['Cc'] = a_h.get('Cc')
        return

    # There are various headers in msg that we don't want, so we basically
    # make a copy of the msg, then delete almost everything and set/copy
    # what we want.
    omsg = copy.deepcopy(msg)
    # If CookHeaders didn't change the Subject: we need to keep it too.
    # Get a fresh list.
    keepers = list(KEEPERS)
    if 'subject' not in [key.lower() for key in
                         msgdata.get('add_header', {}).keys()]:
        keepers.append('subject')
    for key in msg.keys():
        if key.lower() not in keepers:
            del msg[key]
    msg['MIME-Version'] = '1.0'
    msg['Message-ID'] = Utils.unique_message_id(mlist)
    # Add the headers from CookHeaders.
    for k, v in msgdata.get('add_header', {}).items():
        msg[k] = v
    # Are we including dmarc_wrapped_message_text?  I.e., do we have text and
    # are we wrapping because of dmarc_moderation_action?
    if mlist.dmarc_wrapped_message_text and msgdata.get('from_is_list') == 2:
        part1 = MIMEText(Utils.wrap(mlist.dmarc_wrapped_message_text),
                         'plain',
                         Utils.GetCharSet(mlist.preferred_language))
        part1['Content-Disposition'] = 'inline'
        part2 = MIMEMessage(omsg)
        part2['Content-Disposition'] = 'inline'
        msg['Content-Type'] = 'multipart/mixed'
        msg.set_payload([part1, part2])
    else:
        msg['Content-Type'] = 'message/rfc822'
        msg['Content-Disposition'] = 'inline'
        msg.set_payload([omsg])

