#! @PYTHON@
# Originally written by Barry Warsaw <barry@zope.com>
#
# Minimally patched to make it even more xgettext compatible 
# by Peter Funk <pf@artcom-gmbh.de>

"""pygettext -- Python equivalent of xgettext(1)

Many systems (Solaris, Linux, Gnu) provide extensive tools that ease the
internationalization of C programs.  Most of these tools are independent of
the programming language and can be used from within Python programs.  Martin
von Loewis' work[1] helps considerably in this regard.

There's one problem though; xgettext is the program that scans source code
looking for message strings, but it groks only C (or C++).  Python introduces
a few wrinkles, such as dual quoting characters, triple quoted strings, and
raw strings.  xgettext understands none of this.

Enter pygettext, which uses Python's standard tokenize module to scan Python
source code, generating .pot files identical to what GNU xgettext[2] generates
for C and C++ code.  From there, the standard GNU tools can be used.

A word about marking Python strings as candidates for translation.  GNU
xgettext recognizes the following keywords: gettext, dgettext, dcgettext, and
gettext_noop.  But those can be a lot of text to include all over your code.
C and C++ have a trick: they use the C preprocessor.  Most internationalized C
source includes a #define for gettext() to _() so that what has to be written
in the source is much less.  Thus these are both translatable strings:

    gettext("Translatable String")
    _("Translatable String")

Python of course has no preprocessor so this doesn't work so well.  Thus,
pygettext searches only for _() by default, but see the -k/--keyword flag
below for how to augment this.

 [1] http://www.python.org/workshops/1997-10/proceedings/loewis.html
 [2] http://www.gnu.org/software/gettext/gettext.html

NOTE: pygettext attempts to be option and feature compatible with GNU xgettext
where ever possible.  However some options are still missing or are not fully
implemented.  Also, xgettext's use of command line switches with option
arguments is broken, and in these cases, pygettext just defines additional
switches.

Usage: pygettext [options] inputfile ...

Options:

    -a
    --extract-all
        Extract all strings.

    -d name
    --default-domain=name
        Rename the default output file from messages.pot to name.pot.

    -E
    --escape
        Replace non-ASCII characters with octal escape sequences.

    -D
    --docstrings
        Extract module, class, method, and function docstrings.  These do not
        need to be wrapped in _() markers, and in fact cannot be for Python to
        consider them docstrings. (See also the -X option).

    -h
    --help
        Print this help message and exit.

    -k word
    --keyword=word
        Keywords to look for in addition to the default set, which are:
        %(DEFAULTKEYWORDS)s

        You can have multiple -k flags on the command line.

    -K
    --no-default-keywords
        Disable the default set of keywords (see above).  Any keywords
        explicitly added with the -k/--keyword option are still recognized.

    --no-location
        Do not write filename/lineno location comments.

    -n
    --add-location
        Write filename/lineno location comments indicating where each
        extracted string is found in the source.  These lines appear before
        each msgid.  The style of comments is controlled by the -S/--style
        option.  This is the default.

    -o filename
    --output=filename
        Rename the default output file from messages.pot to filename.  If
        filename is `-' then the output is sent to standard out.

    -p dir
    --output-dir=dir
        Output files will be placed in directory dir.

    -S stylename
    --style=stylename
        Specify which style to use for location comments.  Two styles are
        supported:

        Solaris  # File: filename line: line-number
        GNU      #: filename:line

        The style name is case insensitive.  GNU style is the default.

    -v
    --verbose
        Print the names of the files being processed.

    -V
    --version
        Print the version of pygettext and exit.

    -w columns
    --width=columns
        Set width of output to columns.

    -x filename
    --exclude-file=filename
        Specify a file that contains a list of strings that are not be
        extracted from the input files.

    -X filename
    --no-docstrings=filename
        Specify a file that contains a list of files (one per line) that
        should not have their docstrings extracted.  This is only useful in
        conjunction with the -D option above.

If `inputfile' is -, standard input is read.
"""

import os
import sys
import argparse
import tokenize
import token
import glob
import time
import re
import io

__version__ = "1.5"

DEFAULTKEYWORDS = ['_']

def usage(code, msg=''):
    print(__doc__ % {'DEFAULTKEYWORDS': ', '.join(DEFAULTKEYWORDS)}, file=sys.stderr)
    if msg:
        print(msg, file=sys.stderr)
    sys.exit(code)


def make_escapes(pass_iso8859):
    global escapes
    escapes = {}
    for i in range(256):
        if i < 32 or i >= 127:
            if pass_iso8859 and i >= 128:
                escapes[i] = chr(i)
            else:
                escapes[i] = "\\%03o" % i
        elif i == ord('\\'):
            escapes[i] = '\\\\'
        elif i == ord('"'):
            escapes[i] = '\\"'
        elif i == ord('\n'):
            escapes[i] = '\\n'
        elif i == ord('\r'):
            escapes[i] = '\\r'
        elif i == ord('\t'):
            escapes[i] = '\\t'
        else:
            escapes[i] = chr(i)


def escape(s):
    res = ''
    for c in s:
        res += escapes[ord(c)]
    return res


def safe_eval(s):
    # unwrap quotes, safely
    try:
        t = eval(s)
        if isinstance(t, str):
            return t
        return s
    except:
        return s


def normalize(s):
    # This converts the various Python string types into a format that is
    # appropriate for .po files, namely much closer to C style.
    lines = s.split('\n')
    if len(lines) == 1:
        s = '"' + escape(s) + '"'
    else:
        if not lines[-1]:
            del lines[-1]
            lines[-1] = lines[-1] + '\n'
        for i in range(len(lines)):
            lines[i] = escape(lines[i])
        lineterm = '\\n"\n"'
        s = '""\n"' + lineterm.join(lines) + '"'
    return s


class TokenEater:
    def __init__(self, options):
        self.__options = options
        self.__messages = {}
        self.__state = self.__waiting
        self.__data = []
        self.__lineno = 0
        self.__curfile = None

    def __call__(self, ttype, tstring, stup, etup, line):
        # dispatch
        self.__state(ttype, tstring, stup[0])

    def __waiting(self, ttype, tstring, lineno):
        # Look for def or class at the start of a line, or for
        # strings at any time.
        if ttype == token.NAME and tstring in ('def', 'class'):
            self.__state = self.__suiteseen
        elif ttype == token.STRING:
            self.__addentry(safe_eval(tstring), lineno)

    def __suiteseen(self, ttype, tstring, lineno):
        # Ignore anything until we see the colon
        if ttype == token.OP and tstring == ':':
            self.__state = self.__suitedocstring

    def __suitedocstring(self, ttype, tstring, lineno):
        # Ignore any intervening noise
        if ttype == token.STRING:
            self.__addentry(safe_eval(tstring), lineno, isdocstring=1)
            self.__state = self.__waiting
        elif ttype not in (token.NEWLINE, token.INDENT, token.DEDENT,
                          token.COMMENT):
            # There was no doc string
            self.__state = self.__waiting

    def __keywordseen(self, ttype, tstring, lineno):
        if ttype == token.OP and tstring == '(':
            self.__state = self.__openseen
        else:
            self.__state = self.__waiting

    def __openseen(self, ttype, tstring, lineno):
        if ttype == token.STRING:
            self.__addentry(safe_eval(tstring), lineno)
            self.__state = self.__waiting
        elif ttype not in (token.NEWLINE, token.INDENT, token.DEDENT,
                          token.COMMENT):
            # There was no doc string
            self.__state = self.__waiting

    def __addentry(self, msg, lineno=None, isdocstring=0):
        if lineno is None:
            lineno = self.__lineno
        if not msg in self.__messages:
            self.__messages[msg] = [(self.__curfile, lineno, isdocstring)]
        else:
            self.__messages[msg].append((self.__curfile, lineno, isdocstring))

    def set_filename(self, filename):
        self.__curfile = filename

    def write(self, fp):
        options = self.__options
        timestamp = time.strftime('%Y-%m-%d %H:%M%z')
        print(r'''# SOME DESCRIPTIVE TITLE.
# Copyright (C) YEAR ORGANIZATION
# FIRST AUTHOR <EMAIL@ADDRESS>, YEAR.
#
msgid ""
msgstr ""
"Project-Id-Version: PACKAGE VERSION\n"
"POT-Creation-Date: %s\n"
"PO-Revision-Date: YEAR-MO-DA HO:MI+ZONE\n"
"Last-Translator: FULL NAME <EMAIL@ADDRESS>\n"
"Language-Team: LANGUAGE <LL@li.org>\n"
"MIME-Version: 1.0\n"
"Content-Type: text/plain; charset=CHARSET\n"
"Content-Transfer-Encoding: ENCODING\n"
"Generated-By: pygettext.py %s\n"
''' % (timestamp, __version__), file=fp)

        # Sort the entries.  First sort each particular entry's locations,
        # then sort all the entries by their first location.
        reverse = {}
        for k, v in self.__messages.items():
            v.sort()
            reverse.setdefault(v[0], []).append((k, v))
        rkeys = sorted(reverse.keys())
        for k in rkeys:
            for msg, locations in sorted(reverse[k]):
                if options.writelocations:
                    for filename, lineno, isdocstring in locations:
                        if options.locationstyle == options.GNU:
                            print('#: %s:%d' % (filename, lineno), file=fp)
                        else:
                            print('# File: %s, line: %d' % (filename, lineno), file=fp)
                if isdocstring:
                    print('#, docstring', file=fp)
                print('msgid', normalize(msg), file=fp)
                print('msgstr ""\n', file=fp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-a', '--extract-all', action='store_true', help='Extract all strings')
    parser.add_argument('-d', '--default-domain', help='Rename the default output file from messages.pot to name.pot')
    parser.add_argument('-E', '--escape', action='store_true', help='Replace non-ASCII characters with octal escape sequences')
    parser.add_argument('-D', '--docstrings', action='store_true', help='Extract module, class, method, and function docstrings')
    parser.add_argument('-k', '--keyword', action='append', help='Keywords to look for in addition to the default set')
    parser.add_argument('-K', '--no-default-keywords', action='store_true', help='Disable the default set of keywords')
    parser.add_argument('--no-location', action='store_true', help='Do not write filename/lineno location comments')
    parser.add_argument('-n', '--add-location', action='store_true', help='Write filename/lineno location comments')
    parser.add_argument('-o', '--output', help='Rename the default output file from messages.pot to filename')
    parser.add_argument('-p', '--output-dir', help='Output files will be placed in directory dir')
    parser.add_argument('-S', '--style', choices=['solaris', 'gnu'], default='gnu', help='Specify which style to use for location comments')
    parser.add_argument('-v', '--verbose', action='store_true', help='Print the names of the files being processed')
    parser.add_argument('-V', '--version', action='store_true', help='Print the version of pygettext and exit')
    parser.add_argument('-w', '--width', type=int, default=78, help='Set width of output to columns')
    parser.add_argument('-x', '--exclude-file', help='Specify a file that contains a list of strings that are not be extracted')
    parser.add_argument('-X', '--no-docstrings', help='Specify a file that contains a list of files that should not have their docstrings extracted')
    parser.add_argument('inputfiles', nargs='*', help='Input files to process')
    
    args = parser.parse_args()
    
    if args.version:
        print(f"pygettext {__version__}")
        sys.exit(0)
        
    if not args.inputfiles:
        usage(1, "No input files given")
        
    # Process the options
    options = Options()
    options.extractall = args.extract_all
    options.escape = args.escape
    options.docstrings = args.docstrings
    options.keywords = args.keyword or []
    if args.no_default_keywords:
        options.keywords = []
    options.writelocations = not args.no_location
    options.locationstyle = Options.GNU if args.style.lower() == 'gnu' else Options.SOLARIS
    options.verbose = args.verbose
    options.width = args.width
    options.outfile = args.output or 'messages.pot'
    options.outpath = args.output_dir or ''
    options.excludefilename = args.exclude_file or ''
    options.nodocstrings = {}
    if args.no_docstrings:
        try:
            with open(args.no_docstrings) as fp:
                for line in fp:
                    options.nodocstrings[line.strip()] = 1
        except IOError as msg:
            usage(1, msg)

    # calculate escapes
    make_escapes(not options.escape)

    # calculate what keywords to look for
    if not options.keywords:
        options.keywords = DEFAULTKEYWORDS[:]

    # initialize list of strings to exclude
    if options.excludefilename:
        try:
            with open(options.excludefilename) as fp:
                options.exclude = {}
                for line in fp:
                    line = line.strip()
                    if line:
                        options.exclude[line] = None
        except IOError:
            usage(1, "Can't read --exclude-file: %s" % options.excludefilename)
    else:
        options.exclude = {}

    # slurp through all the files
    eater = TokenEater(options)
    for filename in args.inputfiles:
        if filename == '-':
            if options.verbose:
                print('Reading standard input', file=sys.stderr)
            eater.set_filename('<stdin>')
            try:
                for t in tokenize.generate_tokens(sys.stdin.readline):
                    eater(*t)
            except tokenize.TokenError as e:
                print('%s: %s, line %d, column %d' % (
                    'stdin', e.args[0], e.args[1][0], e.args[1][1]),
                    file=sys.stderr)
        else:
            if options.verbose:
                print('Working on %s' % filename, file=sys.stderr)
            try:
                with open(filename, 'rb') as fp:
                    eater.set_filename(filename)
                    for t in tokenize.generate_tokens(fp.readline):
                        eater(*t)
            except IOError as e:
                print('%s: %s' % (filename, e.strerror), file=sys.stderr)
            except tokenize.TokenError as e:
                print('%s: %s, line %d, column %d' % (
                    filename, e.args[0], e.args[1][0], e.args[1][1]),
                    file=sys.stderr)

    # write the output
    if options.outfile == '-':
        fp = sys.stdout
    else:
        if options.outpath:
            options.outfile = os.path.join(options.outpath, options.outfile)
        fp = open(options.outfile, 'w')
    try:
        eater.write(fp)
    finally:
        if fp is not sys.stdout:
            fp.close()


if __name__ == '__main__':
    main()
