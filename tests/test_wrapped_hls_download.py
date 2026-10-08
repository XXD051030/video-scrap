"""Offline real-media downloads through the explicitly enabled PNG transport.

Run with ``.venv/bin/python tests/test_wrapped_hls_download.py``. FFmpeg creates
short synthetic video/audio; every HTTP request stays on loopback. These checks
exercise the ordinary download, audio extraction, fallback and cancellation
paths after the transport proxy has converted the PNG objects into plain HLS.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import urlsplit
import zlib

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.downloader import VideoDownloader, _HlsByteRangeError, _HlsFallbackError
from src.ffmpeg import resolve_ffmpeg
from src.media_proxy import MediaProxyServer
from src.scraper import VideoItem


def png_payload(data: bytes, *, compressed: bool = False) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(body, zlib.crc32(kind)) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    encoded = zlib.compress(data) if compressed else data
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + chunk(b"roUd", bytes([int(compressed)]) + encoded)
        + chunk(b"IEND", b"")
    )


class WrappedHlsDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ffmpeg = resolve_ffmpeg()
        if not cls.ffmpeg:
            raise unittest.SkipTest("FFmpeg is needed for real-media download checks")
        cls.fixture_tmp = tempfile.TemporaryDirectory(prefix="wrapped-hls-media-")
        cls.addClassCleanup(cls.fixture_tmp.cleanup)
        cls.fixtures = Path(cls.fixture_tmp.name)
        cls.ffmpeg_run(
            "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "3.2", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-g", "10", "-c:a", "aac", "-b:a", "96k",
            str(cls.fixtures / "sample.mp4"),
        )
        cls.ffmpeg_run(
            "-i", str(cls.fixtures / "sample.mp4"), "-c", "copy",
            "-hls_time", "1", "-hls_list_size", "0",
            "-hls_segment_filename", str(cls.fixtures / "segment%02d.ts"),
            str(cls.fixtures / "plain.m3u8"),
        )
        plain = (cls.fixtures / "plain.m3u8").read_text("utf-8")
        cls.segments = sorted(cls.fixtures.glob("segment*.ts"))
        if len(cls.segments) < 3:
            raise AssertionError("Cancellation fixture needs several media segments")
        wrapped = re.sub(r"(segment\d+)\.ts", r"\1.png", plain)
        cls.routes = {
            "/cdn/index.png": (200, "image/png", png_payload(wrapped.encode(), compressed=True)),
            "/plain.m3u8": (200, "application/vnd.apple.mpegurl", plain.encode()),
        }
        for idx, source in enumerate(cls.segments):
            data = source.read_bytes()
            cls.routes[f"/cdn/{source.stem}.png"] = (
                200, "image/png", png_payload(data, compressed=bool(idx % 2)),
            )
            cls.routes[f"/{source.name}"] = (200, "video/mp2t", data)
        invalid = bytearray(png_payload(wrapped.encode()))
        invalid[-1] ^= 1
        cls.routes["/broken.png"] = (200, "image/png", bytes(invalid))

        cls.request_lock = threading.Lock()
        cls.requests_seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.serve(head=False)

            def do_HEAD(self):  # noqa: N802
                self.serve(head=True)

            def serve(self, *, head: bool):
                path = urlsplit(self.path).path
                with cls.request_lock:
                    cls.requests_seen.append((self.command, path, self.headers.get("Range")))
                if path == "/api/hls/fixture":
                    self.send_response(302)
                    self.send_header("Location", "/cdn/index.png")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status, content_type, payload = cls.routes.get(
                    path, (404, "text/plain", b"No fixture here"),
                )
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if not head:
                    try:
                        self.wfile.write(payload)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            def log_message(self, *_args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    @classmethod
    def ffmpeg_run(cls, *args):
        result = subprocess.run(
            [cls.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
        return result

    def setUp(self):
        self.output_tmp = tempfile.TemporaryDirectory(prefix="wrapped-hls-output-")
        self.addCleanup(self.output_tmp.cleanup)
        self.output = Path(self.output_tmp.name)
        with self.request_lock:
            self.requests_seen.clear()
        self.proxies = []

        def create_proxy(*args, **kwargs):
            proxy = MediaProxyServer(*args, **kwargs)
            self.assertEqual(proxy.buffer_seconds(), 0)
            self.proxies.append(proxy)
            return proxy

        patcher = mock.patch("src.media_proxy.MediaProxyServer", side_effect=create_proxy)
        self.proxy_factory = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("src.downloader.resolve_ffmpeg", return_value=self.ffmpeg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def downloader(self, *, connections=2):
        downloader = VideoDownloader(self.output, parallel_connections=connections)
        self.addCleanup(downloader.close)
        return downloader

    def item(self, path="/api/hls/fixture", *, wrapped=True):
        return VideoItem(
            title="Synthetic transport sample", url=self.base + path,
            source_url=self.base + "/fixture-page", referer=self.base + "/fixture-page",
            ext="m3u8", is_direct=True, is_hls=True, hls_png_wrapped=wrapped,
        )

    def assert_proxy_cleaned(self):
        self.assertEqual(len(self.proxies), 1)
        proxy = self.proxies[0]
        self.assertIsNone(proxy._server)
        self.assertFalse(proxy.cache_dir.exists(), "Private decoded cache must be removed")
        self.assertFalse(list(self.output.glob(".video-scrap-job-*")))

    def streams(self, path):
        result = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
        )
        return re.findall(
            r"^\s*Stream #\d+:\d+[^\r\n]*?: (Audio|Video):\s+([\w]+)",
            result.stderr, re.MULTILINE,
        )

    def assert_decodes(self, path, *, audio_only=False):
        self.assertGreater(path.stat().st_size, 0)
        streams = self.streams(path)
        if audio_only:
            self.assertEqual(streams, [("Audio", "mp3")])
        else:
            self.assertEqual({kind for kind, _codec in streams}, {"Audio", "Video"})
        result = self.ffmpeg_run("-xerror", "-i", str(path), "-f", "null", "-")
        self.assertEqual(result.returncode, 0)

    def test_wrapped_video_uses_plain_hls_backends_and_keeps_original_item(self):
        item = self.item()
        original_url = item.url
        progress = []

        def on_progress(value):
            progress.append(value)
            if value.status == "completed":
                self.assertIsNotNone(self.proxies[0]._server)
                self.assertTrue(Path(value.filename).is_file())

        output = self.downloader().download(item, on_progress=on_progress)
        self.assertEqual(item.url, original_url)
        self.assertTrue(item.hls_png_wrapped)
        self.assert_decodes(output)
        self.assertEqual(progress[-1].filename, str(output))
        self.assert_proxy_cleaned()
        with self.request_lock:
            seen = list(self.requests_seen)
        self.assertTrue(any(path.startswith("/cdn/segment") for _method, path, _span in seen))
        self.assertTrue(all(method == "GET" and span is None for method, _path, span in seen))

    def test_audio_format_conversion_remains_inside_proxy_lifetime(self):
        downloader = self.downloader()
        original_extract = downloader._extract_audio

        def extract(*args, **kwargs):
            self.assertIsNotNone(self.proxies[0]._server)
            return original_extract(*args, **kwargs)

        with mock.patch.object(downloader, "_extract_audio", side_effect=extract):
            output = downloader.download(self.item(), quality="audio only", audio_format="mp3")
        self.assertEqual(output.suffix, ".mp3")
        self.assert_decodes(output, audio_only=True)
        self.assert_proxy_cleaned()

    def test_unmarked_hls_never_creates_transport_proxy(self):
        output = self.downloader().download(self.item("/plain.m3u8", wrapped=False))
        self.proxy_factory.assert_not_called()
        self.assert_decodes(output)

    def test_corrupt_manifest_fails_without_publishing_png_or_retrying_other_backends(self):
        downloader = self.downloader()
        with mock.patch.object(downloader, "_download_hls_ytdlp") as fallback:
            with self.assertRaises(_HlsByteRangeError):
                downloader.download(self.item("/broken.png"))
            fallback.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assert_proxy_cleaned()

    def test_cancelled_transfer_removes_private_cache_and_unpublished_stage(self):
        downloader = self.downloader(connections=1)

        def on_progress(value):
            if value.message.startswith("Segment 1/"):
                downloader.cancel()

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            downloader.download(self.item(), on_progress=on_progress)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assert_proxy_cleaned()

    def test_cancel_keep_partial_preserves_only_existing_media_result(self):
        downloader = self.downloader(connections=1)
        progress = []

        def on_progress(value):
            progress.append(value)
            if value.message.startswith("Segment 1/"):
                downloader.cancel(keep_partial=True)

        output = downloader.download(self.item(), on_progress=on_progress)
        self.assertIn(".partial", output.name)
        self.assert_decodes(output)
        self.assertEqual(progress[-1].status, "partial")
        self.assert_proxy_cleaned()

    def test_ytdlp_fallback_retains_normalized_local_manifest(self):
        downloader = self.downloader()

        def fallback(item, _progress):
            self.assertFalse(item.hls_png_wrapped)
            self.assertTrue(item.url.startswith(self.proxies[0].base_url + "/play/"))
            response = requests.get(item.url, timeout=5)
            response.raise_for_status()
            self.assertTrue(response.text.startswith("#EXTM3U"))
            urls = [line for line in response.text.splitlines() if line and not line.startswith("#")]
            self.assertTrue(all(url.startswith(self.proxies[0].base_url) for url in urls))
            segment = requests.get(urls[0], timeout=5)
            segment.raise_for_status()
            self.assertEqual(segment.content, self.segments[0].read_bytes())
            target = downloader.output_dir / "fallback.mp4"
            shutil.copyfile(self.fixtures / "sample.mp4", target)
            return target

        with mock.patch.object(downloader, "_remux_hls_segments", side_effect=RuntimeError("forced remux error")):
            with mock.patch.object(downloader, "_download_hls_ytdlp", side_effect=fallback):
                output = downloader.download(self.item())
        self.assert_decodes(output)
        self.assert_proxy_cleaned()

    def test_real_ffmpeg_fallback_can_read_the_same_local_manifest_and_segments(self):
        downloader = self.downloader()
        def force_fallback(item, _progress):
            raise _HlsFallbackError("forced native failure", item.url, None)

        with mock.patch.object(downloader, "_download_hls_parallel", side_effect=force_fallback):
            with mock.patch.object(downloader, "_download_hls_ytdlp", side_effect=RuntimeError("forced yt-dlp failure")):
                output = downloader.download(self.item())
        self.assert_decodes(output)
        self.assert_proxy_cleaned()

    def test_setup_failure_still_stops_the_created_proxy(self):
        with mock.patch.object(MediaProxyServer, "register", side_effect=RuntimeError("setup failed")):
            with self.assertRaisesRegex(RuntimeError, "setup failed"):
                self.downloader().download(self.item())
        self.assertEqual(list(self.output.iterdir()), [])
        self.assert_proxy_cleaned()


if __name__ == "__main__":
    unittest.main(verbosity=2)
