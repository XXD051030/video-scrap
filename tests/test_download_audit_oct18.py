"""Offline regression evidence for downloader audit D01/D02/D03/D04/D09/D23."""

from concurrent.futures import ThreadPoolExecutor
import errno
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tracemalloc
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from Crypto.Cipher import AES
from src import downloader as module
from src.downloader import VideoDownloader, _HlsFallbackError, _HlsIntegrityError, _HlsSegment
from src.parallel_downloader import ParallelDownloader, _publish_unique
from src import parallel_downloader
from src.scraper import VideoItem
from test_parallel_downloader import FakeSession, FakeResponse
from test_hls_integrity_audio import HlsIntegrityAudioTests


class DownloadAuditBoundaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name)
        self.downloader = VideoDownloader(self.output)
        self.addCleanup(self.downloader.close)

    def direct_item(self):
        return VideoItem("direct", "https://example.test/media.mp4", "https://example.test/",
                         is_direct=True, ext="mp4")

    def parallel(self, session):
        with mock.patch.object(parallel_downloader, "build_session", return_value=session):
            result = ParallelDownloader(connections=4, min_segment_size=1024)
        self.addCleanup(result.close)
        return result

    def test_direct_media_rejects_empty_html_and_json_without_publishing(self):
        for body, mime in ((b"", "video/mp4"), (b"<html>sign in</html>", "text/html"),
                           (b'{"error":"expired"}', "application/json"),
                           (b"Login required", "text/plain"),
                           (b"<error>Login required</error>", "application/xml"),
                           (b"not an actual image", "image/jpeg"),
                           (b"<!DOCTYPE html><html>sign in</html>", "application/octet-stream")):
            with self.subTest(mime=mime, body=body):
                session = FakeSession(body, supports_ranges=False)
                real_get = session.get

                def get(*args, **kwargs):
                    response = real_get(*args, **kwargs)
                    response.headers["Content-Type"] = mime
                    return response

                session.get = get
                updates = []
                with mock.patch.object(parallel_downloader, "build_session", return_value=session):
                    with self.assertRaises(RuntimeError):
                        self.downloader.download(self.direct_item(), on_progress=updates.append)
                self.assertEqual(list(self.output.iterdir()), [])
                self.assertFalse(any(event.status == "completed" for event in updates))

    def test_transport_layer_still_accepts_arbitrary_binary_bytes(self):
        session = FakeSession(b"not a media fixture", supports_ranges=False)
        result = self.parallel(session).download("https://example.test/file", self.output / "data.bin")
        self.assertEqual(result.path.read_bytes(), b"not a media fixture")

    def test_changed_or_missing_strong_etag_rejects_parallel_composition(self):
        for new_tag in ('"version-B"', None, 'W/"version-A"'):
            with self.subTest(new_tag=new_tag):
                session = FakeSession(b"A" * 8192, supports_ranges=True, etag='"version-A"')
                real_get = session.get
                observed = []

                def get(*args, **kwargs):
                    headers = kwargs.get("headers", {})
                    response = real_get(*args, **kwargs)
                    if headers.get("Range") != "bytes=0-1":
                        observed.append(headers.get("If-Match"))
                        response.headers.pop("ETag", None)
                        if new_tag is not None:
                            response.headers["ETag"] = new_tag
                        response.body = b"B" * len(response.body)
                    return response

                session.get = get
                with self.assertRaisesRegex(RuntimeError, "resource version"):
                    self.parallel(session).download("https://example.test/file", self.output / "data.bin")
                self.assertTrue(observed)
                self.assertEqual(set(observed), {'"version-A"'})
                self.assertEqual(list(self.output.iterdir()), [])

    def test_no_or_weak_validator_uses_one_full_response(self):
        for tag in (None, 'W/"version-A"'):
            session = FakeSession(b"A" * 8192, supports_ranges=True, etag=tag)
            progress = []
            result = self.parallel(session).download(
                "https://example.test/file", self.output / "data.bin",
                progress=lambda done, total, speed: progress.append((done, total)),
            )
            self.assertEqual(result.path.read_bytes(), b"A" * 8192)
            self.assertEqual(session.requested_ranges, ["bytes=0-1", None])
            self.assertTrue(progress)
            self.assertTrue(all(total == 8192 for done, total in progress))
            result.path.unlink()

    def test_atomic_no_replace_preserves_a_third_party_racing_file(self):
        source = self.output / "private.part"
        source.write_bytes(b"new media")
        target = self.output / "media.mp4"
        original = parallel_downloader._rename_without_replacement
        injected = []

        def third_party_first(src, dst):
            if not injected:
                Path(dst).write_bytes(b"third party")
                injected.append(dst)
            original(src, dst)

        with mock.patch.object(parallel_downloader.os, "link", side_effect=OSError(errno.EOPNOTSUPP, "no links")), \
                mock.patch.object(parallel_downloader, "_rename_without_replacement", side_effect=third_party_first):
            result = _publish_unique(source, target)
        self.assertEqual(target.read_bytes(), b"third party")
        self.assertEqual(result.name, "media (1).mp4")
        self.assertEqual(result.read_bytes(), b"new media")

    def test_bounded_segment_streaming_does_not_duplicate_large_body(self):
        class Response(FakeResponse):
            def iter_content(self, chunk_size):
                for _ in range((20 * 1024 * 1024) // chunk_size):
                    yield b"x" * chunk_size

        destination = self.output / "part.ts"
        with mock.patch.object(self.downloader._session, "get", return_value=Response(200, {})):
            tracemalloc.start()
            try:
                size = self.downloader._download_hls_segment(
                    _HlsSegment(0, "https://example.test/a.ts", 1, None, None), {},
                    destination=destination,
                )
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertEqual(size, 20 * 1024 * 1024)
        self.assertEqual(destination.stat().st_size, size)
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_oversize_segment_fails_once_without_publishing_a_partial_segment(self):
        destination = self.output / "part.ts"
        response = FakeResponse(200, {}, b"x" * 2048)
        with mock.patch.object(module, "MAX_HLS_SEGMENT_BYTES", 1024), \
                mock.patch.object(self.downloader._session, "get", return_value=response) as get:
            with self.assertRaisesRegex(_HlsIntegrityError, "budget"):
                self.downloader._download_hls_segment(
                    _HlsSegment(0, "https://example.test/a.ts", 1, None, None), {},
                    destination=destination,
                )
        get.assert_called_once()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_segment_absolute_deadline_stops_continuously_streaming_response(self):
        response = FakeResponse(200, {}, b"abcdef")
        with mock.patch.object(module, "HLS_READ_DEADLINE_SECONDS", 0), \
                mock.patch.object(self.downloader._session, "get", return_value=response):
            with self.assertRaisesRegex(_HlsIntegrityError, "deadline"):
                self.downloader._download_hls_segment(
                    _HlsSegment(0, "https://example.test/a.ts", 1, None, None), {},
                    destination=self.output / "part.ts",
                )
        self.assertEqual(list(self.output.iterdir()), [])

    def test_manifest_and_aes_key_have_separate_budgets(self):
        response = FakeResponse(200, {}, b"#EXTM3U\n" + b"x" * 1024)
        response.url = "https://example.test/list.m3u8"
        with mock.patch.object(module, "MAX_HLS_MANIFEST_BYTES", 64), \
                mock.patch.object(self.downloader._session, "get", return_value=response):
            with self.assertRaisesRegex(ValueError, "limit"):
                self.downloader._fetch_hls_manifest(response.url, {})
        manifest = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:1,\na.ts\n'
        for length in (0, 15, 17, 1000):
            with self.subTest(length=length):
                with mock.patch.object(self.downloader._session, "get", return_value=FakeResponse(200, {}, b"x" * length)):
                    with self.assertRaises(_HlsIntegrityError):
                        self.downloader._parse_hls_segments(manifest, response.url, {})

    def test_encrypted_segment_streams_across_non_block_chunk_boundaries(self):
        key, iv = bytes(range(16)), bytes(16)
        plain = b"a sample" * 20000
        padding = 16 - len(plain) % 16
        encrypted = AES.new(key, AES.MODE_CBC, iv).encrypt(plain + bytes([padding]) * padding)

        class Response(FakeResponse):
            def iter_content(self, chunk_size):
                for offset in range(0, len(self.body), 17):
                    yield self.body[offset:offset + 17]

        for backend in (module._CryptoAES, None):
            destination = self.output / "plain.ts"
            with mock.patch.object(module, "_CryptoAES", backend), \
                    mock.patch.object(self.downloader._session, "get", return_value=Response(200, {}, encrypted)):
                result = self.downloader._download_hls_segment(
                    _HlsSegment(0, "https://example.test/a.ts", 1, key, iv), {}, destination=destination,
                )
            self.assertEqual(result, len(plain))
            self.assertEqual(destination.read_bytes(), plain)

    def test_unvalidated_hls_ranges_share_one_complete_response_without_mixing(self):
        session = FakeSession(b"A" * 8192, supports_ranges=True, etag=None)
        real_get = session.get
        self.downloader._session.close()
        self.downloader._session = session
        full_requests = []

        def get(*args, **kwargs):
            response = real_get(*args, **kwargs)
            if kwargs["headers"].get("Range") is None:
                full_requests.append(True)
                response.body = b"B" * len(response.body)
            return response

        session.get = get
        segments = [_HlsSegment(i, "https://example.test/single.ts", 1, None, None,
                                range_start=i * 2048, range_length=2048) for i in range(4)]
        with ThreadPoolExecutor(4) as pool:
            parts = list(pool.map(lambda segment: self.downloader._download_hls_segment(segment, {}), segments))
        self.assertEqual(b"".join(parts), b"B" * 8192)
        self.assertEqual(len(full_requests), 1)
        spool = Path(self.downloader._hls_range_spool.name)
        self.downloader.close()
        self.assertFalse(spool.exists())

    def test_unvalidated_hls_object_respects_its_disk_budget(self):
        session = FakeSession(b"A" * 8192, supports_ranges=True, etag=None)
        self.downloader._session.close()
        self.downloader._session = session
        segment = _HlsSegment(0, "https://example.test/shared.ts", 1, None, None,
                              range_start=0, range_length=2)
        with mock.patch.object(module, "MAX_HLS_RANGE_OBJECT_BYTES", 1024):
            with self.assertRaisesRegex(_HlsIntegrityError, "budget"):
                self.downloader._download_hls_segment(segment, {})
        self.assertEqual(self.downloader._hls_range_objects, {})
        self.assertEqual(list(Path(self.downloader._hls_range_spool.name).iterdir()), [])

    def test_hls_ranges_require_same_strong_entity_and_send_if_match(self):
        session = FakeSession(b"A" * 8192, supports_ranges=True, etag='"first"')
        self.downloader._session.close()
        self.downloader._session = session
        segment = _HlsSegment(0, "https://example.test/shared.ts", 1, None, None,
                              range_start=0, range_length=2048)
        self.assertEqual(self.downloader._download_hls_segment(segment, {}), b"A" * 2048)
        real_get = session.get
        seen = []

        def changed(*args, **kwargs):
            seen.append(kwargs["headers"].get("If-Match"))
            response = real_get(*args, **kwargs)
            response.headers["ETag"] = '"second"'
            response.body = b"B" * len(response.body)
            return response

        session.get = changed
        with self.assertRaisesRegex(module._HlsByteRangeError, "resource version"):
            self.downloader._download_hls_segment(segment, {})
        self.assertEqual(seen, ['"first"'])


class HlsFallbackRealMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        HlsIntegrityAudioTests.setUpClass()
        cls.addClassCleanup(HlsIntegrityAudioTests.doClassCleanups)
        cls.fixtures, cls.ffmpeg = HlsIntegrityAudioTests.fixtures, HlsIntegrityAudioTests.ffmpeg

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "out"
        self.output.mkdir()
        self.fixtures_copy = Path(temporary.name) / "source"
        shutil.copytree(self.fixtures, self.fixtures_copy)
        self.downloader = VideoDownloader(self.output)
        self.addCleanup(self.downloader.close)

    def item(self):
        return VideoItem("fallback", str(self.fixtures_copy / "mixed.m3u8"),
                         "https://example.test/", duration=3, is_direct=True, is_hls=True)

    def fallback(self, item, updates):
        with mock.patch.object(self.downloader, "_download_hls_parallel",
                               side_effect=_HlsFallbackError("native failed", item.url, expected_duration=3,
                                                             expected_video_duration=3)), \
                mock.patch.object(self.downloader, "_download_hls_ytdlp", side_effect=RuntimeError("offline unavailable")):
            return self.downloader.download(item, on_progress=updates.append)

    def test_real_ffmpeg_fallback_rejects_missing_first_middle_and_last_fragment(self):
        for index in range(3):
            with self.subTest(index=index):
                missing = self.fixtures_copy / f"mixed{index:02d}.ts"
                payload = missing.read_bytes()
                missing.unlink()
                updates = []
                try:
                    with self.assertRaises(RuntimeError):
                        self.fallback(self.item(), updates)
                finally:
                    missing.write_bytes(payload)
                self.assertEqual(list(self.output.iterdir()), [])
                self.assertFalse(any(event.status == "completed" for event in updates))

    def test_real_ffmpeg_complete_fallback_remains_successful(self):
        updates = []
        result = self.fallback(self.item(), updates)
        self.assertGreater(result.stat().st_size, 0)
        self.assertEqual(updates[-1].status, "completed")

    def test_short_audio_with_full_video_cannot_pass_either_fallback(self):
        short_audio = self.fixtures_copy / "short.m4a"
        HlsIntegrityAudioTests.run_ffmpeg("-i", str(self.fixtures_copy / "source.mp4"),
                                        "-map", "0:a:0", "-t", "2", "-c", "copy", str(short_audio))
        short_media = self.fixtures_copy / "short-audio.mp4"
        HlsIntegrityAudioTests.run_ffmpeg("-i", str(self.fixtures_copy / "source.mp4"), "-i", str(short_audio),
                                        "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", str(short_media))
        item = self.item()

        def incomplete(*_args, **_kwargs):
            output = self.downloader.output_dir / "short.mp4"
            shutil.copyfile(short_media, output)
            return output

        updates = []
        with mock.patch.object(self.downloader, "_download_hls_parallel", side_effect=_HlsFallbackError(
            "native failed", item.url, require_av=True, expected_duration=3.03,
            expected_video_duration=3, expected_audio_duration=3.03,
        )), mock.patch.object(self.downloader, "_download_hls_ytdlp", side_effect=incomplete) as ytdlp, \
                mock.patch.object(self.downloader, "_download_hls_ffmpeg", side_effect=incomplete) as ffmpeg:
            with self.assertRaisesRegex(_HlsIntegrityError, "audio track is shorter"):
                self.downloader.download(item, on_progress=updates.append)
        ytdlp.assert_called_once()
        ffmpeg.assert_called_once()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse(any(event.status == "completed" for event in updates))

    def test_direct_real_media_with_octet_stream_is_accepted(self):
        body = (self.fixtures_copy / "source.mp4").read_bytes()
        session = FakeSession(body, supports_ranges=False)
        with mock.patch.object(parallel_downloader, "build_session", return_value=session):
            result = self.downloader.download(VideoItem(
                "direct", "https://example.test/a.mp4", "https://example.test/", is_direct=True,
            ))
        self.assertEqual(result.read_bytes(), body)

    def test_audio_start_offset_cannot_hide_missing_packet_coverage(self):
        short_audio = self.fixtures_copy / "shift-short.m4a"
        HlsIntegrityAudioTests.run_ffmpeg("-i", str(self.fixtures_copy / "source.mp4"),
                                        "-map", "0:a:0", "-t", "2", "-c", "copy", str(short_audio))
        shifted = self.fixtures_copy / "shifted.mp4"
        HlsIntegrityAudioTests.run_ffmpeg("-i", str(self.fixtures_copy / "source.mp4"),
                                        "-itsoffset", "1", "-i", str(short_audio),
                                        "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", str(shifted))
        with self.assertRaisesRegex(_HlsIntegrityError, "audio track is shorter"):
            self.downloader._verify_hls_output(self.ffmpeg, shifted, 3, require_av=True,
                                               expected_video_duration=3, expected_audio_duration=3)


if __name__ == "__main__":
    # Imported fixture's test cases require a server; this file's own cases do not.
    suite = unittest.TestSuite((unittest.defaultTestLoader.loadTestsFromTestCase(DownloadAuditBoundaryTests),
                               unittest.defaultTestLoader.loadTestsFromTestCase(HlsFallbackRealMediaTests)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
