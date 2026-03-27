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

__author__ = 'Niklas Rosenstein <rosensteinniklas (at) gmail.com>'
__version__ = '1.2'

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#                      Shared Code (SocketFile wrapper class)
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

import sys
if sys.version_info[0] < 3:
    try: from io import StringIO as BytesIO
    except ImportError: from io import StringIO as BytesIO
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
        if isinstance(data, text_type):
            if not self.encoding:
                raise ValueError('got str object and no encoding specified')
            data = data.encode(self.encoding)

        self._socket.sendall(data)
        return len(data)

    def close(self):
        return self._socket.close()


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#                    Request Handling and Server thread
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

import sys, codecs, socket, hashlib, threading, os, json, io, contextlib

DEFAULT_HOST = 'localhost'
DEFAULT_PORT = 2900
DEFAULT_PORT_SCAN_END = 2910
PORT_START_ENV_VAR = 'SENDPYCODE_C4D_PORT_START'
PORT_END_ENV_VAR = 'SENDPYCODE_C4D_PORT_END'
PROTOCOL_ID = 'send-python-code-to-c4d'
PING_COMMAND = 'ping'


def safe_int(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def get_port_range():
    start = safe_int(os.environ.get(PORT_START_ENV_VAR), DEFAULT_PORT)
    end = safe_int(os.environ.get(PORT_END_ENV_VAR), DEFAULT_PORT_SCAN_END)
    if end < start:
        end = start
    return start, end


class SourceObject(object):
    """
    Represents source-code sent over from another machine or
    process which can be executed later.
    """

    def __init__(self, addr, filename, source, origin):
        super(SourceObject, self).__init__()
        self.host, self.port = addr
        self.filename = filename
        self.source = source
        self.origin = origin

    def __repr__(self):
        return '<SourceObject "{0}" sent from "{1}" @ {2}:{3}.>'.format(
            self.filename, self.origin, self.host, self.port)

    def execute(self, scope):
        """
        Execute the source in the specified scope.
        """

        code = compile(self.source, self.filename, 'exec')
        exec(code, scope)


def parse_headers(fp):
    """
    Parses HTTP-like headers into a dictionary until an empty line
    is found. Invalid headers are ignored and if a header is found
    twice, it won't overwrite its previous value. Header-keys are
    converted to lower-case and stripped of whitespace at both ends.
    """

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


class ExecutionRequest(object):
    def __init__(self, client, source):
        super(ExecutionRequest, self).__init__()
        self.client = client
        self.source = source

    def reply_result(self, payload):
        reply_json(self.client, payload)

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass


def reply_json(client, payload):
    body = json.dumps(payload).encode('utf8')
    client.write('status: ok\n')
    client.write('content-type: application/json\n')
    client.write('content-length: {0}\n'.format(len(body)))
    client.write('\n')
    client.write(body)


def reply_ping(client, required_password):
    reply_json(client, {
        'kind': 'ping',
        'protocol': PROTOCOL_ID,
        'plugin_name': 'Remote Code Executor',
        'plugin_id': 1033731,
        'version': __version__,
        'password_required': required_password is not None,
    })


def parse_request(conn, addr, required_password):
    """
    Communicates with the client parsing the headers and source
    code that is to be queued to be executed any time soon.

    Writes one of these lines back to the client:

    - status: invalid-password
    - status: invalid-request
    - status: encoding-error
    - status: ok

    :pass conn: The socket to the client.
    :pass addr: The client address tuple.
    :pass required_password: The password that must match the
        password sent with the "Password" header (as encoded
        utf8 converted to md5). Will be converted to md5 by
        this function.
    :return: :class:`ExecutionRequest` or None
    """

    client = SocketFile(conn, encoding='utf8')
    try:
        headers = parse_headers(client)
        command = headers.get('command', '').strip().lower()
        if command == PING_COMMAND:
            reply_ping(client, required_password)
            client.close()
            return None

        if required_password is not None:
            passhash = hashlib.md5(required_password.encode('utf8')).hexdigest()
            if passhash != headers.get('password'):
                client.write('status: invalid-password\n')
                client.close()
                return None

        content_length = headers.get('content-length', None)
        if content_length is None:
            client.write('status: invalid-request\n')
            client.close()
            return None
        try:
            content_length = int(content_length)
        except ValueError:
            client.write('status: invalid-request\n')
            client.close()
            return None

        encoding = headers.get('encoding', None)
        if encoding is None:
            encoding = 'binary'
        else:
            try:
                codecs.lookup(encoding)
            except LookupError:
                encoding = 'binary'

        origin = headers.get('origin', 'unknown')
        filename = headers.get('filename', 'untitled')
        try:
            source = client.read(content_length)
            if encoding != 'binary':
                source = source.decode(encoding)
        except UnicodeDecodeError:
            client.write('status: encoding-error\n')
            client.close()
            return None

        return ExecutionRequest(client, SourceObject(addr, filename, source, origin))
    except Exception:
        try:
            client.write('status: invalid-request\n')
            client.close()
        except Exception:
            pass
        return None


def get_module_docstring(source_code, filename):
    "Get module-level docstring of Python source text."
    co = compile(source_code, filename, 'exec')
    if co.co_consts and isinstance(co.co_consts[0], str):
        return co.co_consts[0]
    return None


def no_recur_iter(obj):  # no recursion hierachy iteration
    op = obj

    while op:
        yield op

        if op.GetDown():
            op = op.GetDown()
            continue

        while not op.GetNext() and op.GetUp():
            op = op.GetUp()

        op = op.GetNext()


class ServerThread(threading.Thread):
    """
    When the thread is started, the thread binds a server to the
    specified host and port accepting incoming source code, optionally
    password protected, and appends it to the specified queue. A lock
    for synchronization must be passed along with the queue.
    """

    def __init__(self, queue, queue_lock, host, port, password=None):
        super(ServerThread, self).__init__()
        self._queue = queue
        self._queue_lock = queue_lock
        self._socket = None
        self._addr = (host, port)
        self._running = False
        self._lock = threading.Lock()
        self._password = password

    @property
    def running(self):
        with self._lock:
            return self._running

    @running.setter
    def running(self, value):
        with self._lock:
            self._running = value

    @property
    def port(self):
        if self._socket:
            try:
                return self._socket.getsockname()[1]
            except socket.error:
                pass
        return self._addr[1]

    def run(self):
        try:
            while self.running:
                request = self.handle_request()
                if request:
                    with self._queue_lock:
                        self._queue.append(request)
        finally:
            if self._socket:
                self._socket.close()

    def start(self):
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(self._addr)
        self._socket.listen(5)
        self._socket.settimeout(0.5)
        self.running = True
        return super(ServerThread, self).start()

    def handle_request(self):
        conn = None
        try:
            conn, addr = self._socket.accept()
            request = parse_request(conn, addr, self._password)
            if request:
                conn = None
            return request
        except socket.timeout:
            return None
        finally:
            if conn:
                conn.close()


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#                           Cinema 4D integration
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

import c4d, collections, traceback

plugins = []


class CodeExecuterMessageHandler(c4d.plugins.MessageData):

    PLUGIN_ID = 1033731
    PLUGIN_NAME = "Remote Code Executor"

    def __init__(self, host, port, password, port_scan_end=None):
        super(CodeExecuterMessageHandler, self).__init__()
        self.host = host
        self.requested_port = port
        self.port_scan_end = max(port, port_scan_end if port_scan_end is not None else port)
        self.password = password
        self.bound_port = None
        self.queue = collections.deque()
        self.queue_lock = threading.Lock()
        self.thread = None
        self._start_server()

    def _start_server(self):
        for candidate_port in range(self.requested_port, self.port_scan_end + 1):
            print("Binding Remote Code Executor Server to {0}:{1} ...".format(
                self.host, candidate_port))
            thread = ServerThread(
                self.queue, self.queue_lock, self.host, candidate_port, self.password)
            try:
                thread.start()
            except socket.error as exc:
                print("Failed to bind to {0}:{1}\n{2}".format(
                    self.host, candidate_port, exc))
                continue

            self.thread = thread
            self.bound_port = thread.port
            if self.bound_port != self.requested_port:
                print(
                    "Remote Code Executor fallback port selected: {0} "
                    "(requested {1}).".format(self.bound_port, self.requested_port))
            return

        self.thread = None
        self.bound_port = None

    def register(self):
        return c4d.plugins.RegisterMessagePlugin(
            self.PLUGIN_ID, self.PLUGIN_NAME, 0, self)

    def get_scope(self):
        doc = c4d.documents.GetActiveDocument()
        op = doc.GetActiveObject()
        mat = doc.GetActiveMaterial()
        tp = doc.GetParticleSystem()
        return {
            '__name__': '__main__',
            'doc': doc,
            'op': op,
            'mat': mat,
            'tp': tp
        }

    def on_shutdown(self):
        if self.thread:
            print("Shutting down Remote Code Executor Server thread ...")
            self.thread.running = False
            self.thread.join()
            self.thread = None
        self.bound_port = None

    def GetTimer(self):
        if self.thread:
            return 500
        return 0

    def CoreMessage(self, kind, bc):
        # Execute source code objects while they're available.
        while True:
            with self.queue_lock:
                if not self.queue:
                    break
                request = self.queue.popleft()
            try:
                payload = self.execute_request(request.source)
            except Exception:
                payload = {
                    'success': False,
                    'stdout': '',
                    'stderr': traceback.format_exc(),
                    'filename': request.source.filename,
                    'origin': request.source.origin,
                }
            try:
                request.reply_result(payload)
            finally:
                request.close()
        return True

    def execute_request(self, source):
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        success = True

        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            try:
                scope = self.get_scope()
                scope['__file__'] = source.filename
                if not obj_execute(source):
                    source.execute(scope)
            except Exception:
                success = False
                traceback.print_exc(file=stderr_buffer)

        return {
            'success': success,
            'stdout': stdout_buffer.getvalue(),
            'stderr': stderr_buffer.getvalue(),
            'filename': source.filename,
            'origin': source.origin,
        }


def obj_execute(source):
    code = source.source
    mode = py_name = ''
    try:
        scriptdoc = get_module_docstring(code, source.filename)
    except Exception:
        return False

    if scriptdoc:
        for line in scriptdoc.splitlines():
            try:
                mode, py_name = line.split(':', 1)
                mode = mode.strip()
                py_name = py_name.strip()
            except Exception:
                pass

            if mode in ['Generator', 'Effector', 'Tag', 'Field']:
                break

    if mode not in ['Generator', 'Effector', 'Tag', 'Field'] or not py_name:
        return False

    doc = c4d.documents.GetActiveDocument()
    if sys.version_info[0] < 3:
        c4d_code = str(code)
    elif isinstance(code, bytes):
        c4d_code = code.decode('utf-8')
    else:
        c4d_code = code

    # Searching Python objects or tags
    counter = 0
    for obj in no_recur_iter(doc.GetFirstObject()):
        if mode == 'Generator' and obj.GetType() == 1023866 and obj.GetName() == py_name:
            obj[c4d.OPYTHON_CODE] = c4d_code
            counter += 1

        if mode == 'Effector' and obj.GetType() == 1025800 and obj.GetName() == py_name:
            obj[c4d.OEPYTHON_STRING] = c4d_code
            counter += 1

        if mode == 'Field' and obj.GetType() == 440000277 and obj.GetName() == py_name:
            obj[c4d.PYTHON_CODE] = c4d_code
            counter += 1

        if mode == 'Tag':
            tags = [t for t in obj.GetTags() if t.GetType() == 1022749]
            for tag in tags:
                if tag.GetName() == py_name:
                    tag[c4d.TPYTHON_CODE] = c4d_code
                    counter += 1

    print('RemoteCodeRunner: code was changed in {0} {1}s '.format(counter, mode))
    c4d.EventAdd()

    return True


def main():
    global plugins
    preferred_port, port_scan_end = get_port_range()
    handler = CodeExecuterMessageHandler(
        DEFAULT_HOST, preferred_port, 'alpine', port_scan_end)
    handler.register()
    plugins.append(handler)


def PluginMessage(kind, data):
    if kind in [c4d.C4DPL_ENDACTIVITY, c4d.C4DPL_RELOADPYTHONPLUGINS]:
        for plugin in plugins:
            method = getattr(plugin, 'on_shutdown', None)
            if callable(method):
                try:
                    method()
                except Exception:
                    traceback.print_exc()
    return True


if __name__ == "__main__":
    main()
