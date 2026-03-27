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
__version__ = '1.6'

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#                    Shared Code (SocketFile wrapper class)
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

import sys
import argparse
import json
from os.path import exists

if sys.version_info[0] < 3:
    try: from cStringIO import StringIO as BytesIO
    except ImportError: from StringIO import StringIO as BytesIO
else:
    from io import BytesIO

try:
    text_type = unicode
except NameError:
    text_type = str


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
        if isinstance(data, bytes):
            self._socket.sendall(data)
            return len(data)

        if not isinstance(data, text_type):
            raise ValueError('unsupported data type: {0}'.format(type(data)))

        if not self.encoding:
            raise ValueError('got str object and no encoding specified')

        payload = data.encode(self.encoding)
        self._socket.sendall(payload)
        return len(payload)

    def close(self):
        return self._socket.close()


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#                   Communication with code receiver server
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

import socket
import hashlib
import traceback

DEFAULT_PORT_SCAN_END = 2910
PROTOCOL_ID = 'send-python-code-to-c4d'
PING_COMMAND = 'ping'
DEFAULT_CONNECT_TIMEOUT = 0.2


def parse_headers(fp):
    headers = {}
    while True:
        line = fp.readline().strip()
        if not line:
            break

        if sys.version_info[0] < 3:
            key, _, value = line.partition(':')
        else:
            key, _, value = line.decode().partition(':')

        key = key.rstrip().lower()
        if key not in headers:
            headers[key] = value.lstrip()

    return headers


def decode_text(data, encoding='utf8'):
    if sys.version_info[0] < 3:
        return data.decode(encoding, 'replace')
    if isinstance(data, str):
        return data
    return data.decode(encoding, 'replace')


def safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_port_range(start, end):
    start = safe_int(start, 2900)
    end = safe_int(end, DEFAULT_PORT_SCAN_END)
    if end < start:
        end = start
    return start, end


def read_response(client):
    status_line = client.readline()
    if not status_line:
        return {'status': 'no-response', 'payload': None}

    if sys.version_info[0] >= 3:
        status_line = decode_text(status_line, 'ascii')

    status = status_line.partition(':')[2].strip() or 'invalid-response'
    headers = parse_headers(client)

    payload = None
    content_length = safe_int(headers.get('content-length'), 0)
    if content_length > 0:
        body = client.read(content_length)
        content_type = headers.get('content-type')
        if content_type == 'application/json':
            try:
                payload = json.loads(decode_text(body))
            except Exception:
                payload = {'raw_body': decode_text(body)}
        else:
            payload = {'raw_body': decode_text(body)}

    return {'status': status, 'payload': payload}


def send_ping(host, port, timeout):
    sock = socket.socket()
    sock.settimeout(timeout)
    client = SocketFile(sock, encoding='ascii')
    client.connect((host, port))
    client.write('Command: {0}\n'.format(PING_COMMAND))
    client.write('Content-length: 0\n')
    client.write('\n')
    try:
        response = read_response(client)
    finally:
        client.close()

    payload = response.get('payload') or {}
    if response['status'] != 'ok':
        return None
    if payload.get('kind') != 'ping':
        return None
    if payload.get('protocol') != PROTOCOL_ID:
        return None
    return payload


def discover_server(host, port_start, port_end, timeout):
    for port in range(port_start, port_end + 1):
        try:
            payload = send_ping(host, port, timeout)
        except socket.error:
            continue

        if payload is not None:
            return {
                'host': host,
                'port': port,
                'handshake': payload,
            }

    return None


def send_code_once(filename, code, password, host, port, origin, timeout):
    """
    Sends Python code to Cinema 4D running at the specified location
    using the supplied password.
    """

    sock = socket.socket()
    sock.settimeout(timeout)
    client = SocketFile(sock, encoding='ascii')
    client.connect((host, port))

    client.write("Content-length: {0}\n".format(len(code)))
    client.write("Encoding: binary\n")
    client.write("Filename: {0}\n".format(filename))
    client.write("Origin: {0}\n".format(origin))

    if password:
        passhash = hashlib.md5(password.encode('utf8')).hexdigest()
        client.write("Password: {0}\n".format(passhash))
    client.write('\n')
    client.write(code)

    try:
        return read_response(client)
    finally:
        client.close()


def send_code(filename, code, password, host, port_start, port_end, origin, timeout):
    target = discover_server(host, port_start, port_end, timeout)
    if target is None:
        return {
            'status': 'server-not-found',
            'payload': None,
            'host': host,
            'port': None,
            'port_source': 'scan',
            'scanned_range': (port_start, port_end),
        }

    response = send_code_once(
        filename, code, password, target['host'], target['port'], origin, timeout)
    response['host'] = target['host']
    response['port'] = target['port']
    response['port_source'] = 'handshake'
    response['handshake'] = target['handshake']
    response['scanned_range'] = (port_start, port_end)
    return response


def print_output_block(label, text):
    if not text:
        return
    text = text.rstrip()
    if not text:
        return
    print('{0}:\n{1}'.format(label, text))


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#                          Code Editor Integration
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


class SendPythonCodeCommand(object):

    def __init__(self, pars):
        self.pars = pars

    def run(self):
        filename = self.pars['file']

        if not exists(filename):
            print("File '{0}' does not exist.".format(filename))
            return

        code = open(filename, 'rb').read()
        password = self.pars['password']
        host = self.pars['host']
        port_start, port_end = normalize_port_range(self.pars['port'], self.pars['port_end'])
        origin = self.pars['origin']
        timeout = self.pars['connect_timeout']

        try:
            response = send_code(
                filename, code, password, host, port_start, port_end, str(origin), timeout)
        except socket.error:
            print('socket.error occurred, see console')
            traceback.print_exc()
            return

        host = response['host']
        port = response.get('port')
        payload = response.get('payload') or {}
        status = response['status']

        if response.get('port_source') == 'handshake':
            print('Resolved Cinema 4D port by handshake scan: {0}'.format(port))

        if status == 'invalid-password':
            print('Password was not accepted by the Remote Code Executor Server at {0}:{1}'.format(
                host, port))
        elif status == 'server-not-found':
            print('Remote Code Executor Server not found on {0} ports {1}-{2}'.format(
                host, port_start, port_end))
        elif status == 'invalid-request':
            print('Request was invalid, maybe this plugin is outdated?')
        elif status == 'encoding-error':
            print('Cinema 4D could not decode the received source code.')
        elif status == 'no-response':
            print('No response from Remote Code Executor Server at {0}:{1}'.format(host, port))
        elif status != 'ok':
            print('error (unexpected): {0}'.format(status))
        else:
            print_output_block('stdout', payload.get('stdout', ''))
            print_output_block('stderr', payload.get('stderr', ''))
            if payload and not payload.get('success', True):
                print('Code sent to {0}:{1}, but Cinema 4D reported an execution error.'.format(
                    host, port))
            else:
                print('Code sent to {0}:{1}'.format(host, port))


def main():
    parser = argparse.ArgumentParser(
        description='This script sends Python code to Cinema 4D running at the '
                    'specified location using the supplied password.')

    parser.add_argument('--file', type=str, help='Editing python code file.', required=True)
    parser.add_argument('--password', type=str, help='Socket Password', default='alpine')
    parser.add_argument('--port', type=int, help='Socket Port range start', default=2900)
    parser.add_argument('--port-end', type=int, help='Socket Port range end', default=DEFAULT_PORT_SCAN_END)
    parser.add_argument('--host', type=str, help='Socket Host', default='localhost')
    parser.add_argument('--origin', type=str, help='Python code source application name', default='PythonEditor')
    parser.add_argument(
        '--connect-timeout',
        type=float,
        help='Socket connect/read timeout per port during scan',
        default=DEFAULT_CONNECT_TIMEOUT)

    args = parser.parse_args()

    py_command = SendPythonCodeCommand(vars(args))
    py_command.run()


if __name__ == '__main__':
    main()
