"""Real-media regressions for audio-only downloads, without external services.

Run with ``build/.venv/bin/python tests/test_audio_downloads.py``. FFmpeg creates
small fixtures and decodes every successful result. A local HTTP server exercises
the actual direct and byte-range HLS downloaders; only website extraction is
replaced so these tests do not depend on site logins or availability.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.downloader import VideoDownloader, _HlsByteRangeError
from src.ffmpeg import resolve_ffmpeg
from src.scraper import VideoItem


def _ffmpeg_binary() -> str:
    binary = shutil.which("ffmpeg")
    if binary:
        return binary
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise RuntimeError("These real-media tests require FFmpeg") from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


class AudioDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ffmpeg = _ffmpeg_binary()
        cls.fixture_dir = tempfile.TemporaryDirectory(prefix="audio-test-media-")
        cls.addClassCleanup(cls.fixture_dir.cleanup)
        cls.fixtures = Path(cls.fixture_dir.name)
        cls.requests_seen: list[tuple[str, str | None]] = []
        cls.requests_lock = threading.Lock()
        cls.invalid_ranges: dict[str, str] = {}

        cls.run_ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "3.2", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-g", "10", "-c:a", "aac",
            "-b:a", "96k", "-movflags", "+faststart", str(cls.fixtures / "av.mp4"),
        )
        cls.run_ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000",
            "-t", "1.4", "-c:v", "libvpx", "-deadline", "realtime",
            "-cpu-used", "8", "-c:a", "libopus", str(cls.fixtures / "av.webm"),
        )
        cls.run_ffmpeg(
            "-i", str(cls.fixtures / "av.mp4"), "-map", "0:v:0",
            "-c", "copy", "-an", str(cls.fixtures / "silent.mp4"),
        )
        cls.run_ffmpeg(
            "-i", str(cls.fixtures / "av.webm"), "-map", "0:a:0",
            "-c", "copy", str(cls.fixtures / "audio.webm"),
        )
        cls.run_ffmpeg(
            "-i", str(cls.fixtures / "av.mp4"), "-c", "copy",
            "-hls_time", "1", "-hls_list_size", "0", "-hls_flags", "single_file",
            "-hls_segment_filename", str(cls.fixtures / "media.ts"),
            str(cls.fixtures / "av.m3u8"),
        )
        manifest = (cls.fixtures / "av.m3u8").read_text("utf-8")
        if manifest.count("#EXT-X-BYTERANGE:") < 3:
            raise AssertionError("Fixture must contain several real HLS byte ranges")
        cls.run_ffmpeg(
            "-i", str(cls.fixtures / "av.mp4"), "-map", "0:a:0", "-c", "copy",
            "-hls_time", "1", "-hls_list_size", "0", "-hls_flags", "single_file",
            "-hls_segment_filename", str(cls.fixtures / "audio-media.ts"),
            str(cls.fixtures / "audio-main.m3u8"),
        )
        audio_manifest = (cls.fixtures / "audio-main.m3u8").read_text("utf-8")
        if audio_manifest.count("#EXT-X-BYTERANGE:") < 3:
            raise AssertionError("Audio rendition fixture must have real byte ranges")
        (cls.fixtures / "master.m3u8").write_text(
            '#EXTM3U\n'
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="wrong",NAME="Wrong group",'
            'DEFAULT=YES,AUTOSELECT=YES,URI="wrong-group.m3u8"\n'
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="main",NAME="Alternate",'
            'DEFAULT=NO,AUTOSELECT=YES,URI="nondefault-audio.m3u8"\n'
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="main",NAME="Default",'
            'DEFAULT=YES,AUTOSELECT=YES,URI="audio-main.m3u8"\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=300000,CODECS="avc1.42c00a,mp4a.40.2",'
            'AUDIO="main"\n'
            'missing-video.m3u8\n', encoding="utf-8",
        )
        (cls.fixtures / "shared-video-master.m3u8").write_text(
            '#EXTM3U\n'
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="A",NAME="High variant audio",'
            'DEFAULT=YES,AUTOSELECT=YES,URI="audio-main.m3u8"\n'
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="B",NAME="Low variant audio",'
            'DEFAULT=YES,AUTOSELECT=YES,URI="wrong-group.m3u8"\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=500000,AUDIO="A"\n'
            'missing-video.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=200000,AUDIO="B"\n'
            'missing-video.m3u8\n', encoding="utf-8",
        )
        (cls.fixtures / "inband-master.m3u8").write_text(
            '#EXTM3U\n'
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="main",NAME="Default in-band",'
            'DEFAULT=YES,AUTOSELECT=YES\n'
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="main",NAME="Another language",'
            'DEFAULT=NO,AUTOSELECT=YES,URI="nondefault-audio.m3u8"\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=300000,AUDIO="main"\n'
            'av.m3u8\n', encoding="utf-8",
        )

        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self) -> None:
                self.serve(head=True)

            def do_GET(self) -> None:
                self.serve(head=False)

            def serve(self, *, head: bool) -> None:
                name = unquote(urlsplit(self.path).path).lstrip("/")
                with cls.requests_lock:
                    cls.requests_seen.append((name, self.headers.get("Range")))
                source = cls.fixtures / name
                if source.parent != cls.fixtures or not source.is_file():
                    self.send_error(404)
                    return
                data = source.read_bytes()
                total = len(data)
                start, end = 0, total - 1
                span = self.headers.get("Range")
                if span:
                    match = re.fullmatch(r"bytes=(\d+)-(\d*)", span)
                    if match is None:
                        self.send_error(416)
                        return
                    start = int(match.group(1))
                    end = min(int(match.group(2) or total - 1), total - 1)
                    if not 0 <= start <= end < total:
                        self.send_error(416)
                        return
                    invalid_mode = cls.invalid_ranges.get(name)
                    if invalid_mode == "whole-file-200":
                        start, end = 0, total - 1
                        self.send_response(200)
                    else:
                        self.send_response(206)
                        offset = 1 if invalid_mode == "wrong-206" else 0
                        self.send_header("Content-Range", f"bytes {start + offset}-{end + offset}/{total}")
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                if not head:
                    try:
                        self.wfile.write(data[start:end + 1])
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            def log_message(self, *_args: object) -> None:
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    @classmethod
    def run_ffmpeg(cls, *args: str) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [cls.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
        return result

    def setUp(self) -> None:
        self.output_tmp = tempfile.TemporaryDirectory(prefix="audio-test-downloads-")
        self.addCleanup(self.output_tmp.cleanup)
        self.output = Path(self.output_tmp.name)
        patcher = mock.patch(
            "src.downloader.resolve_ffmpeg", return_value=self.ffmpeg,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def downloader(self, *, connections: int = 2) -> VideoDownloader:
        result = VideoDownloader(self.output, parallel_connections=connections)
        self.addCleanup(result.close)
        return result

    def item(self, media: str = "av.mp4", *, title: str = "Audio sample") -> VideoItem:
        url = f"{self.base}/{media}"
        return VideoItem(
            title=title, url=url, source_url=self.base, ext=Path(media).suffix[1:],
            is_direct=True, is_hls=media.endswith(".m3u8"),
        )

    def streams(self, path: Path) -> list[tuple[str, str]]:
        result = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
        )
        return re.findall(
            r"^\s*Stream #\d+:\d+[^\r\n]*?: (Audio|Video):\s+([\w]+)",
            result.stderr, re.MULTILINE,
        )

    def assert_audio(self, path: Path, codec: str) -> None:
        self.assertTrue(path.is_file())
        self.assertGreater(path.stat().st_size, 0)
        self.assertEqual(self.streams(path), [("Audio", codec)])
        decoded = self.run_ffmpeg(
            "-xerror", "-i", str(path), "-map", "0:a:0", "-f", "s16le", "-",
        )
        self.assertGreater(len(decoded.stdout), 20_000, "Audio must decode into actual samples")

    def compressed_packet_hashes(self, path: Path) -> list[tuple[str, str]]:
        result = self.run_ffmpeg(
            "-i", str(path), "-map", "0:a:0", "-c", "copy", "-f", "framehash", "-",
        )
        # Ignore container-dependent timestamps. Packet sizes and SHA256 hashes
        # prove the actual encoded audio packets survived unchanged.
        entries = []
        for line in result.stdout.decode().splitlines():
            if not line.startswith("#") and line.strip():
                fields = [field.strip() for field in line.split(",")]
                # Later fields can describe skip-sample side data. The packet
                # payload itself is always the fifth and sixth fields.
                entries.append((fields[4], fields[5]))
        self.assertTrue(entries)
        return entries

    def assert_clean(self, *expected: Path) -> None:
        self.assertEqual(set(self.output.iterdir()), set(expected))

    def fake_ydl(self, source: Path, captured: list[dict]):
        class FakeYDL:
            def __init__(self, options: dict) -> None:
                self.options = options
                captured.append(options)

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                pass

            def download(self, _urls: list[str]) -> int:
                final = Path(self.options["outtmpl"].replace("%(ext)s", source.suffix[1:]))
                shutil.copyfile(source, final)
                for hook in self.options["progress_hooks"]:
                    hook({"status": "finished", "filename": str(final)})
                for hook in self.options["post_hooks"]:
                    hook(str(final))
                return 0

        return FakeYDL

    def test_direct_mp4_and_webm_produce_all_three_audio_formats(self) -> None:
        results = []
        for media, original_codec, original_ext in (
            ("av.mp4", "aac", ".m4a"), ("av.webm", "opus", ".opus"),
        ):
            for audio_format, codec, extension in (
                ("original", original_codec, original_ext),
                ("mp3", "mp3", ".mp3"), ("m4a", "aac", ".m4a"),
            ):
                with self.subTest(source=media, audio_format=audio_format):
                    updates = []
                    result = self.downloader().download(
                        self.item(media), "audio only", updates.append,
                        audio_format=audio_format,
                    )
                    results.append(result)
                    self.assertEqual(result.suffix, extension)
                    self.assert_audio(result, codec)
                    self.assertEqual(updates[-1].status, "completed")
                    self.assertEqual(updates[-1].filename, str(result))
                    self.assertEqual(updates[-1].downloaded_bytes, result.stat().st_size)
                    if audio_format == "original":
                        self.assertEqual(
                            self.compressed_packet_hashes(result),
                            self.compressed_packet_hashes(self.fixtures / media),
                        )
        self.assertEqual(len(set(results)), 6)
        self.assert_clean(*results)

    def test_website_with_audio_stream_retains_packets_or_converts(self) -> None:
        captured: list[dict] = []
        results = []
        source = self.fixtures / "audio.webm"
        item = VideoItem(title="Website audio", url="https://example.test/watch/1",
                         source_url="https://example.test/watch/1")
        with mock.patch("src.downloader.yt_dlp.YoutubeDL", self.fake_ydl(source, captured)):
            for format_name, codec in (("original", "opus"), ("mp3", "mp3"), ("m4a", "aac")):
                with self.subTest(audio_format=format_name):
                    result = self.downloader().download(item, "audio only", audio_format=format_name)
                    results.append(result)
                    self.assert_audio(result, codec)
                    if format_name == "original":
                        self.assertEqual(self.compressed_packet_hashes(result),
                                         self.compressed_packet_hashes(source))
        self.assertTrue(all(options["format"].startswith("bestaudio") for options in captured))
        self.assert_clean(*results)

    def test_website_fallback_with_muxed_video_still_publishes_only_audio(self) -> None:
        captured: list[dict] = []
        item = VideoItem(title="Muxed website", url="https://example.test/watch/2",
                         source_url="https://example.test/watch/2")
        source = self.fixtures / "av.mp4"
        with mock.patch("src.downloader.yt_dlp.YoutubeDL", self.fake_ydl(source, captured)):
            result = self.downloader().download(item, "audio only", audio_format="original")
        self.assert_audio(result, "aac")
        self.assertEqual(self.compressed_packet_hashes(result), self.compressed_packet_hashes(source))
        self.assert_clean(result)

    def test_hls_byte_ranges_produce_audio_and_preserve_original_packets(self) -> None:
        with self.requests_lock:
            start = len(self.requests_seen)
        result = self.downloader().download(self.item("av.m3u8"), "audio only")
        self.assertEqual(result.suffix, ".m4a")
        self.assert_audio(result, "aac")
        self.assertEqual(self.compressed_packet_hashes(result),
                         self.compressed_packet_hashes(self.fixtures / "av.mp4"))
        with self.requests_lock:
            ranges = [span for name, span in self.requests_seen[start:] if name == "media.ts"]
        self.assertGreaterEqual(len(ranges), 3)
        self.assertTrue(all(span and span.startswith("bytes=") for span in ranges))
        self.assert_clean(result)

    def test_hls_master_selects_default_audio_in_matching_group_for_all_formats(self) -> None:
        results = []
        with self.requests_lock:
            start = len(self.requests_seen)
        for audio_format, codec in (("original", "aac"), ("mp3", "mp3"), ("m4a", "aac")):
            with self.subTest(audio_format=audio_format):
                result = self.downloader().download(
                    self.item("master.m3u8"), "audio only", audio_format=audio_format,
                )
                results.append(result)
                self.assert_audio(result, codec)
                if audio_format == "original":
                    self.assertEqual(self.compressed_packet_hashes(result),
                                     self.compressed_packet_hashes(self.fixtures / "av.mp4"))
        with self.requests_lock:
            requests = self.requests_seen[start:]
        names = {name for name, _span in requests}
        self.assertEqual(names, {"master.m3u8", "audio-main.m3u8", "audio-media.ts"})
        ranges = [span for name, span in requests if name == "audio-media.ts"]
        self.assertGreaterEqual(len(ranges), 9)
        self.assertTrue(all(span and span.startswith("bytes=") for span in ranges))
        self.assert_clean(*results)

    def test_hls_audio_rendition_rejects_bad_ranges_without_fallback_or_files(self) -> None:
        for mode in ("whole-file-200", "wrong-206"):
            with self.subTest(response=mode):
                downloader = self.downloader()
                updates = []
                with mock.patch.dict(self.invalid_ranges, {"audio-media.ts": mode}), \
                        mock.patch("src.downloader.yt_dlp.YoutubeDL") as ytdlp_fallback, \
                        mock.patch.object(downloader, "_download_hls_ffmpeg") as ffmpeg_fallback:
                    with self.assertRaises(_HlsByteRangeError):
                        downloader.download(self.item("master.m3u8"), "audio only", updates.append)
                ytdlp_fallback.assert_not_called()
                ffmpeg_fallback.assert_not_called()
                self.assertFalse(any(progress.status in {"completed", "partial"}
                                     for progress in updates))
                self.assert_clean()

    def test_hls_shared_video_uri_keeps_audio_group_from_highest_bandwidth_variant(self) -> None:
        with self.requests_lock:
            start = len(self.requests_seen)
        result = self.downloader().download(self.item("shared-video-master.m3u8"), "audio only")
        self.assert_audio(result, "aac")
        self.assertEqual(self.compressed_packet_hashes(result),
                         self.compressed_packet_hashes(self.fixtures / "av.mp4"))
        with self.requests_lock:
            names = {name for name, _span in self.requests_seen[start:]}
        self.assertEqual(names, {"shared-video-master.m3u8", "audio-main.m3u8", "audio-media.ts"})
        self.assert_clean(result)

    def test_hls_default_inband_audio_is_preserved_over_external_other_language(self) -> None:
        with self.requests_lock:
            start = len(self.requests_seen)
        result = self.downloader().download(self.item("inband-master.m3u8"), "audio only")
        self.assert_audio(result, "aac")
        self.assertEqual(self.compressed_packet_hashes(result),
                         self.compressed_packet_hashes(self.fixtures / "av.mp4"))
        with self.requests_lock:
            names = {name for name, _span in self.requests_seen[start:]}
        self.assertEqual(names, {"inband-master.m3u8", "av.m3u8", "media.ts"})
        self.assert_clean(result)

    def test_partial_hls_audio_keeps_partial_name_with_and_without_progress(self) -> None:
        results = []
        for report_progress in (True, False):
            with self.subTest(progress=report_progress):
                downloader = self.downloader(connections=1)
                download_segment = downloader._download_hls_segment

                def cancel_after_initial_segment(segment, headers):
                    payload = download_segment(segment, headers)
                    if segment.index == 0:
                        downloader.cancel(keep_partial=True)
                    return payload

                updates = []
                with mock.patch.object(downloader, "_download_hls_segment",
                                       side_effect=cancel_after_initial_segment):
                    result = downloader.download(
                        self.item("av.m3u8", title=f"Partial {report_progress}"), "audio only",
                        updates.append if report_progress else None,
                    )
                results.append(result)
                self.assertTrue(result.name.endswith(".partial.m4a"))
                self.assert_audio(result, "aac")
                if report_progress:
                    self.assertEqual(updates[-1].status, "partial")
                    self.assertEqual(updates[-1].filename, str(result))
                    self.assertFalse(any(progress.status == "completed" for progress in updates))
        self.assert_clean(*results)

    def test_same_title_concurrent_audio_downloads_never_replace_existing_file(self) -> None:
        existing = self.output / "Shared.m4a"
        existing.write_bytes(b"existing user's download")
        downloaders = [self.downloader() for _ in range(3)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(downloader.download, self.item(title="Shared"), "audio only")
                       for downloader in downloaders]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(existing.read_bytes(), b"existing user's download")
        self.assertEqual(len(set(results)), 3)
        self.assertNotIn(existing, results)
        for result in results:
            self.assert_audio(result, "aac")
            self.assertEqual(self.compressed_packet_hashes(result),
                             self.compressed_packet_hashes(self.fixtures / "av.mp4"))
        self.assert_clean(existing, *results)

    def test_no_audio_fails_without_publishing_or_leaving_staged_files(self) -> None:
        for audio_format in ("original", "mp3", "m4a"):
            with self.subTest(audio_format=audio_format):
                updates = []
                with self.assertRaisesRegex(RuntimeError, "[Aa]udio"):
                    self.downloader().download(self.item("silent.mp4"), "audio only",
                                               updates.append, audio_format=audio_format)
                self.assertFalse(any(progress.status == "completed" for progress in updates))
                self.assert_clean()

    def test_missing_ffmpeg_fails_before_transfer_and_leaves_no_files(self) -> None:
        with self.requests_lock:
            count = len(self.requests_seen)
        with mock.patch("src.downloader.resolve_ffmpeg", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "FFmpeg"):
                self.downloader().download(self.item(), "audio only", audio_format="mp3")
        with self.requests_lock:
            self.assertEqual(len(self.requests_seen), count)
        self.assert_clean()

    def test_source_audio_uses_imageio_without_path_or_build_assets(self) -> None:
        """Exercise real discovery and conversion, not an injected tool path."""
        from yt_dlp import YoutubeDL
        from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

        results = []
        captured: list[dict] = []
        website = VideoItem(title="Source audio", url="https://example.test/watch/1",
                            source_url="https://example.test/watch/1")
        with tempfile.TemporaryDirectory(prefix="audio-source-checkout-") as project:
            with mock.patch("src.downloader.resolve_ffmpeg", resolve_ffmpeg), \
                    mock.patch("src.ffmpeg.shutil.which", return_value=None), \
                    mock.patch("src.ffmpeg.__file__", str(Path(project) / "src" / "ffmpeg.py")), \
                    mock.patch("src.downloader.yt_dlp.YoutubeDL",
                               self.fake_ydl(self.fixtures / "av.mp4", captured)):
                binary = resolve_ffmpeg()
                self.assertIsNotNone(binary)
                # yt-dlp must support the provider's versioned filename too.
                with YoutubeDL({"quiet": True, "ffmpeg_location": binary}) as ydl:
                    processor = FFmpegPostProcessor(ydl)
                    self.assertTrue(processor.available)
                    self.assertEqual(processor.executable, binary)
                for source in (self.item(), self.item("av.m3u8"), website):
                    for format_name, codec in (("original", "aac"), ("mp3", "mp3"), ("m4a", "aac")):
                        with self.subTest(source=source.url, audio_format=format_name):
                            result = self.downloader().download(
                                source, "audio only", audio_format=format_name,
                            )
                            results.append(result)
                            self.assert_audio(result, codec)
                hls = self.item("av.m3u8")
                native = self.downloader()._download_hls_ytdlp(hls, None)
                self.assertEqual(native.read_bytes(), (self.fixtures / "av.mp4").read_bytes())
                results.append(native)
                self.assertEqual(len(captured), 4)
                self.assertTrue(all(options["ffmpeg_location"] == binary for options in captured))
        self.assert_clean(*results)

    def test_system_ffmpeg_keeps_ytdlp_default_tool_discovery(self) -> None:
        captured: list[dict] = []
        source = self.fixtures / "av.mp4"
        item = VideoItem(title="System tools", url="https://example.test/watch/4",
                         source_url="https://example.test/watch/4")
        with mock.patch("src.downloader.shutil.which", return_value=self.ffmpeg), \
                mock.patch("src.downloader.yt_dlp.YoutubeDL", self.fake_ydl(source, captured)):
            result = self.downloader().download(item)
            native = self.downloader()._download_hls_ytdlp(self.item("av.m3u8"), None)
        self.assertEqual(result.read_bytes(), source.read_bytes())
        self.assertEqual(native.read_bytes(), source.read_bytes())
        self.assertEqual(len(captured), 2)
        self.assertTrue(all("ffmpeg_location" not in options for options in captured))
        self.assert_clean(result, native)

    def test_conversion_cancellation_never_publishes_partial_audio(self) -> None:
        downloader = self.downloader()
        actual_popen = subprocess.Popen
        converting = threading.Event()
        conversion_process = []
        updates = []

        def slow_conversion(command, *args, **kwargs):
            if "-map" in command and "0:a:0" in command:
                command = list(command)
                command.insert(command.index("-i"), "-re")
                process = actual_popen(command, *args, **kwargs)
                conversion_process.append(process)
                converting.set()
                return process
            return actual_popen(command, *args, **kwargs)

        with mock.patch("src.downloader.subprocess.Popen", side_effect=slow_conversion):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(downloader.download, self.item(), "audio only",
                                     updates.append, "mp3")
                self.assertTrue(converting.wait(5), "Actual FFmpeg conversion must begin")
                deadline = time.monotonic() + 3
                while downloader._ffmpeg_proc is not conversion_process[0]:
                    if time.monotonic() >= deadline:
                        self.fail("Conversion process was not available for cancellation")
                    time.sleep(0.01)
                downloader.cancel(keep_partial=True)
                with self.assertRaisesRegex(RuntimeError, "cancelled"):
                    future.result(timeout=5)
        self.assertIsNone(downloader._ffmpeg_proc)
        self.assertIsNotNone(conversion_process[0].poll())
        self.assertFalse(any(progress.status in {"completed", "partial"} for progress in updates))
        self.assert_clean()

    def test_normal_video_downloads_ignore_audio_format_and_keep_all_bytes(self) -> None:
        result = self.downloader().download(self.item(), "best", audio_format="mp3")
        self.assertEqual(result.read_bytes(), (self.fixtures / "av.mp4").read_bytes())
        self.assertEqual(self.streams(result), [("Video", "h264"), ("Audio", "aac")])
        captured: list[dict] = []
        source = self.fixtures / "av.mp4"
        item = VideoItem(title="Website video", url="https://example.test/watch/3",
                         source_url="https://example.test/watch/3")
        with mock.patch("src.downloader.yt_dlp.YoutubeDL", self.fake_ydl(source, captured)):
            website_result = self.downloader().download(item, "best", audio_format="m4a")
        self.assertEqual(website_result.read_bytes(), source.read_bytes())
        self.assertIn("bestvideo", captured[0]["format"])
        self.assert_clean(result, website_result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
