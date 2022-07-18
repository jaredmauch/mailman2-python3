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

"""MailList mixin class managing the privacy options."""

import os
import re

from Mailman import mm_cfg
from Mailman import Utils
from Mailman.i18n import _
from Mailman.Gui.GUIBase import GUIBase

try:
    True, False
except NameError:
    True = 1
    False = 0



class Privacy(GUIBase):
    def GetConfigCategory(self):
        return 'privacy', _('Privacy options...')

    def GetConfigSubCategories(self, category):
        if category == 'privacy':
            return [('subscribing', _('Subscription&nbsp;rules')),
                    ('sender',      _('Sender&nbsp;filters')),
                    ('recipient',   _('Recipient&nbsp;filters')),
                    ('spam',        _('Spam&nbsp;filters')),
                    ]
        return None

    def GetConfigInfo(self, mlist, category, subcat=None):
        if category <> 'privacy':
            return None
        # Pre-calculate some stuff.  Technically, we shouldn't do the
        # sub_cfentry calculation here, but it's too ugly to indent it any
        # further, and besides, that'll mess up i18n catalogs.
        WIDTH = mm_cfg.TEXTFIELDWIDTH
        if mm_cfg.ALLOW_OPEN_SUBSCRIBE:
            sub_cfentry = ('subscribe_policy', mm_cfg.Radio,
                           # choices
                           (_('None'),
                            _('Confirm'),
                            _('Require approval'),
                            _('Confirm and approve')),
                           0,
                           _('What steps are required for subscription?<br>'),
                           _("""None - no verification steps (<em>Not
                           Recommended </em>)<br>
                           Confirm (*) - email confirmation step required <br>
                           Require approval - require list administrator
                           Approval for subscriptions <br>
                           Confirm and approve - both confirm and approve
                           
                           <p>(*) when someone requests a subscription,
                           Mailman sends them a notice with a unique
                           subscription request number that they must reply to
                           in order to subscribe.<br>

                           This prevents mischievous (or malicious) people
                           from creating subscriptions for others without
                           their consent."""))
        else:
            sub_cfentry = ('subscribe_policy', mm_cfg.Radio,
                           # choices
                           (_('Confirm'),
                            _('Require approval'),
                            _('Confirm and approve')),
                           1,
                           _('What steps are required for subscription?<br>'),
                           _("""Confirm (*) - email confirmation required <br>
                           Require approval - require list administrator
                           approval for subscriptions <br>
                           Confirm and approve - both confirm and approve
                           
                           <p>(*) when someone requests a subscription,
                           Mailman sends them a notice with a unique
                           subscription request number that they must reply to
                           in order to subscribe.<br> This prevents
                           mischievous (or malicious) people from creating
                           subscriptions for others without their consent."""))

        # some helpful values
        admin = mlist.GetScriptURL('admin')

        subscribing_rtn = [
            _("""This section allows you to configure subscription and
            membership exposure policy.  You can also control whether this
            list is public or not.  See also the
            <a href="%(admin)s/archive">Archival Options</a> section for
            separate archive-related privacy settings."""),

            _('Subscribing'),
            ('advertised', mm_cfg.Radio, (_('No'), _('Yes')), 0,
             _("""Advertise this list when people ask what lists are on this
             machine?""")),

            sub_cfentry,

            ('subscribe_auto_approval', mm_cfg.EmailListEx, (10, WIDTH), 1,
             _("""List of addresses (or regexps) whose subscriptions do not
             require approval."""),

             (_("""When subscription requires approval, addresses in this list
             are allowed to subscribe without administrator approval. Add
             addresses one per line. You may begin a line with a ^ character
             to designate a (case insensitive) regular expression match.""")
             + ' ' +
             _("""You may also use the @listname notation to designate the
             members of another list in this installation."""))),

            ('unsubscribe_policy', mm_cfg.Radio, (_('No'), _('Yes')), 0,
             _("""Is the list moderator's approval required for unsubscription
             requests?  (<em>No</em> is recommended)"""),

             _("""When members want to leave a list, they will make an
             unsubscription request, either via the web or via email.
             Normally it is best for you to allow open unsubscriptions so that
             users can easily remove themselves from mailing lists (they get
             really upset if they can't get off lists!).

             <p>For some lists though, you may want to impose moderator
             approval before an unsubscription request is processed.  Examples
             of such lists include a corporate mailing list that all employees
             are required to be members of.""")),

            _('Ban list'),
            ('ban_list', mm_cfg.EmailListEx, (10, WIDTH), 1,
             _("""List of addresses which are banned from membership in this
             mailing list."""),

             _("""Addresses in this list are banned outright from subscribing
             to this mailing list, with no further moderation required.  Add
             addresses one per line; start the line with a ^ character to
             designate a regular expression match.""")),

            _("Membership exposure"),
            ('private_roster', mm_cfg.Radio,
             (_('Anyone'), _('List members'), _('List admin only')), 0,
             _('Who can view subscription list?'),

             _("""When set, the list of subscribers is protected by member or
             admin password authentication.""")),

            ('obscure_addresses', mm_cfg.Radio, (_('No'), _('Yes')), 0,
             _("""Show member addresses so they're not directly recognizable
             as email addresses?"""),
             _("""Setting this option causes member email addresses to be
             transformed when they are presented on list web pages (both in
             text and as links), so they're not trivially recognizable as
             email addresses.  The intention is to prevent the addresses
             from being snarfed up by automated web scanners for use by
             spammers.""")),
            ]

        adminurl = mlist.GetScriptURL('admin', absolute=1)
    
        if mlist.dmarc_quarantine_moderation_action:
            quarantine = _('/Quarantine')
        else:
            quarantine = ''
        sender_rtn = [
            _("""When a message is posted to the list, a series of
            moderation steps are taken to decide whether a moderator must
            first approve the message or not.  This section contains the
            controls for moderation of both member and non-member postings.

            <p>Member postings are held for moderation if their
            <b>moderation flag</b> is turned on.  You can control whether
            member postings are moderated by default or not.

            <p>Non-member postings can be automatically
            <a href="?VARHELP=privacy/sender/accept_these_nonmembers"
            >accepted</a>,
            <a href="?VARHELP=privacy/sender/hold_these_nonmembers">held for
            moderation</a>,
            <a href="?VARHELP=privacy/sender/reject_these_nonmembers"
            >rejected</a> (bounced), or
            <a href="?VARHELP=privacy/sender/discard_these_nonmembers"
            >discarded</a>,
            either individually or as a group.  Any
            posting from a non-member who is not explicitly accepted,
            rejected, or discarded, will have their posting filtered by the
            <a href="?VARHELP=privacy/sender/generic_nonmember_action">general
            non-member rules</a>.

            <p>In the text boxes below, add one address per line; start the
            line with a ^ character to designate a <a href=
            "https://docs.python.org/2/library/re.html"
            >Python regular expression</a>.  When entering backslashes, do so
            as if you were using Python raw strings (i.e. you generally just
            use a single backslash).

            <p>Note that non-regexp matches are always done first."""),

            _('Member filters'),

            ('default_member_moderation', mm_cfg.Radio, (_('No'), _('Yes')),
             0, _('By default, should new list member postings be moderated?'),

             _("""Each list member has a <em>moderation flag</em> which says
             whether messages from the list member can be posted directly to
             the list, or must first be approved by the list moderator.  When
             the moderation flag is turned on, list member postings must be
             approved first.  You, the list administrator can decide whether a
             specific individual's postings will be moderated or not.

             <p>When a new member is subscribed, their initial moderation flag
             takes its value from this option.  Turn this option off to accept
             member postings by default.  Turn this option on to, by default,
             moderate member postings first.  You can always manually set an
             individual member's moderation bit by using the
             <a href="%(adminurl)s/members">membership management
             screens</a>.""")),

            ('member_verbosity_threshold', mm_cfg.Number, 5, 0,
             _("""Ceiling on acceptable number of member posts, per interval,
               before automatic moderation."""),

             _("""If a member posts this many times, within a period of time
               the member is automatically moderated.  Use 0 to disable.  See
               <a href="?VARHELP=privacy/sender/member_verbosity_interval"
               >member_verbosity_interval</a> for details on the time period.

               <p>This is intended to stop people who join a list or lists and
               then use a bot to send many spam messages in a short interval.

               <p>Be careful when using this setting.  If it is set too low,
               this can be triggered by a single post cross-posted to
               multiple lists or by a single post to an umbrella list.""")),

            ('member_verbosity_interval', mm_cfg.Number, 5, 0,
             _("""Number of seconds to remember posts to this list to determine
               member_verbosity_threshold for automatic moderation of a
               member."""),

             _("""If a member's total posts to all lists in this installation
               with member_verbosity_threshold enabled reaches this list's
               member_verbosity_threshold, the member is automatically
               moderated on this list.

               <p>Posts which are counted towards this list's
               member_verbosity_threshold are all posts to any list with
               member_verbosity_threshold enabled that arrived within that
               list's member_verbosity_interval.""")),

            ('member_moderation_action', mm_cfg.Radio,
             (_('Hold'), _('Reject'), _('Discard')), 0,
             _("""Action to take when a moderated member posts to the
             list."""),
             _("""<ul><li><b>Hold</b> -- this holds the message for approval
             by the list moderators.

             <p><li><b>Reject</b> -- this automatically rejects the message by
             sending a bounce notice to the post's author.  The text of the
             bounce notice can be <a
             href="?VARHELP=privacy/sender/member_moderation_notice"
             >configured by you</a>.

             <p><li><b>Discard</b> -- this simply discards the message, with
             no notice sent to the post's author.
             </ul>""")),

            ('member_moderation_notice', mm_cfg.Text, (10, WIDTH), 1,
             _("""Text to include in any
             <a href="?VARHELP/privacy/sender/member_moderation_action"
             >rejection notice</a> to
             be sent to moderated members who post to this list.""")),

            ('dmarc_moderation_action', mm_cfg.Radio,
             (_('Accept'), _('Munge From'), _('Wrap Message'), _('Reject'),
                 _('Discard')), 0,
             _("""Action to take when anyone posts to the
             list from a domain with a DMARC Reject%(quarantine)s Policy."""),

             _("""<ul><li><b>Munge From</b> -- applies the <a
             href="?VARHELP=general/from_is_list">from_is_list Munge From</a>
             transformation to these messages.

             <p><li><b>Wrap Message</b> -- applies the <a
             href="?VARHELP=general/from_is_list">from_is_list Wrap
             Message</a> transformation to these messages.

             <p><li><b>Reject</b> -- this automatically rejects the message by
             sending a bounce notice to the post's author.  The text of the
             bounce notice can be <a
             href="?VARHELP=privacy/sender/dmarc_moderation_notice"
             >configured by you</a>.

             <p><li><b>Discard</b> -- this simply discards the message, with
             no notice sent to the post's author.
             </ul>

             <p>This setting takes precedence over the <a
             href="?VARHELP=general/from_is_list"> from_is_list</a> setting
             if the message is From: an affected domain and the setting is
             other than Accept.""")),

            ('dmarc_quarantine_moderation_action', mm_cfg.Radio,
             (_('No'), _('Yes')), 0,
             _("""Shall the above dmarc_moderation_action apply to messages
               From: domains with DMARC p=quarantine as well as p=reject"""),

             _("""<ul><li><b>No</b> -- this applies dmarc_moderation_action to
               only those posts From: a domain with DMARC p=reject.  This is
               appropriate if you are concerned about bounced messages, but
               want to apply dmarc_moderation_action to as few messages as
               possible.
               <p><li><b>Yes</b> -- this applies dmarc_moderation_action to
               posts From: a domain with DMARC p=reject or p=quarantine.
               </ul><p>If a message is From: a domain with DMARC p=quarantine
               and dmarc_moderation_action is not applied (this set to No)
               the message will likely not bounce, but will be delivered to
               recipients' spam folders or other hard to find places.""")),

            ('dmarc_none_moderation_action', mm_cfg.Radio,
             (_('No'), _('Yes')), 0,
             _("""Shall the above dmarc_moderation_action apply to messages
               From: domains with DMARC p=none as well as p=quarantine and
               p=reject"""),

             _("""<ul><li><b>No</b> -- this applies dmarc_moderation_action to
               only those posts From: a domain with DMARC p=reject and
               possibly p=quarantine depending on the setting of
               dmarc_quarantine_moderation_action.
               <p><li><b>Yes</b> -- this applies dmarc_moderation_action to
               posts From: a domain with DMARC p=none if
               dmarc_moderation_action is Munge From or Wrap Message and
               dmarc_quarantine_moderation_action is Yes.
               <p>The intent of this setting is to eliminate failure reports
               to the owner of a domain that publishes DMARC p=none by applying
               the message transformations that would be applied if the
               domain's DMARC policy were stronger.""")),

            ('dmarc_moderation_notice', mm_cfg.Text, (10, WIDTH), 1,
             _("""Text to include in any
             <a href="?VARHELP=privacy/sender/dmarc_moderation_action"
             >rejection notice</a> to
             be sent to anyone who posts to this list from a domain
             with a DMARC Reject%(quarantine)s Policy.""")),
             
            ('dmarc_moderation_addresses', mm_cfg.EmailListEx, (10, WIDTH), 1,
             _("""List of addresses (or regexps) whose posts should always apply
             <a href="?VARHELP=privacy/sender/dmarc_moderation_action"
             >dmarc_moderation_action</a> 
             regardless of any domain specific DMARC Policy."""),

             _("""Postings from any of these addresses will automatically
             apply any DMARC action mitigation.  This can be utilized to
             automatically wrap or munge postings from known addresses or
             domains that might have policies rejecting external mail From:
             themselves.

             <p>Add member addresses one per line; start the line with a ^
             character to designate a regular expression match.""")),
             
            ('dmarc_wrapped_message_text', mm_cfg.Text, (10, WIDTH), 1,
             _("""If dmarc_moderation_action applies and is Wrap Message,
             and this text is provided, the text will be placed in a
             separate text/plain MIME part preceding the original message
             part in the wrapped message."""),
             
             _("""A wrapped message will either be a multipart/mixed message
             with up to four sub-parts; a text/plain part containing
             msg_header, a text/plain part containing 
             dmarc_wrapped_message_text, a message/rfc822 part containing the
             original message and a text/plain part containing msg_footer, or
             a message/rfc822 message containing only the original message if
             none of the other parts are applicable.""")),

            ('equivalent_domains', mm_cfg.Text, (10, WIDTH), 1,
             _("""A 'two dimensional' list of email address domains which are
               considered equivalent when checking if a post is from a list
               member."""),

             _("""If two poster addresses with the same local part but
               different domains are to be considered equivalents for list
               membership tests, the domains are put here.  The format is
               one or more groups of equivalent domains.  Within a group,
               the domains are separated by commas and multiple groups are
               separated by semicolons. White space is ignored.
               <p>For example:<pre>
               example.com,mail.example.com;mac.com,me.com,icloud.com
               </pre>
               <p>In this example, if user@example.com is a list member,
               a post from user@mail.example.com will be treated as if it is
               from user@example.com for list membership/moderation purposes,
               and likewise, if user@me.com is a list member, posts from
               user@mac.com or user@icloud.com will be treated as if from
               user@me.com.
               <p>Note that the poster's address is first tested for list
               membership, and the equivalent domain addresses are only tested
               if the poster's address is not that of a member.
               <p>Also note that moderation of the equivalent domain address
               will apply to the post, but other options such as 'ack' or
               'not&nbsp;metoo' will not.""")),

            _('Non-member filters'),

            ('accept_these_nonmembers', mm_cfg.EmailListEx, (10, WIDTH), 1,
             _("""List of non-member addresses whose postings should be
             automatically accepted."""),

             # XXX Needs to be reviewed for list@domain names.  Also, the
             # implementation allows the @listname to work in all
             # *_these_nonmembers. It doesn't make much sense in the others,
             # but it could be useful. Should we mention it?
             _("""Postings from any of these non-members will be automatically
             accepted with no further moderation applied.  Add member
             addresses one per line; start the line with a ^ character to
             designate a regular expression match.  A line consisting of
             the @ character followed by a list name specifies another
             Mailman list in this installation, all of whose member
             addresses will be accepted for this list.""")),

            ('hold_these_nonmembers', mm_cfg.EmailListEx, (10, WIDTH), 1,
             _("""List of non-member addresses whose postings will be
             immediately held for moderation."""),

             _("""Postings from any of these non-members will be immediately
             and automatically held for moderation by the list moderators.
             The sender will receive a notification message which will allow
             them to cancel their held message.  Add member addresses one per
             line; start the line with a ^ character to designate a regular
             expression match.""")),

            ('reject_these_nonmembers', mm_cfg.EmailListEx, (10, WIDTH), 1,
             _("""List of non-member addresses whose postings will be
             automatically rejected."""),

             _("""Postings from any of these non-members will be automatically
             rejected.  In other words, their messages will be bounced back to
             the sender with a notification of automatic rejection.  This
             option is not appropriate for known spam senders; their messages
             should be
             <a href="?VARHELP=privacy/sender/discard_these_nonmembers"
             >automatically discarded</a>.

             <p>Add member addresses one per line; start the line with a ^
             character to designate a regular expression match.""")),

            ('discard_these_nonmembers', mm_cfg.EmailListEx, (10, WIDTH), 1,
             _("""List of non-member addresses whose postings will be
             automatically discarded."""),

             _("""Postings from any of these non-members will be automatically
             discarded.  That is, the message will be thrown away with no
             further processing or notification.  The sender will not receive
             a notification or a bounce, however the list moderators can
             optionally <a href="?VARHELP=privacy/sender/forward_auto_discards"
             >receive copies of auto-discarded messages.</a>.

             <p>Add member addresses one per line; start the line with a ^
             character to designate a regular expression match.""")),

            ('generic_nonmember_action', mm_cfg.Radio,
             (_('Accept'), _('Hold'), _('Reject'), _('Discard')), 0,
             _("""Action to take for postings from non-members for which no
             explicit action is defined."""),

             _("""When a post from a non-member is received, the message's
             sender is matched against the list of explicitly
             <a href="?VARHELP=privacy/sender/accept_these_nonmembers"
             >accepted</a>,
             <a href="?VARHELP=privacy/sender/hold_these_nonmembers">held</a>,
             <a href="?VARHELP=privacy/sender/reject_these_nonmembers"
             >rejected</a> (bounced), and
             <a href="?VARHELP=privacy/sender/discard_these_nonmembers"
             >discarded</a> addresses.  If no match is found, then this action
             is taken.""")),

            ('forward_auto_discards', mm_cfg.Radio, (_('No'), _('Yes')), 0,
             _("""Should messages from non-members, which are automatically
             discarded, be forwarded to the list moderator?""")),

            ('nonmember_rejection_notice', mm_cfg.Text, (10, WIDTH), 1,
             _("""Text to include in any rejection notice to be sent to
             non-members who post to this list. This notice can include
             the list's owner address by %%(listowner)s and replaces the
             internally crafted default message.""")),

            ]

        recip_rtn = [
            _("""This section allows you to configure various filters based on
            the recipient of the message."""),

            _('Recipient filters'),

            ('require_explicit_destination', mm_cfg.Radio,
             (_('No'), _('Yes')), 0,
             _("""Must posts have list named in destination (to, cc) field
             (or be among the acceptable alias names, specified below)?"""),

             _("""Many (in fact, most) spams do not explicitly name their
             myriad destinations in the explicit destination addresses - in
             fact often the To: field has a totally bogus address for
             obfuscation.  The constraint applies only to the stuff in the
             address before the '@' sign, but still catches all such spams.

             <p>The cost is that the list will not accept unhindered any
             postings relayed from other addresses, unless

             <ol>
                 <li>The relaying address has the same name, or

                 <li>The relaying address name is included on the options that
                 specifies acceptable aliases for the list.

             </ol>""")),

            ('acceptable_aliases', mm_cfg.Text, (4, WIDTH), 0,
             _("""Alias names (regexps) which qualify as explicit to or cc
             destination names for this list."""),

             _("""Alternate addresses that are acceptable when
             `require_explicit_destination' is enabled.  This option takes a
             list of regular expressions, one per line, which is matched
             against every recipient address in the message.  The matching is
             performed with Python's re.match() function, meaning they are
             anchored to the start of the string.
             
             <p>For backwards compatibility with Mailman 1.1, if the regexp
             does not contain an `@', then the pattern is matched against just
             the local part of the recipient address.  If that match fails, or
             if the pattern does contain an `@', then the pattern is matched
             against the entire recipient address.
             
             <p>Matching against the local part is deprecated; in a future
             release, the pattern will always be matched against the entire
             recipient address.""")),

            ('max_num_recipients', mm_cfg.Number, 5, 0,
             _('Ceiling on acceptable number of recipients for a posting.'),

             _("""If a posting has this number, or more, of recipients, it is
             held for admin approval.  Use 0 for no ceiling.""")),
            ]

        spam_rtn = [
            _("""This section allows you to configure various anti-spam
            filters posting filters, which can help reduce the amount of spam
            your list members end up receiving.
            """),

            _('Header filters'),

            ('header_filter_rules', mm_cfg.HeaderFilter, 0, 0,
             _('Filter rules to match against the headers of a message.'),

             _("""Each header filter rule has two parts, a list of regular
             expressions, one per line, and an action to take.  Mailman
             matches the message's headers against every regular expression in
             the rule and if any match, the message is rejected, held, or
             discarded based on the action you specify.  Use <em>Defer</em> to
             temporarily disable a rule.

             You can have more than one filter rule for your list.  In that
             case, each rule is matched in turn, with processing stopped after
             the first match.

             Note that headers are collected from all the attachments
             (except for the mailman administrivia message) and
             matched against the regular expressions. With this feature,
             you can effectively sort out messages with dangerous file
             types or file name extensions.""")),

            _('Legacy anti-spam filters'),

            ('bounce_matching_headers', mm_cfg.Text, (6, WIDTH), 0,
             _('Hold posts with header value matching a specified regexp.'),
             _("""Use this option to prohibit posts according to specific
             header values.  The target value is a regular-expression for
             matching against the specified header.  The match is done
             disregarding letter case.  Lines beginning with '#' are ignored
             as comments.

             <p>For example:<pre>to: .*@public.com </pre> says to hold all
             postings with a <em>To:</em> mail header containing '@public.com'
             anywhere among the addresses.

             <p>Note that leading whitespace is trimmed from the regexp.  This
             can be circumvented in a number of ways, e.g. by escaping or
             bracketing it.""")),
          ]

        if subcat == 'sender':
            return sender_rtn
        elif subcat == 'recipient':
            return recip_rtn
        elif subcat == 'spam':
            return spam_rtn
        else:
            return subscribing_rtn

    def _setValue(self, mlist, property, val, doc):
        # Ignore any hdrfilter_* form variables
        if property.startswith('hdrfilter_'):
            return
        # For subscribe_policy when ALLOW_OPEN_SUBSCRIBE is true, we need to
        # add one to the value because the page didn't present an open list as
        # an option.
        if property == 'subscribe_policy' and not mm_cfg.ALLOW_OPEN_SUBSCRIBE:
            val += 1
        if (property == 'dmarc_moderation_action' and
                val < mm_cfg.DEFAULT_DMARC_MODERATION_ACTION):
            doc.addError(_("""dmarc_moderation_action must be >= the configured
                           default value."""))
            val = mm_cfg.DEFAULT_DMARC_MODERATION_ACTION
        setattr(mlist, property, val)

    # We need to handle the header_filter_rules widgets specially, but
    # everything else can be done by the base class's handleForm() method.
    # However, to do this we need an awful hack.  _setValue() and
    # _getValidValue() will essentially ignore any hdrfilter_* form variables.
    # TK: we should call this function only in subcat == 'spam'
    def _handleForm(self, mlist, category, subcat, cgidata, doc):
        # TK: If there is no hdrfilter_* in cgidata, we should not touch
        # the header filter rules.
        if not cgidata.has_key('hdrfilter_rebox_01'):
            return
        # First deal with
        rules = []
        # We start i at 1 and keep going until we no longer find items keyed
        # with the marked tags.
        i = 1
        downi = None
        while True:
            deltag    = 'hdrfilter_delete_%02d' % i
            reboxtag  = 'hdrfilter_rebox_%02d' % i
            actiontag = 'hdrfilter_action_%02d' % i
            wheretag  = 'hdrfilter_where_%02d' % i
            addtag    = 'hdrfilter_add_%02d' % i
            newtag    = 'hdrfilter_new_%02d' % i
            uptag     = 'hdrfilter_up_%02d' % i
            downtag   = 'hdrfilter_down_%02d' % i
            i += 1
            # Was this a delete?  If so, we can just ignore this entry
            if cgidata.has_key(deltag):
                continue
            # Get the data for the current box
            pattern = cgidata.getfirst(reboxtag)
            try:
                action  = int(cgidata.getfirst(actiontag))
                # We'll get a TypeError when the actiontag is missing and the
                # .getvalue() call returns None.
            except (ValueError, TypeError):
                action = mm_cfg.DEFER
            if pattern is None:
                # We came to the end of the boxes
                break
            if cgidata.has_key(newtag) and not pattern:
                # This new entry is incomplete.
                if i == 2:
                    # OK it is the first.
                    continue
                doc.addError(_("""Header filter rules require a pattern.
                Incomplete filter rules will be ignored."""))
                continue
            # Make sure the pattern was a legal regular expression.
            # Convert it to unicode if necessary.
            mo = re.match('.*charset=([-_a-z0-9]+)',
                          os.environ.get('CONTENT_TYPE', ''),
                          re.IGNORECASE
                         )
            if mo:
                cset = mo.group(1)
            else:
                cset = Utils.GetCharSet(mlist.preferred_language)
            try:
                upattern = Utils.xml_to_unicode(pattern, cset)
                re.compile(upattern)
                pattern = upattern
            except (re.error, TypeError):
                safepattern = Utils.websafe(pattern)
                doc.addError(_("""The header filter rule pattern
                '%(safepattern)s' is not a legal regular expression.  This
                rule will be ignored."""))
                continue
            # Was this an add item?
            if cgidata.has_key(addtag):
                # Where should the new one be added?
                where = cgidata.getfirst(wheretag)
                if where == 'before':
                    # Add a new empty rule box before the current one
                    rules.append(('', mm_cfg.DEFER, True))
                    rules.append((pattern, action, False))
                    # Default is to add it after...
                else:
                    rules.append((pattern, action, False))
                    rules.append(('', mm_cfg.DEFER, True))
            # Was this an up movement?
            elif cgidata.has_key(uptag):
                # As long as this one isn't the first rule, move it up
                if rules:
                    rules.insert(-1, (pattern, action, False))
                else:
                    rules.append((pattern, action, False))
            # Was this the down movement?
            elif cgidata.has_key(downtag):
                downi = i - 2
                rules.append((pattern, action, False))
            # Otherwise, just retain this one in the list
            else:
                rules.append((pattern, action, False))
        # Move any down button filter rule
        if downi is not None:
            rule = rules[downi]
            del rules[downi]
            rules.insert(downi+1, rule)
        mlist.header_filter_rules = rules

    def handleForm(self, mlist, category, subcat, cgidata, doc):
        if subcat == 'spam':
            self._handleForm(mlist, category, subcat, cgidata, doc)
        # Everything else is dealt with by the base handler
        GUIBase.handleForm(self, mlist, category, subcat, cgidata, doc)
