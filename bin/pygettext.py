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
    --style stylename
        Specify which style to use for location comments.  Two styles are
        supported:

        Solaris  # File: filename, line: line-number
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
        extracted from the input files.  Each string to be excluded must
        appear on a line by itself in the file.

    -X filename
    --no-docstrings=filename
        Specify a file that contains a list of files (one per line) that
        should not have their docstrings extracted.  This is only useful in
        conjunction with the -D option above.

If `inputfile' is -, standard input is read.
"""

import os
import sys
import time
import argparse
import tokenize
import operator

# for selftesting
try:
    import fintl
    _ = fintl.gettext
except ImportError:
    def _(s): return s

__version__ = '1.4'

default_keywords = ['_']
DEFAULTKEYWORDS = ', '.join(default_keywords)

EMPTYSTRING = ''


# The normal pot-file header. msgmerge and Emacs's po-mode work better if it's
# there.
pot_header = _('''\
# SOME DESCRIPTIVE TITLE.
# Copyright (C) YEAR ORGANIZATION
# FIRST AUTHOR <EMAIL@ADDRESS>, YEAR.
#
msgid ""
msgstr ""
"Project-Id-Version: PACKAGE VERSION\\n"
"POT-Creation-Date: %(time)s\\n"
"PO-Revision-Date: YEAR-MO-DA HO:MI+ZONE\\n"
"Last-Translator: FULL NAME <EMAIL@ADDRESS>\\n"
"Language-Team: LANGUAGE <LL@li.org>\\n"
"MIME-Version: 1.0\\n"
"Content-Type: text/plain; charset=CHARSET\\n"
"Content-Transfer-Encoding: ENCODING\\n"
"Generated-By: pygettext.py %(version)s\\n"

''')

def parse_args():
    parser = argparse.ArgumentParser(description='Python equivalent of xgettext(1)')
    parser.add_argument('-a', '--extract-all', action='store_true',
                       help='Extract all strings')
    parser.add_argument('-d', '--default-domain',
                       help='Rename the default output file from messages.pot to name.pot')
    parser.add_argument('-E', '--escape', action='store_true',
                       help='Replace non-ASCII characters with octal escape sequences')
    parser.add_argument('-D', '--docstrings', action='store_true',
                       help='Extract module, class, method, and function docstrings')
    parser.add_argument('-k', '--keyword', action='append',
                       help='Keywords to look for in addition to the default set')
    parser.add_argument('-K', '--no-default-keywords', action='store_true',
                       help='Disable the default set of keywords')
    parser.add_argument('--no-location', action='store_true',
                       help='Do not write filename/lineno location comments')
    parser.add_argument('-n', '--add-location', action='store_true',
                       help='Write filename/lineno location comments')
    parser.add_argument('-o', '--output',
                       help='Rename the default output file from messages.pot to filename')
    parser.add_argument('-p', '--output-dir',
                       help='Output files will be placed in directory dir')
    parser.add_argument('-S', '--style', choices=['GNU', 'Solaris'],
                       help='Specify which style to use for location comments')
    parser.add_argument('-v', '--verbose', action='store_true',
                       help='Print the names of the files being processed')
    parser.add_argument('-V', '--version', action='version',
                       version='%(prog)s ' + __version__)
    parser.add_argument('-w', '--width', type=int,
                       help='Set width of output to columns')
    parser.add_argument('-x', '--exclude-file',
                       help='Specify a file that contains a list of strings to exclude')
    parser.add_argument('-X', '--no-docstrings',
                       help='Specify a file that contains a list of files to exclude from docstring extraction')
    parser.add_argument('inputfiles', nargs='+',
                       help='Input files to process')
    return parser.parse_args()


escapes = []

def make_escapes(pass_iso8859):
    global escapes
    escapes = []
    for i in range(256):
        if not pass_iso8859 and i >= 0x80:
            escapes.append('\\%03o' % i)
        elif i == 0:
            escapes.append('\\0')
        elif i == 9:
            escapes.append('\\t')
        elif i == 10:
            escapes.append('\\n')
        elif i == 13:
            escapes.append('\\r')
        elif i == 34:
            escapes.append('\\"')
        elif i == 92:
            escapes.append('\\\\')
        else:
            escapes.append(chr(i))


def escape(s):
    global escapes
    s = list(s)
    for i in range(len(s)):
        s[i] = escapes[ord(s[i])]
    return EMPTYSTRING.join(s)


def safe_eval(s):
    # unwrap quotes, safely
    r = s.strip()
    if r.startswith('"""') or r.startswith("'''"):
        quote = r[:3]
        r = r[3:-3]
    else:
        quote = r[0]
        r = r[1:-1]
    return r


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
        self.__lineno = -1
        self.__freshmodule = 1
        self.__curfile = None
        self.__keywords = options.keywords
        if not options.no_default_keywords:
            self.__keywords.extend(default_keywords)

    def __call__(self, ttype, tstring, stup, etup, line):
        # dispatch
        self.__state(ttype, tstring, line[0])

    def __waiting(self, ttype, tstring, lineno):
        # ignore anything until we see the keyword
        if ttype == tokenize.NAME and tstring in self.__keywords:
            self.__state = self.__keywordseen
            self.__lineno = lineno

    def __suiteseen(self, ttype, tstring, lineno):
        # ignore anything until we see the colon
        if ttype == tokenize.OP and tstring == ':':
            self.__state = self.__suitedocstring

    def __suitedocstring(self, ttype, tstring, lineno):
        # ignore any intervening noise
        if ttype == tokenize.STRING:
            self.__addentry(safe_eval(tstring), lineno, isdocstring=1)
            self.__state = self.__waiting
        elif ttype not in (tokenize.NEWLINE, tokenize.INDENT,
                          tokenize.COMMENT):
            # there was no doc string
            self.__state = self.__waiting

    def __keywordseen(self, ttype, tstring, lineno):
        # ignore anything until we see the opening paren
        if ttype == tokenize.OP and tstring == '(':
            self.__state = self.__openseen
        else:
            self.__state = self.__waiting

    def __openseen(self, ttype, tstring, lineno):
        # ignore anything until we see the string
        if ttype == tokenize.STRING:
            self.__addentry(safe_eval(tstring), lineno)
            self.__state = self.__waiting
        elif ttype not in (tokenize.NEWLINE, tokenize.INDENT,
                          tokenize.COMMENT):
            # there was no string
            self.__state = self.__waiting

    def __addentry(self, msg, lineno=None, isdocstring=0):
        if msg in self.__messages:
            entry = self.__messages[msg]
        else:
            entry = []
            self.__messages[msg] = entry
        if lineno is not None:
            entry.append((self.__curfile, lineno, isdocstring))

    def set_filename(self, filename):
        self.__curfile = filename

    def write(self, fp):
        options = self.__options
        if options.style == options.GNU:
            location_format = '#: %(filename)s:%(lineno)d'
        else:
            location_format = '# File: %(filename)s, line: %(lineno)d'
        #
        # write the header
        #
        header = pot_header % {
            'time': time.strftime('%Y-%m-%d %H:%M%z'),
            'version': __version__,
            }
        fp.write(header)
        #
        # Sort the entries.  First sort each particular entry's locations,
        # then sort all the entries by their first location.
        #
        reverse = {}
        for k, v in self.__messages.items():
            if not v:
                continue
            # v is a list of (filename, lineno, isdocstring) tuples
            v.sort()
            first = v[0]
            reverse.setdefault(first, []).append((k, v))
        keys = sorted(reverse.keys())
        #
        # Now write all the entries
        #
        for first in keys:
            entries = reverse[first]
            for k, v in entries:
                if options.writelocations:
                    for filename, lineno, isdocstring in v:
                        if isdocstring:
                            fp.write('#. ')
                        fp.write(location_format % {
                            'filename': filename,
                            'lineno': lineno,
                            })
                        fp.write('\n')
                fp.write('msgid %s\n' % normalize(k))
                fp.write('msgstr ""\n')
                fp.write('\n')


def main():
    args = parse_args()
    
    class Options:
        # constants
        GNU = 1
        SOLARIS = 2
        # defaults
        extractall = args.extract_all
        escape = args.escape
        keywords = args.keyword or []
        outpath = args.output_dir or ''
        outfile = args.output or 'messages.pot'
        writelocations = not args.no_location
        locationstyle = args.style == 'Solaris' and SOLARIS or GNU
        verbose = args.verbose
        width = args.width or 78
        excludefilename = args.exclude_file or ''
        docstrings = args.docstrings
        nodocstrings = {}
        if args.no_docstrings:
            try:
                fp = open(args.no_docstrings)
                nodocstrings = {}
                for line in fp:
                    nodocstrings[line.strip()] = None
                fp.close()
            except IOError:
                pass

    options = Options()
    eater = TokenEater(options)
    
    # Make escapes dictionary
    make_escapes(not options.escape)
    
    # Read the exclusion file, if any
    excluded = {}
    if options.excludefilename:
        try:
            fp = open(options.excludefilename)
            for line in fp:
                line = line.strip()
                excluded[line] = None
            fp.close()
        except IOError:
            pass
    
    # Process each input file
    for filename in args.inputfiles:
        if filename == '-':
            if options.verbose:
                print('Reading standard input')
            fp = sys.stdin
            eater.set_filename('stdin')
            try:
                tokenize.tokenize(fp.readline, eater)
            except tokenize.TokenError as e:
                print('%s: %s' % (filename, e), file=sys.stderr)
                continue
        else:
            if options.verbose:
                print('Working on %s' % filename)
            try:
                fp = open(filename)
                eater.set_filename(filename)
                tokenize.tokenize(fp.readline, eater)
                fp.close()
            except IOError as e:
                print('%s: %s' % (filename, e), file=sys.stderr)
                continue
    
    # Write the output
    if options.outfile == '-':
        fp = sys.stdout
    else:
        fp = open(options.outfile, 'w')
    try:
        eater.write(fp)
    finally:
        if fp is not sys.stdout:
            fp.close()


if __name__ == '__main__':
    main()
    # some more test strings
    _(u'a unicode string')
