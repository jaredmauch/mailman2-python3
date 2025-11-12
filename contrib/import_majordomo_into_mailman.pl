#!/usr/bin/perl

#
# Create Mailman list(s) from Majordomo list configuration files.
#
# main() is fully commented and provides a good outline of this script.
#
# LIMITATIONS:
#  - Archives are not currently handled.
#  - A few Majordomo configuration values are ignored (documented in the comment
#    above the getMailmanConfig() function) because they are either inactive,
#    system/constant settings, or don't tranlsate into Mailman.
#  - This script was tested against Majordomo 1.94.4/5 and Mailman 2.1.14-1.
#    Different versions of Majordomo or Mailman may not work with this script.
#    However, some legacy settings for Majordomo are handled.
#
# REQUIREMENTS/ASSUMPTIONS:
#  - Mailman is installed so that its bin/* scripts can be called.
#  - Majordomo has all of its list configurations in a single, local directory.
#  - Majordomo's aliases file exists locally.
#  - $DOMO_INACTIVITY_LIMIT set to zero or the output of Majordomo's
#    consistency_check
#    command is stored locally.
#  - Run this script as root.
#
# BEFORE RUNNING THIS SCRIPT:
#  - Change the "ENVIRONMENT-SPECIFIC VALUES" below to match your system.
#  - It is recommended to run this script with the --stats option first to get
#    a sense of your data. Fields with many 'other' or 'no value' values, or 
#    fields that don't get imported (e.g. message_headers) that have many
#    'has value' values probably need to be considered more closely.
#
# TODO: IMPORT ARCHIVE OPTION
#  - One solution: get all archives inot a 'Unix mbox' file and then use the
#    bin/arch tool. bin/cleanarch can sanity check the mbox before running
#    bin/arch.
#

use strict;
use warnings;

use Getopt::Long;
use Log::Handler;
use File::Temp qw(tempfile);
use Email::Simple;
use Email::Sender::Simple qw(try_to_sendmail);
use Data::Dump qw(dump);


#----------------------- ENVIRONMENT-SPECIFIC VALUES --------------------------#
my $DOMO_PATH              = '/opt/majordomo';
my $DOMO_LIST_DIR          = "$DOMO_PATH/lists";
my $MM_PATH                = '/usr/local/mailman';
my $DOMO_ALIASES           = "$MM_PATH/majordomo/aliases";
my $DOMO_CHECK_CONSISTENCY = "$MM_PATH/majordomo/check_consistency.txt";
my $BOUNCED_OWNERS         = "/opt/mailman-2.1.14-1/uo/majordomo/" .
                             "email_addresses_that_bounced.txt";
my $TMP_DIR                = '/tmp';
# Only import lists that have been active in the last N days.
my $DOMO_INACTIVITY_LIMIT  = 548;   # Optional.  548 days = 18 months.
# If set, overwrite Majordomo's "resend_host" and thus Mailman's "host_name".
my $NEW_HOSTNAME           = '';  # Optional
my $LANGUAGE               = 'en';  # Preferred language for all Mailman lists
my $MAX_MSG_SIZE           = 20000;  # In KB. Used for the Mailman config.
#------------------------------------------------------------------------------#

#
# Global constants
#
my $MM_LIST_DIR    = "$MM_PATH/lists";
my $MM_LIST_LISTS  = "$MM_PATH/bin/list_lists";
my $MM_NEWLIST     = "$MM_PATH/bin/newlist";
my $MM_CONFIGLIST  = "$MM_PATH/bin/config_list";
my $MM_ADDMEMBERS  = "$MM_PATH/bin/add_members";
my $MM_CHECK_PERMS = "$MM_PATH/bin/check_perms";
my $SCRIPT_NAME    = $0 =~ /\/?(\b\w+\b)\.pl$/ ? $1 : '<script name>';
my $LOG_FILE       = "$TMP_DIR/$SCRIPT_NAME.log";

#
# Global namespace
#
my $log = Log::Handler->new();
my $domoStats = {
   'general_stats' => {
      'Lists without owner in aliases' => 0,
      'Total lists' => 0
    }
};


#
# Main program execution
#
main();


#
# Functions
#
sub main {
   # Verify the environment.
   preImportChecks();
   # Get the CLI options.
   my $opts = getCLIOpts();
   # Set up logging.
   addLogHandler($opts);
   # Get lists to import.
   my @domoListNames = getDomoListsToImport($opts);
   # Get a mapping of list names to list owners.
   my $listOwnerMap = getListToOwnerMap();
   # Get lists that already exist in Mailman.
   my %existingLists = getExistingLists();
   # Get all lists that have been inactive longer than the specified limit.
   my $inactiveLists = getInactiveLists();

   if ($opts->{'email_notify'}) {
      # Email Majordomo list owners about the upcoming migration to Mailman.
      sendCustomEmailsToDomoOwners($listOwnerMap, $inactiveLists, 1);
      exit;
   }

   if ($opts->{'email_test'}) {
      # Email Majordomo list owners about the upcoming migration to Mailman.
      sendCustomEmailsToDomoOwners($listOwnerMap, $inactiveLists);
      exit;
   }

   # Iterate through every list, collecting stats, and possibly importing.
   for my $listName (@domoListNames) {
      $log->info("Starting list $listName...");

      if (not exists $listOwnerMap->{$listName}) {
         $log->warning("List $listName has no owner in aliases. Skipping...");
         $domoStats->{'general_stats'}->{'Lists without owner in aliases'} += 1;
         next;
      }

      # Don't import lists that are pending deletion. This is a University of
      # Oregon customization, but it should be harmless for others.
      if (-e "$DOMO_LIST_DIR/$listName.pendel") {
         $log->info("List $listName has a .pendel file. Skipping...");
         $domoStats->{'general_stats'}->{'Lists pending deletion'} += 1;
         next;
      }

      # Don't import the list if it's been inactive beyond the specified limit.
      if (exists $inactiveLists->{$listName}) {
         $log->notice("List $listName has been inactive for " .
                      "$inactiveLists->{$listName} days. Skipping...");
         $domoStats->{'general_stats'}->{'Lists inactive for more than ' .
                                         "$DOMO_INACTIVITY_LIMIT days"} += 1;
         next;
      }

      # Get the Majordomo configuration.
      $log->info("Getting Majordomo config for list $listName...");
      my %domoConfig = getDomoConfig($listName, $listOwnerMap);
      if (not %domoConfig) {
         $log->debug("No config returned by getDomoConfig(). Skipping...");
         next;
      }

      # Add this list to the stats data structure and then skip if --stats.
      $log->debug("Appending this list's data into the stats structure...");
      appendDomoStats(%domoConfig);
      if ($opts->{'stats'}) {
         next;
      }

      # Don't import this list if it already exists in Mailman.
      if (exists $existingLists{$listName}) {
         $log->notice("$listName already exists. Skipping...");
         next;
      }

      # Get a hash of Mailman config values mapped from Majordomo.
      my %mmConfig = getMailmanConfig(%domoConfig);

      # Create the template configuration file for this list.
      my $mmConfigFilePath =
         createMailmanConfigFile($domoConfig{'approve_passwd'}, %mmConfig);

      # Create the Mailman list.
      createMailmanList($listName,
                        $mmConfig{'owner'},
                        $domoConfig{'admin_passwd'});

      # Apply the configuration template to the list.
      configureMailmanList($listName, $mmConfigFilePath);

      # Add members to the list.
      if ($opts->{'subscribers'}) {
         # Create files of digest and non-digest member emails to be used
         # when calling Mailman's bin/config_list.
         my $membersFilePath =
            createMailmanMembersList($domoConfig{'subscribers'});
         my $digestMembersFilePath =
            createMailmanMembersList($domoConfig{'digest_subscribers'});

         # Subscribe the member emails to the Mailman list.
         if ($membersFilePath or $digestMembersFilePath) {
            addMembersToMailmanList($listName,
                                    $membersFilePath,
                                    $digestMembersFilePath);
         }
      }
   }

   # Output stats if requested or else the resuls of the import.
   if ($opts->{'stats'}) {
      printStats();
   } else {
      cleanUp();
      print "Import complete!  " .
            "$domoStats->{'general_stats'}->{'Total lists'} lists imported.\n";
   }
   print "Complete log: $LOG_FILE\n";
}

# Environment/system/setting checks before modifying state
sub preImportChecks {
   # User "mailman" is required because of various calls to the mailman/bin/*
   # scripts.
   my $script_executor = $ENV{LOGNAME} || $ENV{USER} || getpwuid($<);
   if ($script_executor !~ /^(mailman|root)$/) {
      die "Error: Please run this script as user mailman (or root).\n";
   }
   # Check that the Majordomo and Mailman list directories exist.
   for my $dir ($DOMO_LIST_DIR, $MM_LIST_DIR) {
      if (not $dir or not -d $dir) {
         die "Error: Lists directory does not exist: $dir\n";
      }
   }
   # Check that the Mailman binaries exist.
   for my $bin ($MM_LIST_LISTS, $MM_NEWLIST, $MM_CONFIGLIST, $MM_ADDMEMBERS) {
      if (not $bin or not -e $bin) {
         die "Error: Mailman binary doesn't exist: $bin\n";
      }
   }
   # Check the path of $DOMO_CHECK_CONSISTENCY.
   if ($DOMO_CHECK_CONSISTENCY and not -e $DOMO_CHECK_CONSISTENCY) {
      die "Error: \$DOMO_CHECK_CONSISTENCY does not exist: " .
          "$DOMO_CHECK_CONSISTENCY\nCorrect the value or set it to ''.\n";
   }
   # If $DOMO_CHECK_CONSISTENCY exists, then so must $DOMO_ACTIVITY_LIMIT.
   if ($DOMO_CHECK_CONSISTENCY and not $DOMO_INACTIVITY_LIMIT) {
      die "Error: \$DOMO_CHECK_CONSISTENCY exists but " .
          "\$DOMO_INACTIVITY_LIMIT does not.\nPlease set this value.\n";
   }
   # $LANGUAGE must be present and should only contain a-z.
   if (not $LANGUAGE or $LANGUAGE !~ /[a-z]+/i) {
      die "Error: \$LANGUAGE was not set or invalid: $LANGUAGE\n";
   }
   # $MAX_MSG_SIZE must be present and should really be above a minimum size.
   if (not $MAX_MSG_SIZE or $MAX_MSG_SIZE < 5) {
      die "Error: \$MAX_MSG_SIZE was not set or less than 5KB: $MAX_MSG_SIZE\n";
   }
}

# Get CLI options.
sub getCLIOpts {
   my $opts = {};
   GetOptions('list=s'       => \$opts->{'list'},
              'all'          => \$opts->{'all'},
              'subscribers'  => \$opts->{'subscribers'},
              'stats'        => \$opts->{'stats'},
              'email-notify' => \$opts->{'email_notify'},
              'email-test'   => \$opts->{'email_test'},
              'loglevel=s'   => \$opts->{'loglevel'},
              'help'         => \$opts->{'help'},
   );

   if ($opts->{'help'}) {
      help();
   }

   # If --all or --list was not specified, get stats for all lists.
   if (($opts->{'stats'} or $opts->{'email_notify'} or $opts->{'email_test'})
       and not ($opts->{'all'} or $opts->{'list'})) {
      $opts->{'all'} = 1;
   }

   # Validate --loglevel.
   if ($opts->{'loglevel'}) {
      if ($opts->{'loglevel'} !~ /^(debug|info|notice|warning|error)$/) {
         print "ERROR: invalid --loglevel value: $opts->{'loglevel'}\n";
         help();
      }
   } else {
      $opts->{'loglevel'} = 'error';
   }

   return $opts;
}

sub addLogHandler {
   my $opts = shift;
   $log->add(file   => { filename => $LOG_FILE,
                         #mode     => 'trunc',
                         maxlevel => 'debug',
                         minlevel => 'emerg' },
             screen => { log_to   => 'STDOUT',
                         maxlevel => $opts->{'loglevel'},
                         minlevel => 'emerg' }
   );
}

# Return an array of all list names in Majordomo that have a <list>.config file.
sub getDomoListsToImport {
   my $opts = shift;
   my @domoListNames = ();
   # If only one list was specified, validate and return that list.
   if ($opts->{'list'}) {
      my $listConfig = $opts->{'list'} . '.config';
      my $listPath = "$DOMO_LIST_DIR/$listConfig";
      if (not -e $listPath) {
         $log->die(crit => "Majordomo list config does not exist: $listPath");
      }
      @domoListNames = ($opts->{'list'});
   # If all lists were requested, grab all list names from .config files in the
   # $DOMO_LIST_DIR, ignoring digest lists (i.e. *-digest.config files).
   } elsif ($opts->{'all'}) {
      $log->info("Collecting all Majordomo list config files...");
      opendir DIR, $DOMO_LIST_DIR or
         $log->die("Can't open dir $DOMO_LIST_DIR: $!");
      # Don't get digest lists because these are not separate lists in Mailman.
      @domoListNames = grep !/\-digest$/,
                       map { /^([a-zA-Z0-9_\-]+)\.config$/ }
                       readdir DIR;
      closedir DIR;
      if (not @domoListNames) {
         $log->die(crit => "No Majordomo configs found in $DOMO_LIST_DIR");
      }
   # If we're here, --list or --all was not used, so exit.
   } else {
      $log->error("--list=NAME or --all was not used. Nothing to do.");
      help();
   }
   return @domoListNames;
}

# Find all list owners from aliases and create a map of lists to aliases.
sub getListToOwnerMap {
   my %listOwnerMap = ();
   open ALIASES, $DOMO_ALIASES or $log->die("Can't open $DOMO_ALIASES: $!");
   while (my $line = <ALIASES>) {
      if ($line =~ /^owner\-([^:]+):\s*(.*)$/) {
         my ($listName, $listOwners) = (strip($1), strip($2));
         # Some lists in Majordomo's aliases file have the same email listed
         # twice as the list owner (e.g. womenlaw).
         # Converting the listed owners into a hash prevents duplicates.
         my %ownersHash = map { $_ => 1 } split /,/, $listOwners;
         $listOwnerMap{$listName} =
            "'" . (join "', '", keys %ownersHash) . "'";
      }
   }
   close ALIASES or $log->die("Can't close $DOMO_ALIASES: $!");

   return \%listOwnerMap;
}

# Return a hash of all lists that already exist in Mailman.
sub getExistingLists {
   my $cmd = "$MM_LIST_LISTS -b";
   $log->debug("Calling $cmd...");
   my %lists = map { strip($_) => 1 } `$cmd` or $log->die("Command failed: $!");
   return %lists;
}

# By parsing the output of Majordomo's "consistency_check" command, get a list
# of all Majordomo lists inactive beyond the specified $DOMO_INACTIVITY_LIMIT.
sub getInactiveLists {
   my %lists = ();
   if ($DOMO_CHECK_CONSISTENCY) {
      for my $line (split /\n/, getFileTxt($DOMO_CHECK_CONSISTENCY)) {
           
         if ($line =~ /(\S+) has been inactive for (\d+) days/) {
            if ($2 >= $DOMO_INACTIVITY_LIMIT) {
               $lists{$1} = $2;
            }
         }
      }
   }

   return \%lists;
}

sub getBouncedOwners {
   my @bouncedOwners = ();
   for my $line (split /\n/, getFileTxt($BOUNCED_OWNERS)) {
      if ($line =~ /Failed to send mail to (\S+)\./) {
         push @bouncedOwners, $1;
      }
   }

   return @bouncedOwners;
}

sub getOwnerListMap {
   my ($listOwnerMap, $inactiveLists) = @_;
   my $ownerListMap = {};

   for my $list (keys %$listOwnerMap) {
      for my $owner (split /,/, $listOwnerMap->{$list}) {
         my $type = exists $inactiveLists->{$list} ? 'inactive' : 'active';
         my $owner = strip($owner, { full => 1 });
         push @{$ownerListMap->{$owner}->{$type}}, $list;
      }
   }

   return $ownerListMap;
}

# Send an individualized email to each Majordomo list owner that details
# the upcoming migration procedure and their active and inactive lists.
sub sendCustomEmailsToDomoOwners {
   my ($listOwnerMap, $inactiveLists, $notify) = @_;
   $notify = 0 if not defined $notify;

   print "Send email to all Majordomo list owners?\n";
   my $answer = '';
   while ($answer !~ /^(yes|no)$/) {
      print "Please type 'yes' or 'no':  ";
      $answer = <>;
      chomp $answer;
   }

   if ($answer ne "yes") {
      print "No emails were sent.\n";
      return;
   }

   # Create the body of the email for each owner
   my $ownerListMap = getOwnerListMap($listOwnerMap, $inactiveLists);
   for my $owner (keys %$ownerListMap) {
      my $body = $notify ? getEmailNotifyText('top') : getEmailTestText('top');

      # Append the active and inactive lists section to the email body
      my $listsTxt = '';
      for my $listType (qw(active inactive)) {
         if ($ownerListMap->{$owner}->{$listType} and
             @{$ownerListMap->{$owner}->{$listType}}) {
            $listsTxt .= $notify ?
                         getEmailNotifyText($listType,
                            $ownerListMap->{$owner}->{$listType},
                            $inactiveLists) :
                         getEmailTestText($listType,
                            $ownerListMap->{$owner}->{$listType},
                            $inactiveLists);
         }
      }

      if (not $listsTxt) {
         $log->warning("No active or inactive lists found for owner $owner");
         next;
      }

      $body .= $listsTxt;
      $body .= $notify ? getEmailNotifyText('bottom') :
                         getEmailTestText('bottom');

      # Create and send the email.
      my $email = Email::Simple->create(
         header => [ 'From'     => 'listmaster@lists-test.uoregon.edu',
                     'To'       => $owner,
                     'Reply-To' => 'listmaster@lists.uoregon.edu',
                     'Subject'  => 'Mailman (Majordomo replacement) ready ' .
                                   'for testing' ],
         body   => $body,
      );
      $log->debug("Sending notification email to $owner...");
      my $result = try_to_sendmail($email);
      if (not defined $result) {
         $log->notice("Failed to send mail to $owner.");
      }
   }
}

# Return various sections, as requested, of a notification email for
# current Majordomo list owners.
sub getEmailNotifyText {
   my ($section, $lists, $inactiveLists) = @_;

   if ($section eq 'top') {
      return <<EOF;
Greetings;

You are receiving this email because you have been identified as the owner
of one or more Majordomo lists. We're in the process of informing all
current Majordomo list owners of an impending change in the list
processing software used by the University.

Majordomo, the current list processing software in use by the University,
hasn't been updated by its developers since January of 2000. The versions
of the underlying software as well as the operating system required by
Majordomo are no longer supported or maintained. As a result, we are
implementing a new list processing software for the UO campus.

Mailman (http://www.gnu.org/software/mailman/index.html) has been
identified as a robust replacement for Majordomo. Mailman has a Web-based
interface as well as a set of commands issued through email. There is a
wide array of configuration options for the individual lists. Connections
to the Mailman services through the Web are secured with SSL, and
authentication into the server will be secured with LDAP, tied to the
DuckID information.

Information Services is currently in the process of configuring the
Mailman server and testing the list operations. Our plan is to have the
service ready for use very soon, with existing lists showing activity
within the last 18 months being migrated seamlessly onto the new service.

EOF
   } elsif ($section eq 'active') {
      my $listTxt = join "\n", sort @$lists;
      return <<EOF;
Our records indicate that you are the owner of the following lists:

$listTxt

These lists have had activity in the last 18 months, indicating that they
may still be active. Unless you contact us, these active lists will be
automatically migrated to Mailman.

If you no longer want to keep any of the lists shown above, please send
email to email indicating the name(s) of the list, and
that you'd like the list(s) ignored during our migration procedure. You
could also go to the Web page http://lists.uoregon.edu/delap.html and delete the
list prior to the migration.

EOF
   } elsif ($section eq 'inactive') {
      my $listTxt = join "\n",
                    map { my $years = int($inactiveLists->{$_} / 365);
                          $years = $years == 1 ? "$years year" : "$years years";
                          my $days = $inactiveLists->{$_} % 365;
                          $days = $days == 1 ? "$days day" : "$days days";
                          "$_ (inactive $years, $days)" } sort @$lists;
      return <<EOF;
The following lists, for which you are the owner, have had no activity in
the last 18 months:

$listTxt

Lists with no activity within the last 18 months will not be migrated, and
will be unavailable after the migration to Mailman.

If you want to retain one of the lists detailed above, you can either send
a message to the list (which will update its activity), or send an email
to email with an explanation as to why this
inactive list should be migrated and maintained.

EOF
   } elsif ($section eq 'bottom') {
      return <<EOF;
There should be only brief interruptions in service during the migration.
The change to Mailman will be scheduled to take place during the standard
Tuesday maintenance period between 5am and 7am. If your list has been
migrated to Mailman, you and your subscribers will be able to send posts
to your list with the same address, and those messages will be distributed
in the same way that Majordomo would.

Documentation on using Mailman is in development, and will be available to
facilitate the list migration. We will be making a test environment
available to the list owners prior to the migration to the production
Mailman server, so that you can see the settings for your lists and become
accustomed to the new Web interface.

If you have any questions or concerns, please contact email,
or email me directly at email.
   }

   return '';
}

sub getEmailTestText {
   my ($section, $lists, $inactiveLists) = @_;

   if ($section eq 'top') {
      return <<EOF;
Greetings;

Information Services is in the process of configuring Mailman as the UO list
processing software, replacing Majordomo. You're receiving this email because
you have been identified as the owner of one or more lists, either active or
inactive.

The final migration from Majordomo to Mailman is scheduled to take place on
April 27th, 2012. At that time, your active lists will be migrated to the new
service, and lists.uoregon.edu will begin using Mailman instead of Majordomo
for its list processing. This move should be seamless with no downtime.

We have set up a test server and would greatly appreciate your feedback.
Specifically, we'd like to know:
- how well the Majordomo settings for your lists were translated into Mailman
- whether Mailman works as you'd expect and, if not, what didn't work
- any other thoughts, concerns, responses, etc you have

The test server is at https://domain. You will encounter a
browser warning about an "unsigned certificate".  This is expected: please
add an exception for the site.  Once Mailman goes live, however, there will
be a signed certificate in place and this issue will not exist.

All of your Majordomo list settings were migrated onto the Mailman test server
except for subscribers.  We did not import subscribers so that you could test
the email functionality of Mailman for your list.  To do so, subscribe your
email address to your list, and perhaps a few other people who might be
interested in testing Mailman (fellow moderators?), and send off a few emails.

The links to your specific Mailman lists are listed below.  Please feel free
to change anything you want: it is a test server, after all, and we'd love
for you to thoroughly test Mailman.  Anything you change, however, will not
show up on the production server when it goes live.  The production server
will create your Majordomo lists in Mailman based on the Majordomo settings
for your list on April 27, 2012.

The password for these lists will be the same as the adminstrative password
used on the Majordomo list. If you forgot that password, email
email and we will reply with the owner information for
your list, including passwords and a subset of the current list configuration.
EOF
   } elsif ($section eq 'active') {
      my $listTxt = join "\n",
                    map { "https://domain/mailman/listinfo/$_" }
                    sort @$lists;
      return <<EOF;

----

Here is the list of your active lists:

$listTxt

EOF
   } elsif ($section eq 'inactive') {
      my $listTxt = join "\n",
                    map { my $years = int($inactiveLists->{$_} / 365);
                          $years = $years == 1 ? "$years year" : "$years years";
                          my $days = $inactiveLists->{$_} % 365;
                          $days = $days == 1 ? "$days day" : "$days days";
                          "$_ (inactive $years, $days)" } sort @$lists;
      return <<EOF;
----

Here is the list of your inactive lists (no activity in the last 18 months):

$listTxt

Lists with no activity within the last 18 months will not be migrated, and
will be unavailable after the migration to Mailman.

If you want to retain one of the lists detailed above, you can either send
a message to the list (which will update its activity), or send an email
to email with an explanation as to why this
inactive list should be migrated and maintained.

----

EOF
   } elsif ($section eq 'bottom') {
      return <<EOF;
We are continuing to develop documentation for Mailman, which you can access
through the IT Web site. That documentation is still a work in progress;
the final versions have yet to be published. User and administrator guides for
Mailman can be found at http://it.uoregon.edu/mailman. We'll provide further
UO-specific documentation as it becomes available.

It is our intention that this transition proceed smoothly, with a minimum of
disruption in services for you. Please report any problems or concerns to
email, and we will address your concerns as quickly as
possible.
EOF
   }
}


# Parse all text configuration files for a Majordomo list and return that
# info in the %config hash with fields as keys and field values
# as hash values.  Example: {'subscribe_policy' => 'open'}.
# Note that every text configuration file is parsed, not just <listname>.config.
# So, for example, <listname>.post is added to %config as
# {'restrict_post_emails': 'email1,email2,...'}. The following files
# are examined: listname, listname.info, listname.intro, listname.config,
# listname.post, listname.moderator, listname-digest, listname-digest.config,
# listname.closed, listname.private, listname.auto, listname.passwd,
# listname.strip, and listname.hidden.
sub getDomoConfig {
   my ($listName, $listOwnerMap) = @_;
   my $listPath = "$DOMO_LIST_DIR/$listName";
   # All of these values come from <listname>.config unless a comment
   # says otherwise.
   my %config = (
      'admin_passwd'           => '',  # from the list config or passwd files
      'administrivia'          => '',
      'advertise'              => '',
      'aliases_owner'          => $listOwnerMap->{$listName},
      'announcements'          => 'yes',
      'approve_passwd'         => '',
      'description'            => "$listName Mailing List",
      'digest_subscribers'     => '',
      'get_access'             => '',
      'index_access'           => '',
      'info_access'            => '',
      'intro_access'           => '',
      'info'                   => '',  # from the <listname>.info file
      'intro'                  => '',  # from the <listname>.intro file
      'list_name'              => $listName,
      'message_footer'         => '',
      'message_footer_digest'  => '',  # from the <listname>-digest.config file
      'message_fronter'        => '',
      'message_fronter_digest' => '',  # from the <listname>-digest.config file
      'message_headers'        => '',
      'moderate'               => 'no',
      'moderator'              => '',
      'moderators'             => '',  # from the emails in <listname>.moderator
      'noadvertise'            => '',
      'post_access'            => '',
      'reply_to'               => '',
      'resend_host'            => '',
      'restrict_post'          => '',
      'restrict_post_emails'   => '',  # from the emails in restrict_post files
      'strip'                  => '',
      'subject_prefix'         => '',
      'subscribe_policy'       => '',
      'subscribers'            => '',  # from the emails in the <listname> file
      'taboo_body'             => '',
      'taboo_headers'          => '',
      'unsubscribe_policy'     => '',
      'welcome'                => 'yes',
      'which_access'           => '',
      'who_access'             => '',
   );

   # Parse <listname>.config for list configuration options
   my $configPath = "$listPath.config";
   open CONFIG, $configPath or $log->die("Can't open $configPath: $!");
   while (my $line = <CONFIG>) {
      # Pull out the config field and its value.
      if ($line =~ /^\s*([^#\s]+)\s*=\s*(.+)\s*$/) {
         my ($option, $value) = ($1, $2);
         $config{$option} = $value;
      # Some config option values span multiple lines.
      } elsif ($line =~ /^\s*([^#\s]+)\s*<<\s*(\b\S+\b)\s*$/) {
         my ($option, $heredocTag) = ($1, $2);
         while (my $line = <CONFIG>) {
            last if $line =~ /^$heredocTag\s*$/;
            $config{$option} .= $line;
         }
      }
   }

   # Parse <listname> for subscribers
   my @subscribers = getFileEmails($listPath);
   $config{'subscribers'} = join ',', @subscribers;

   # Parse <listname>-digest for digest subscribers
   my @digestSubscribers = getFileEmails("$listPath-digest");
   $config{'digest_subscribers'} = join ',', @digestSubscribers;

   # Parse filenames listed in restrict_post for emails with post permissions
   if ($config{'restrict_post'}) {
      my @postPermissions = ();
      for my $restrictPostFilename (split /[\s:]/, $config{'restrict_post'}) {
         # No need to be explicit in Mailman about letting members post to the
         # list because it is the default behavior.
         if ($restrictPostFilename eq $listName) {
            next;
         }

         # If posting is restricted to another list, use Mailman's shortcut
         # reference of '@<listname>' instead of adding those emails
         # individually.
         if ($restrictPostFilename !~ /\-digest$/ and
             exists $listOwnerMap->{$restrictPostFilename}) {
            $log->info("Adding '\@$restrictPostFilename' shortcut list " .
                       "reference to restrict_post_emails...");
            push @postPermissions, "\@$restrictPostFilename";
         } else {
            my @emails = getFileEmails("$DOMO_LIST_DIR/$restrictPostFilename");
            if (@emails) {
               push @postPermissions, @emails;
            }
         }
      }
      $config{'restrict_post_emails'} =
         "'" . (join "','", @postPermissions) . "'";
   } else {
      # If restrict_post is empty, then anyone can post to it. This can be set
      # in Mailman with a regex that matches everything. Mailman requires
      # regexes in the accept_these_nonmembers field to begin with a caret.
      # TODO: test this setting!
      $config{'restrict_post_emails'} = "'^.*'";
   }

   # Parse <listname>.moderator for moderators
   my @moderators = getFileEmails("$listPath.moderator");
   if (defined $config{'moderator'} and $config{'moderator'} and
       not $config{'moderator'} ~~ @moderators) {
      push @moderators, $config{'moderator'};
   }
   if (@moderators) {
      $config{'moderators'} = "'" . (join "', '", @moderators) . "'";
   }

   $config{'info'} = getFileTxt("$listPath.info", ('skip_dates' => 1));
   $config{'intro'} = getFileTxt("$listPath.intro", ('skip_dates' => 1));

   #
   # Overwrite some config values if legacy files/settings exist.
   #
   if (-e "$listPath.private") {
      for my $option (qw/get_access index_access which_access who_access/) {
         $config{$option} = "closed";
      }
   }

   if (-e "$listPath.closed") {
      $config{'subscribe_policy'} = "closed";
      $config{'unsubscribe_policy'} = "closed";
      if (-e "$listPath.auto") {
         $log->warning("$listName.auto and $listName.closed exist. Setting " .
                       "the list as closed.");
      }
   } elsif (-e "$listPath.auto") {
      $config{'subscribe_policy'} = "auto";
      $config{'unsubscribe_policy'} = "auto";
   }

   $config{'strip'} = 1 if -e "$listPath.strip";
   $config{'noadvertise'} = '/.*/' if -e "$listPath.hidden";

   # Password precedence:
   #  (1) $DOMO_LIST_DIR/$config{(admin|approve)_passwd} file
   #  (2) The (admin|approve)_passwd value itself in <listname>.config
   #  (3) <listname>.passwd file
   for my $passwdOption (qw/admin_passwd approve_passwd/) {
      my $passwdFile = "$DOMO_LIST_DIR/$config{$passwdOption}";
      if (-e $passwdFile) {
         $config{$passwdOption} = getFileTxt($passwdFile,
                                             ('first_line_only' => 1));
      } elsif (not $config{$passwdOption} and -e "$listPath.passwd") {
         $config{$passwdOption} = getFileTxt("$listPath.passwd",
                                             ('first_line_only' => 1));
      }
   }

   # admin_password is required to non-interactively run Mailman's bin/newlist.
   if (not $config{'admin_passwd'}) {
      $log->warning("No admin_passwd or $listName.passwd file. Skipping...");
      $domoStats->{'general_stats'}->{'Lists without admin_passwd'} += 1;
      return;
   }

   # Munge Majordomo text that references Majordomo-specific commands, etc
   for my $field (qw/info intro message_footer message_fronter
                     message_headers/) {
      # Convert references from the majordomo@ admin email to the Mailman one.
      $config{$field} =~ s/majordomo\@/$listName-request\@/mgi;
      # Change owner-<listname>@... to <listname>-owner@...
      $config{$field} =~ s/owner-$listName\@/$listName-owner\@/mgi;
      # Remove the mailing list name from the Majordomo commands.
      $config{$field} =~
         s/(subscribe|unsubscribe)\s*$listName(\-digest)?/$1/mgi;
      # Remove the "end" on a single line listed after all Majordomo commands.
      $config{$field} =~ s/(\s+)end(\s+|\s*\n|$)/$1   $2/mgi;
   }
   $log->debug("Majordomo config for list $listName:\n" . dump(\%config) .
               "\n");

   return %config;
}

# Create and return a hash of Mailman configuration options and values.
# The hash is initialized to the most common default values and then modified
# based on the Majordomo list configuration.
#
# **** The following Majordomo configuration options are not imported. ****
# archive_dir - dead option in Majordomo, so safe to ignore.
# comments - notes section for list admin; safe to ignore.
# date_info - puts a datetime header at top of info file; very safe to ignore.
# date_intro - puts a datetime header at top of intro file; very safe to ignore.
# debug - only useful for the Majordomo admin; very safe to ignore.
# digest_* - digest options don't match up well in Mailman; semi-safe to ignore.
# get_access - who can retrieve files from archives.  Safe to ignore because the
#              "index_access" is consulted to determine archive access.
# message_headers - email headers. Not in Mailman, and probably important for
#                   some lists.
# mungedomain - not recommended to be set in Majordomo, so safe to ignore.
# precedence - mailman handles precedence internally, so safe to ignore.
# purge_received - majordomo recommends not setting this, so safe to ignore.
# resend_host - system setting that never changes, so safe to ignore.
# sender - system setting that never changes, so safe to ignore.
# strip - whether to strip everything but the email address. Not in Mailman.
# taboo_body - message body filtering; roughly used below.  Not in Mailman.
# which_access - get the lists an email is subscribed to.  Not in Mailman.
#
# Additionally, the message_fronter and message_footer of the digest version of
# the list is not imported because of the difficulty in parsing and translating
# that text and because Mailman by default includes its own message header.
sub getMailmanConfig {
   my (%domoConfig) = @_;
   my $listName = $domoConfig{'list_name'};

   # Set default Mailman list configuration values
   my %mmConfig = (
      'accept_these_nonmembers'   => "[$domoConfig{'restrict_post_emails'}]",
      'admin_immed_notify'        => 1,
      'admin_notify_mchanges'     =>
         $domoConfig{'announcements'} =~ /y/i ? 1 : 0,
      'administrivia'             => 'True',
      'advertised'                => 1,
      'anonymous_list'            => 'False',
      'from_is_list'            => 'False',
      # NOTE: some may wish to map some Majordomo setting, such as index_access
      # to Mailman's archive. As is, all archiving is turned off for imported
      # lists.
      'archive'                   => 'False',  # This doesn't change below
      'archive_private'           =>
         $domoConfig{'index_access'} =~ /open/ ? 0 : 1,
      'bounce_processing'         => 1,  # This doesn't change below
      'default_member_moderation' => 0,
      'description'               => "'''$domoConfig{'description'}'''",
      'digest_header'             =>
         "'''$domoConfig{'message_fronter_digest'}'''",
      'digest_footer'             =>
         "'''$domoConfig{'message_footer_digest'}'''",
      'digest_is_default'         => 'False',
      'digestable'                => 'True',
      'filter_content'            => 'False',
      'forward_auto_discards'     => 1,  # This doesn't change below
      'generic_nonmember_action'  => 3,  # 3: discard
      'goodbye_msg'               => '',
      'header_filter_rules'       => '[]',
      'host_name'                 => "'$NEW_HOSTNAME'",
      'info'                      => '',
      'max_message_size'          => 100,  # KB (40 is Mailman's default)
      'moderator'                 => "[$domoConfig{'moderators'}]",
      'msg_header'                => '',
      'msg_footer'                => '',
      'nondigestable'             => 1,
      'obscure_addresses'         => 1,  # This doesn't change below
      'owner'                     => "[$domoConfig{'aliases_owner'}]",
      'personalize'               => 0,
      'preferred_language'        => "'$LANGUAGE'",
      'private_roster'            => 2,  # 0: open; 1: members; 2: admin
      'real_name'                 => "'$listName'",
      'reply_goes_to_list'        => 0,  # 0: poster, 1: list, 2: address
      'reply_to_address'          => '',
      'respond_to_post_requests'  => 1,
      'send_reminders'            => 'False',
      'send_welcome_msg'          => $domoConfig{'welcome'} =~ /y/i ? 1 : 0,
      'subject_prefix'            => "'$domoConfig{'subject_prefix'}'",
      'subscribe_policy'          => 3,  # 1: confirm; 3: confirm and approval
      'unsubscribe_policy'        => 0,  # 0: can unsubscribe; 1: can not
      'welcome_msg'               => '',
   );

   # Majordomo's "who_access" => Mailman's "private_roster"
   if ($domoConfig{'who_access'} =~ /list/i) {
      $mmConfig{'private_roster'} = 1;
   } elsif ($domoConfig{'who_access'} =~ /open/i) {
      $mmConfig{'private_roster'} = 0;
   }

   # Majordomo's "administrivia" => Mailman's "administrivia"
   if ($domoConfig{'administrivia'} =~ /no/i) {
      $mmConfig{'administrivia'} = 'False';
   }

   # Majordomo's "resend_host" => Mailman's "host_name"
   if ($domoConfig{'resend_host'} and not $NEW_HOSTNAME) {
      $mmConfig{'host_name'} = "'$domoConfig{'resend_host'}'";
   }

   # Majordomo's "message_fronter" => Mailman's "msg_header"
   # Majordomo's "message_footer" => Mailman's "msg_footer"
   for my $fieldsArray (['message_fronter', 'msg_header'],
                        ['message_footer', 'msg_footer']) {
      my ($domoOption, $mmOption) = @$fieldsArray;
      if ($domoConfig{$domoOption}) {
         $mmConfig{$mmOption} = "'''$domoConfig{$domoOption}'''";
      }
   }

   # Majordomo's "maxlength" (# chars) => Mailman's "max_message_size" (KB)
   if ($domoConfig{'maxlength'}) {
      my $charsInOneKB = 500;  # 1KB = 500 characters
      $mmConfig{'max_message_size'} = $domoConfig{'maxlength'} / $charsInOneKB;
      if ($mmConfig{'max_message_size'} > $MAX_MSG_SIZE) {
         $mmConfig{'max_message_size'} = $MAX_MSG_SIZE;
      }
   }

   # Majordomo's "taboo_headers" => Mailman's "header_filter_rules"
   if ($domoConfig{'taboo_headers'}) {
      my @rules = split /\n/, $domoConfig{'taboo_headers'};
      $mmConfig{'header_filter_rules'} = "[('" . (join '\r\n', @rules) .
                                         "', 3, False)]";
   }

   # Majordomo's "taboo_body" and "taboo_headers" => Mailman's "filter_content"
   #
   # NOTE: This is a very rough mapping.  What we're doing here is turning on
   # default content filtering in Mailman if there was *any* header or body
   # filtering in Majordomo.  The regexes in the taboo_* fields in Majordomo are
   # too varied for pattern-parsing.  This blunt method is a paranoid,
   # conservative approach.
   if ($domoConfig{'taboo_headers'} or $domoConfig{'taboo_body'}) {
      $mmConfig{'filter_content'} = "True";
   }

   # Majordomo's "subscribe_policy" => Mailman's "subscribe_policy"
   if ($domoConfig{'subscribe_policy'} =~ /open(\+confirm)?/i) {
      $mmConfig{'subscribe_policy'} = 1;
   }

   # Majordomo's "unsubscribe_policy" => Mailman's "unsubscribe_policy"
   if ($domoConfig{'unsubscribe_policy'} =~ /closed/i) {
      $mmConfig{'unsubscribe_policy'} = 1;
   }

   # Majordomo's "moderate" => Mailman's "default_member_moderation"
   if ($domoConfig{'moderate'} =~ /yes/i) {
      $mmConfig{'default_member_moderation'} = 1;
   }

   # Majordomo's "advertise", "noadvertise", "intro_access", "info_access",
   # and "subscribe_policy" => Mailman's "advertised"
   #
   # NOTE: '(no)?advertise' in Majordomo contain regexes, which would be
   # difficult to parse accurately, so just be extra safe here by considering
   # the existence of anything in '(no)?advertise' to mean that the list should
   # be hidden. Also hide the list if intro_access, info_access, or
   # subscribe_policy are at all locked down. This is an appropriate setting
   # for organizations with sensitive data policies (e.g. SOX, FERPA, etc), but
   # not ideal for open organizations with their Mailman instance hidden from
   # the Internet.
   if ($domoConfig{'advertise'} or
       $domoConfig{'noadvertise'} or
       $domoConfig{'intro_access'} =~ /(list|closed)/i or
       $domoConfig{'info_access'} =~ /(list|closed)/i or
       $domoConfig{'subscribe_policy'} =~ /close/i) {
      $mmConfig{'advertised'} = 0;
   }

   # Majordomo's "reply_to" => Mailman's "reply_goes_to_list" and
   # "reply_to_address"
   if ($domoConfig{'reply_to'} =~ /\$sender/i) {
      $mmConfig{'reply_goes_to_list'} = 0;
   } elsif ($domoConfig{'reply_to'} =~ /(\$list|$listName@)/i) {
       $domoConfig{'reply_to'} =~ /\$list/i or
      $mmConfig{'reply_goes_to_list'} = 1;
   } elsif ($domoConfig{'reply_to'} =~ /\s*[^@]+@[^@]+\s*/) {
      $mmConfig{'reply_goes_to_list'} = 2;
      $mmConfig{'reply_to_address'} = "'" . strip($domoConfig{'reply_to'}) .
                                      "'";
   }

   # Majordomo's "subject_prefix" => Mailman's "subject_prefix"
   if ($mmConfig{'subject_prefix'}) {
      $mmConfig{'subject_prefix'} =~ s/\$list/$listName/i;
   }

   # Majordomo's "welcome to the list" message for new subscribers exists in
   # <listname>.intro or <listname>.info.  <listname>.intro takes precedence
   # so this is checked first.  If it doesn't exist, <listname>.info is used,
   # if it exists.
   if ($domoConfig{'intro'}) {
      $mmConfig{'welcome_msg'} = "'''$domoConfig{'intro'}'''";
   } elsif ($domoConfig{'info'}) {
      $mmConfig{'welcome_msg'} = "'''$domoConfig{'info'}'''";
   }

   if ($domoConfig{'message_headers'}) {
      $log->warning("List $listName has message_headers set in Majordomo, " .
                    "but they can't be imported.");
   }

   $log->debug("Mailman config for list $listName: " . dump(\%mmConfig) .
               "\n");

   return %mmConfig;
}

# Call $MM_NEWLIST to create a new Mailman list.
sub createMailmanList {
   my ($listName, $ownerEmail, $listPassword) = @_;
   # Any additional owners will be added when configureMailmanList() is called.
   $ownerEmail = (split /,/, $ownerEmail)[0];
   $ownerEmail =~ s/['"\[\]]//g;
   my $cmd = "$MM_NEWLIST -l $LANGUAGE -q $listName $ownerEmail " .
             "'$listPassword' >> $LOG_FILE 2>&1";
   $log->debug("Calling $cmd...");
   system($cmd) == 0 or $log->die("Command failed: $!");
}

# Create a temporary file that contains a list's configuration values that have
# been translated from Majordomo into Mailman's list template format.
sub createMailmanConfigFile {
   my ($domoApprovePasswd, %mmConfig) = @_;
   my $configFh = File::Temp->new(SUFFIX => ".mm.cfg", UNLINK => 0);
   print $configFh "# coding: utf-8\n";
   for my $cfgField (sort keys %mmConfig) {
      if ($mmConfig{$cfgField}) {
         print $configFh "$cfgField = $mmConfig{$cfgField}\n";
      }
   }

   # The moderator password must be set with Python instead of a config setting.
   if ($domoApprovePasswd) {
      print $configFh <<END;

from Mailman.Utils import sha_new
mlist.mod_password = sha_new('$domoApprovePasswd').hexdigest()
END
   }
   return $configFh->filename;
}

# Call $MM_CONFIGLIST to apply the just-created Mailman configuration options
# file to a Mailman list.
sub configureMailmanList {
   my ($listName, $configFilePath) = @_;
   # Redirect STDOUT/STDERR to the log file to hide the "attribute 'sha_new'
   # ignored" message. This message occurs because Python code to set the
   # moderator password exists at the bottom of the Mailman config file that
   # this script created.
   my $cmd = "$MM_CONFIGLIST -i $configFilePath $listName >> $LOG_FILE 2>&1";
   $log->debug("Calling $cmd...");
   system($cmd) == 0 or $log->die("Command failed: $!");
}

# Create a temporary file with a single email address per line to be used later
# on to subscribe these emails to a Mailman list.
sub createMailmanMembersList {
   my $membersString = shift;
   if ($membersString) {
      my $membersFh = File::Temp->new(SUFFIX => ".mm.members", UNLINK => 0);
      for my $memberEmail (split ',', $membersString) {
         print $membersFh strip($memberEmail) . "\n";
      }
      return $membersFh->filename;
   }
   return '';
}

# Call $MM_ADDMEMBERS to subscribe email addresses to a Mailman list.
sub addMembersToMailmanList {
   my ($listName, $membersFilePath, $digestMembersFilePath) = @_;
   my $cmd = "$MM_ADDMEMBERS -w n -a n";
   $cmd .= " -r $membersFilePath" if $membersFilePath;
   $cmd .= " -d $digestMembersFilePath" if $digestMembersFilePath;
   $cmd .= " $listName >> $LOG_FILE";
   $log->debug("Calling $cmd...");
   system($cmd) == 0 or $log->die("Command failed: $!");
}

# Take the passed in list's Majordomo config and append many of its values to
# the global $domoStats hash ref.
sub appendDomoStats {
   my (%domoConfig) = @_;
   my $listName = $domoConfig{'list_name'};
   # Some fields are uninteresting or part of other fields (e.g. 'moderator'
   # is in 'moderators').
   my @skipFields = qw/archive_dir comments date_info date_intro date_post
                       list_name message_footer_digest message_fronter_digest
                       moderator restrict_post_emails/;
   # Some fields are highly variable, so collapse them into 'has value' and
   # 'no value' values.
   my @yesNoFields = qw/admin_passwd advertise aliases_owner approve_passwd
                        bounce_text description info intro message_footer
                        message_fronter message_headers noadvertise
                        taboo_body taboo_headers/;

   # Run through every Majordomo configuration field and count values.
   for my $field (keys %domoConfig) {
      # Standardize/tidy up the fields and their values.
      $field = lc($field);
      my $value = lc(strip($domoConfig{$field}));

      # Skip unimportant fields
      next if $field ~~ @skipFields;

      # Handle all of the highly variable fields by collapsing their values into
      # one of two choices: does the field have a value or not?
      if ($field ~~ @yesNoFields) {
         $value = $value ? 'has value' : 'no value';
         $domoStats->{$field}->{$value} += 1;
         next;
      }

      # Some fields are moderately variable, but they are important to know
      # about. Handle those fields individually to provide more granular data.
      if ($field eq 'restrict_post') {
         for my $restriction (split /[\s:]/, $domoConfig{'restrict_post'}) {
            if (strip($restriction) eq $listName) {
               $domoStats->{$field}->{'list'} += 1;
            } elsif (strip($restriction) eq "$listName-digest") {
               $domoStats->{$field}->{'list-digest'} += 1;
            } elsif (strip($restriction) eq "$listName.post") {
               $domoStats->{$field}->{'list.post'} += 1;
            } else {
               $domoStats->{$field}->{'other'} += 1;
            }
         }
         next;
      } elsif ($field eq 'sender') {
         if (not $value) {
            $value = 'no value';
         } elsif ($value =~ /^owner-$listName/i) {
            $value = 'owner-list';
         } elsif ($value =~ /^owner-/ and $value !~ /@/) {
            $value = 'owner of another list';
         } else {
            $value = 'other';
         }
      } elsif ($field eq 'subject_prefix' or $field eq 'digest_name') {
         if (not $value) {
            $value = 'no value';
         } elsif ($value =~ /^\s*(\$list|\W*$listName\W*)/i) {
            $value = 'list';
         } else {
            $value = 'other';
         }
      } elsif ($field eq 'reply_to') {
         if (not $value) {
            $value = 'no value';
         } elsif ($value =~ /\$(list|sender)/i) {
            $value = $1;
         } elsif ($value =~ /^$listName(-list)?/) {
            $value = 'list';
         } else {
            $value = 'other';
         }
      } elsif ($field =~ /^(subscribers|digest_subscribers|moderators)/) {
         my $count = () = split /,/, $value, -1;
         if (not $count) {
            $domoStats->{$field}->{'0'} += 1;
            next;
         }
         $domoStats->{$field}->{'2000+'} += 1 if $count >= 2000;
         $domoStats->{$field}->{'1000-2000'} += 1 if $count >= 1000 and $count < 2000;
         $domoStats->{$field}->{'500-999'} += 1 if $count >= 500 and $count < 1000;
         $domoStats->{$field}->{'101-500'} += 1 if $count <= 500 and
                                                   $count > 100;
         $domoStats->{$field}->{'26-100'} += 1 if $count <= 100 and $count > 25;
         $domoStats->{$field}->{'6-25'} += 1 if $count <= 25 and $count > 5;
         $domoStats->{$field}->{'1-5'} += 1 if $count < 5;
         $domoStats->{'general_stats'}->{'Total subscribers'} += $count;
         next;
      } elsif ($field eq 'maxlength') {
         $value = 0 if not $value;
         $domoStats->{$field}->{'1,000,000+'} += 1 if $value > 1000000;
         $domoStats->{$field}->{'100,000-999,999'} += 1 if $value >= 100000 and
                                                           $value < 1000000;
         $domoStats->{$field}->{'50,000-99,999'} += 1 if $value >= 50000 and
                                                         $value < 100000;
         $domoStats->{$field}->{'0-49,999'} += 1 if $value < 50000;
         next;
      }

      $value = 'no value' if not $value;
      $domoStats->{$field}->{$value} += 1;

   }
   $domoStats->{'general_stats'}->{'Total lists'} += 1;
}

sub printStats {
   if (not %$domoStats) {
      print "No stats were generated.\n";
      return;
   }

   print <<END;
+-----------------+
| Majordomo Stats |
+-----------------+
Total Lists: $domoStats->{'general_stats'}->{'Total lists'}

Config Options
--------------
END
   for my $option (sort keys %$domoStats) {
      next if $option eq 'general_stats';
      print " * $option: ";
      for my $value (sort { $domoStats->{$option}->{$b} <=>
                            $domoStats->{$option}->{$a} }
                     keys %{$domoStats->{$option}}) {
         print "$value ($domoStats->{$option}->{$value}), ";
      }
      print "\n";
   }

   if ($domoStats and 
       exists $domoStats->{'general_stats'} and
       defined $domoStats->{'general_stats'} and
       keys %{$domoStats->{'general_stats'}}) {
      print "\nImportant Information" .
            "\n---------------------\n";
      for my $field (sort keys %{$domoStats->{'general_stats'}}) {
         next if $field eq 'Total lists';
         print " * $field: $domoStats->{'general_stats'}->{$field}\n";
      }
      print "\n";
   }
}

#
# Utility functions
#

# Print the help menu to screen and exit.
sub help {
   print <<EOF

   Usage: $SCRIPT_NAME [--loglevel=LEVEL] [--stats]
          [--list=NAME] [--all] [--subscribers]

   Examples:
      # Print stats about your Majordomo lists
      ./$SCRIPT_NAME --stats

      # Verbosely import the 'law-school' mailing list and its subscribers
      ./$SCRIPT_NAME --loglevel=debug --list=law-school --subscribers

      # Import all Majordomo lists and their subscribers
      ./$SCRIPT_NAME --all --subscribers

   Options:
      --all          Import all Majordomo lists
      --list=NAME    Import a single list
      --subscribers  Import subscribers in addition to creating the list
      --stats        Print some stats about your Majordomo lists
      --email-notify Email Majordomo list owners about the upcoming migration
      --email-test   Email Majordomo list owners with link to test server
      --loglevel     Set STDOUT log level.
                     Possible values: debug, info, notice, warning, error
                     Note: All log entries still get written to the log file.
      --help         Print this screen

EOF
;
   exit;
}

# Slurp a file into a variable, optionally skipping the Majordomo datetime
# header or only grabbing the first line.
sub getFileTxt {
   my $filePath = shift;
   my %args = (
      'skip_dates'      => 0,
      'first_line_only' => 0,
      @_
   );
   my $fileTxt = '';
   if (-e $filePath) {
      open FILE, $filePath or $log->die("Can't open $filePath: $!");
      while (my $line = <FILE>) {
         next if $args{'skip_dates'} and $line =~ /^\s*\[Last updated on:/;
         $fileTxt .= $line;
         if ($args{'first_line_only'}) {
            $fileTxt = strip($fileTxt);
            last;
         }
      }
      close FILE or $log->die("Can't close $filePath: $!");
   }
   return $fileTxt;
}

# Given a text file, extract one email per line.  Return these emails in a hash.
sub getFileEmails {
   my $filePath = shift;
   my %emails = ();
   if (-e $filePath) {
      open FILE, $filePath or $log->die("Can't open $filePath: $!");
      while (my $line = <FILE>) {
         if ($line =~ /^#/) {
            next;
         }
         if ($line =~ /\b([A-Za-z0-9._-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,4})\b/) {
            $emails{lc($1)} = 1;
         }
      }
      close FILE or $log->die("Can't close $filePath: $!");
   }
   return keys %emails;
}

# Undo any side-effects of this script (e.g. temporary files, permissions, etc).
sub cleanUp {
   # Delete temporary config files.
   $log->debug("Deleting $TMP_DIR/*.mm.* files...");
   opendir DIR, $TMP_DIR or $log->die("Can't open dir $TMP_DIR: $!");
   my @tmpFiles = grep { /\.mm\.[a-z]+$/i } readdir DIR;
   closedir DIR or $log->die("Can't close dir $TMP_DIR: $!");
   for my $tmpFile (@tmpFiles) {
      unlink "$TMP_DIR/$tmpFile";
   }

   # Fix permissions of newly created Mailman list files.
   my $cmd = "$MM_CHECK_PERMS -f >> $LOG_FILE 2>&1";
   $log->debug("Calling $cmd...");
   system($cmd) == 0 or $log->die("Command failed: $!");
   $log->debug("Calling $cmd again for good measure...");
   system($cmd) == 0 or $log->die("Command failed: $!");
}

# Strip whitespace from the beginning and end of a string.
sub strip {
   my $string = shift || '';
   my $args = shift;
   $string =~ s/(^\s*|\s*$)//g;
   if (exists $args->{'full'}) {
      $string =~ s/(^['"]+|['"]+$)//g;
   }
   return $string;
}
