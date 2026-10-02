"""Offline HLS byte-range integrity and safe-failure regressions.

Run directly with ``.venv/bin/python tests/test_hls_byte_ranges.py``.
Responses and remuxing are simulated; fragment bytes and staged files are real.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import unittest
from unittest import mock

import requests
from Crypto.Cipher import AES

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.downloader import VideoDownloader, _HlsByteRangeError, _HlsSegment
from src.scraper import VideoItem


MANIFEST_URL = "https://example.test/video/list.m3u8"
MEDIA_URL = "https://example.test/video/blob.ts"
MEDIA_BYTES = b"xxABCDEyy"
RANGE_MANIFEST = """#EXTM3U
#EXTINF:1,
#EXT-X-BYTERANGE:3@2
blob.ts
#EXTINF:1,
#EXT-X-BYTERANGE:2
blob.ts
#EXT-X-ENDLIST
"""


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200, headers=None, *, url=""):
        self.payload = payload
        self.status_code = status
        self.headers = dict(headers or {})
        self.url = url

    @property
    def text(self):
        return self.payload.decode("utf-8")

    @property
    def content(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        # Cross chunk boundaries even for tiny ranges.
        for offset in range(0, len(self.payload), min(chunk_size, 2)):
            yield self.payload[offset:offset + min(chunk_size, 2)]


class FakeSession:
    def __init__(self, manifest: str, respond):
        self.manifest = manifest
        self.respond = respond
        self.media_requests = []
        self.lock = threading.Lock()

    def get(self, url, **kwargs):
        if url == MANIFEST_URL:
            return FakeResponse(self.manifest.encode(), url=MANIFEST_URL)
        with self.lock:
            self.media_requests.append((url, dict(kwargs.get("headers", {}))))
        return self.respond(url, kwargs)

    def close(self):
        pass


def valid_range_response(_url, kwargs):
    span = kwargs["headers"]["Range"].removeprefix("bytes=")
    start, end = map(int, span.split("-"))
    payload = MEDIA_BYTES[start:end + 1]
    return FakeResponse(payload, 206, {
        "Content-Range": f"bytes {start}-{end}/{len(MEDIA_BYTES)}",
        "Content-Length": str(len(payload)),
    })


class HlsByteRangeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output_dir = Path(directory.name)

    def downloader(self, manifest=RANGE_MANIFEST, respond=valid_range_response):
        downloader = VideoDownloader(self.output_dir, parallel_connections=2)
        downloader._session.close()
        downloader._session = FakeSession(manifest, respond)
        self.addCleanup(downloader.close)
        return downloader

    def item(self):
        return VideoItem(
            title="Range clip", url=MANIFEST_URL, source_url=MANIFEST_URL,
            is_direct=True, is_hls=True,
        )

    @staticmethod
    def remux(_ffmpeg, concat_list, output):
        sources = [Path(shlex.split(line)[1])
                   for line in concat_list.read_text("utf-8").splitlines()]
        output.write_bytes(b"".join(source.read_bytes() for source in sources))

    def test_explicit_and_implicit_ranges_preserve_segment_order(self):
        downloader = self.downloader()
        segments = downloader._parse_hls_segments(RANGE_MANIFEST, MANIFEST_URL, {})
        self.assertEqual([(s.range_start, s.range_length) for s in segments],
                         [(2, 3), (5, 2)])
        self.assertEqual([s.url for s in segments], [MEDIA_URL, MEDIA_URL])
        self.assertEqual([s.index for s in segments], [0, 1])

    def test_explicit_range_can_follow_different_uri_or_whole_segment(self):
        text = """#EXTM3U
#EXT-X-BYTERANGE:3@2
blob.ts
whole.ts
#EXT-X-BYTERANGE:2@5
other.ts
"""
        segments = self.downloader()._parse_hls_segments(text, MANIFEST_URL, {})
        self.assertEqual([(s.range_start, s.range_length) for s in segments],
                         [(2, 3), (None, None), (5, 2)])

    def test_invalid_ranges_are_rejected(self):
        invalid = {
            "first implicit offset": "#EXT-X-BYTERANGE:3\nblob.ts",
            "different previous URI": "#EXT-X-BYTERANGE:3@2\nblob.ts\n#EXT-X-BYTERANGE:2\nother.ts",
            "whole previous segment": "#EXT-X-BYTERANGE:3@2\nblob.ts\nblob.ts\n#EXT-X-BYTERANGE:2\nblob.ts",
            "zero length": "#EXT-X-BYTERANGE:0@2\nblob.ts",
            "negative length": "#EXT-X-BYTERANGE:-3@2\nblob.ts",
            "negative offset": "#EXT-X-BYTERANGE:3@-2\nblob.ts",
            "noninteger length": "#EXT-X-BYTERANGE:nope@2\nblob.ts",
            "noninteger offset": "#EXT-X-BYTERANGE:3@nope\nblob.ts",
            "duplicate offset": "#EXT-X-BYTERANGE:3@2@4\nblob.ts",
            "missing colon": "#EXT-X-BYTERANGE\nblob.ts",
            "missing URI": "#EXT-X-BYTERANGE:3@2",
            "duplicate tag": "#EXT-X-BYTERANGE:3@2\n#EXT-X-BYTERANGE:2@5\nblob.ts",
        }
        downloader = self.downloader()
        for name, text in invalid.items():
            with self.subTest(name=name):
                with self.assertRaises(_HlsByteRangeError):
                    downloader._parse_hls_segments("#EXTM3U\n" + text, MANIFEST_URL, {})
        self.assertEqual(downloader._session.media_requests, [])

    def test_complex_ranges_fail_before_fetching_keys_or_fragments(self):
        unsupported = [
            '#EXT-X-MAP:URI="blob.ts",BYTERANGE="2@0"\n#EXTINF:1,\nwhole.ts',
            '#EXT-X-MAP:URI="init.mp4"\n#EXT-X-BYTERANGE:3@2\nblob.ts',
            '#EXT-X-I-FRAMES-ONLY\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXT-X-BYTERANGE:16@0\nblob.ts',
        ]
        downloader = self.downloader()
        for text in unsupported:
            with self.subTest(manifest=text):
                with self.assertRaises(_HlsByteRangeError):
                    downloader._parse_hls_segments("#EXTM3U\n" + text, MANIFEST_URL, {})
        self.assertEqual(downloader._session.media_requests, [])

    def test_successful_ranges_publish_only_requested_bytes_and_keep_old_file(self):
        old = self.output_dir / "Range clip.mp4"
        old.write_bytes(b"older complete video")
        downloader = self.downloader()
        updates = []
        with mock.patch("src.downloader.shutil.which", return_value="/fake/ffmpeg"), \
                mock.patch.object(downloader, "_remux_hls_segments", side_effect=self.remux), \
                mock.patch("src.downloader.yt_dlp.YoutubeDL") as fallback:
            result = downloader.download(self.item(), on_progress=updates.append)
        self.assertEqual(result.read_bytes(), b"ABCDE")
        self.assertEqual(old.read_bytes(), b"older complete video")
        self.assertNotEqual(old, result)
        self.assertEqual(updates[-1].status, "completed")
        self.assertEqual(updates[-1].filename, str(result))
        self.assertEqual({headers["Range"] for _, headers in downloader._session.media_requests},
                         {"bytes=2-4", "bytes=5-6"})
        self.assertTrue(all(headers["Accept-Encoding"] == "identity"
                            for _, headers in downloader._session.media_requests))
        self.assertEqual(list(self.output_dir.glob(".video-scrap-job-*")), [])
        fallback.assert_not_called()

    def test_invalid_range_responses_never_publish_or_use_unsafe_fallback(self):
        invalid = {
            "ignored range": FakeResponse(MEDIA_BYTES, 200, {"Content-Length": "9"}),
            "wrong start": FakeResponse(b"ABC", 206, {"Content-Range": "bytes 0-2/9", "Content-Length": "3"}),
            "wrong end": FakeResponse(b"ABC", 206, {"Content-Range": "bytes 2-5/9", "Content-Length": "3"}),
            "impossible total": FakeResponse(b"ABC", 206, {"Content-Range": "bytes 2-4/4", "Content-Length": "3"}),
            "missing range": FakeResponse(b"ABC", 206, {"Content-Length": "3"}),
            "wrong length header": FakeResponse(b"ABC", 206, {"Content-Range": "bytes 2-4/9", "Content-Length": "2"}),
            "invalid length header": FakeResponse(b"ABC", 206, {"Content-Range": "bytes 2-4/9", "Content-Length": "bad"}),
            "short response": FakeResponse(b"AB", 206, {"Content-Range": "bytes 2-4/9", "Content-Length": "3"}),
            "long response": FakeResponse(b"ABCD", 206, {"Content-Range": "bytes 2-4/9", "Content-Length": "3"}),
            "encoded response": FakeResponse(b"ABC", 206, {"Content-Range": "bytes 2-4/9", "Content-Length": "3", "Content-Encoding": "gzip"}),
        }
        text = "#EXTM3U\n#EXTINF:1,\n#EXT-X-BYTERANGE:3@2\nblob.ts\n"
        old = self.output_dir / "Range clip.mp4"
        old.write_bytes(b"older complete video")
        for name, response in invalid.items():
            with self.subTest(name=name):
                downloader = self.downloader(text, lambda _url, _kwargs: response)
                with ExitStack() as stack:
                    stack.enter_context(mock.patch("src.downloader.shutil.which", return_value="/fake/ffmpeg"))
                    fallback = stack.enter_context(mock.patch("src.downloader.yt_dlp.YoutubeDL"))
                    ffmpeg_fallback = stack.enter_context(mock.patch.object(downloader, "_download_hls_ffmpeg"))
                    remux = stack.enter_context(mock.patch.object(downloader, "_remux_hls_segments"))
                    with self.assertRaises(_HlsByteRangeError):
                        downloader.download(self.item())
                fallback.assert_not_called()
                ffmpeg_fallback.assert_not_called()
                remux.assert_not_called()
                self.assertEqual(old.read_bytes(), b"older complete video")
                self.assertEqual(list(self.output_dir.iterdir()), [old])
                self.assertEqual(len(downloader._session.media_requests), 1)

    def test_range_response_without_length_header_still_checks_actual_bytes(self):
        response = FakeResponse(b"ABC", 206, {"Content-Range": "bytes 2-4/9"})
        downloader = self.downloader(respond=lambda _url, _kwargs: response)
        segment = _HlsSegment(0, MEDIA_URL, 1.0, None, None, range_start=2, range_length=3)
        self.assertEqual(downloader._download_hls_segment(segment, {}), b"ABC")

    def test_aes_ranges_use_ciphertext_lengths_and_media_sequence_ivs(self):
        key = b"0123456789abcdef"
        clear_segments = (b"first segment", b"second segment")
        encrypted = []
        for sequence, clear in enumerate(clear_segments, 42):
            padding = 16 - len(clear) % 16
            encrypted.append(AES.new(key, AES.MODE_CBC, sequence.to_bytes(16, "big"))
                             .encrypt(clear + bytes([padding]) * padding))
        media = b"prefix" + b"".join(encrypted) + b"tail"
        text = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:42
#EXT-X-KEY:METHOD=AES-128,URI="key.bin"
#EXTINF:1,
#EXT-X-BYTERANGE:16@6
blob.ts
#EXTINF:1,
#EXT-X-BYTERANGE:16
blob.ts
"""

        def respond(url, kwargs):
            if url.endswith("/key.bin"):
                return FakeResponse(key)
            start, end = map(int, kwargs["headers"]["Range"][6:].split("-"))
            return FakeResponse(media[start:end + 1], 206, {
                "Content-Range": f"bytes {start}-{end}/{len(media)}",
                "Content-Length": str(end - start + 1),
            })

        downloader = self.downloader(text, respond)
        with mock.patch("src.downloader.shutil.which", return_value="/fake/ffmpeg"), \
                mock.patch.object(downloader, "_remux_hls_segments", side_effect=self.remux):
            result = downloader.download(self.item())
        self.assertEqual(result.read_bytes(), b"".join(clear_segments))
        self.assertEqual(sum(url.endswith("/key.bin") for url, _ in downloader._session.media_requests), 1)

    def test_cancelled_range_download_keeps_only_verified_initial_segments(self):
        release_second = threading.Event()

        def respond(url, kwargs):
            if kwargs["headers"]["Range"] == "bytes=5-6":
                if not release_second.wait(timeout=5):
                    raise requests.Timeout("second segment was not released")
            return valid_range_response(url, kwargs)

        downloader = self.downloader(respond=respond)
        updates = []

        def progress(update):
            updates.append(update)
            if update.message == "Segment 1/2":
                downloader.cancel(keep_partial=True)
                release_second.set()

        try:
            with mock.patch("src.downloader.shutil.which", return_value="/fake/ffmpeg"), \
                    mock.patch.object(downloader, "_remux_hls_segments", side_effect=self.remux), \
                    mock.patch("src.downloader.yt_dlp.YoutubeDL") as fallback:
                result = downloader.download(self.item(), on_progress=progress)
        finally:
            release_second.set()
        self.assertEqual(result.name, "Range clip.partial.mp4")
        self.assertEqual(result.read_bytes(), b"ABC")
        self.assertEqual(updates[-1].status, "partial")
        self.assertEqual(updates[-1].filename, str(result))
        self.assertEqual(list(self.output_dir.iterdir()), [result])
        fallback.assert_not_called()

    def test_missing_ffmpeg_cannot_bypass_range_protection(self):
        for text in (RANGE_MANIFEST, '#EXTM3U\n#EXT-X-MAP:URI="blob.ts",BYTERANGE="2@0"\nwhole.ts'):
            with self.subTest(manifest=text):
                downloader = self.downloader(text)
                with mock.patch("src.downloader.shutil.which", return_value=None), \
                        mock.patch("src.downloader.yt_dlp.YoutubeDL") as fallback, \
                        mock.patch.object(downloader, "_download_hls_ffmpeg") as ffmpeg_fallback:
                    with self.assertRaises(_HlsByteRangeError):
                        downloader.download(self.item())
                fallback.assert_not_called()
                ffmpeg_fallback.assert_not_called()
                self.assertEqual(downloader._session.media_requests, [])
                self.assertEqual(list(self.output_dir.iterdir()), [])

    def test_unreadable_manifest_never_enters_unvalidated_fallback(self):
        master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nchild.m3u8\n"
        for failed_stage in ("initial manifest", "selected child manifest"):
            with self.subTest(stage=failed_stage):
                downloader = self.downloader(master)
                original_get = downloader._session.get

                def failed_get(url, **kwargs):
                    if failed_stage == "initial manifest" or url != MANIFEST_URL:
                        raise requests.ConnectionError("manifest unavailable")
                    return original_get(url, **kwargs)

                with mock.patch.object(downloader._session, "get", side_effect=failed_get), \
                        mock.patch("src.downloader.shutil.which", return_value="/fake/ffmpeg"), \
                        mock.patch.object(downloader, "_download_hls_ytdlp") as fallback, \
                        mock.patch.object(downloader, "_download_hls_ffmpeg") as ffmpeg_fallback:
                    with self.assertRaises(_HlsByteRangeError):
                        downloader.download(self.item())
                fallback.assert_not_called()
                ffmpeg_fallback.assert_not_called()
                self.assertEqual(list(self.output_dir.iterdir()), [])

    def test_uninspected_child_playlist_cannot_be_treated_as_media(self):
        master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nchild.m3u8\n"
        child_url = "https://example.test/video/child.m3u8"
        children = (
            "<html>temporary server error</html>",
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nleaf.m3u8\n",
        )
        for child in children:
            with self.subTest(child=child):
                downloader = self.downloader(master, lambda _url, _kwargs: FakeResponse(child.encode(), url=child_url))
                with mock.patch("src.downloader.shutil.which", return_value="/fake/ffmpeg"), \
                        mock.patch.object(downloader, "_download_hls_ytdlp") as fallback, \
                        mock.patch.object(downloader, "_download_hls_ffmpeg") as ffmpeg_fallback:
                    with self.assertRaises(_HlsByteRangeError):
                        downloader.download(self.item())
                fallback.assert_not_called()
                ffmpeg_fallback.assert_not_called()
                self.assertEqual(list(self.output_dir.iterdir()), [])

    def test_plain_hls_keeps_existing_fallback_without_ffmpeg(self):
        downloader = self.downloader("#EXTM3U\n#EXTINF:1,\nwhole.ts\n")

        def fallback(_item, _on_progress):
            output = downloader.output_dir / "Range clip.mp4"
            output.write_bytes(b"plain HLS fallback")
            return output

        with mock.patch("src.downloader.shutil.which", return_value=None), \
                mock.patch.object(downloader, "_download_hls_ytdlp", side_effect=fallback) as fallback_backend:
            result = downloader.download(self.item())
        self.assertEqual(result.read_bytes(), b"plain HLS fallback")
        fallback_backend.assert_called_once()

    def test_fallback_stays_on_the_inspected_variant_and_preserves_metadata(self):
        master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=640x360
plain.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=100000,RESOLUTION=1280x720
ranged.m3u8
"""
        plain_url = "https://example.test/video/plain.m3u8"
        plain_manifest = "#EXTM3U\n#EXTINF:1,\nwhole.ts\n"
        for backend in ("yt-dlp", "ffmpeg"):
            with self.subTest(backend=backend):
                downloader = self.downloader(master, lambda _url, _kwargs: FakeResponse(plain_manifest.encode(), url=plain_url))
                video = self.item()
                video.referer = "https://example.test/watch"

                def finished(fallback_item, *_args):
                    self.assertEqual(fallback_item.url, plain_url)
                    self.assertEqual(fallback_item.source_url, MANIFEST_URL)
                    self.assertEqual(fallback_item.title, video.title)
                    self.assertEqual(fallback_item.referer, video.referer)
                    output = downloader.output_dir / "Range clip.mp4"
                    output.write_bytes(b"verified plain variant")
                    return output

                with ExitStack() as stack:
                    stack.enter_context(mock.patch("src.downloader.shutil.which", return_value="/fake/ffmpeg"))
                    stack.enter_context(mock.patch.object(downloader, "_download_hls_segments_parallel", side_effect=RuntimeError("native failed")))
                    ytdlp = stack.enter_context(mock.patch.object(downloader, "_download_hls_ytdlp",
                        side_effect=finished if backend == "yt-dlp" else RuntimeError("yt-dlp failed")))
                    ffmpeg = stack.enter_context(mock.patch.object(downloader, "_download_hls_ffmpeg", side_effect=finished))
                    result = downloader.download(video)
                self.assertEqual(result.read_bytes(), b"verified plain variant")
                self.assertEqual(ytdlp.call_args.args[0].url, plain_url)
                if backend == "ffmpeg":
                    self.assertEqual(ffmpeg.call_args.args[0].url, plain_url)
                else:
                    ffmpeg.assert_not_called()
                self.assertTrue(all(url == plain_url for url, _ in downloader._session.media_requests))
                result.unlink()

    @staticmethod
    def audio_master():
        return """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="sound",NAME="English",URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=640x360,AUDIO="sound"
plain.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=100000,RESOLUTION=1280x720,AUDIO="sound"
other.m3u8
"""

    def test_plain_external_audio_fallback_keeps_master_after_inspecting_candidates(self):
        candidates = {
            "https://example.test/video/plain.m3u8": "#EXTM3U\n#EXTINF:1,\nvideo.ts\n",
            "https://example.test/video/other.m3u8": "#EXTM3U\n#EXTINF:1,\nother.ts\n",
            "https://example.test/video/audio.m3u8": "#EXTM3U\n#EXTINF:1,\naudio.ts\n",
        }

        def respond(url, _kwargs):
            return FakeResponse(candidates[url].encode(), url=url)

        downloader = self.downloader(self.audio_master(), respond)

        def finished(fallback_item, *_args):
            self.assertEqual(fallback_item.url, MANIFEST_URL)
            self.assertEqual(fallback_item.source_url, MANIFEST_URL)
            output = downloader.output_dir / "Range clip.mp4"
            output.write_bytes(b"verified video with audio")
            return output

        with mock.patch("src.downloader.shutil.which", return_value="/fake/ffmpeg"), \
                mock.patch.object(downloader, "_download_hls_segments_parallel", side_effect=RuntimeError("native failed")), \
                mock.patch.object(downloader, "_download_hls_ytdlp", side_effect=RuntimeError("yt-dlp failed")) as ytdlp, \
                mock.patch.object(downloader, "_download_hls_ffmpeg", side_effect=finished):
            result = downloader.download(self.item())
        self.assertEqual(result.read_bytes(), b"verified video with audio")
        self.assertEqual(ytdlp.call_args.args[0].url, MANIFEST_URL)
        self.assertTrue(set(candidates).issubset({url for url, _ in downloader._session.media_requests}))

    def test_external_audio_master_rejects_unchecked_video_or_audio_ranges(self):
        urls = {
            "video": "https://example.test/video/other.m3u8",
            "audio": "https://example.test/video/audio.m3u8",
        }
        plain = "#EXTM3U\n#EXTINF:1,\nwhole.ts\n"
        old = self.output_dir / "Range clip.mp4"
        old.write_bytes(b"older complete video")
        for kind, ranged_url in urls.items():
            with self.subTest(kind=kind):
                def respond(url, _kwargs):
                    text = RANGE_MANIFEST if url == ranged_url else plain
                    return FakeResponse(text.encode(), url=url)

                downloader = self.downloader(self.audio_master(), respond)
                with mock.patch("src.downloader.shutil.which", return_value="/fake/ffmpeg"), \
                        mock.patch.object(downloader, "_download_hls_segments_parallel", side_effect=RuntimeError("native failed")), \
                        mock.patch.object(downloader, "_download_hls_ytdlp") as ytdlp, \
                        mock.patch.object(downloader, "_download_hls_ffmpeg") as ffmpeg:
                    with self.assertRaises(_HlsByteRangeError):
                        downloader.download(self.item())
                ytdlp.assert_not_called()
                ffmpeg.assert_not_called()
                self.assertEqual(old.read_bytes(), b"older complete video")
                self.assertEqual(list(self.output_dir.iterdir()), [old])

    def test_range_network_failure_never_falls_back_or_publishes(self):
        downloader = self.downloader(respond=mock.Mock(side_effect=requests.ConnectionError("network down")))
        with mock.patch("src.downloader.shutil.which", return_value="/fake/ffmpeg"), \
                mock.patch("src.downloader.yt_dlp.YoutubeDL") as fallback:
            with self.assertRaises(_HlsByteRangeError):
                downloader.download(self.item())
        fallback.assert_not_called()
        self.assertEqual(list(self.output_dir.iterdir()), [])

    def test_range_remux_failure_cleans_staged_file_without_fallback(self):
        downloader = self.downloader()

        def failed_remux(_ffmpeg, _concat, target):
            target.write_bytes(b"incomplete output")
            raise RuntimeError("remux failed")

        with mock.patch("src.downloader.shutil.which", return_value="/fake/ffmpeg"), \
                mock.patch.object(downloader, "_remux_hls_segments", side_effect=failed_remux), \
                mock.patch("src.downloader.yt_dlp.YoutubeDL") as fallback:
            with self.assertRaises(_HlsByteRangeError):
                downloader.download(self.item())
        fallback.assert_not_called()
        self.assertEqual(list(self.output_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
