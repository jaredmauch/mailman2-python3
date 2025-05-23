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

"""MailList mixin class managing the general options."""

import re

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Errors
from Mailman.i18n import _
from Mailman.htmlformat import Document
from Mailman.Gui.GUIBase import GUIBase

OPTIONS = ('hide', 'ack', 'notmetoo', 'nodupes')



class General(GUIBase):
    def GetConfigCategory(self):
        return 'general', _('General Options')

    def GetConfigInfo(self, mlist, category, subcat):
        if category != 'general':
            return None
        WIDTH = mm_cfg.TEXTFIELDWIDTH

        # These are for the default_options checkboxes below.
        bitfields = {'hide'     : mm_cfg.ConcealSubscription,
                     'ack'      : mm_cfg.AcknowledgePosts,
                     'notmetoo' : mm_cfg.DontReceiveOwnPosts,
                     'nodupes'  : mm_cfg.DontReceiveDuplicates
                     }
        bitdescrs = {
            'hide'     : _("Conceal the member's address"),
            'ack'      : _("Acknowledge the member's posting"),
            'notmetoo' : _("Do not send a copy of a member's own post"),
            'nodupes'  :
            _('Filter out duplicate messages to list members (if possible)'),
            }

        optvals = [mlist.new_member_options & bitfields[o] for o in OPTIONS]
        opttext = [bitdescrs[o] for o in OPTIONS]

        rtn = [
            _('''Fundamental list characteristics, including descriptive
            info and basic behaviors.'''),

            _('General list personality'),

            ('real_name', mm_cfg.String, WIDTH, 0,
             _('The public name of this list (make case-changes only).'),
             _('''The capitalization of this name can be changed to make it
             presentable in polite company as a proper noun, or to make an
             acronym part all upper case, etc.  However, the name will be
             advertised as the email address (e.g., in subscribe confirmation
             notices), so it should <em>not</em> be otherwise altered.  (Email
             addresses are not case sensitive, but they are sensitive to
             almost everything else :-)''')),

            ('owner', mm_cfg.EmailList, (3, WIDTH), 0,
             _("""The list administrator email addresses.  Multiple
             administrator addresses, each on separate line is okay."""),

             _('''There are two ownership roles associated with each mailing
             list.  The <em>list administrators</em> are the people who have
             ultimate control over all parameters of this mailing list.  They
             are able to change any list configuration variable available
             through these administration web pages.

             <p>The <em>list moderators</em> have more limited permissions;
             they are not able to change any list configuration variable, but
             they are allowed to tend to pending administration requests,
             including approving or rejecting held subscription requests, and
             disposing of held postings.  Of course, the <em>list
             administrators</em> can also tend to pending requests.

             <p>In order to split the list ownership duties into
             administrators and moderators, you must
             <a href="passwords">set a separate moderator password</a>,
             and also provide the <a href="?VARHELP=general/moderator">email
             addresses of the list moderators</a>.  Note that the field you
             are changing here specifies the list administrators.''')),

            ('moderator', mm_cfg.EmailList, (3, WIDTH), 0,
             _("""The list moderator email addresses.  Multiple
             moderator addresses, each on separate line is okay."""),

             _('''There are two ownership roles associated with each mailing
             list.  The <em>list administrators</em> are the people who have
             ultimate control over all parameters of this mailing list.  They
             are able to change any list configuration variable available
             through these administration web pages.

             <p>The <em>list moderators</em> have more limited permissions;
             they are not able to change any list configuration variable, but
             they are allowed to tend to pending administration requests,
             including approving or rejecting held subscription requests, and
             disposing of held postings.  Of course, the <em>list
             administrators</em> can also tend to pending requests.

             <p>In order to split the list ownership duties into
             administrators and moderators, you must
             <a href="passwords">set a separate moderator password</a>,
             and also provide the email addresses of the list moderators in
             this section.  Note that the field you are changing here
             specifies the list moderators.''')),

            ('description', mm_cfg.String, WIDTH, 0,
             _('A terse phrase identifying this list.'),

             _('''This description is used when the mailing list is listed with
                other mailing lists, or in headers, and so forth.  It should
                be as succinct as you can get it, while still identifying what
                the list is.''')),

            ('info', mm_cfg.Text, (7, WIDTH), 0,
             _('''An introductory description - a few paragraphs - about the
             list.  It will be included, as html, at the top of the listinfo
             page.  Carriage returns will end a paragraph - see the details
             for more info.'''),
             _("""The text will be treated as html <em>except</em> that
             newlines will be translated to &lt;br&gt; - so you can use links,
             preformatted text, etc, but don't put in carriage returns except
             where you mean to separate paragraphs.  And review your changes -
             bad html (like some unterminated HTML constructs) can prevent
             display of the entire listinfo page.""")),

            ('subject_prefix', mm_cfg.String, WIDTH, 0,
             _('Prefix for subject line of list postings.'),
             _("""This text will be prepended to subject lines of messages
             posted to the list, to distinguish mailing list messages in
             mailbox summaries.  Brevity is premium here, it's ok to shorten
             long mailing list names to something more concise, as long as it
             still identifies the mailing list.
             You can also add a sequential number by %%d substitution
             directive. eg.; [listname %%d] -> [listname 123]
                            (listname %%05d) -> (listname 00123)
             """)),

            ('from_is_list', mm_cfg.Radio,
             (_('No'), _('Munge From'), _('Wrap Message')), 0,
             _("""Replace the From: header address with the list's posting
             address to mitigate issues stemming from the original From:
             domain's DMARC or similar policies."""),
             _("""Several protocols now in wide use attempt to ensure that use
             of the domain in the author's address (ie, in the From: header
             field) is authorized by that domain.  These protocols may be
             incompatible with common list features such as footers, causing
             participating email services to bounce list traffic merely
             because of the address in the From: field.  <b>This has resulted
             in members being unsubscribed despite being perfectly able to
             receive mail.</b>
             <p>
             The following actions are applied to all list messages when
             selected here.  To apply these actions only to messages where the
             domain in the From: header is determined to use such a protocol,
             see the <a
             href="?VARHELP=privacy/sender/dmarc_moderation_action">
             dmarc_moderation_action</a> settings under Privacy options...
             -&gt; Sender filters.
             <p>Settings:<p>
             <dl>
             <dt>No</dt>
             <dd>Do nothing special.  This is appropriate for anonymous lists.
             It is appropriate for dedicated announcement lists, unless the
             From: address of authorized posters might be in a domain with a
             DMARC or similar policy. It is also appropriate if you choose to
             use dmarc_moderation_action other than Accept for this list.</dd>
             <dt>Munge From</dt>
             <dd>This action replaces the poster's address in the From: header
             with the list's posting address and adds the poster's address to
             the addresses in the original Reply-To: header.</dd>
             <dt>Wrap Message</dt>
             <dd>Just wrap the message in an outer message with the From:
             header containing the list's posting address and with the original
             From: address added to the addresses in the original Reply-To:
             header and with Content-Type: message/rfc822.  This is effectively
             a one message MIME format digest.</dd>
             </dl>
             <p>The transformations for anonymous_list are applied before
             any of these actions. It is not useful to apply actions other
             than No to an anonymous list, and if you do so, the result may
             be surprising.
             <p>The Reply-To: header munging actions below interact with these
             actions as follows:
             <p> first_strip_reply_to = Yes will remove all the incoming
             Reply-To: addresses but will still add the poster's address to
             Reply-To: for all three settings of reply_goes_to_list which
             respectively will result in just the poster's address, the
             poster's address and the list posting address or the poster's
             address and the explicit reply_to_address in the outgoing
             Reply-To: header. If first_strip_reply_to = No the poster's
             address in the original From: header, if not already included in
             the Reply-To:, will be added to any existing Reply-To:
             address(es).
             <p>These actions, whether selected here or via <a
             href="?VARHELP=privacy/sender/dmarc_moderation_action">
             dmarc_moderation_action</a>, do not apply to messages in digests
             or archives or sent to usenet via the Mail&lt;-&gt;News gateways.
             <p>If <a
             href="?VARHELP=privacy/sender/dmarc_moderation_action">
             dmarc_moderation_action</a> applies to this message with an
             action other than Accept, that action rather than this is
             applied""")),

            ('anonymous_list', mm_cfg.Radio, (_('No'), _('Yes')), 0,
             _("""Hide the sender of a message, replacing it with the list
             address (Removes From, Sender and Reply-To fields)""")),

            _('''<tt>Reply-To:</tt> header munging'''),

            ('first_strip_reply_to', mm_cfg.Radio, (_('No'), _('Yes')), 0,
             _('''Should any existing <tt>Reply-To:</tt> header found in the
             original message be stripped?  If so, this will be done
             regardless of whether an explict <tt>Reply-To:</tt> header is
             added by Mailman or not.''')),

            ('reply_goes_to_list', mm_cfg.Radio,
             (_('Poster'), _('This list'), _('Explicit address')), 0,
             _('''Where are replies to list messages directed?
             <tt>Poster</tt> is <em>strongly</em> recommended for most mailing
             lists.'''),

             # Details for reply_goes_to_list
             _("""This option controls what Mailman does to the
             <tt>Reply-To:</tt> header in messages flowing through this
             mailing list.  When set to <em>Poster</em>, no <tt>Reply-To:</tt>
             header is added by Mailman, although if one is present in the
             original message, it is not stripped.  Setting this value to
             either <em>This list</em> or <em>Explicit address</em> causes
             Mailman to insert a specific <tt>Reply-To:</tt> header in all
             messages, overriding the header in the original message if
             necessary (<em>Explicit address</em> inserts the value of <a
             href="?VARHELP=general/reply_to_address">reply_to_address</a>).

             <p>There are many reasons not to introduce or override the
             <tt>Reply-To:</tt> header.  One is that some posters depend on
             their own <tt>Reply-To:</tt> settings to convey their valid
             return address.  Another is that modifying <tt>Reply-To:</tt>
             makes it much more difficult to send private replies.  See <a
             href="http://marc.merlins.org/netrants/reply-to-harmful.html">`Reply-To'
             Munging Considered Harmful</a> for a general discussion of this
             issue.  See <a
             href="http://marc.merlins.org/netrants/reply-to-useful.html">
             Reply-To Munging Considered Useful</a> for a dissenting opinion.

             <p>Some mailing lists have restricted posting privileges, with a
             parallel list devoted to discussions.  Examples are `patches' or
             `checkin' lists, where software changes are posted by a revision
             control system, but discussion about the changes occurs on a
             developers mailing list.  To support these types of mailing
             lists, select <tt>Explicit address</tt> and set the
             <tt>Reply-To:</tt> address below to point to the parallel
             list.""")),

            ('reply_to_address', mm_cfg.Email, WIDTH, 0,
             _('Explicit <tt>Reply-To:</tt> header.'),
             # Details for reply_to_address
             _("""This is the address set in the <tt>Reply-To:</tt> header
             when the <a
             href="?VARHELP=general/reply_goes_to_list">reply_goes_to_list</a>
             option is set to <em>Explicit address</em>.

             <p>There are many reasons not to introduce or override the
             <tt>Reply-To:</tt> header.  One is that some posters depend on
             their own <tt>Reply-To:</tt> settings to convey their valid
             return address.  Another is that modifying <tt>Reply-To:</tt>
             makes it much more difficult to send private replies.  See <a
             href="http://marc.merlins.org/netrants/reply-to-harmful.html">`Reply-To'
             Munging Considered Harmful</a> for a general discussion of this
             issue.  See <a
             href="http://marc.merlins.org/netrants/reply-to-useful.html">
             Reply-To Munging Considered Useful</a> for a dissenting opinion.

             <p>Some mailing lists have restricted posting privileges, with a
             parallel list devoted to discussions.  Examples are `patches' or
             `checkin' lists, where software changes are posted by a revision
             control system, but discussion about the changes occurs on a
             developers mailing list.  To support these types of mailing
             lists, specify the explicit <tt>Reply-To:</tt> address here.  You
             must also specify <tt>Explicit address</tt> in the
             <tt>reply_goes_to_list</tt>
             variable.

             <p>Note that if the original message contains a
             <tt>Reply-To:</tt> header, it will not be changed.""")),

            _('Umbrella list settings'),

            ('umbrella_list', mm_cfg.Radio, (_('No'), _('Yes')), 0,
             _('''Send password reminders to, eg, "-owner" address instead of
             directly to user.'''),

             _("""Set this to yes when this list is intended to cascade only
             to other mailing lists.  When set, meta notices like
             confirmations and password reminders will be directed to an
             address derived from the member\'s address - it will have the
             value of "umbrella_member_suffix" appended to the member's
             account name.""")),

            ('umbrella_member_suffix', mm_cfg.String, WIDTH, 0,
             _('''Suffix for use when this list is an umbrella for other
             lists, according to setting of previous "umbrella_list"
             setting.'''),

             _("""When "umbrella_list" is set to indicate that this list has
             other mailing lists as members, then administrative notices like
             confirmations and password reminders need to not be sent to the
             member list addresses, but rather to the owner of those member
             lists.  In that case, the value of this setting is appended to
             the member's account name for such notices.  `-owner' is the
             typical choice.  This setting has no effect when "umbrella_list"
             is "No".""")),

            _('Notifications'),

            ('send_reminders', mm_cfg.Radio, (_('No'), _('Yes')), 0,
             _('''Send monthly password reminders?'''),

             _('''Turn this on if you want password reminders to be sent once
             per month to your members.  Note that members may disable their
             own individual password reminders.''')),

            ('welcome_msg', mm_cfg.Text, (4, WIDTH), 0,
             _('''List-specific text prepended to new-subscriber welcome
             message'''),

             _("""This value, if any, will be added to the front of the
             new-subscriber welcome message.  The rest of the welcome message
             already describes the important addresses and URLs for the
             mailing list, so you don't need to include any of that kind of
             stuff here.  This should just contain mission-specific kinds of
             things, like etiquette policies or team orientation, or that kind
             of thing.

             <p>Note that this text will be wrapped, according to the
             following rules:
             <ul><li>Each paragraph is filled so that no line is longer than
                     70 characters.
                 <li>Any line that begins with whitespace is not filled.
                 <li>A blank line separates paragraphs.
             </ul>""")),

            ('send_welcome_msg', mm_cfg.Radio, (_('No'), _('Yes')), 0,
             _('Send welcome message to newly subscribed members?'),
             _("""Turn this off only if you plan on subscribing people manually
             and don't want them to know that you did so.  This option is most
             useful for transparently migrating lists from some other mailing
             list manager to Mailman.""")),

            ('goodbye_msg', mm_cfg.Text, (4, WIDTH), 0,
             _('''Text sent to people leaving the list.  If empty, no special
             text will be added to the unsubscribe message.''')),

            ('send_goodbye_msg', mm_cfg.Radio, (_('No'), _('Yes')), 0,
             _('Send goodbye message to members when they are unsubscribed?')),

            ('admin_immed_notify', mm_cfg.Radio, (_('No'), _('Yes')), 0,
             _('''Should the list moderators get immediate notice of new
             requests, as well as daily notices about collected ones?'''),

             _('''List moderators (and list administrators) are sent daily
             reminders of requests pending approval, like subscriptions to a
             moderated list, or postings that are being held for one reason or
             another.  Setting this option causes notices to be sent
             immediately on the arrival of new requests as well.''')),

            ('admin_notify_mchanges', mm_cfg.Radio, (_('No'), _('Yes')), 0,
             _('''Should administrator get notices of subscribes and
             unsubscribes?''')),

            ('respond_to_post_requests', mm_cfg.Radio,
             (_('No'), _('Yes')), 0,
             _('Send mail to poster when their posting is held for approval?')
            ),

            _('Additional settings'),

            ('emergency', mm_cfg.Toggle, (_('No'), _('Yes')), 0,
             _('Emergency moderation of all list traffic.'),
             _("""When this option is enabled, all list traffic is emergency
             moderated, i.e. held for moderation.  Turn this option on when
             your list is experiencing a flamewar and you want a cooling off
             period.""")),

            ('new_member_options', mm_cfg.Checkbox,
             (opttext, optvals, 0, OPTIONS),
             # The description for new_member_options includes a kludge where
             # we add a hidden field so that even when all the checkboxes are
             # deselected, the form data will still have a new_member_options
             # key (it will always be a list).  Otherwise, we'd never be able
             # to tell if all were deselected!
             0, _('''Default options for new members joining this list.<input
             type="hidden" name="new_member_options" value="ignore">'''),

             _("""When a new member is subscribed to this list, their initial
             set of options is taken from this variable's setting.""")),

            ('administrivia', mm_cfg.Radio, (_('No'), _('Yes')), 0,
             _('''(Administrivia filter) Check postings and intercept ones
             that seem to be administrative requests?'''),

             _("""Administrivia tests will check postings to see whether it's
             really meant as an administrative request (like subscribe,
             unsubscribe, etc), and will add it to the the administrative
             requests queue, notifying the administrator of the new request,
             in the process.""")),

            ('max_message_size', mm_cfg.Number, 7, 0,
             _('''Maximum length in kilobytes (KB) of a message body.  Use 0
             for no limit.''')),

            ('admin_member_chunksize', mm_cfg.Number, 7, 0,
             _('''Maximum number of members to show on one page of the
             Membership List.''')),

            ('host_name', mm_cfg.Host, WIDTH, 0,
             _('Host name this list prefers for email.'),

             _("""The "host_name" is the preferred name for email to
             mailman-related addresses on this host, and generally should be
             the mail host's exchanger address, if any.  This setting can be
             useful for selecting among alternative names of a host that has
             multiple addresses.""")),

          ]

        if mm_cfg.ALLOW_RFC2369_OVERRIDES:
            rtn.append(
                ('include_rfc2369_headers', mm_cfg.Radio,
                 (_('No'), _('Yes')), 0,
                 _("""Should messages from this mailing list include the
                 <a href="http://www.faqs.org/rfcs/rfc2369.html">RFC 2369</a>
                 (i.e. <tt>List-*</tt>) headers?  <em>Yes</em> is highly
                 recommended."""),

                 _("""RFC 2369 defines a set of List-* headers that are
                 normally added to every message sent to the list membership.
                 These greatly aid end-users who are using standards compliant
                 mail readers.  They should normally always be enabled.

                 <p>However, not all mail readers are standards compliant yet,
                 and if you have a large number of members who are using
                 non-compliant mail readers, they may be annoyed at these
                 headers.  You should first try to educate your members as to
                 why these headers exist, and how to hide them in their mail
                 clients.  As a last resort you can disable these headers, but
                 this is not recommended (and in fact, your ability to disable
                 these headers may eventually go away)."""))
                )
        # Suppression of List-Post: headers
        rtn.append(
            ('include_list_post_header', mm_cfg.Radio,
             (_('No'), _('Yes')), 0,
             _('Should postings include the <tt>List-Post:</tt> header?'),
             _("""The <tt>List-Post:</tt> header is one of the headers
             recommended by
             <a href="http://www.faqs.org/rfcs/rfc2369.html">RFC 2369</a>.
             However for some <em>announce-only</em> mailing lists, only a
             very select group of people are allowed to post to the list; the
             general membership is usually not allowed to post.  For lists of
             this nature, the <tt>List-Post:</tt> header is misleading.
             Select <em>No</em> to disable the inclusion of this header. (This
             does not affect the inclusion of the other <tt>List-*:</tt>
             headers.)"""))
            )
        # Suppression of Sender header modification
        if mm_cfg.ALLOW_SENDER_OVERRIDES:
            rtn.append(
                ('include_sender_header', mm_cfg.Radio,
                 (_('No'), _('Yes')), 0,
                 _("""Should the <tt>Sender</tt> header be rewritten for this
                 mailing list to avoid stray bounces?  <em>Yes</em> is
                 recommended."""),

                 _("""<a href="http://www.faqs.org/rfcs/rfc2822.html">RFC
                 2822</a> defines the <tt>Sender</tt> header and defines it
                 as "the mailbox of the agent responsible for the actual
                 transmission of the message."  Mailman replaces this header
                 by default with the list's bounce address.
                 
                 <p>While it is debatable if Mailman is such an agent, setting
                 this header helps directing bounces from some broken MTAs to
                 the right destination.  On the other hand, some mail
                 readers show unexpected behaviour if this header is set (like
                 missing addresses in forwarded mails and copies sent to the
                 bounce address on reply-to-all), so it can be disabled 
                 here."""))
                )

        # Do we munge Cc: in AvoidDuplicates.py
        rtn.append(
            ('drop_cc', mm_cfg.Radio,
             (_('No'), _('Yes')), 0,
             _('Should duplicate avoidance drop addresses from Cc: headers'),
             _("""The process which avoids sending a list copy of a message to
               a member who is also directly addressed in To: or Cc: can drop
               the address from Cc: to avoid growing a long Cc: list in long
               threads.  This can be undesirable as it can break DKIM
               signatures and possibly cause confusion.  To avoid changes to
               Cc: headers, set this to No."""))
             )

        # Discard held messages after this number of days
        rtn.append(
            ('max_days_to_hold', mm_cfg.Number, 7, 0,
            _("""Discard held messages older than this number of days.
            Use 0 for no automatic discarding."""))
            )

        return rtn

    def _setValue(self, mlist, property, val, doc):
        if property == 'real_name' and \
               val.lower() != mlist.internal_name().lower():
            # These values can't differ by other than case
            doc.addError(_("""<b>real_name</b> attribute not
            changed!  It must differ from the list's name by case
            only."""))
        elif property == 'new_member_options':
            # Get current value because there are valid bits not in OPTIONS.
            # If we're the admin CGI, we then process the bits in OPTIONS,
            # turning them on or off as appropriate.  Otherwise we process all
            # the bits in mm_cfg.OPTINFO so that config_list can set and reset
            # them.
            newopts = mlist.new_member_options
            if isinstance(doc, Document):
                opts = OPTIONS
            else:
                opts = mm_cfg.OPTINFO
            for opt in opts:
                bitfield = mm_cfg.OPTINFO[opt]
                if opt in val:
                    newopts |= bitfield
                else:
                    newopts &= ~bitfield
            mlist.new_member_options = newopts
        elif property == 'subject_prefix':
            # Convert any html entities to Unicode
            mlist.subject_prefix = Utils.canonstr(
                val, mlist.preferred_language)
        elif property == 'info':
            if val != mlist.info:
                if Utils.suspiciousHTML(val):
                    doc.addError(_("""The <b>info</b> attribute you saved
contains suspicious HTML that could potentially expose your users to cross-site
scripting attacks.  This change has therefore been rejected.  If you still want
to make these changes, you must have shell access to your Mailman server.
This change can be made with bin/withlist or with bin/config_list by setting
mlist.info.
                        """))
                else:
                    mlist.info = val
        elif property == 'admin_member_chunksize' and (val < 1
                                          or not isinstance(val, int)):
            doc.addError(_("""<b>admin_member_chunksize</b> attribute not
            changed!  It must be an integer > 0."""))
        elif property == 'host_name':
            try:
                Utils.ValidateEmail('user@' + val)
            except Errors.EmailAddressError:
                doc.addError(_("""<b>host_name</b> attribute not changed!
                It must be a valid domain name."""))
            else:
                GUIBase._setValue(self, mlist, property, val, doc)
        else:
            GUIBase._setValue(self, mlist, property, val, doc)


    def _postValidate(self, mlist, doc):
        if not mlist.reply_to_address.strip() and \
               mlist.reply_goes_to_list == 2:
            # You can't go to an explicit address that is blank
            doc.addError(_("""You cannot add a Reply-To: to an explicit
            address if that address is blank.  Resetting these values."""))
            mlist.reply_to_address = ''
            mlist.reply_goes_to_list = 0

    def getValue(self, mlist, kind, varname, params):
        if varname != 'subject_prefix':
            return None
        # The subject_prefix may be Unicode
        return Utils.uncanonstr(mlist.subject_prefix, mlist.preferred_language)
