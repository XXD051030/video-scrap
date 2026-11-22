"""Body cancellation must retain ownership across EOF and pooled reuse."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.browser_cookies import _CancelableAdapter, build_scoped_session
from src.http_limits import abort_response, iter_continuous_response
from urllib3.connection import HTTPConnection, HTTPSConnection


class RawResponse:
    def __init__(self):
        self.closed = False
        self.close_count = 0

    def close(self):
        self.closed = True
        self.close_count += 1


class ConnectionLeaseTests(unittest.TestCase):
    def setUp(self):
        self.adapter = _CancelableAdapter()
        self.connection = self.adapter._pool_classes["http"].ConnectionCls("example.test")
        self.left, self.right = socket.socketpair()
        self.left.settimeout(1)
        self.connection.sock = self.left

    def tearDown(self):
        self.connection.close()
        self.left.close()
        self.right.close()
        self.adapter.close()

    def response(self, raw=None):
        raw = raw or RawResponse()
        with patch.object(HTTPConnection, "getresponse", return_value=raw):
            self.assertIs(self.connection.getresponse(), raw)
        return SimpleNamespace(raw=raw, headers={}, close=raw.close)

    def test_request_claims_new_generation_before_request_io(self):
        response = self.response()
        with patch.object(HTTPConnection, "request", side_effect=lambda *_a, **_k: abort_response(response)):
            self.connection.request("GET", "/next")
        self.right.sendall(b"A")
        self.assertEqual(self.left.recv(1), b"A")
        self.assertEqual(response.raw.close_count, 0)

    def test_connect_claims_new_generation_before_http_or_https_io(self):
        for scheme, base in (("http", HTTPConnection), ("https", HTTPSConnection)):
            with self.subTest(scheme=scheme):
                connection = self.adapter._pool_classes[scheme].ConnectionCls("example.test")
                left, right = socket.socketpair()
                connection.sock = left
                raw = RawResponse()
                try:
                    with patch.object(base, "getresponse", return_value=raw):
                        response = SimpleNamespace(raw=connection.getresponse())
                    with patch.object(base, "connect", side_effect=lambda: abort_response(response)):
                        connection.connect()
                    right.sendall(b"B")
                    self.assertEqual(left.recv(1), b"B")
                    self.assertEqual(raw.close_count, 0)
                finally:
                    connection.close()
                    left.close()
                    right.close()

    def test_eof_release_cannot_start_next_request_during_guarded_shutdown(self):
        shutdown_entered, permit_shutdown, eof, request_started = (threading.Event() for _ in range(4))
        owner = self

        class GateSocket(socket.socket):
            def shutdown(sock, how):
                shutdown_entered.set()
                if not permit_shutdown.wait(3):
                    raise AssertionError("shutdown gate was not released")
                return super().shutdown(how)

        self.left = GateSocket(fileno=self.left.detach())
        self.connection.sock = self.left

        class FinishingRaw(RawResponse):
            def read1(raw, _amount, decode_content=True):
                if not shutdown_entered.wait(3):
                    raise AssertionError("watchdog did not enter shutdown")
                # urllib3 can finish and release its connection inside read1,
                # before iter_continuous_response reacquires its reader lock.
                raw.closed = True
                eof.set()
                return b"end"

        response = self.response(FinishingRaw())
        errors, new_sockets = [], []

        def read():
            try:
                list(iter_continuous_response(response, 8, .02, raw=True))
            except Exception as error:
                errors.append(error)

        def next_request(*_args, **_kwargs):
            left, right = socket.socketpair()
            left.settimeout(1)
            new_sockets.extend((left, right))
            owner.connection.sock = left
            request_started.set()

        reader = threading.Thread(target=read)
        requester = threading.Thread(target=lambda: self.connection.request("GET", "/next"))
        reader.start()
        try:
            self.assertTrue(eof.wait(2))
            with patch.object(HTTPConnection, "request", side_effect=next_request):
                requester.start()
                self.assertFalse(request_started.wait(.05))
                permit_shutdown.set()
                requester.join(2)
            reader.join(2)
            self.assertFalse(reader.is_alive())
            self.assertFalse(requester.is_alive())
            self.assertTrue(request_started.is_set())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], TimeoutError)
            new_sockets[1].sendall(b"C")
            self.assertEqual(new_sockets[0].recv(1), b"C")
            abort_response(response)
            new_sockets[1].sendall(b"D")
            self.assertEqual(new_sockets[0].recv(1), b"D")
        finally:
            permit_shutdown.set()
            reader.join(2)
            if requester.ident is not None:
                requester.join(2)
            for sock in new_sockets:
                sock.close()

    def test_guard_rejection_does_not_fall_back_to_closing_wrapper(self):
        closed = []
        response = SimpleNamespace(
            raw=SimpleNamespace(closed=False, _abort_owned_response=lambda: False),
            close=lambda: closed.append(True),
        )
        abort_response(response)
        self.assertEqual(closed, [])


class NativeConnectionLeaseTests(unittest.TestCase):
    def setUp(self):
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def handle(self):
                try:
                    super().handle()
                except ConnectionResetError:
                    pass

            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path == "/detached":
                    self.protocol_version = "HTTP/1.0"
                self.send_response(200)
                self.send_header("Content-Length", "4")
                self.end_headers()
                try:
                    if self.path == "/first":
                        self.wfile.write(b"done")
                    else:
                        self.wfile.write(b"N")
                        self.wfile.flush()
                        owner.release.wait(4)
                        self.wfile.write(b"ext")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.session = build_scoped_session()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.release.set()
        self.session.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_completed_response_cannot_abort_actual_keepalive_reuse(self):
        with self.session.get(self.base + "/first", stream=True, timeout=2) as first:
            connection = first.raw._connection
            old_socket = connection.sock
            self.assertEqual(b"".join(iter_continuous_response(first, 4, 1)), b"done")
            with self.session.get(self.base + "/next", stream=True, timeout=2) as second:
                self.assertIs(second.raw._connection, connection)
                self.assertIs(connection.sock, old_socket)
                self.assertEqual(second.raw.read1(1), b"N")
                self.assertFalse(first.raw._abort_owned_response())
                abort_response(first)
                self.release.set()
                self.assertEqual(b"".join(iter_continuous_response(second, 4, 1)), b"ext")

    def test_http10_detached_active_body_cancellation_still_interrupts_read(self):
        cancelled, finished, reading = (threading.Event() for _ in range(3))
        with self.session.get(self.base + "/detached", stream=True, timeout=3) as response:
            self.assertIsNone(response.raw._connection.sock)
            self.assertEqual(response.raw.read1(1), b"N")
            errors = []
            original_read1 = response.raw.read1

            def read1(*args, **kwargs):
                reading.set()
                return original_read1(*args, **kwargs)

            def read():
                try:
                    list(iter_continuous_response(response, 4, 2, cancelled.is_set))
                except Exception as error:
                    errors.append(error)
                finally:
                    finished.set()

            reader = threading.Thread(target=read)
            with patch.object(response.raw, "read1", side_effect=read1):
                reader.start()
                try:
                    self.assertTrue(reading.wait(1))
                    start = time.monotonic()
                    cancelled.set()
                    self.assertTrue(finished.wait(1))
                    self.assertLess(time.monotonic() - start, 1)
                    self.assertEqual(len(errors), 1)
                    self.assertIsInstance(errors[0], InterruptedError)
                    self.assertTrue(response.raw.closed)
                finally:
                    cancelled.set()
                    self.release.set()
                    reader.join(3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
