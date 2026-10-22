"""Keep fallback inputs fixed and validate encrypted resources before use."""
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from Crypto.Cipher import AES

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
from src.downloader import (
    VideoDownloader, _HlsIntegrityError, _parse_hls_iv,
    _aes_cbc_decrypt_bytes, _strip_pkcs7_padding,
)
from src.hls_snapshot import HlsSnapshotServer, ManifestSnapshot
from src.scraper import VideoItem
from test_hls_integrity_audio import HlsIntegrityAudioTests


def validate_aes(body, key, iv):
    if not body or len(body) % 16:
        raise _HlsIntegrityError("Invalid AES block size")
    previous = body[-32:-16] if len(body) > 16 else iv
    _strip_pkcs7_padding(_aes_cbc_decrypt_bytes(body[-16:], key, previous))


class SnapshotResourceTests(unittest.TestCase):
    def snapshot(self, text, fetch):
        url = "https://origin.example/list.m3u8"
        return HlsSnapshotServer({url: ManifestSnapshot(url, text)}, fetch,
                                 validate_aes, _parse_hls_iv, (_HlsIntegrityError,))

    def test_backend_range_retry_uses_one_frozen_representation(self):
        calls = []
        def fetch(url, *_):
            calls.append(url)
            return b"ABCDEFGH" if len(calls) == 1 else b"XXXXXXXX"
        server = self.snapshot("#EXTM3U\n#EXTINF:1,\nclip.ts\n#EXT-X-ENDLIST\n", fetch)
        with server:
            manifest = requests.get(server.url("https://origin.example/list.m3u8"), timeout=2)
            media = next(line for line in manifest.text.splitlines() if line.startswith("http"))
            first = requests.get(media, headers={"Range": "bytes=0-3"}, timeout=2)
            second = requests.get(media, headers={"Range": "bytes=4-7"}, timeout=2)
            self.assertEqual((first.status_code, second.status_code), (206, 206))
            self.assertEqual(first.content + second.content, b"ABCDEFGH")
            self.assertEqual(len(calls), 1)
            self.assertEqual(requests.head(media, timeout=2).headers["Content-Length"], "8")
            directory = Path(server._temporary.name)
        self.assertFalse(directory.exists())

    def test_upstream_cannot_reference_a_local_file_or_unknown_playlist(self):
        for text in (
            "#EXTM3U\n#EXTINF:1,\nfile:///etc/passwd\n",
            '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="file:///etc/passwd"\n#EXTINF:1,\na.ts\n',
            '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key",IV=0xnothex\n#EXTINF:1,\na.ts\n',
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nunknown.m3u8\n",
            "#EXTM3U\n#EXTINF:1,\ndata:video/mp2t;base64,AAAA\n",
        ):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    with self.snapshot(text, lambda *_: b"must not be fetched"):
                        self.fail("Unsafe input accepted")

    def test_invalid_padding_is_rejected_before_backend_receives_ciphertext(self):
        key, iv = bytes(range(16)), bytes(16)
        text = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key",IV=0x0\n#EXTINF:1,\na.ts\n'
        for good in (False, True):
            plaintext = b"media" + (b"\x0b" * 11 if good else b"\0" * 11)
            ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(plaintext)
            def fetch(url, *_):
                return key if url.endswith("/key") else ciphertext
            with self.subTest(good=good), self.snapshot(text, fetch) as server:
                manifest = requests.get(server.url("https://origin.example/list.m3u8"), timeout=2)
                media = next(line for line in manifest.text.splitlines() if line.startswith("http"))
                response = requests.get(media, timeout=2)
                if good:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, ciphertext)
                    server.raise_if_failed()
                else:
                    self.assertEqual(response.status_code, 502)
                    with self.assertRaises(_HlsIntegrityError):
                        server.raise_if_failed()

    def test_stop_interrupts_a_backend_that_is_not_reading_its_response(self):
        server = self.snapshot("#EXTM3U\n#EXTINF:1,\na.ts\n", lambda *_: b"x" * (4 * 1024 * 1024))
        with server:
            text = requests.get(server.url("https://origin.example/list.m3u8"), timeout=2).text
            media = next(line for line in text.splitlines() if line.startswith("http"))
            parsed = urlsplit(media)
            client = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
            client.request("GET", parsed.path)
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            started = time.monotonic()
        self.assertLess(time.monotonic() - started, 2)
        response.close()
        client.close()


class RealFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        HlsIntegrityAudioTests.setUpClass()
        cls.addClassCleanup(HlsIntegrityAudioTests.doClassCleanups)
        cls.ffmpeg = HlsIntegrityAudioTests.ffmpeg
        cls.fragments = [(HlsIntegrityAudioTests.fixtures / f"video{i:02d}.ts").read_bytes()
                         for i in range(2)]

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name)
        self.requests = Counter()
        self.lock = threading.Lock()
        self.failures = 10
        self.encrypted = False
        self.invalid_padding = False
        owner = self
        self.plain = b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\na.ts\n#EXTINF:1,\nb.ts\n#EXT-X-ENDLIST\n"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                with owner.lock:
                    owner.requests[self.path] += 1
                    count = owner.requests[self.path]
                if self.path in {"/a.ts", "/b.ts"} and count <= owner.failures:
                    self.send_error(503)
                    return
                key, iv = bytes(range(16)), bytes(16)
                if self.path == "/list.m3u8":
                    if owner.encrypted:
                        body = (b'#EXTM3U\n#EXT-X-TARGETDURATION:1\n'
                                b'#EXT-X-KEY:METHOD=AES-128,URI="key",IV=0x0\n'
                                b'#EXTINF:1,\na.ts\n#EXT-X-ENDLIST\n')
                    elif count == 1:
                        body = owner.plain
                    else:
                        first, second = map(len, owner.fragments)
                        body = (f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n"
                                f"#EXT-X-BYTERANGE:{first}@0\nobject.ts\n#EXTINF:1,\n"
                                f"#EXT-X-BYTERANGE:{second}@{first}\nobject.ts\n#EXT-X-ENDLIST\n").encode()
                    mime = "application/vnd.apple.mpegurl"
                elif self.path == "/key":
                    body, mime = key, "application/octet-stream"
                else:
                    body = (b"".join(owner.fragments) if self.path == "/object.ts"
                            else owner.fragments[0 if self.path == "/a.ts" else 1])
                    if owner.encrypted:
                        count = 16 - len(body) % 16
                        body += (b"\0" if owner.invalid_padding else bytes([count])) * count
                        body = AES.new(key, AES.MODE_CBC, iv).encrypt(body)
                    mime = "video/mp2t"
                self.send_response(200)  # Deliberately ignores any Range.
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", mime)
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.runner = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.runner.start()
        self.addCleanup(self.runner.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/list.m3u8"
        self.downloader = VideoDownloader(self.output)
        self.addCleanup(self.downloader.close)

    def item(self):
        return VideoItem("fixed", self.url, self.url, is_direct=True, is_hls=True)

    def assert_duration(self, path, seconds):
        result = subprocess.run([self.ffmpeg, "-hide_banner", "-i", str(path)],
                                capture_output=True, text=True, timeout=10)
        duration = re.search(r"Duration: 00:00:([0-9.]+)", result.stderr)
        self.assertIsNotNone(duration, result.stderr)
        self.assertAlmostEqual(float(duration[1]), seconds, delta=.15)

    def test_real_503_retry_uses_the_first_manifest_and_publishes_two_seconds(self):
        result = self.downloader.download(self.item())
        self.assert_duration(result, 2)
        self.assertEqual(self.requests["/list.m3u8"], 1)
        self.assertEqual(self.requests["/object.ts"], 0)
        self.assertGreater(self.requests["/a.ts"], 10)

    def test_real_ffmpeg_fallback_also_uses_the_fixed_manifest(self):
        self.failures = 0
        with patch.object(self.downloader, "_download_hls_segments_parallel", side_effect=RuntimeError("native unavailable")), \
                patch.object(self.downloader, "_download_hls_ytdlp", side_effect=RuntimeError("yt-dlp unavailable")):
            result = self.downloader.download(self.item())
        self.assert_duration(result, 2)
        self.assertEqual(self.requests["/list.m3u8"], 1)

    def test_valid_encrypted_fallback_still_completes(self):
        self.failures, self.encrypted = 0, True
        with patch.object(self.downloader, "_download_hls_segments_parallel", side_effect=RuntimeError("native unavailable")):
            result = self.downloader.download(self.item())
        self.assert_duration(result, 1)

    def test_ffmpeg_live_window_fallback_finishes_without_reloading_upstream(self):
        self.failures = 0
        self.plain = self.plain.replace(b"#EXT-X-ENDLIST\n", b"")
        timeout = threading.Timer(15, self.downloader.cancel)
        timeout.start()
        try:
            with patch.object(self.downloader, "_download_hls_segments_parallel", side_effect=RuntimeError("native unavailable")), \
                    patch.object(self.downloader, "_download_hls_ytdlp", side_effect=RuntimeError("yt-dlp unavailable")):
                result = self.downloader.download(self.item())
            self.assert_duration(result, 2)
            self.assertEqual(self.requests["/list.m3u8"], 1)
        finally:
            timeout.cancel()

    def test_invalid_native_padding_never_reaches_another_backend(self):
        self.failures, self.encrypted, self.invalid_padding = 0, True, True
        with patch.object(self.downloader, "_download_hls_ytdlp") as fallback:
            with self.assertRaises(_HlsIntegrityError):
                self.downloader.download(self.item())
        fallback.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_fallback_rejects_bad_padding_and_padding_only_ciphertext_before_publication(self):
        self.failures, self.encrypted = 0, True
        for empty in (False, True):
            with self.subTest(empty=empty):
                self.invalid_padding = not empty
                if empty:
                    self.fragments = [b"", self.fragments[1]]

                def consume(snapshot_item, *_args):
                    manifest = requests.get(snapshot_item.url, timeout=2)
                    media = next(line for line in manifest.text.splitlines() if line.startswith("http"))
                    response = requests.get(media, timeout=2)
                    response.raise_for_status()
                    raise AssertionError("Malformed encrypted resource reached the backend")

                with patch.object(self.downloader, "_download_hls_segments_parallel", side_effect=RuntimeError("native unavailable")), \
                        patch.object(self.downloader, "_download_hls_ytdlp", side_effect=consume), \
                        patch.object(self.downloader, "_download_hls_ffmpeg") as final_backend:
                    with self.assertRaises(_HlsIntegrityError):
                        self.downloader.download(self.item())
                final_backend.assert_not_called()
                self.assertEqual(list(self.output.iterdir()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
