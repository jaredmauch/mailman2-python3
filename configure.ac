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

dnl Process this file with autoconf to produce a configure script.
AC_REVISION($Revision: 8122 $)
AC_PREREQ([2.69])
AC_INIT
AC_CONFIG_SRCDIR([src/common.h])

# Store the configure command and arguments for reconfigure target
CONFIGURE_CMD=`echo "$0"`
CONFIGURE_ARGS=`echo "$*"`
AC_SUBST(CONFIGURE_CMD)
AC_SUBST(CONFIGURE_ARGS)

# /usr/local/mailman is the default installation directory
AC_PREFIX_DEFAULT(/usr/local/mailman)

CONFIGURE_OPTS=`echo $@`
AC_SUBST(CONFIGURE_OPTS)
BUILD_DATE=`date`
AC_SUBST(BUILD_DATE)

# Check for Python!  Better be found on $PATH
AC_MSG_CHECKING(for --with-python)
AC_ARG_WITH(python, dnl
[  --with-python                specify path to Python interpreter])
case "$with_python" in
    "") ans="no";;
    *)  ans="$with_python"
esac
AC_MSG_RESULT($ans)

if test -z "$with_python"
then
        AC_PATH_PROG(with_python, python3, /usr/local/bin/python3)
fi

# Set PYTHON variable for Makefile substitution
PYTHON=$with_python
AC_SUBST(PYTHON)

AC_MSG_CHECKING(Python interpreter)
if test ! -x $with_python
then
    AC_MSG_ERROR([
        Python interpreter not found at $with_python
        Please specify the correct path to Python using --with-python
    ])
fi
AC_MSG_RESULT($with_python)

# Check for optional nntplib module
AC_MSG_CHECKING(whether to enable NNTP support)
AC_ARG_ENABLE(nntp,
    [  --enable-nntp           enable NNTP support (requires python3-nntplib)],
    [enable_nntp=$enableval],
    [enable_nntp=no]
)
AC_MSG_RESULT($enable_nntp)

if test "$enable_nntp" = "yes"; then
    AC_MSG_CHECKING(for Python nntplib module)
    $with_python -c "import nntplib" >/dev/null 2>&1
    if test $? -ne 0
    then
        AC_MSG_ERROR([
            Python nntplib module not found but NNTP support was requested
            Please install python3-nntplib package
            On Debian/Ubuntu: apt-get install python3-nntplib
            On RHEL/CentOS: yum install python3-nntplib
            Or disable NNTP support with --disable-nntp
        ])
    fi
    AC_MSG_RESULT(found)
    AC_DEFINE(HAVE_NNTP, 1, [Define if NNTP support is enabled])
fi
AM_CONDITIONAL([HAVE_NNTP], [test "$enable_nntp" = "yes"])

# See if Python is new enough.
AC_MSG_CHECKING(Python version)
changequote(,)
cat > conftest.py <<EOF
import sys
try:
    v = sys.hexversion
except AttributeError:
    v = 0
if v >= 0x3000000:
    s = sys.version.split()[0]
else:
    s = ""
with open("conftest.out", "w") as fp:
    fp.write("%s\n" % s)
EOF
changequote([, ])
$with_python conftest.py
version=`cat conftest.out`
rm -f conftest.out conftest.py
if test -z "$version"
then
    AC_MSG_ERROR([

***** $with_python is too old (or broken)
***** Python 3.0 or newer is required])
fi
AC_MSG_RESULT($version)

# See if dnspython is installed.
AC_MSG_CHECKING(dnspython)
changequote(,)
cat > conftest.py <<EOF
try:
    import dns.resolver
    res = 'ok'
except ImportError:
    res = 'no'
with open("conftest.out", "w") as fp:
    fp.write("%s\n" % res)
EOF
changequote([, ])
$with_python conftest.py
havednspython=`cat conftest.out`
rm -f conftest.out conftest.py
if test "$havednspython" = "no"
then
    AC_MSG_ERROR([
 
***** dnspython <2.0 not found. It is required for the new
***** dmarc_moderation_action feature. Get it from
***** <http://www.dnspython.org/> or
***** <https://pypi.python.org/pypi/dnspython/>
***** You must get a version < 2.0])
fi
AC_MSG_RESULT($havednspython)
 
# Check the email package version.
AC_MSG_CHECKING(Python's email package)
changequote(,)
cat > conftest.py <<EOF
import sys
try:
    import email
except ImportError:
    res = "not ok"
else:
    res = "ok"
with open("conftest.out", "w") as fp:
    fp.write("%s\n" % res)
EOF
changequote([, ])
$with_python conftest.py
needemailpkg=`cat conftest.out`
rm -f conftest.out conftest.py

cat > getver.py <<EOF
from Mailman.Version import VERSION
with open("getver.out", "w") as fp:
    fp.write("%s\n" % VERSION)
EOF
$with_python getver.py
MM_VERSION=`cat getver.out`
rm -f getver.out getver.py
AC_SUBST(MM_VERSION)

# Should we rely on Python's own email package or use the pre-packaged version?
AC_SUBST(EMAILPKG)
if test "$needemailpkg" = "ok"
then
    EMAILPKG=""
else
    EMAILPKG=email-2.5.8
fi
AC_MSG_RESULT($needemailpkg)

# Check Japanese codecs.
AC_MSG_CHECKING(Japanese codecs)
changequote(,)
cat > conftest.py <<EOF
try:
    str('OK').encode('iso-2022-jp')
except LookupError:
    res = "not ok"
else:
    res = "ok"
with open("conftest.out", "w") as fp:
    fp.write("%s\n" % res)
EOF
changequote([, ])
$with_python conftest.py
needjacodecs=`cat conftest.out`
rm -f conftest.out conftest.py

# Does Python have Japanese codecs or do we need the pre-packaged version?
AC_SUBST(JACODECSPKG)
if test "$needjacodecs" = "ok"
then
    JACODECSPKG=""
else
    JACODECSPKG=JapaneseCodecs-1.4.11
fi
AC_MSG_RESULT($needjacodecs)

# Check Korean codecs.
AC_MSG_CHECKING(Korean codecs)
changequote(,)
cat > conftest.py <<EOF
try:
    str('OK').encode('euc-kr')
except LookupError:
    res = "not ok"
else:
    res = "ok"
with open("conftest.out", "w") as fp:
    fp.write("%s\n" % res)
EOF
changequote([, ])
$with_python conftest.py
needkocodecs=`cat conftest.out`
rm -f conftest.out conftest.py

# Does Python have Korean codecs or do we need the pre-packaged version?
AC_SUBST(KOCODECSPKG)
if test "$needkocodecs" = "ok"
then
    KOCODECSPKG=""
else
    KOCODECSPKG=KoreanCodecs-2.0.5
fi
AC_MSG_RESULT($needkocodecs)

# Make sure distutils is available.  Some Linux Python packages split
# distutils into the "-devel" package, so they need both.
AC_MSG_CHECKING(that Python has a working distutils)
changequote(,)
cat > conftest.py <<EOF
try:
    import distutils.errors
    import distutils.sysconfig
except ImportError:
    res = "no"
else:
    try:
        distutils.sysconfig.get_config_vars()
    except distutils.errors.DistutilsPlatformError:
        res = "no"
    else:
        # some RedHat packages put distutils in python, but the C headers
        # are in python-devel so check for headers too.
        import os.path
        pdothpath = distutils.sysconfig.get_python_inc()
        if os.path.isfile(os.path.join(pdothpath, "Python.h")):
            res = "yes"
        else:
            res = "no"
with open("conftest.out", "w") as fp:
    fp.write("%s\n" % res)
EOF
changequote([, ])
$with_python conftest.py
havedistutils=`cat conftest.out`
rm -f conftest.out conftest.py
if test "$havedistutils" != "yes"
then
    AC_MSG_ERROR([

***** Distutils is not available or is incomplete for $with_python
***** If you installed Python from RPM (or other package manager)
***** be sure to install the -devel package, or install Python
***** from source.  See sec. 15.1 of the Installation Manual for
***** details])
fi
AC_MSG_RESULT($havedistutils)

# Checks for programs.
AC_PROG_INSTALL
AC_PROG_MAKE_SET
AC_PATH_PROG(TRUE, true, true, $PATH:/bin:/usr/bin)

# Find compiler, allow alternatives to gcc
AC_MSG_CHECKING(for --without-gcc)
AC_ARG_WITH(gcc, [  --without-gcc               never use gcc], [
        case $withval in
        no)     CC=cc
                without_gcc=yes;;
        yes)    CC=gcc
                without_gcc=no;;
        *)      CC=$withval
                without_gcc=$withval;;
        esac], without_gcc=no;)
AC_MSG_RESULT($without_gcc)

# If the user switches compilers, we can't believe the cache
if test ! -z "$ac_cv_prog_CC" -a ! -z "$CC" -a "$CC" != "$ac_cv_prog_CC"
then
  AC_MSG_ERROR(cached CC is different -- throw away $cache_file
(it is also a good idea to do 'make clean' before compiling))
fi

AC_PROG_CC


# Optimizer/debugger flags passed between Makefiles
AC_SUBST(OPT)
if test -z "$OPT"
then
        case $GCC in
        yes)
                case $ac_cv_prog_cc_g in
                yes)    OPT="-g -O2";;
                *)      OPT="-O2";;
                esac
                ;;
        *)      OPT="-O";;
        esac
fi

# We better be able to execute interpreters
AC_SYS_INTERPRETER
if test "$ac_cv_sys_interpreter" != "yes"
then
    AC_MSG_ERROR([

***** Cannot execute interpreter scripts?
***** Are you sure you system doesn't support this?])
fi


# Check for an alternate data directory, separate from installation dir.
default_var_prefix="/var/mailman"
AC_SUBST(VAR_PREFIX)
AC_MSG_CHECKING(for --with-var-prefix)
AC_ARG_WITH(var-prefix, dnl
[  --with-var-prefix     directory for mutable data [/var/mailman]])
case "$with_var_prefix" in
        yes)    VAR_PREFIX="$default_var_prefix"; ans=$VAR_PREFIX;;
        ""|no)  VAR_PREFIX="$prefix"; ans="no";;
        *)      VAR_PREFIX="$with_var_prefix"; ans=$VAR_PREFIX;
esac
AC_MSG_RESULT($ans)

AC_MSG_CHECKING(for --with-permcheck)
AC_ARG_WITH(permcheck, dnl
[  --without-permcheck  skip the check for target directory permissions])
if test -z "$with_permcheck"
then
        with_permcheck="yes"
fi
AC_MSG_RESULT($with_permcheck)
# Now make sure that $prefix is set up correctly.  It must be group
# owned by the target group, it must have the group sticky bit set, and
# it must be a+rx
if test "$VAR_PREFIX" = "NONE"
then
    VAR_PREFIX=$ac_default_prefix
    prefixcheck=$ac_default_prefix
else
    prefixcheck=$VAR_PREFIX
fi

# new macro for finding group names
AC_DEFUN([MM_FIND_GROUP_NAME], [
# $1 == variable name
# $2 == user id to check for
AC_SUBST($1)
changequote(,)
if test -z "$$1"
then
    cat > conftest.py <<EOF
import grp
gid = ''
for group in "$2".split():
    try:
        try:
            gname = grp.getgrgid(int(group))[0]
            break
        except ValueError:
            gname = grp.getgrnam(group)[0]
            break
    except KeyError:
        gname = ''
with open("conftest.out", "w") as fp:
    fp.write("%s\n" % gname)
EOF
    $with_python conftest.py
    $1=`cat conftest.out`
fi
changequote([, ])
rm -f conftest.out conftest.py])


# new macro for finding UIDs
AC_DEFUN([MM_FIND_USER_NAME], [
# $1 == variable name
# $2 == user id to check for
AC_SUBST($1)
changequote(,)
if test -z "$$1"
then
    cat > conftest.py <<EOF
import pwd
uid = ''
for user in "$2".split():
    try:
        try:
            uname = pwd.getpwuid(int(user))[0]
            break
        except ValueError:
            uname = pwd.getpwnam(user)[0]
            break
    except KeyError:
        uname = ''
with open("conftest.out", "w") as fp:
    fp.write("%s\n" % uname)
EOF
    $with_python conftest.py
    $1=`cat conftest.out`
fi
changequote([, ])
rm -f conftest.out conftest.py])

# Check for some other uid to use than `mailman'
AC_MSG_CHECKING(for --with-username)
AC_ARG_WITH(username, dnl
[  --with-username      specify a user name other than \"mailman\"])

if test -z "$with_username"
then
        with_username="mailman"
fi
USERNAME=$with_username
AC_MSG_RESULT($USERNAME)

# User `mailman' must exist
AC_SUBST(MAILMAN_USER)
AC_MSG_CHECKING(for user name $USERNAME)
MM_FIND_USER_NAME(MAILMAN_USER, $USERNAME)
if test -z "$MAILMAN_USER"
then
  if test "$with_permcheck" = "yes"
  then
    AC_MSG_ERROR([
***** No $USERNAME user found!
***** Your system must have a $USERNAME user defined
***** (usually in your /etc/passwd file).  Please see the INSTALL
***** file for details.])
  fi
fi
AC_MSG_RESULT(okay)


# Check for some other gid to use than `mailman'
AC_MSG_CHECKING(for --with-groupname)
AC_ARG_WITH(groupname, dnl
[  --with-groupname     specify a group name other than \"mailman\"])

if test -z "$with_groupname"
then
        with_groupname="mailman"
fi
GROUPNAME=$with_groupname
AC_MSG_RESULT($GROUPNAME)


# Target group must exist
AC_SUBST(MAILMAN_GROUP)
AC_MSG_CHECKING(for group name $GROUPNAME)
MM_FIND_GROUP_NAME(MAILMAN_GROUP, $GROUPNAME)
if test -z "$MAILMAN_GROUP"
then
  if test "$with_permcheck" = "yes"
  then
    AC_MSG_ERROR([
***** No $GROUPNAME group found!
***** Your system must have a $GROUPNAME group defined
***** (usually in your /etc/group file).  Please see the INSTALL
***** file for details.])
  fi
fi
AC_MSG_RESULT(okay)


AC_MSG_CHECKING(permissions on $prefixcheck)
changequote(,)
cat > conftest.py <<EOF
import os, grp
from stat import *
prefix = "$prefixcheck"
groupname = "$GROUPNAME"
mailmangroup = "$MAILMAN_GROUP"
try:
    mailmangid = grp.getgrnam(mailmangroup)[2]
except KeyError:
    mailmangid = -1
problems = []
try: statdata = os.stat(prefix)
except OSError:
    problems.append("Directory doesn't exist: " + prefix)
else:
    mode = statdata[ST_MODE]
    gid = statdata[ST_GID]
    if mailmangid != gid:
        problems.append("Directory must be owned by group " +
                        groupname + ": " + prefix)
    if (mode & S_ISGID) != S_ISGID:
        problems.append("Set-gid bit must be set for directory: " + prefix)
    perms = S_IRWXU | S_IRWXG | S_IROTH | S_IXOTH
    if (mode & perms) != perms:
        problems.append("Permissions should be at least 02775: " + prefix)
if not problems:
    msg = "okay\n"
else:
    msg = '***** ' + '\n***** '.join(problems) + '\n'
with open("conftest.out", "w") as fp:
    fp.write(msg)
EOF
changequote([, ])
$with_python conftest.py
status=`cat conftest.out`
rm -f conftest.out conftest.py
if test "$with_permcheck" = "yes"
then
    if test "$status" != "okay"
    then
        AC_MSG_ERROR([
***** Installation directory $prefixcheck is not configured properly!
$status])
    fi
else
    status="skipped"
fi
AC_MSG_RESULT($status)


# Now find the UIDs and GIDs
# Support --with-mail-gid and --with-cgi-gid
AC_MSG_CHECKING(for mail wrapper group; i.e. --with-mail-gid)
AC_ARG_WITH(mail-gid, dnl
[  --with-mail-gid      group name mail programs run as])
if test -z "$with_mail_gid"
then
    with_mail_gid="mailman other mail daemon"
fi
MM_FIND_GROUP_NAME(MAIL_GROUP, $with_mail_gid)
if test -z "$MAIL_GROUP"
then
  if test "$with_permcheck" = "yes"
  then
    AC_MSG_ERROR([
***** No group name $with_mail_gid found for the mail wrapper program.
***** This is the group that your mail server will use to run Mailman's
***** programs.  You should specify an existing group with the
***** --with-mail-gid configure option, or use --without-permcheck to
***** skip this verification step.  See also your mail server's documentation,
***** and Mailman's INSTALL file for details])
  else
    MAIL_GROUP=$with_mail_gid
  fi
fi
AC_MSG_RESULT($MAIL_GROUP)


AC_MSG_CHECKING(for CGI wrapper group; i.e. --with-cgi-gid)
AC_ARG_WITH(cgi-gid, dnl
[  --with-cgi-gid       group name CGI programs run as])
if test -z "$with_cgi_gid"
then
    with_cgi_gid="www www-data nobody"
fi

MM_FIND_GROUP_NAME(CGI_GROUP, $with_cgi_gid)
if test -z "$CGI_GROUP"
then
  if test "$with_permcheck" = "yes"
  then
    AC_MSG_ERROR([
***** No group name $with_cgi_gid found for the CGI wrapper program.
***** This is the group that your web server will use to run Mailman's
***** programs.  You should specify an existing group with the
***** --with-cgi-gid configure option, or use --without-permcheck to
***** skip this verification step.  See also your web server's documentation,
***** and Mailman's INSTALL file for details])
  else
    CGI_GROUP=$with_cgi_gid
  fi
fi
AC_MSG_RESULT($CGI_GROUP)


# Check for CGI extensions, required by some Web servers
AC_SUBST(CGIEXT)
AC_MSG_CHECKING(for CGI extensions)
AC_ARG_WITH(cgi-ext, dnl
[  --with-cgi-ext       specify extension for CGI programs (include dot)])
if test -z "$with_cgi_ext"
then
        CGIEXT=''
        with_cgi_ext='no'
else
        CGIEXT=$with_cgi_ext
fi
AC_MSG_RESULT($with_cgi_ext)


# figure out the default mail hostname and url host component
AC_SUBST(MAILHOST)
AC_MSG_CHECKING(for --with-mailhost)
AC_ARG_WITH(mailhost, dnl
[  --with-mailhost      specify the hostname part for outgoing email])
if test -z "$with_mailhost"
then
        MAILHOST=''
        with_mailhost='no'
else
        MAILHOST=$with_mailhost
fi
AC_MSG_RESULT($with_mailhost)

AC_SUBST(URLHOST)
AC_MSG_CHECKING(for --with-urlhost)
AC_ARG_WITH(urlhost, dnl
[  --with-urlhost       specify the hostname part of urls])
if test -z "$with_urlhost"
then
        URLHOST=''
        with_urlhost='no'
else
        URLHOST=$with_urlhost
fi
AC_MSG_RESULT($with_urlhost)


changequote(,)
cat > conftest.py <<EOF
# python
from socket import *
fqdn = getfqdn()
with open('conftest.out', 'w') as fp:
    print(fqdn, file=fp)
    print(fqdn, file=fp)
EOF
$with_python conftest.py
changequote([, ])
AC_MSG_CHECKING(for default mail host name)
if test -z "$MAILHOST"
then
    MAILHOST=`sed q conftest.out`
fi
AC_MSG_RESULT($MAILHOST)
AC_MSG_CHECKING(for default URL host component)
if test -z "$URLHOST"
then
    URLHOST=`sed -n '$p' conftest.out`
fi
AC_MSG_RESULT($URLHOST)
rm -f conftest.out conftest.py

# Checks for libraries.
AC_CHECK_FUNCS(strerror setregid syslog)
if test $ac_cv_func_syslog = no; then
  # syslog is not in the default libraries.  See if it's in some other.
  # Additionally, for at least SCO OpenServer, syslog() is #defined to
  # one of several _real_ functions in syslog.h, so we need to do the test
  # with the appropriate include.
  for lib in bsd socket inet; do
    AC_MSG_CHECKING(for syslog in -l$lib)
    Mailman_LIBS_save="$LIBS"; LIBS="$LIBS -l$lib"
    AC_LINK_IFELSE([AC_LANG_PROGRAM([[#include <syslog.h>]], [[syslog(LOG_DEBUG, "Just a test...");]])],[AC_MSG_RESULT(yes)
      AC_DEFINE(HAVE_SYSLOG)
      break],[AC_MSG_RESULT(no)
      LIBS="$Mailman_LIBS_save"])
    unset Mailman_LIBS_save
  done
fi

# Checks for header files.
m4_warn([obsolete],
[The preprocessor macro `STDC_HEADERS' is obsolete.
  Except in unusual embedded environments, you can safely include all
  ISO C90 headers unconditionally.])dnl

AC_CHECK_HEADERS(syslog.h)

# Checks for typedefs, structures, and compiler characteristics.
AC_TYPE_UID_T
AC_TYPE_GETGROUPS

# Checks for library functions.
AC_CHECK_FUNCS(vsnprintf)

dnl Expand PYTHON path in the scripts, output into build/scriptname

AC_DEFUN([MM_SCRIPTS], [dnl
bin/add_members \
bin/arch \
bin/change_pw \
bin/check_db \
bin/check_perms \
bin/cleanarch \
bin/clone_member \
bin/config_list \
bin/convert.py \
bin/discard \
bin/dumpdb \
bin/export.py \
bin/find_member \
bin/fix_url.py \
bin/genaliases \
bin/inject \
bin/list_admins \
bin/list_lists \
bin/list_members \
bin/list_owners \
bin/mailmanctl \
bin/mmsitepass \
bin/msgfmt.py \
bin/msgfmt-python2.py \
bin/newlist \
bin/pygettext.py \
bin/qrunner \
bin/remove_members \
bin/reset_pw.py \
bin/rmlist \
bin/show_qfiles \
bin/sync_members \
bin/transcheck \
bin/unshunt \
bin/update \
bin/version \
bin/withlist \
bin/mailman-config \
bin/b4b5-archfix \
bin/rb-archfix \
contrib/check_perms_grsecurity.py \
contrib/qmail-to-mailman.py \
contrib/courier-to-mailman.py \
contrib/rotatelogs.py \
cron/bumpdigests \
cron/checkdbs \
cron/cull_bad_shunt \
cron/disabled \
cron/gate_news \
cron/mailpasswds \
cron/nightly_gzip \
cron/senddigests \
])

dnl Please make sure to leave a space at the end of the last entry.
dnl (This is so we don't have to use [a-z/] style character classes
dnl in the regexp below and mess with m4 quoting, which is not fun.)

dnl This regexp munges each line in MM_SCRIPTS, replacing:
dnl   path/script \
dnl with:
dnl   build/path/script:path/script \
dnl so that we can macro-expand variables in scripts without using
dnl script.in filenames, outputting the new files in build/ .

SCRIPTS="patsubst(MM_SCRIPTS, \(.+\) \(\\?\), build/\1:\1 \2)"

AC_SUBST(SCRIPTS)

# These directories are temporary directories to store macro-expanded
# scripts.  They're removed on a make distclean, so we make them here.
mkdir -p build/bin build/contrib build/cron

dnl Output everything
AC_CONFIG_FILES([misc/paths.py Mailman/Defaults.py Mailman/mm_cfg.py.dist
           src/Makefile misc/Makefile bin/Makefile
           Mailman/Makefile Mailman/Cgi/Makefile Mailman/Logging/Makefile
           Mailman/Archiver/Makefile Mailman/Commands/Makefile
           Mailman/Handlers/Makefile Mailman/Bouncers/Makefile
           Mailman/Queue/Makefile Mailman/MTA/Makefile Mailman/Gui/Makefile
           templates/Makefile cron/Makefile scripts/Makefile messages/Makefile
           cron/crontab.in misc/mailman Makefile
           tests/Makefile tests/bounces/Makefile tests/msgs/Makefile Mailman/__init__.py
           $SCRIPTS])
AC_CONFIG_COMMANDS([default],[echo "configuration completed at" `date`],[])
AC_OUTPUT

# Make sure all the build scripts are executable.
chmod -R +x build

# Test for the Chinese codecs.
dnl AC_MSG_CHECKING(for Python Chinese Unicode codecs)
dnl cat > conftest.py <<EOF
dnl try:
dnl   str("abc", "big5-tw")
dnl   print "found"
dnl except LookupError:
dnl   print "not found"
dnl EOF
dnl chinese=`$PYTHON conftest.py 2>/dev/null`
dnl rm -f conftest.py
dnl AC_MSG_RESULT($chinese)
dnl if test "x$chinese" != "xfound"
dnl then
dnl     AC_MSG_WARN([

dnl ***** Python Unicode codecs for Chinese not found.
dnl ***** Chinese emails generated by or manipulated in Mailman will not
dnl ***** be sent in the correct encoding and may be unreadable.
dnl ***** Please uncompress and install the Chinese codecs from:
dnl ***** http://sourceforge.net/projects/python-codecs/
dnl *****
dnl ***** Note: Everything else will work just fine.
dnl ])
dnl fi
