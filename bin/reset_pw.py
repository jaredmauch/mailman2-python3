#! @PYTHON@
#
# Copyright (C) 2004-2018 by the Free Software Foundation, Inc.
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

# Inspired by Florian Weimer.

"""Reset the passwords for members of a mailing list.

This script resets all the passwords of a mailing list's members.  It can also
be used to reset the lists of all members of all mailing lists, but it is your
responsibility to let the users know that their passwords have been changed.

This script is intended to be run as a bin/withlist script, i.e.

% bin/withlist -l -r reset_pw listname [options]

Options:
    -v / --verbose
        Print what the script is doing.
"""

import sys
import argparse

import paths
from Mailman import Utils
from Mailman.i18n import C_


def parse_args(args):
    parser = argparse.ArgumentParser(description='Reset the passwords for members of a mailing list.')
    parser.add_argument('-v', '--verbose', action='store_true',
                       help='Print what the script is doing')
    return parser.parse_args(args)


def reset_pw(mlist, *args):
    args = parse_args(args)

    listname = mlist.internal_name()
    if args.verbose:
        print(C_('Changing passwords for list: %(listname)s'))

    for member in mlist.getMembers():
        randompw = Utils.MakeRandomPassword()
        mlist.setMemberPassword(member, randompw)
        if args.verbose:
            print(C_('New password for member %(member)40s: %(randompw)s'))

    mlist.Save()


if __name__ == '__main__':
    print(C_(__doc__.replace('%', '%%')))
    sys.exit(0)
