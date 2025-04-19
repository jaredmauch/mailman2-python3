#!/usr/bin/env python3
#
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

"""Generate binary message catalog from textual translation description.

This program converts a textual Uniforum-style message catalog (.po file) into
a binary GNU catalog (.mo file).  This is essentially the same function as the
GNU msgfmt program, however, it is a simpler implementation.

Usage: msgfmt.py [options] filename.po ...

Options:
    -o file
    --output-file=file
        Specify the output file.

    -h
    --help
        Print this message and exit.

    -V
    --version
        Display version information and exit.
"""

import sys
import os
import argparse
import struct
import array
from email.parser import HeaderParser

__version__ = "1.1"

MESSAGES = {}

def usage(code, msg=''):
    """Print usage message and exit with given code."""
    if code:
        fd = sys.stderr
    else:
        fd = sys.stdout
    print(__doc__, file=fd)
    if msg:
        print(msg, file=fd)
    sys.exit(code)

def add(id, str, fuzzy):
    """Add a non-fuzzy translation to the dictionary."""
    global MESSAGES
    if not fuzzy and str:
        MESSAGES[id] = str

def generate():
    """Generate the binary message catalog."""
    # The keys are sorted in the .mo file
    keys = sorted(MESSAGES.keys())
    offsets = []
    ids = strs = b''
    for id in keys:
        # For each string, we need size and file offset.  Each string is NUL
        # terminated; the NUL does not count into the size.
        offsets.append((len(ids), len(id), len(strs), len(MESSAGES[id])))
        ids += id + b'\0'
        strs += MESSAGES[id] + b'\0'
    output = ''
    # The header is 7 32-bit unsigned integers.  We don't use hash tables, so
    # the keys start right after the index tables.
    # translated string.
    keystart = 7*4+16*len(keys)
    # and the values start after the keys
    valuestart = keystart + len(ids)
    koffsets = []
    voffsets = []
    # The string table first has the list of keys, then the list of values.
    # Each entry has first the size of the string, then the file offset.
    for o1, l1, o2, l2 in offsets:
        koffsets += [l1, o1 + keystart]
        voffsets += [l2, o2 + valuestart]
    offsets = koffsets + voffsets
    output = struct.pack("Iiiiiii",
                         0x950412de,       # Magic
                         0,                 # Version
                         len(keys),         # # of entries
                         7*4,               # start of key index
                         7*4+len(keys)*8,   # start of value index
                         0, 0)              # size and offset of hash table
    output += array.array("i", offsets).tostring()
    output += ids
    output += strs
    return output

def make(filename, outfile):
    """Generate binary message catalog from textual translation description."""
    try:
        with open(filename, 'rb') as fp:
            lines = fp.readlines()
    except IOError as msg:
        print(f"Cannot read {filename}: {msg}", file=sys.stderr)
        sys.exit(1)
    try:
        with open(outfile, 'wb') as fp:
            fp.write(generate())
    except IOError as msg:
        print(f"Cannot write {outfile}: {msg}", file=sys.stderr)
        sys.exit(1)

def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-o', '--output-file', help='Specify the output file')
    parser.add_argument('-V', '--version', action='store_true', help='Display version information and exit')
    parser.add_argument('files', nargs='*', help='Input .po files')
    
    args = parser.parse_args()
    
    if args.version:
        print(f"msgfmt (GNU gettext-tools) {__version__}")
        sys.exit(0)
        
    if not args.files:
        print('No input file given', file=sys.stderr)
        print("Try `msgfmt --help' for more information.", file=sys.stderr)
        return

    for filename in args.files:
        outfile = args.output_file
        if outfile is None:
            outfile = os.path.splitext(filename)[0] + '.mo'
        make(filename, outfile)

if __name__ == '__main__':
    main()
