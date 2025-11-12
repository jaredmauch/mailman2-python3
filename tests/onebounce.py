#! /usr/bin/env python

# Copyright (C) 2002-2018 by the Free Software Foundation, Inc.
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

"""Test the bounce detection for files containing bounces.

Usage: %(PROGRAM)s [options] file1 ...

Options:
    -h / --help
        Print this text and exit.

    -v / --verbose
        Verbose output.

    -a / --all
        Run the message through all the bounce modules.  Normally this script
        stops at the first one that finds a match.
"""
from __future__ import print_function

import sys
import email
import argparse

import paths
from Mailman.Bouncers import BouncerAPI

PROGRAM = sys.argv[0]
COMMASPACE = ', '


def parse_args():
    parser = argparse.ArgumentParser(description='Test the bounce detection for files containing bounces.')
    parser.add_argument('-v', '--verbose', action='store_true',
                       help='Verbose output')
    parser.add_argument('-a', '--all', action='store_true',
                       help='Run the message through all the bounce modules')
    parser.add_argument('files', nargs='+',
                       help='Files to process')
    return parser.parse_args()


def main():
    args = parse_args()

    for file in args.files:
        fp = open(file)
        msg = email.message_from_file(fp)
        fp.close()
        for module in BouncerAPI.BOUNCE_PIPELINE:
            modname = 'Mailman.Bouncers.' + module
            __import__(modname)
            addrs = sys.modules[modname].process(msg)
            if addrs is BouncerAPI.Stop:
                print(module, 'got a Stop')
                if not args.all:
                    break
                continue
            if not addrs:
                if args.verbose:
                    print(module, 'found no matches')
            else:
                print(module, 'found', COMMASPACE.join(addrs))
                if not args.all:
                    break


if __name__ == '__main__':
    main()
