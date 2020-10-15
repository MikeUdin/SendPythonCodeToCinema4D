# -*- coding: utf8 -*-
#
# Copyright (C) 2014  Niklas Rosenstein
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

__author__ = 'Niklas Rosenstein <rosensteinniklas (at) gmail.com>, Mike Udin <admin (at) mikeudin.net>'
__version__ = '1.5'

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#                    Shared Code (SocketFile wrapper class)
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

import sys
import argparse
import code
from os.path import exists

if sys.version_info[0] < 3:
    try: from cStringIO import StringIO as BytesIO
    except ImportError: from StringIO import StringIO as BytesIO
else:
    from io import BytesIO

class SocketFile(object):
    """
    File-like wrapper for reading socket objects.
    """

    def __init__(self, socket, encoding=None):
        super(SocketFile, self).__init__()
        self._socket = socket
        self._buffer = BytesIO()
        self.encoding = encoding

    def _append_buffer(self, data):
        pos = self._buffer.tell()
        self._buffer.seek(0, 2)
        self._buffer.write(data)
        self._buffer.seek(pos)

    def bind(self, *args, **kwargs):
        return self._socket.bind(*args, **kwargs)

    def connect(self, *args, **kwargs):
        return self._socket.connect(*args, **kwargs)

    def read(self, length, blocking=True):
        data = self._buffer.read(length)
        delta = length - len(data)
        if delta > 0:
            self._socket.setblocking(blocking)
            try:
                data += self._socket.recv(delta)
            except socket.error:
                pass
        return data

    def readline(self):
        parts = []
        while True:
            # Read the waiting data from the socket.
            data = self.read(1024, blocking=False)

            # If it contains a line-feed character, we add it
            # to the result list and append the rest of the data
            # to the buffer.
            if b'\n' in data:
                left, right = data.split(b'\n', 1)
                parts.append(left + b'\n')
                self._append_buffer(right)
                break

            else:
                if data:
                    parts.append(data)

                # Read a blocking byte for which we will get an empty
                # bytes object if the socket is closed-
                byte = self.read(1, blocking=True)
                if not byte:
                    break

                # Add the byte to the buffer. Stop here if it is a
                # newline character.
                parts.append(byte)
                if byte == b'\n':
                    break

        return b''.join(parts)

    def write(self, data):
        # if isinstance(data, str):
        #     if not self.encoding:
        #         raise ValueError('got str object and no encoding specified')
        #     data = data.encode(self.encoding)

        return self._socket.send(data.encode())

    def close(self):
        return self._socket.close()

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#                   Communication with code reciever server
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

import socket
import hashlib

def send_code(filename, code, encoding, password, host, port, origin):
    """
    Sends Python code to Cinema 4D running at the specified location
    using the supplied password.

    :raise ConnectionRefusedError:
    """

    client = SocketFile(socket.socket())
    client.connect((host, port)) #! ConnectionRefusedError

    # print encoding
    # if isinstance(code, str):
        # code = code.encode(encoding)

    client.encoding = 'ascii'
    client.write("Content-length: {0}\n".format(len(code)))
    # The Python instance on the other end will check for a coding
    # declaration or otherwise raise a SyntaxError if an invalid
    # character was found.
    client.write("Encoding: binary\n")
    client.write("Filename: {0}\n".format(filename))
    client.write("Origin: {0}\n".format(origin))

    if password:
        passhash = hashlib.md5(password.encode('utf8')).hexdigest()
        client.write("Password: {0}\n".format(passhash))
    client.write('\n') # end headers

    client.encoding = encoding
    client.write(code)

    # Read the response from the server.
    result = client.readline().decode('ascii')
    client.close()

    status = result.partition(':')[2].strip()
    if status == 'ok':
        return None
    return status # error code

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#                          Code Editor Integration
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

import traceback

class SendPythonCodeCommand():

    def __init__(self,pars):
        self.pars = pars

    def run(self):

        # view = sublime.active_window().active_view()
        filename = self.pars['file']
        
        if not exists(filename):
            print ("File \'{0}\' is not exist.".format(filename))
            return


        code = open(filename, "r").read()

        if sys.version_info[0] < 3:
            encoding = 'UTF-8'
            try:
                decoded = code.decode('UTF-8')
            except UnicodeDecodeError:
                encoding = 'ascii'
            else:
                for ch in decoded:
                    if 0xD800 <= ord(ch) <= 0xDFFF:
                        encoding = 'ascii'
                # encoding = 'UTF-8'
        else:
            encoding = 'UTF-8'
        # if encoding == 'Undefined':
        #     encoding = 'UTF-8'

        try:
            password, host, port, origin = self.pars['password'],self.pars['host'],self.pars['port'],self.pars['origin']
        except ValueError as exc:
            print('Invalid credentials.')
            return

        try:
            error = send_code(filename, code, encoding, password, host, port, str(origin))
        # except ConnectionRefusedError as exc:
        #     print('Could not connect to {0}:{1}'.format(host, port))
        #     return
        except socket.error as exc:
            print('socket.error occured, see console')
            # show_console()
            traceback.print_exc()
            return
        except UnicodeDecodeError:
            print('UnicodeDecodeError occured, see console')
            traceback.print_exc()
            return

        if error == 'invalid-password':
            print('Password was not accepted by the Remote Code Executor Server at {0}:{1}'.format(host, port))
        elif error == 'invalid-request':
            print('Request was invalid, maybe this plugin is outdated?')
        elif error is not None:
            print('error (unexpected): {0}'.format(error))
        else:
            print('Code sent to {0}:{1}'.format(host, port))


def main():

    parser = argparse.ArgumentParser(description= 'This script sends Python code to Cinema 4D running at the specified location using the supplied password.')

    parser.add_argument('--file', type=str, help='Editing python code file.',required=True)
    parser.add_argument('--password', type=str, help='Socket Password',default='alpine')
    parser.add_argument('--port', type=int, help='Socket Port',default=2900)
    parser.add_argument('--host', type=str, help='Socket Host',default='localhost')
    parser.add_argument('--origin', type=str, help='Python code source application name',default='PythonEditor')

    # parser.print_help()

    args = parser.parse_args()
    # print vars(args)

    py_command = SendPythonCodeCommand(vars(args))
    py_command.run()


if __name__ == '__main__':
    main()
