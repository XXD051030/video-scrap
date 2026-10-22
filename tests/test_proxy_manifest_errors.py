"""Real loopback HLS body failures return bounded errors and release responses."""
from __future__ import annotations

import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import sys
import threading
import time
import unittest

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.media_proxy import MAX_PROXY_ERROR_CHARS, MediaProxyServer


class RecordingRequest:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.headers = {}
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers[name] = value

    def end_headers(self):
        pass


class ProxyManifestErrorTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.entered = threading.Event()
        self.release = threading.Event()
        owner = self

        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                body = b"#EXTM3U\n#EXTINF:1,\nclip.ts\n#EXT-X-ENDLIST\n"
                if self.path == "/valid-gzip.m3u8":
                    body = gzip.compress(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                extra = 8 if self.path in {"/truncated.m3u8", "/stall.m3u8"} else 0
                self.send_header("Content-Length", str(len(body) + extra))
                if self.path in {"/bad-gzip.m3u8", "/valid-gzip.m3u8"}:
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                if self.path == "/stall.m3u8":
                    owner.entered.set()
                    owner.release.wait(4)

        self.fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        self.fixture.daemon_threads = True
        self.runner = threading.Thread(target=self.fixture.serve_forever, daemon=True)
        self.runner.start()
        self.proxy = MediaProxyServer(buffer_seconds=0, on_status=self.logs.append).start()

    def tearDown(self):
        self.release.set()
        self.proxy.stop()
        self.fixture.shutdown()
        self.fixture.server_close()
        self.runner.join(2)

    def register(self, route):
        local = self.proxy.register(f"http://127.0.0.1:{self.fixture.server_port}{route}", is_hls=True)
        return local, self.proxy._lookup(local)

    def assert_bad_body(self, route):
        local, entry = self.register(route)
        response = requests.get(local, timeout=2)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(int(response.headers["Content-Length"]), len(response.content))
        self.assertLessEqual(len(response.content), MAX_PROXY_ERROR_CHARS * 4)
        self.assertTrue(response.content)
        self.assertTrue(any("Player manifest error:" in line for line in self.logs))
        deadline = time.monotonic() + 1
        while entry._responses and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(entry._responses)
        self.assertFalse(entry.hls_segment_entries or entry.children)

    def test_truncated_manifest_body_returns_502_and_logs_the_error(self):
        self.assert_bad_body("/truncated.m3u8")

    def test_invalid_gzip_returns_502_and_logs_the_error(self):
        self.assert_bad_body("/bad-gzip.m3u8")

    def test_valid_plain_and_gzip_manifests_still_rewrite_after_a_failed_request(self):
        self.assert_bad_body("/bad-gzip.m3u8")
        for route in ("/valid.m3u8", "/valid-gzip.m3u8"):
            local, entry = self.register(route)
            response = requests.get(local, timeout=2)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(int(response.headers["Content-Length"]), len(response.content))
            self.assertTrue(response.text.startswith("#EXTM3U\n"))
            self.assertIn(self.proxy.base_url + "/play/", response.text)
            self.assertEqual(len(entry.hls_segment_entries), 1)
            self.assertNotIn("Content-Encoding", response.headers)

    def test_unregister_still_cancels_a_stalled_manifest_body(self):
        local, entry = self.register("/stall.m3u8")
        results = []
        def read():
            try:
                results.append(requests.get(local, timeout=3).status_code)
            except requests.RequestException as exc:
                results.append(type(exc).__name__)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            self.assertTrue(self.entered.wait(2))
            self.proxy.unregister(local)
            reader.join(2)
            self.assertFalse(reader.is_alive())
            self.assertTrue(results)
            self.assertTrue(entry.closed)
            self.assertFalse(entry._responses)
        finally:
            self.release.set()
            reader.join(4)

    def test_error_payload_is_bounded_valid_utf8_and_has_exact_content_length(self):
        request = RecordingRequest()
        self.proxy._send_manifest_error(request, 502, "😀" * (MAX_PROXY_ERROR_CHARS + 100))
        body = request.wfile.getvalue()
        self.assertEqual(request.status, 502)
        self.assertEqual(len(body), MAX_PROXY_ERROR_CHARS * 4)
        self.assertEqual(int(request.headers["Content-Length"]), len(body))
        self.assertEqual(body.decode("utf-8"), "😀" * MAX_PROXY_ERROR_CHARS)

    def test_head_error_declares_bounded_body_length_without_writing_a_body(self):
        request = RecordingRequest()
        self.proxy._send_manifest_error(request, 502, "bad\ud800origin" * 500, body=False)
        self.assertEqual(request.status, 502)
        self.assertEqual(request.wfile.getvalue(), b"")
        self.assertGreater(int(request.headers["Content-Length"]), 0)
        self.assertLessEqual(int(request.headers["Content-Length"]), MAX_PROXY_ERROR_CHARS * 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
