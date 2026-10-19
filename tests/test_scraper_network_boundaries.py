"""Cancellation of real local page headers/body and direct-media discovery."""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.scraper import VideoScraper


class ScraperNetworkTests(unittest.TestCase):
    def setUp(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                if self.path == "/headers":
                    self.wfile.write(b"HTTP/1.0 200 OK\r\nX-Wait: ")
                elif self.path == "/body":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", "1024")
                    self.end_headers()
                    self.wfile.write(b"<")
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(1024 ** 3))
                    self.end_headers()
                self.wfile.flush()
                owner.entered.set()
                owner.release.wait(5)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.release.set()
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def _cancel(self, path):
        event = threading.Event()
        scraper = VideoScraper()
        scraper._cancel_event = event
        errors, results = [], []
        def run():
            try:
                results.append(scraper._scrape_html(self.base + path, lambda _: None))
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            self.assertTrue(self.entered.wait(2))
            started = time.monotonic()
            event.set()
            thread.join(2)
            self.assertFalse(thread.is_alive(), "Cancelled page request still owns its worker")
            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue(errors)
            self.assertFalse(results)
        finally:
            self.release.set(); thread.join(5)

    def test_cancel_interrupts_a_stalled_header_before_get_returns(self):
        self._cancel("/headers")

    def test_cancel_interrupts_a_stalled_body(self):
        self._cancel("/body")

    def test_media_response_is_discovered_without_downloading_its_body(self):
        start = time.monotonic()
        scan = VideoScraper()._scrape_html(self.base + "/media", lambda _: None)
        self.assertLess(time.monotonic() - start, 2)
        self.assertTrue(scan.items[0].is_direct)
        self.assertEqual(scan.html, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
