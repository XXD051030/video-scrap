"""Actual Qt network thumbnail tests against a loopback-only slow HTTP server."""
from __future__ import annotations
import os
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt6.QtCore import QCoreApplication, QEvent, QThread
from PyQt6.QtGui import QImage
from src.gui import workers
from test_gui_resource_boundaries import APP, image_bytes, pump_until


class ThumbnailNetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = image_bytes(2000, 1000)
        cls.stalled = threading.Event()
        cls.release = threading.Event()
        cls.seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                cls.seen.append((self.path, self.headers.get("Cookie", "")))
                try:
                    if self.path == "/headers":
                        self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nX-Drip: ")
                        for _ in range(80):
                            self.wfile.write(b"a")
                            self.wfile.flush()
                            time.sleep(0.02)
                        self.wfile.write(b"\r\n\r\n")
                        return
                    if self.path in {"/redirect", "/redirect-cross-host", "/private/start", "/redirect-loop", "/redirect-unsafe"}:
                        self.send_response(302)
                        target = "/image"
                        if self.path == "/redirect-cross-host":
                            target = f"http://localhost:{cls.server.server_address[1]}/cross-host-image"
                        elif self.path == "/private/start":
                            target = "/public-image"
                        elif self.path == "/redirect-loop":
                            target = "/redirect-loop"
                        elif self.path == "/redirect-unsafe":
                            target = "file:///tmp/thumbnail-must-not-open"
                        self.send_header("Location", target)
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", "5")
                        self.end_headers()
                        self.wfile.write(b"redir")
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(cls.image)))
                    self.end_headers()
                    if self.path == "/stall":
                        cls.stalled.set()
                        cls.release.wait(3)
                        return
                    if self.path == "/body":
                        for part in cls.image:
                            self.wfile.write(bytes([part]))
                            self.wfile.flush()
                            time.sleep(0.02)
                        return
                    self.wfile.write(cls.image)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.release.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(1)

    def tearDown(self):
        self.release.set()
        workers.finish_background_workers(APP)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def start(self, path, **kwargs):
        worker = workers.ThumbnailWorker(0, self.origin + path, **kwargs)
        ready, failed = [], []
        worker.ready.connect(lambda *_args: ready.append(_args))
        worker.failed.connect(lambda *_args: failed.append(_args))
        workers.track_background_worker(worker)
        worker.start()
        return worker, ready, failed

    def test_real_network_scaled_image_and_worker_thread_decode(self):
        decode_threads = []
        original = workers.decode_thumbnail
        def decode(data):
            decode_threads.append(QThread.currentThread())
            return original(data)
        with mock.patch.object(workers, "decode_thumbnail", side_effect=decode):
            worker, ready, failed = self.start("/image")
            pump_until(lambda: bool(ready or failed))
            self.assertFalse(failed)
            image = QImage.fromData(ready[0][1])
            self.assertEqual((image.width(), image.height()), (1280, 640))
            self.assertIs(decode_threads[0], worker)
            self.assertIsNot(decode_threads[0], APP.thread())

    def test_absolute_deadline_aborts_dripping_headers_and_body(self):
        for path in ("/headers", "/body"):
            with mock.patch.object(workers, "THUMB_TOTAL_SECONDS", 0.15):
                started = time.monotonic()
                _worker, ready, failed = self.start(path)
                pump_until(lambda: bool(ready or failed), timeout=1.0)
                self.assertLess(time.monotonic() - started, 0.8)
                self.assertFalse(ready)
                self.assertIn("total time limit", failed[0][1])

    def test_cancel_aborts_a_stalled_response_without_waiting_for_read_timeout(self):
        self.stalled.clear()
        self.release.clear()
        worker, ready, failed = self.start("/stall")
        self.assertTrue(self.stalled.wait(1))
        started = time.monotonic()
        worker.cancel()
        pump_until(lambda: worker not in workers._BACKGROUND_WORKERS, timeout=0.8)
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertEqual((ready, failed), ([], []))

    def test_redirect_preserves_valid_raster_and_scoped_browser_cookies(self):
        cookie = {"name": "fixture", "value": "ok", "domain": "127.0.0.1",
                  "host_only": True, "path": "/", "secure": False}
        _worker, ready, failed = self.start("/redirect", browser_cookies=[cookie])
        pump_until(lambda: bool(ready or failed))
        self.assertFalse(failed)
        self.assertFalse(QImage.fromData(ready[0][1]).isNull())
        self.assertTrue(any(path == "/image" and "fixture=ok" in seen for path, seen in self.seen))

    def test_cross_host_redirect_does_not_send_original_host_cookie(self):
        cookie = {"name": "private_fixture", "value": "secret", "domain": "127.0.0.1",
                  "host_only": True, "path": "/", "secure": False}
        _worker, ready, failed = self.start("/redirect-cross-host", browser_cookies=[cookie])
        pump_until(lambda: bool(ready or failed))
        self.assertFalse(failed)
        records = [(path, value) for path, value in self.seen if path in {"/redirect-cross-host", "/cross-host-image"}]
        self.assertTrue(any(path == "/redirect-cross-host" and "private_fixture=secret" in value
                            for path, value in records))
        self.assertTrue(any(path == "/cross-host-image" and not value for path, value in records))

    def test_redirect_rechecks_cookie_path(self):
        cookie = {"name": "private_path", "value": "secret", "domain": "127.0.0.1",
                  "host_only": True, "path": "/private", "secure": False}
        _worker, ready, failed = self.start("/private/start", browser_cookies=[cookie])
        pump_until(lambda: bool(ready or failed))
        self.assertFalse(failed)
        self.assertTrue(any(path == "/private/start" and "private_path=secret" in value
                            for path, value in self.seen))
        self.assertTrue(any(path == "/public-image" and not value for path, value in self.seen))

    def test_manual_redirect_limit_and_protocol_gate(self):
        before = sum(path == "/redirect-loop" for path, _ in self.seen)
        _worker, ready, failed = self.start("/redirect-loop")
        pump_until(lambda: bool(ready or failed))
        self.assertFalse(ready)
        self.assertIn("redirect limit", failed[0][1])
        self.assertEqual(sum(path == "/redirect-loop" for path, _ in self.seen) - before, 6)
        _worker, ready, failed = self.start("/redirect-unsafe")
        pump_until(lambda: bool(ready or failed))
        self.assertFalse(ready)
        self.assertIn("unsafe destination", failed[0][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
