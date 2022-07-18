Import Majordomo into Mailman
=============================

Create Mailman list(s) from Majordomo list configuration files.

Features
--------

* Import a single list (--list=NAME)
* Import all lists (--all)
* Import subscribers (--subscribers)
* Just generate information about all Majordomo lists (--stats)
* Verbose logging to file
* Control over the console log level (--log-level=[debug|info|notice|warning])
* Only import lists that have been active within the past N days


Requirements
------------

* Mailman is installed so that its bin/* scripts can be called.
* Majordomo has all of its list configurations in a single, local directory.
* Majordomo's aliases file exists locally.
* $DOMO_INACTIVITY_LIMIT set to zero or the file path of the output of
Majordomo's consistency_check command.
* Run as root.


Before running this script
--------------------------

* Change the global variables under "ENVIRONMENT-SPECIFIC VALUES" to match your
system.
* It is recommended to run this script with the --stats option first to get
a sense of your data. Fields with many 'other' or 'no value' values, or
fields that don't get imported (e.g. message_headers) that have many
'has value' values might need to be considered more closely.


Testing
-------

This script was tested against Majordomo 1.94.4/5 and Mailman 2.1.14-1.
Different versions of Majordomo or Mailman may not work with this script.
However, some legacy settings for Majordomo are handled.


Limitations
-----------

* Archives are not currently handled.
* A few Majordomo configuration values are ignored (documented in the comment
above the getMailmanConfig() function) because they are either inactive,
system/constant settings, or don't tranlsate into Mailman.


Todo
----

* Add an --archives option to also import archives.
