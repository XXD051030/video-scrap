"""Exercise Windows cancellation even when shutdown cannot wake a socket wait."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import socket
import ssl
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from src.browser_cookies import _CancelableAdapter, _PollingResponseReader, build_scoped_session
from src.http_limits import iter_continuous_response
from test_connection_lease_recheck import NativeConnectionLeaseTests
from test_http_lifecycle_recheck import NativeContinuousBodyTests
from test_scraper_network_boundaries import ScraperNetworkTests


class ForcePollingMixin:
    def setUp(self):
        self.polling = patch.object(_CancelableAdapter, "POLL_RESPONSE_READS", True)
        self.polling.start()
        self.addCleanup(self.polling.stop)
        # A deterministic reproduction of a Windows wait which stays asleep
        # after shutdown. Cancellation must succeed via the reader's polls.
        self.shutdown = patch.object(socket.socket, "shutdown", return_value=None)
        self.shutdown.start()
        self.addCleanup(self.shutdown.stop)
        super().setUp()


class PollingLeaseTests(ForcePollingMixin, NativeConnectionLeaseTests):
    pass


class PollingContinuousBodyTests(ForcePollingMixin, NativeContinuousBodyTests):
    pass


class PollingScraperTests(ForcePollingMixin, ScraperNetworkTests):
    pass


class PollingReaderTests(unittest.TestCase):
    def setUp(self):
        self.left, self.right = socket.socketpair()
        self.left.settimeout(.4)
        self.cancelled = threading.Event()
        self.raw = _PollingResponseReader(self.left, self.cancelled)
        self.raw.POLL_SECONDS = .02
        self.buffered = io.BufferedReader(self.raw)
        self.addCleanup(self.left.close)
        self.addCleanup(self.right.close)
        self.addCleanup(self.buffered.close)

    def test_internal_polls_preserve_buffered_partial_lines_and_timeout(self):
        def send():
            self.right.sendall(b"first ")
            time.sleep(.08)
            self.right.sendall(b"line\r\nsecond\r\n")

        sender = threading.Thread(target=send)
        sender.start()
        try:
            self.assertEqual(self.buffered.readline(), b"first line\r\n")
            self.assertEqual(self.buffered.readline(), b"second\r\n")
            self.assertEqual(self.left.gettimeout(), .4)
        finally:
            sender.join(2)

    def test_original_read_timeout_is_not_reset_by_internal_polls(self):
        self.left.settimeout(.12)
        started = time.monotonic()
        with self.assertRaises(socket.timeout):
            self.buffered.read(1)
        self.assertGreaterEqual(time.monotonic() - started, .1)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.left.gettimeout(), .12)

    def test_makefile_keeps_detached_socket_alive_until_reader_closes(self):
        descriptor = self.left.fileno()
        self.left.close()
        self.right.sendall(b"x")
        self.assertEqual(self.buffered.read(1), b"x")
        self.assertEqual(self.left.fileno(), descriptor)
        self.buffered.close()
        self.assertEqual(self.left.fileno(), -1)


class PollingTlsTests(unittest.TestCase):
    def test_tls_headers_and_body_survive_poll_timeouts_then_cancel(self):
        entered, release = threading.Event(), threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                try:
                    # Delays exceed an internal poll, below the real timeout.
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Split: ")
                    self.wfile.flush()
                    time.sleep(.15)
                    self.wfile.write(b"ok\r\nContent-Length: 4\r\n\r\nN")
                    self.wfile.flush()
                    entered.set()
                    release.wait(4)
                    self.wfile.write(b"ext")
                    self.wfile.flush()
                except (OSError, ssl.SSLError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # TLS 1.2 keeps encrypted application records distinguishable from
        # handshake records, allowing the TCP relay to split one deliberately.
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(Path(__file__).parent / "fixtures/localhost-test-tls.pem"))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        service = threading.Thread(target=server.serve_forever, daemon=True)
        service.start()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        split_record, relay_stopped = threading.Event(), threading.Event()
        relay_sockets, relay_threads, relay_errors = [], [], []

        def forward(source, destination, split=False):
            pending = b""
            try:
                while not relay_stopped.is_set():
                    try:
                        data = source.recv(65536)
                    except socket.timeout:
                        continue
                    if not data:
                        return
                    if not split:
                        destination.sendall(data)
                        continue
                    pending += data
                    while len(pending) >= 5:
                        length = 5 + int.from_bytes(pending[3:5], "big")
                        if len(pending) < length:
                            break
                        record, pending = pending[:length], pending[length:]
                        if record[0] == 23 and not split_record.is_set():
                            destination.sendall(record[:6])
                            time.sleep(.15)
                            destination.sendall(record[6:])
                            split_record.set()
                        else:
                            destination.sendall(record)
            except OSError:
                if not relay_stopped.is_set():
                    relay_errors.append(sys.exc_info()[1])

        def relay():
            client, _ = listener.accept()
            upstream = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
            client.settimeout(.2)
            upstream.settimeout(.2)
            relay_sockets.extend((client, upstream))
            writer = threading.Thread(target=forward, args=(client, upstream), daemon=True)
            relay_threads.append(writer)
            writer.start()
            forward(upstream, client, split=True)

        relay_thread = threading.Thread(target=relay, daemon=True)
        relay_thread.start()
        session = build_scoped_session()
        cancelled, reading, finished = (threading.Event() for _ in range(3))
        errors = []
        try:
            with patch.object(_CancelableAdapter, "POLL_RESPONSE_READS", True):
                # Trust only this public test certificate; normal sessions
                # retain their existing verification defaults.
                with session.get(f"https://localhost:{listener.getsockname()[1]}/", stream=True, timeout=2,
                                 verify=str(Path(__file__).parent / "fixtures/localhost-test-tls.pem")) as response:
                    self.assertTrue(entered.wait(1))
                    self.assertTrue(split_record.wait(1), "TLS record was not split across a poll timeout")
                    self.assertEqual(relay_errors, [])
                    self.assertEqual(response.headers["X-Split"], "ok")
                    self.assertEqual(response.raw.read1(1), b"N")
                    original_read1 = response.raw.read1

                    def read1(*args, **kwargs):
                        reading.set()
                        return original_read1(*args, **kwargs)

                    def consume():
                        try:
                            list(iter_continuous_response(response, 4, 2, cancelled.is_set))
                        except Exception as error:
                            errors.append(error)
                        finally:
                            finished.set()

                    reader = threading.Thread(target=consume)
                    with patch.object(response.raw, "read1", side_effect=read1), \
                         patch.object(ssl.SSLSocket, "shutdown", return_value=None):
                        reader.start()
                        try:
                            self.assertTrue(reading.wait(1))
                            cancelled.set()
                            self.assertTrue(finished.wait(1))
                            self.assertEqual(len(errors), 1)
                            self.assertIsInstance(errors[0], InterruptedError)
                        finally:
                            release.set()
                            reader.join(3)
        finally:
            relay_stopped.set()
            for sock in relay_sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
            listener.close()
            relay_thread.join(3)
            for thread in relay_threads:
                thread.join(2)
            release.set()
            session.close()
            server.shutdown()
            server.server_close()
            service.join(2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
