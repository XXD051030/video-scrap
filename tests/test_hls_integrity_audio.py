"""Real FFmpeg + loopback HLS integrity and separate-audio regressions."""

from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import urlsplit

from Crypto.Cipher import AES

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.downloader import VideoDownloader, _HlsByteRangeError, _HlsIntegrityError, _HlsSegment
from src.ffmpeg import resolve_ffmpeg
from src.scraper import VideoItem


class HlsIntegrityAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = resolve_ffmpeg()
        if not cls.ffmpeg:
            raise unittest.SkipTest("Real FFmpeg is required")
        cls.fixture_tmp = tempfile.TemporaryDirectory(prefix="hls-integrity-fixture-")
        cls.addClassCleanup(cls.fixture_tmp.cleanup)
        cls.fixtures = Path(cls.fixture_tmp.name)
        cls.run_ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "3", "-c:v", "libx264", "-preset", "ultrafast", "-g", "10",
            "-c:a", "aac", str(cls.fixtures / "source.mp4"),
        )
        for name, mapping in (("video", "0:v:0"), ("audio", "0:a:0")):
            cls.run_ffmpeg(
                "-i", str(cls.fixtures / "source.mp4"), "-map", mapping, "-c", "copy",
                "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
                "-hls_segment_filename", str(cls.fixtures / f"{name}%02d.ts"),
                str(cls.fixtures / f"{name}.m3u8"),
            )
        cls.run_ffmpeg(
            "-i", str(cls.fixtures / "source.mp4"), "-c", "copy", "-f", "hls",
            "-hls_time", "1", "-hls_list_size", "0",
            "-hls_segment_filename", str(cls.fixtures / "mixed%02d.ts"),
            str(cls.fixtures / "mixed.m3u8"),
        )

    @classmethod
    def run_ffmpeg(cls, *args):
        result = subprocess.run(
            [cls.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        return result

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hls-integrity-output-")
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.routes = {"/" + path.name: path.read_bytes()
                       for path in self.fixtures.iterdir() if path.suffix != ".mp4"}
        self.routes["/master.m3u8"] = (
            '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="sound",NAME="Main",'
            'DEFAULT=YES,AUTOSELECT=YES,URI="audio.m3u8"\n'
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="unrelated",NAME="Other",URI="missing.m3u8"\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=200000,AUDIO="sound"\nvideo.m3u8\n'
        ).encode()
        self.requests = []
        self.counts = Counter()
        self.lock = threading.Lock()
        self.empty_path = None
        self.empty_once = False
        self.fail_after_empty = False
        self.ignore_ranges = False
        self.emit_etag = True
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def handle(self):
                try:
                    super().handle()
                except (ConnectionResetError, BrokenPipeError):
                    # Rejected Range responses and cancellation intentionally
                    # close a pooled client socket before its next request.
                    pass

            def log_message(self, *_args):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                with owner.lock:
                    owner.requests.append((path, self.headers.get("Range")))
                    owner.counts[path] += 1
                    count = owner.counts[path]
                body = owner.routes.get(path)
                if body is None:
                    self.send_error(404)
                    return
                if path == owner.empty_path and owner.fail_after_empty and count > 1:
                    self.send_error(503)
                    return
                if path == owner.empty_path and (not owner.empty_once or count == 1):
                    body = b""
                span = self.headers.get("Range")
                status, content_range = 200, None
                if span and not owner.ignore_ranges:
                    first, last = span.removeprefix("bytes=").split("-")
                    start = int(first) if first else max(0, len(body) - int(last))
                    end = min(int(last), len(body) - 1) if first and last else len(body) - 1
                    content_range = f"bytes {start}-{end}/{len(body)}"
                    body, status = body[start:end + 1], 206
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                if owner.emit_etag:
                    self.send_header("ETag", '"stable-fixture"')
                if content_range:
                    self.send_header("Content-Range", content_range)
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def downloader(self, workers=2):
        downloader = VideoDownloader(self.output, parallel_connections=workers)
        self.addCleanup(downloader.close)
        return downloader

    def item(self, path="/master.m3u8"):
        return VideoItem(title="Synthetic", url=self.base + path,
                         source_url=self.base + path, is_direct=True, is_hls=True)

    def inspect(self, path):
        result = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        match = re.search(r"Duration: (\d+):(\d+):([\d.]+)", result.stderr)
        self.assertIsNotNone(match, result.stderr)
        duration = int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])
        return duration, result.stderr

    def ranged(self, name):
        original = self.routes[f"/{name}.m3u8"].decode()
        blob, lines, offset = bytearray(), [], 0
        for line in original.splitlines():
            if line and not line.startswith("#"):
                data = self.routes["/" + line]
                lines.extend((f"#EXT-X-BYTERANGE:{len(data)}@{offset}", f"{name}.bin"))
                blob.extend(data)
                offset += len(data)
            else:
                lines.append(line)
        self.routes[f"/{name}.m3u8"] = "\n".join(lines).encode()
        self.routes[f"/{name}.bin"] = bytes(blob)

    def test_empty_middle_never_publishes_or_uses_skipping_fallback(self):
        self.empty_path = "/video01.ts"
        downloader = self.downloader()
        old = self.output / "Synthetic.mp4"
        old.write_bytes(b"existing complete file")
        updates = []
        with mock.patch.object(downloader, "_download_hls_ytdlp") as ytdlp, \
                mock.patch.object(downloader, "_download_hls_ffmpeg") as fallback:
            with self.assertRaises(_HlsIntegrityError):
                downloader.download(self.item("/video.m3u8"), on_progress=updates.append)
        self.assertEqual(self.counts[self.empty_path], 10)
        self.assertFalse(any(update.status == "completed" for update in updates))
        self.assertEqual(old.read_bytes(), b"existing complete file")
        self.assertEqual(list(self.output.iterdir()), [old])
        ytdlp.assert_not_called()
        fallback.assert_not_called()

    def test_transient_empty_middle_retries_and_real_output_keeps_three_seconds(self):
        self.empty_path, self.empty_once = "/video01.ts", True
        downloader = self.downloader()
        result = downloader.download(self.item("/video.m3u8"))
        duration, details = self.inspect(result)
        self.assertAlmostEqual(duration, 3.0, delta=0.15)
        self.assertIn("Video:", details)
        self.assertEqual(self.counts[self.empty_path], 2)

    def test_empty_then_http_errors_cannot_erase_integrity_failure(self):
        self.empty_path, self.fail_after_empty = "/video01.ts", True
        downloader = self.downloader()
        with mock.patch.object(downloader, "_download_hls_ytdlp") as fallback:
            with self.assertRaises(_HlsIntegrityError):
                downloader.download(self.item("/video.m3u8"))
        self.assertEqual(self.counts[self.empty_path], 10)
        self.assertEqual(list(self.output.iterdir()), [])
        fallback.assert_not_called()

    def test_plain_separate_audio_normal_success_has_both_streams(self):
        downloader = self.downloader()
        updates = []
        with mock.patch.object(downloader, "_download_hls_ytdlp") as fallback:
            result = downloader.download(self.item(), on_progress=updates.append)
        duration, details = self.inspect(result)
        self.assertAlmostEqual(duration, 3.0, delta=0.15)
        self.assertIn("Video:", details)
        self.assertIn("Audio:", details)
        self.assertGreater(self.counts["/audio.m3u8"], 0)
        self.assertEqual(self.counts["/missing.m3u8"], 0)
        self.assertEqual(updates[-1].status, "completed")
        finishing = next(index for index, update in enumerate(updates)
                         if update.status == "finalizing")
        self.assertFalse(any(update.status == "downloading" for update in updates[finishing:]))
        fallback.assert_not_called()

    def test_unvalidated_ranged_tracks_spool_each_object_once_and_cleanup_at_completion(self):
        self.emit_etag = False
        self.ranged("video")
        self.ranged("audio")
        downloader = self.downloader()
        result = downloader.download(self.item())
        duration, details = self.inspect(result)
        self.assertAlmostEqual(duration, 3.0, delta=0.15)
        self.assertIn("Video:", details)
        self.assertIn("Audio:", details)
        for name in ("/video.bin", "/audio.bin"):
            self.assertEqual(sum(path == name and span is None for path, span in self.requests), 1)
        self.assertIsNone(downloader._hls_range_spool)
        self.assertEqual(list(self.output.iterdir()), [result])

    def test_separate_audio_and_video_ranges_keep_strict_native_success(self):
        self.ranged("video")
        self.ranged("audio")
        downloader = self.downloader()
        with mock.patch.object(downloader, "_download_hls_ytdlp") as fallback:
            result = downloader.download(self.item())
        duration, details = self.inspect(result)
        self.assertAlmostEqual(duration, 3.0, delta=0.15)
        self.assertIn("Video:", details)
        self.assertIn("Audio:", details)
        self.assertTrue(all(span for path, span in self.requests if path.endswith(".bin")))
        fallback.assert_not_called()

    def test_audio_range_200_cannot_fallback_or_publish_video_only(self):
        self.ranged("audio")
        self.ignore_ranges = True
        downloader = self.downloader()
        with mock.patch.object(downloader, "_download_hls_ytdlp") as fallback:
            with self.assertRaises(_HlsByteRangeError):
                downloader.download(self.item())
        self.assertEqual(list(self.output.iterdir()), [])
        fallback.assert_not_called()

    def test_audio_map_with_ranges_keeps_existing_safe_rejection(self):
        self.ranged("audio")
        self.routes["/audio.m3u8"] = self.routes["/audio.m3u8"].replace(
            b"#EXTM3U\n", b'#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n'
        )
        downloader = self.downloader()
        with mock.patch.object(downloader, "_download_hls_ytdlp") as fallback:
            with self.assertRaises(_HlsByteRangeError):
                downloader.download(self.item())
        self.assertFalse(any(path.endswith((".ts", ".bin", ".mp4")) for path, _span in self.requests))
        self.assertEqual(list(self.output.iterdir()), [])
        fallback.assert_not_called()

    def test_audio_parse_failure_still_requires_av_from_every_fallback(self):
        self.routes["/missing.m3u8"] = self.routes["/audio.m3u8"]
        video_only = self.output / "fixture-video-only.mp4"
        self.run_ffmpeg("-i", str(self.fixtures / "source.mp4"),
                        "-map", "0:v:0", "-c", "copy", str(video_only))
        payload = video_only.read_bytes()
        video_only.unlink()
        downloader = self.downloader()
        original_parse = downloader._parse_hls_segments

        def native_audio_parser_unavailable(text, url, headers):
            if url.endswith("/audio.m3u8"):
                raise RuntimeError("Native audio parser unavailable")
            return original_parse(text, url, headers)

        def incomplete_fallback(_item, *_args):
            output = downloader.output_dir / "Synthetic.mp4"
            output.write_bytes(payload)
            return output

        with mock.patch.object(downloader, "_parse_hls_segments", side_effect=native_audio_parser_unavailable), \
                mock.patch.object(downloader, "_download_hls_ytdlp", side_effect=incomplete_fallback) as ytdlp, \
                mock.patch.object(downloader, "_download_hls_ffmpeg", side_effect=incomplete_fallback) as ffmpeg:
            with self.assertRaisesRegex(_HlsIntegrityError, "missing.*audio"):
                downloader.download(self.item())
        ytdlp.assert_called_once()
        ffmpeg.assert_called_once()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_real_ffmpeg_fallback_preserves_separate_audio(self):
        self.routes["/master.m3u8"] = self.routes["/master.m3u8"].replace(
            b'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="unrelated",NAME="Other",URI="missing.m3u8"\n', b""
        )
        downloader = self.downloader()
        with mock.patch.object(downloader, "_download_hls_segments_parallel", side_effect=RuntimeError("native unavailable")), \
                mock.patch.object(downloader, "_download_hls_ytdlp", side_effect=RuntimeError("yt-dlp unavailable")):
            result = downloader.download(self.item())
        duration, details = self.inspect(result)
        self.assertAlmostEqual(duration, 3.0, delta=0.15)
        self.assertIn("Video:", details)
        self.assertIn("Audio:", details)

    def test_selected_default_without_uri_keeps_in_band_audio(self):
        self.routes["/master.m3u8"] = (
            '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="sound",NAME="Main",DEFAULT=YES\n'
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="sound",NAME="Other",URI="missing.m3u8"\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=200000,AUDIO="sound"\nmixed.m3u8\n'
        ).encode()
        result = self.downloader().download(self.item())
        _duration, details = self.inspect(result)
        self.assertIn("Video:", details)
        self.assertIn("Audio:", details)
        self.assertEqual(self.counts["/missing.m3u8"], 0)

    def test_empty_audio_never_succeeds_with_only_video(self):
        self.empty_path = "/audio01.ts"
        downloader = self.downloader()
        with mock.patch.object(downloader, "_download_hls_ytdlp") as fallback:
            with self.assertRaises(_HlsIntegrityError):
                downloader.download(self.item())
        self.assertEqual(list(self.output.iterdir()), [])
        fallback.assert_not_called()

    def test_cancel_at_merge_does_not_publish_dual_track_output(self):
        downloader = self.downloader()

        def progress(update):
            if update.status == "finalizing":
                downloader.cancel()

        with mock.patch.object(downloader, "_remux_hls_segments") as merge, \
                mock.patch.object(downloader, "_download_hls_ytdlp") as fallback:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                downloader.download(self.item(), on_progress=progress)
        merge.assert_not_called()
        fallback.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_cancelled_dual_track_without_audio_prefix_cannot_publish_partial(self):
        downloader = self.downloader(workers=1)

        # Do not rely on an encoder-specific number of AAC priming segments.
        count = sum(not line.startswith("#") and bool(line) for line in
                    self.routes["/video.m3u8"].decode().splitlines())
        count += sum(not line.startswith("#") and bool(line) for line in
                     self.routes["/audio.m3u8"].decode().splitlines())

        def progress(update):
            if update.message == f"Segment 1/{count}":
                downloader.cancel(keep_partial=True)

        with self.assertRaises(RuntimeError):
            downloader.download(self.item(), on_progress=progress)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_keep_partial_after_both_prefixes_retains_real_audio_and_video(self):
        downloader = self.downloader(workers=1)
        count = sum(not line.startswith("#") and bool(line) for track in ("video", "audio")
                    for line in self.routes[f"/{track}.m3u8"].decode().splitlines())
        updates = []

        def progress(update):
            updates.append(update)
            if update.message == f"Segment {count}/{count}":
                downloader.cancel(keep_partial=True)

        result = downloader.download(self.item(), on_progress=progress)
        self.assertIn(".partial.", result.name)
        _duration, details = self.inspect(result)
        self.assertIn("Video:", details)
        self.assertIn("Audio:", details)
        self.assertEqual(updates[-1].status, "partial")

    def test_zero_exit_truncated_merge_is_rejected_by_real_probe(self):
        downloader = self.downloader()
        output = self.output / "short.mp4"
        self.run_ffmpeg("-i", str(self.fixtures / "source.mp4"), "-t", "1", "-c", "copy", str(output))
        real_run = downloader._run_audio_ffmpeg

        def merged_but_short(command, **kwargs):
            if "concat" in command:
                return 0, ""
            return real_run(command, **kwargs)

        with mock.patch.object(downloader, "_run_audio_ffmpeg", side_effect=merged_but_short):
            with self.assertRaisesRegex(_HlsIntegrityError, "shorter"):
                downloader._remux_hls_segments(
                    self.ffmpeg, self.output / "unused.txt", output, expected_duration=3.0
                )

    def test_padding_only_decrypted_segment_retries_and_fails(self):
        downloader = self.downloader()
        key, iv = b"0123456789abcdef", bytes(16)
        self.routes["/padding.ts"] = AES.new(key, AES.MODE_CBC, iv).encrypt(bytes([16]) * 16)
        with self.assertRaises(_HlsIntegrityError):
            downloader._download_hls_segment(
                _HlsSegment(0, self.base + "/padding.ts", 1.0, key, iv), {}
            )
        self.assertEqual(self.counts["/padding.ts"], 10)


if __name__ == "__main__":
    unittest.main()
