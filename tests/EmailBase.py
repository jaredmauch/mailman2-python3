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

"""Base class for tests that email things.

Uses a stdlib socket-based SMTP sink so the tests keep working after
asyncore and smtpd were removed in Python 3.12 (PEP 594).
"""

import socket

from Mailman import mm_cfg

from TestBase import TestBase



MSGTEXT = None


class SinkSMTPServer:
    """Minimal one-connection SMTP sink for unit tests."""

    def __init__(self, localaddr, remoteaddr, timeout=30.0):
        # remoteaddr is unused; kept for call-site compatibility with the
        # old smtpd.SMTPServer constructor.
        self._timeout = timeout
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(localaddr)
        self._sock.listen(5)
        self._sock.settimeout(timeout)

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass

    def handle_one_session(self):
        """Accept one SMTP client; return DATA payload as str, or None."""
        global MSGTEXT
        MSGTEXT = None
        try:
            conn, _addr = self._sock.accept()
        except socket.timeout:
            return None
        try:
            conn.settimeout(self._timeout)
            MSGTEXT = self._smtp_session(conn)
            return MSGTEXT
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _smtp_session(self, conn):
        def sendline(line):
            conn.sendall((line + '\r\n').encode('ascii', 'replace'))

        def recvline():
            buf = b''
            while not buf.endswith(b'\n'):
                chunk = conn.recv(1)
                if not chunk:
                    break
                buf += chunk
            return buf.decode('utf-8', 'replace').rstrip('\r\n')

        sendline('220 localhost SMTP sink ready')
        data = None
        while True:
            line = recvline()
            if not line:
                break
            cmd = line[:4].upper()
            if cmd in ('HELO', 'EHLO'):
                sendline('250 localhost')
            elif cmd == 'MAIL':
                sendline('250 OK')
            elif cmd == 'RCPT':
                sendline('250 OK')
            elif cmd == 'DATA':
                sendline('354 End data with <CR><LF>.<CR><LF>')
                lines = []
                while True:
                    dline = recvline()
                    if dline == '.':
                        break
                    # Remove SMTP dot-stuffing.
                    if dline.startswith('.'):
                        dline = dline[1:]
                    lines.append(dline)
                data = '\n'.join(lines)
                sendline('250 OK')
            elif cmd == 'RSET':
                sendline('250 OK')
            elif cmd == 'NOOP':
                sendline('250 OK')
            elif cmd == 'QUIT':
                sendline('221 Bye')
                break
            else:
                sendline('500 Error: command not recognized')
        return data



class EmailBase(TestBase):
    def setUp(self):
        TestBase.setUp(self)
        if mm_cfg.SMTPPORT == 0:
            mm_cfg.SMTPPORT = 25
        # Second argument tuple is ignored.
        self._server = SinkSMTPServer(('localhost', mm_cfg.SMTPPORT),
                                      ('localhost', 25))

    def tearDown(self):
        self._server.close()
        TestBase.tearDown(self)

    def _readmsg(self):
        global MSGTEXT
        # Save and unlock the list so that the qrunner process can open it and
        # lock it if necessary.  We'll re-lock the list in our finally clause
        # since that if an invariant of the test harness.
        self._mlist.Unlock()
        try:
            MSGTEXT = self._server.handle_one_session()
            return MSGTEXT
        finally:
            self._mlist.Lock()
