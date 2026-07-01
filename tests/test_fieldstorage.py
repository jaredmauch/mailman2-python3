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

"""Unit tests for Mailman.Utils.FieldStorage multipart parsing."""

import io
import os
import sys
import unittest

try:
    from Mailman import __init__
except ImportError:
    import paths

from Mailman.Utils import FieldStorage


def _multipart_body(boundary, fields):
    parts = []
    for name, value in fields:
        parts.append(
            '--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
            % (boundary, name, value))
    parts.append('--%s--\r\n' % boundary)
    return ''.join(parts).encode()


class TestFieldStorageMultipart(unittest.TestCase):
    def _parse(self, fields):
        boundary = '----TestBoundary'
        body = _multipart_body(boundary, fields)
        environ = {
            'REQUEST_METHOD': 'POST',
            'CONTENT_TYPE': 'multipart/form-data; boundary=%s' % boundary,
            'CONTENT_LENGTH': str(len(body)),
        }
        sys.stdin = io.BytesIO(body)
        return FieldStorage(keep_blank_values=1, environ=environ)

    def test_member_unsub_fields(self):
        cgidata = self._parse([
            ('jackplimpton%40gmail.com_unsub', 'off'),
            ('user', 'jackplimpton%40gmail.com'),
            ('setmemberopts_btn', 'Submit Your Changes'),
        ])
        self.assertIn('setmemberopts_btn', cgidata)
        self.assertIn('user', cgidata)
        self.assertIn('jackplimpton%40gmail.com_unsub', cgidata)
        self.assertEqual(
            cgidata.getfirst('jackplimpton%40gmail.com_unsub'), 'off')


if __name__ == '__main__':
    unittest.main()
