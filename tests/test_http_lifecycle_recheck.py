"""N03/N05: independent header ownership and healthy continuous reads."""
from __future__ import annotations

import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.browser_cookies import _CancelableAdapter, build_scoped_session
from src.http_limits import iter_continuous_response, resource_identity_url
from urllib3.connection import HTTPConnection
from urllib3.exceptions import ReadTimeoutError
from urllib3.exceptions import HTTPError


class HeaderTimerOwnershipTests(unittest.TestCase):
    def test_callback_already_past_timer_cancel_check_cannot_close_returned_response(self):
        entered, release, complete = threading.Event(), threading.Event(), threading.Event()
        class GateTimer(threading.Timer):
            def run(timer):
                timer.finished.wait(.01)
                if not timer.finished.is_set():
                    entered.set()
                    release.wait(3)
                    try:
                        timer.function(*timer.args, **timer.kwargs)
                    finally:
                        complete.set()
                timer.finished.set()
        def headers(connection):
            self.assertTrue(entered.wait(2))
            return Mock()
        left, right = socket.socketpair()
        left.settimeout(1)
        adapter = _CancelableAdapter()
        connection = adapter._pool_classes["http"].ConnectionCls("example.test")
        connection.sock = left
        try:
            with patch("src.browser_cookies.threading.Timer", GateTimer), patch.object(HTTPConnection, "getresponse", headers):
                self.assertIsNotNone(connection.getresponse())
            right.sendall(b"A")
            self.assertEqual(left.recv(1), b"A")
            release.set()
            self.assertTrue(complete.wait(2))
            right.sendall(b"B")
            self.assertEqual(left.recv(1), b"B")
        finally:
            release.set(); connection.close(); right.close(); adapter.close()

    def test_timer_winning_before_headers_complete_rejects_response(self):
        callback = threading.Event()
        class DeadlineTimer(threading.Timer):
            def run(timer):
                timer.function(*timer.args, **timer.kwargs)
                callback.set()
        response = Mock()
        def headers(connection):
            self.assertTrue(callback.wait(2))
            return response
        left, right = socket.socketpair()
        adapter = _CancelableAdapter()
        connection = adapter._pool_classes["http"].ConnectionCls("example.test")
        connection.sock = left
        try:
            with patch("src.browser_cookies.threading.Timer", DeadlineTimer), patch.object(HTTPConnection, "getresponse", headers):
                with self.assertRaises(ReadTimeoutError):
                    connection.getresponse()
            response.close.assert_called_once()
        finally:
            connection.close(); right.close(); adapter.close()


class Response:
    def __init__(self, body=b"x" * 64, delay=.02):
        self.body, self.delay = body, delay
        self.headers = {"Content-Length": str(len(body))}
        self.closed = threading.Event()
    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), 8):
            time.sleep(self.delay)
            yield self.body[start:start + 8]
    def close(self): self.closed.set()


class ContinuousBodyTests(unittest.TestCase):
    def test_healthy_chunks_can_exceed_old_total_deadline(self):
        response = Response(delay=0)
        clock = [0.0]

        def chunks(chunk_size):
            for start in range(0, len(response.body), 8):
                clock[0] += .02
                yield response.body[start:start + 8]

        # Test the total-vs-idle distinction without depending on a shared
        # CI runner scheduling every simulated packet within 60 milliseconds.
        response.iter_content = chunks
        with patch("src.http_limits.transfer_monotonic", side_effect=lambda: clock[0]):
            actual = b"".join(iter_continuous_response(response, 64, .06))
        self.assertEqual(actual, response.body)
        self.assertGreater(clock[0], .06)
        self.assertFalse(response.closed.is_set())

    def test_slow_consumer_does_not_count_as_upstream_idle(self):
        response = Response(b"x" * 16, delay=0)
        stream = iter_continuous_response(response, 16, .04)
        self.assertEqual(next(stream), b"x" * 8)
        time.sleep(.12)
        self.assertFalse(response.closed.is_set())
        self.assertEqual(b"".join(stream), b"x" * 8)

    def test_cancel_still_interrupts_a_slow_consumer(self):
        response = Response(b"x" * 16, delay=0)
        cancelled = threading.Event()
        stream = iter_continuous_response(response, 16, 1, cancelled.is_set)
        next(stream)
        cancelled.set()
        self.assertTrue(response.closed.wait(1))
        with self.assertRaises(InterruptedError): next(stream)

    def test_byte_budget_still_rejects_a_large_response(self):
        with self.assertRaises(ValueError):
            list(iter_continuous_response(Response(), 16, 1))

    def test_final_identity_normalizes_origin_but_preserves_path_and_query(self):
        self.assertEqual(resource_identity_url(None, "HTTPS://EXAMPLE.test:443/a?x=1#f"), "https://example.test/a?x=1")
        self.assertNotEqual(resource_identity_url(None, "https://a.test/object-a?x=1"), resource_identity_url(None, "https://a.test/object-b?x=1"))
        self.assertNotEqual(resource_identity_url(None, "https://a.test/a?x=1&y=2"), resource_identity_url(None, "https://a.test/a?y=2&x=1"))
        with self.assertRaises(ValueError): resource_identity_url(None, "https://user:pass@a.test/a")


class NativeContinuousBodyTests(unittest.TestCase):
    def setUp(self):
        self.release = threading.Event()
        self.drip_received = [threading.Event() for _ in b"abcdefgh"]
        self.drip_trace = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *_): pass
            def do_GET(self):
                # The fixture controls one-byte delivery; TCP batching is not
                # the behavior this read1 regression is meant to measure.
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.send_response(200)
                self.send_header("Content-Length", "8")
                self.end_headers()
                try:
                    if self.path == "/stall":
                        owner.release.wait(5)
                    else:
                        for index, byte in enumerate(b"abcdefgh"):
                            if owner.release.is_set():
                                return
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            owner.drip_trace.append(("sent", index, time.monotonic()))
                            # Do not send the next byte until read1 has yielded
                            # this one. A reader which waits to fill its buffer
                            # cannot pass even on a very fast machine.
                            if not owner.drip_received[index].wait(5):
                                owner.drip_trace.append(("ack_timeout", index, time.monotonic()))
                                self.close_connection = True
                                return
                except (BrokenPipeError, ConnectionResetError): pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.session = build_scoped_session()
    def tearDown(self):
        self.release.set()
        for received in self.drip_received:
            received.set()
        self.session.close()
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)
    def test_read1_counts_small_incoming_packets_as_progress(self):
        url = f"http://127.0.0.1:{self.server.server_port}/drip"
        with self.session.get(url, stream=True, timeout=3) as response:
            clock = [0.0]
            original_read1 = response.raw.read1

            def read1(*args, **kwargs):
                chunk = original_read1(*args, **kwargs)
                self.drip_trace.append(("read", chunk.hex(), time.monotonic()))
                clock[0] += .03
                return chunk

            # Real HTTP reads prove small packets are returned immediately.
            # Controlled transfer time tests progress over a total > .1s
            # without treating CI scheduling delays as an upstream stall.
            with patch.object(response.raw, "read1", side_effect=read1), \
                 patch("src.http_limits.transfer_monotonic", side_effect=lambda: clock[0]):
                stream = iter_continuous_response(response, 8, .1)
                try:
                    for index, byte in enumerate(b"abcdefgh"):
                        self.assertEqual(next(stream), bytes([byte]))
                        self.drip_received[index].set()
                    with self.assertRaises(StopIteration):
                        next(stream)
                    self.assertGreater(clock[0], .1)
                except Exception:
                    print(f"HTTP drip send/read trace: {self.drip_trace!r}", file=sys.stderr)
                    raise
                finally:
                    stream.close()
    def test_idle_watchdog_interrupts_an_actual_blocked_read(self):
        url = f"http://127.0.0.1:{self.server.server_port}/stall"
        start = time.monotonic()
        with self.session.get(url, stream=True, timeout=3) as response:
            with self.assertRaises((TimeoutError, OSError, HTTPError)):
                list(iter_continuous_response(response, 8, .1))
        self.assertLess(time.monotonic() - start, 1.5)


if __name__ == "__main__": unittest.main(verbosity=2)
