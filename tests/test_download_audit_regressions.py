"""Focused regressions for the verified downloader audit findings."""

from __future__ import annotations

from contextlib import contextmanager
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yt_dlp

from src import downloader as module
from src import parallel_downloader
from src.download_job import DownloadJob, _item_payload
from src.downloader import DownloadProgress, VideoDownloader
from src.ffmpeg import resolve_ffmpeg
from src.scraper import VideoItem
from test_parallel_downloader import FakeSession, PAYLOAD


MASTER = '''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="low-a",NAME="low",URI="audio-low.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="high-a",NAME="high",URI="audio-high.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=1920x1080,AUDIO="high-a"
high.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=1280x720,AUDIO="high-a"
middle.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=100000,RESOLUTION=640x360,AUDIO="low-a"
low.m3u8
'''
BASE = "https://media.example.test/"
MEDIA = "#EXTM3U\n#EXTINF:1,\npart.ts\n#EXT-X-ENDLIST\n"


class Response:
    def __init__(self, text, url):
        self.text, self.url = text, url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def raise_for_status(self):
        pass


class DownloadAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.downloader = VideoDownloader(self.directory)
        self.addCleanup(self.downloader.close)

    def item(self, **options):
        return VideoItem(title="Audit", url=BASE + "master.m3u8",
                         source_url=BASE + "watch", is_direct=True, is_hls=True,
                         **options)

    def route_manifests(self):
        seen = []

        def get(url, **_kwargs):
            seen.append(url)
            return Response(MASTER if url.endswith("master.m3u8") else MEDIA, url)

        return seen, mock.patch.object(self.downloader._session, "get", side_effect=get)

    def test_quality_selects_declared_height_and_associated_audio(self):
        for quality, child, audio in (("480p", "low", "low"),
                                      ("720p", "middle", "high"),
                                      ("1080p", "high", "high"),
                                      ("best", "high", "high")):
            with self.subTest(quality=quality):
                selected = self.downloader._pick_hls_variant(MASTER, BASE, quality)
                self.assertEqual(selected, BASE + child + ".m3u8")
                self.assertEqual(self.downloader._pick_hls_audio_rendition(
                    MASTER, BASE, selected), BASE + "audio-" + audio + ".m3u8")

    def test_quality_does_not_silently_exceed_requested_height(self):
        with self.assertRaisesRegex(RuntimeError, "at or below 240p"):
            self.downloader._pick_hls_variant(MASTER, BASE, "240p")
        unknown = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nunknown.m3u8\n"
        with self.assertRaisesRegex(RuntimeError, "declared height"):
            self.downloader._pick_hls_variant(unknown, BASE, "480p")

    def test_native_quality_fetches_low_video_and_low_audio_only(self):
        seen, routes = self.route_manifests()
        with routes, mock.patch.object(module, "resolve_ffmpeg", return_value="ffmpeg"), \
                mock.patch.object(self.downloader, "_download_hls_segments_parallel", return_value=self.directory / "done.mp4"):
            self.downloader._download_hls_parallel(self.item(), None, quality="480p")
        self.assertEqual(seen, [BASE + "master.m3u8", BASE + "low.m3u8", BASE + "audio-low.m3u8"])

    def test_hls_ytdlp_receives_quality_constraint(self):
        captured = {}

        class FakeYDL:
            def __init__(_, options):
                captured.update(options)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def download(_, _urls):
                path = self.directory / "fallback.mp4"
                path.write_bytes(b"media")
                captured["post_hooks"][0](str(path))
                return 0

        with mock.patch.object(module.yt_dlp, "YoutubeDL", FakeYDL):
            self.downloader._download_hls_ytdlp(self.item(), None, quality="480p")
        self.assertEqual(captured["format"], module.QUALITY_FORMATS["480p"])
        self.assertFalse(captured["skip_unavailable_fragments"])

    def test_ffmpeg_quality_fallback_maps_selected_video_and_audio(self):
        seen, routes = self.route_manifests()
        commands = []

        @contextmanager
        def process(command, **_kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"media")
            yield mock.Mock(stderr=[], wait=lambda: 0)

        item = self.item()
        item.duration = 1
        with routes, mock.patch.object(self.downloader, "_ffmpeg_process", process):
            self.downloader._download_hls_ffmpeg(item, "ffmpeg", None, quality="480p")
        inputs = [commands[0][index + 1] for index, value in enumerate(commands[0]) if value == "-i"]
        self.assertEqual(inputs, [BASE + "low.m3u8", BASE + "audio-low.m3u8"])
        self.assertEqual(seen, [BASE + "master.m3u8", BASE + "low.m3u8", BASE + "audio-low.m3u8"])

    def test_direct_dash_uses_ytdlp_instead_of_plain_file_or_hls(self):
        item = self.item(is_dash=True)
        item.is_hls = False
        item.url = BASE + "media.mpd"
        captured = {}

        class FakeYDL:
            def __init__(_, options):
                captured.update(options)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def download(_, urls):
                captured["urls"] = urls
                path = Path(captured["outtmpl"].replace("%(ext)s", "mp4"))
                path.write_bytes(b"merged media")
                captured["post_hooks"][0](str(path))
                return 0

        with mock.patch.object(module.yt_dlp, "YoutubeDL", FakeYDL), \
                mock.patch.object(self.downloader, "_download_direct") as direct, \
                mock.patch.object(self.downloader, "_download_hls_parallel") as hls:
            result = self.downloader.download(item, quality="720p")
        direct.assert_not_called()
        hls.assert_not_called()
        self.assertEqual(result.read_bytes(), b"merged media")
        self.assertEqual(captured["urls"], [item.url])
        self.assertEqual(captured["format"], module.QUALITY_FORMATS["720p"])

    def test_retry_progress_restores_pause_without_overriding_cancellation(self):
        states = []
        job = DownloadJob(self.item(), self.directory, on_state=states.append)
        job._update_progress_state(DownloadProgress(status="finalizing"))
        self.assertFalse(job.pause())
        job._update_progress_state(DownloadProgress(status="downloading"))
        self.assertEqual(states, ["finalizing", "downloading"])
        self.assertTrue(job.pause())
        job.cancel()
        job._update_progress_state(DownloadProgress(status="downloading"))
        self.assertEqual(job.state, "cancelling")

    def test_job_payload_preserves_dash_and_cookie_records(self):
        cookies = [{"name": "auth", "value": "session", "domain": "media.example.test",
                    "path": "/video/", "secure": True, "host_only": True}]
        item = self.item(is_dash=True, browser_cookies=cookies)
        restored = VideoItem(**_item_payload(item))
        self.assertTrue(restored.is_dash)
        self.assertEqual(restored.browser_cookies, cookies)

    def test_fsync_actual_storage_errors_never_publish(self):
        for code in (errno.EIO, errno.ENOSPC, errno.EROFS):
            with self.subTest(errno=code):
                path = self.directory / "existing.mp4"
                path.write_bytes(b"existing")
                session = FakeSession(PAYLOAD, supports_ranges=True)
                with mock.patch.object(parallel_downloader, "build_session", return_value=session):
                    parallel = parallel_downloader.ParallelDownloader(connections=2, min_segment_size=1024)
                with mock.patch.object(parallel_downloader.os, "fsync", side_effect=OSError(code, "writeback failed")):
                    with self.assertRaises(OSError):
                        parallel.download(BASE + "file.mp4", path)
                self.assertEqual(list(self.directory.iterdir()), [path])
                self.assertEqual(path.read_bytes(), b"existing")
                path.unlink()

    def test_fsync_explicitly_unsupported_filesystem_can_publish(self):
        session = FakeSession(PAYLOAD, supports_ranges=False)
        with mock.patch.object(parallel_downloader, "build_session", return_value=session):
            parallel = parallel_downloader.ParallelDownloader(connections=1)
        with mock.patch.object(parallel_downloader.os, "fsync", side_effect=OSError(errno.ENOTSUP, "unsupported")):
            result = parallel.download(BASE + "file.mp4", self.directory / "done.mp4")
        self.assertEqual(result.path.read_bytes(), PAYLOAD)

    def test_concat_quotes_preserve_real_paths_in_ffmpeg(self):
        ffmpeg = resolve_ffmpeg()
        if not ffmpeg:
            self.fail("FFmpeg is required to verify concat escaping")
        # A backslash is a legal POSIX filename character and a Windows path
        # separator; this exercises token preservation as well as apostrophes.
        directory = self.directory / "space ' quote \\ directory"
        directory.mkdir()
        source = directory / "segment ' 1.ts"
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "sine=frequency=400", "-t", "0.3", "-c:a", "aac", str(source)],
                       check=True, capture_output=True)
        concat = directory / "concat.txt"
        self.downloader._write_ffmpeg_concat_list([source], concat)
        output = directory / "merged.m4a"
        result = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                                 "-safe", "0", "-i", str(concat), "-c", "copy", str(output)],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertGreater(output.stat().st_size, 0)

    def test_ydl_browser_cookie_policy_preserves_host_path_secure_and_expiry(self):
        records = [
            {"name": "host", "value": "1", "domain": "media.example.test", "host_only": True},
            {"name": "domain", "value": "1", "domain": ".example.test", "host_only": False},
            {"name": "path", "value": "1", "domain": "media.example.test", "path": "/private"},
            {"name": "secure", "value": "1", "domain": "media.example.test", "secure": True},
            {"name": "expired", "value": "1", "domain": "media.example.test", "expires": int(time.time()) - 1},
        ]
        item = self.item(browser_cookies=records)
        options = {"quiet": True, "proxy": ""}
        self.downloader._configure_browser_ydl(item, options)
        with yt_dlp.YoutubeDL(options) as ydl:
            self.downloader._install_browser_cookies(item, ydl)
            header = ydl.cookiejar.get_cookie_header(BASE + "private/video") or ""
            self.assertIn("host=1", header)
            self.assertIn("path=1", header)
            self.assertIn("secure=1", header)
            self.assertNotIn("expired=", header)
            self.assertEqual(ydl.cookiejar.get_cookie_header("https://child.media.example.test/public"), "domain=1")
            insecure = ydl.cookiejar.get_cookie_header("http://media.example.test/public") or ""
            self.assertNotIn("secure=", insecure)
            self.assertNotIn("path=", insecure)
            self.assertIsNone(ydl.cookiejar.get_cookie_header("https://unrelated.test/private"))
            self.assertEqual(options["external_downloader"], "native")


class AuthenticatedDownloadTests(unittest.TestCase):
    """Actual HTTP, native HLS and yt-dlp DASH with synthetic media only."""

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.ffmpeg = resolve_ffmpeg()
        if not cls.ffmpeg:
            raise AssertionError("FFmpeg is required")
        source = cls.root / "source.mp4"
        common = [cls.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        subprocess.run(common + ["-f", "lavfi", "-i", "testsrc2=size=160x90:rate=5",
                                "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1.2",
                                "-c:v", "libx264", "-preset", "ultrafast", "-g", "5",
                                "-c:a", "aac", str(source)], check=True, capture_output=True)
        subprocess.run(common + ["-i", str(source), "-c", "copy", "-hls_time", "0.5",
                                "-hls_list_size", "0", "-hls_segment_filename", str(cls.root / "part%02d.ts"),
                                str(cls.root / "media.m3u8")], check=True, capture_output=True)
        subprocess.run(common + ["-i", str(source), "-map", "0:v:0", "-map", "0:a:0",
                                "-c", "copy", "-seg_duration", "0.5", "-f", "dash",
                                str(cls.root / "media.mpd")], check=True, capture_output=True)
        cls.seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_HEAD(self):
                self.serve(head=True)

            def do_GET(self):
                self.serve(head=False)

            def serve(self, *, head):
                path = urlsplit(self.path).path
                cls.seen.append((path, self.headers.get("Host", ""), self.headers.get("Cookie", "")))
                if path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", f"http://localhost:{self.server.server_port}/echo")
                    self.end_headers()
                    return
                if path == "/echo":
                    payload = self.headers.get("Cookie", "").encode()
                elif path.startswith("/protected/"):
                    if "auth=allowed" not in self.headers.get("Cookie", ""):
                        self.send_error(403)
                        return
                    resource = cls.root / path.removeprefix("/protected/")
                    if not resource.is_file():
                        self.send_error(404)
                        return
                    payload = resource.read_bytes()
                else:
                    self.send_error(404)
                    return
                start, end = 0, len(payload) - 1
                ranged = self.headers.get("Range", "").startswith("bytes=")
                if ranged:
                    span = self.headers["Range"][6:].split("-", 1)
                    start, end = int(span[0]), int(span[1]) if span[1] else end
                    end = min(end, len(payload) - 1)
                self.send_response(206 if ranged else 200)
                self.send_header("Content-Length", str(max(0, end - start + 1)))
                self.send_header("Accept-Ranges", "bytes")
                if path.endswith(".mpd"):
                    self.send_header("Content-Type", "application/dash+xml")
                if ranged:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self.end_headers()
                if not head:
                    self.wfile.write(payload[start:end + 1])

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.server.shutdown)
        cls.addClassCleanup(cls.server.server_close)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.cookies = [{"name": "auth", "value": "allowed", "domain": "127.0.0.1",
                        "host_only": True, "path": "/", "secure": False}]

    def setUp(self):
        self.temporary_output = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_output.cleanup)
        self.downloader = VideoDownloader(Path(self.temporary_output.name))
        self.addCleanup(self.downloader.close)

    def test_real_direct_download_receives_browser_session(self):
        item = VideoItem("Private direct", self.base + "/protected/source.mp4", self.base,
                         is_direct=True, browser_cookies=self.cookies)
        result = self.downloader.download(item)
        self.assertEqual(result.read_bytes(), (self.root / "source.mp4").read_bytes())

    def test_real_hls_proxy_forwards_session_to_manifest_and_segments(self):
        item = VideoItem("Private HLS", self.base + "/protected/media.m3u8", self.base,
                         is_direct=True, is_hls=True, browser_cookies=self.cookies)
        result = self.downloader.download(item)
        probe = subprocess.run([self.ffmpeg, "-hide_banner", "-i", str(result)], capture_output=True)
        self.assertIn(b"Video:", probe.stderr)
        self.assertIn(b"Audio:", probe.stderr)
        self.assertTrue(any(path.endswith(".ts") and "auth=allowed" in cookie
                            for path, _host, cookie in self.seen))

    def test_real_dash_ytdlp_receives_session_for_manifest_and_segments(self):
        item = VideoItem("Private DASH", self.base + "/protected/media.mpd", self.base,
                         is_direct=True, is_dash=True, browser_cookies=self.cookies)
        result = self.downloader.download(item)
        probe = subprocess.run([self.ffmpeg, "-hide_banner", "-i", str(result)], capture_output=True)
        self.assertIn(b"Video:", probe.stderr)
        self.assertIn(b"Audio:", probe.stderr)

    def test_real_ydl_redirect_does_not_forward_host_only_cookie_to_alias(self):
        item = VideoItem("Cookie scopes", self.base, self.base, browser_cookies=self.cookies)
        options = {"quiet": True, "proxy": ""}
        self.downloader._configure_browser_ydl(item, options)
        with yt_dlp.YoutubeDL(options) as ydl:
            self.downloader._install_browser_cookies(item, ydl)
            # Inspect the actual selected handler as well as its wire behavior.
            with ydl.urlopen(self.base + "/redirect") as response:
                self.assertEqual(response.read(), b"")
        start = next(row for row in reversed(self.seen) if row[0] == "/redirect")
        self.assertIn("auth=allowed", start[2])
        self.assertEqual(self.seen[-1][0], "/echo")
        self.assertEqual(self.seen[-1][2], "")


if __name__ == "__main__":
    unittest.main()
