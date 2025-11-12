#! @PYTHON@
#
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

"""Reset a list's web_page_url attribute to the default setting.

This script is intended to be run as a bin/withlist script, i.e.

% bin/withlist -l -r fix_url listname [options]

Options:
    -u urlhost
    --urlhost=urlhost
        Look up urlhost in the virtual host table and set the web_page_url and
        host_name attributes of the list to the values found.  This
        essentially moves the list from one virtual domain to another.

        Without this option, the default web_page_url and host_name values are
        used.

    -v / --verbose
        Print what the script is doing.

If run standalone, it prints this help text and exits.
"""
from __future__ import print_function

import sys
import argparse

import paths
from Mailman import mm_cfg
from Mailman.i18n import C_


def parse_args(args):
    parser = argparse.ArgumentParser(description='Reset a list\'s web_page_url attribute to the default setting.')
    parser.add_argument('-u', '--urlhost',
                       help='Look up urlhost in the virtual host table and set the web_page_url and host_name attributes')
    parser.add_argument('-v', '--verbose', action='store_true',
                       help='Print what the script is doing')
    return parser.parse_args(args)


def usage(code, msg=''):
    print(C_(__doc__.replace('%', '%%')))
    if msg:
        print(msg)
    sys.exit(code)


def fix_url(mlist, *args):
    try:
        args = parse_args(args)
    except SystemExit:
        usage(1)

    # Make sure list is locked.
    if not mlist.Locked():
        if args.verbose:
            print(C_('Locking list'))
        mlist.Lock()
    if args.urlhost:
        web_page_url = mm_cfg.DEFAULT_URL_PATTERN % args.urlhost
        mailhost = mm_cfg.VIRTUAL_HOSTS.get(args.urlhost.lower(), args.urlhost)
    else:
        web_page_url = mm_cfg.DEFAULT_URL_PATTERN % mm_cfg.DEFAULT_URL_HOST
        mailhost = mm_cfg.DEFAULT_EMAIL_HOST

    if args.verbose:
        print(C_('Setting web_page_url to: %(web_page_url)s'))
    mlist.web_page_url = web_page_url
    if args.verbose:
        print(C_('Setting host_name to: %(mailhost)s'))
    mlist.host_name = mailhost
    print('Saving list')
    mlist.Save()
    mlist.Unlock()


if __name__ == '__main__':
    usage(0)
