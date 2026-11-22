"""Redirect identity and bounded native HLS scheduling regressions (N04/N06)."""

from concurrent.futures import ThreadPoolExecutor as RealPool
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import downloader as module
from src import parallel_downloader
from src.downloader import VideoDownloader, _HlsByteRangeError, _HlsIntegrityError, _HlsSegment
from src.parallel_downloader import ParallelDownloader
from src.scraper import VideoItem
from test_parallel_downloader import FakeSession, FakeResponse


def playlist(count, prefix="segment", duration=6):
    return "#EXTM3U\n" + "".join(
        f"#EXTINF:{duration},\n{prefix}{index}.ts\n" for index in range(count)
    ) + "#EXT-X-ENDLIST\n"


class DownloadRecheckTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name)
        self.downloader = VideoDownloader(self.output, parallel_connections=4)
        self.addCleanup(self.downloader.close)

    def replace_session(self, session):
        self.downloader._session.close()
        self.downloader._session = session

    def parallel(self, session):
        with mock.patch.object(parallel_downloader, "build_session", return_value=session):
            downloader = ParallelDownloader(connections=4, min_segment_size=1024)
        self.addCleanup(downloader.close)
        return downloader

    def test_direct_changing_redirect_with_equal_strong_etag_does_not_publish(self):
        session = FakeSession(b"A" * 8192, supports_ranges=True, etag='"1"')
        original = session.get

        def get(*args, **kwargs):
            response = original(*args, **kwargs)
            start = int(kwargs["headers"]["Range"][6:].split("-")[0])
            response.url = "https://cdn.test/a" if start == 0 else "https://cdn.test/b"
            response.body = (b"A" if start == 0 else b"B") * len(response.body)
            return response

        session.get = get
        with self.assertRaisesRegex(RuntimeError, "resource URL changed"):
            self.parallel(session).download("https://origin.test/redirect", self.output / "bad.bin")
        self.assertEqual(list(self.output.iterdir()), [])

    def test_hls_aliases_share_final_resource_validator_and_total(self):
        for fault in ("etag", "total"):
            with self.subTest(fault=fault):
                self.downloader._clear_hls_range_cache()
                session = FakeSession(b"A" * 8192, supports_ranges=True, etag='"1"')
                original = session.get

                def get(url, **kwargs):
                    response = original(url, **kwargs)
                    response.url = "https://CDN.test:443/object#fragment"
                    if url.endswith("alias-b"):
                        if fault == "etag":
                            response.headers["ETag"] = '"2"'
                        else:
                            response.headers["Content-Range"] = "bytes 2048-4095/9000"
                    return response

                session.get = get
                self.replace_session(session)
                first = _HlsSegment(0, "https://origin.test/alias-a", 1, None, None, 0, 2048)
                second = _HlsSegment(1, "https://origin.test/alias-b", 1, None, None, 2048, 2048)
                self.assertEqual(self.downloader._download_hls_segment(first, {}), b"A" * 2048)
                with self.assertRaisesRegex(_HlsByteRangeError, "resource (version|length) changed"):
                    self.downloader._download_hls_segment(second, {})

    def test_unvalidated_hls_full_response_cannot_redirect_to_another_resource(self):
        session = FakeSession(b"A" * 8192, supports_ranges=True, etag=None)
        original = session.get

        def get(*args, **kwargs):
            response = original(*args, **kwargs)
            response.url = ("https://cdn.test/a" if kwargs["headers"].get("Range")
                            else "https://cdn.test/b")
            return response

        session.get = get
        self.replace_session(session)
        segment = _HlsSegment(0, "https://origin.test/alias", 1, None, None, 0, 2048)
        with self.assertRaisesRegex(_HlsByteRangeError, "resource URL changed"):
            self.downloader._download_hls_segment(segment, {}, destination=self.output / "0.ts")
        self.assertFalse((self.output / "0.ts").exists())
        self.assertEqual(list(self.output.glob("*.pending")), [])

    def test_each_uri_needs_an_independent_finite_positive_extinf(self):
        malformed = (
            "a.ts\n", "#EXTINF:1,\na.ts\nb.ts\n",
            "#EXTINF:1,\n#EXTINF:2,\na.ts\n", "#EXTINF:1,\n",
            *(f"#EXTINF:{duration},\na.ts\n" for duration in
              ("nan", "inf", "-inf", "0", "-1", "bad", "1e999")),
            "#EXTINF:1\na.ts\n",
        )
        with mock.patch.object(self.downloader._session, "get") as get:
            for text in malformed:
                with self.subTest(text=text), self.assertRaises(_HlsIntegrityError):
                    # A valid AES key tag cannot cause a network read before
                    # validation discovers a later malformed media entry.
                    self.downloader._parse_hls_segments(
                        '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n' + text,
                        "https://example.test/list.m3u8", {},
                    )
        get.assert_not_called()

    def test_playlist_resource_limit_is_checked_before_fetching_keys(self):
        text = playlist(8).replace("#EXTM3U\n", '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n')
        with mock.patch.object(module, "MAX_HLS_PLAYLIST_RESOURCES", 8), \
                mock.patch.object(self.downloader._session, "get") as get, \
                self.assertRaisesRegex(_HlsIntegrityError, "resource limit"):
            self.downloader._parse_hls_segments(text, "https://example.test/list.m3u8", {})
        get.assert_not_called()

    def test_malformed_fetched_playlist_cannot_bypass_validation_with_fallback(self):
        body = b"#EXTM3U\n#EXTINF:1,\na.ts\na.ts\n#EXT-X-ENDLIST\n"
        response = FakeResponse(200, {"Content-Length": str(len(body))}, body)
        response.url = "https://example.test/list.m3u8"
        with mock.patch.object(self.downloader._session, "get", return_value=response), \
                mock.patch.object(self.downloader, "_download_hls_ytdlp") as native_fallback, \
                mock.patch.object(self.downloader, "_download_hls_ffmpeg") as ffmpeg_fallback, \
                self.assertRaisesRegex(_HlsByteRangeError, "requires its own EXTINF"):
            self.downloader.download(VideoItem(
                "malformed", response.url, response.url, is_direct=True, is_hls=True,
            ))
        native_fallback.assert_not_called()
        ffmpeg_fallback.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_valid_long_playlist_cancellation_stops_before_queuing_all_segments(self):
        segments = self.downloader._parse_hls_segments(
            playlist(1000), "https://example.test/list.m3u8", {},
        )
        gate, window_full = threading.Event(), threading.Event()
        pools, errors, submitted = [], [], []

        class Pool(RealPool):
            def submit(pool_self, function, segment):
                submitted.append(segment.index)
                future = super().submit(function, segment)
                if len(submitted) == 8:
                    window_full.set()
                return future

        def create_pool(**kwargs):
            pool = Pool(**kwargs)
            pools.append(pool)
            return pool

        def fetch(_segment, _headers, *, destination):
            self.assertTrue(gate.wait(10))
            self.downloader._raise_if_cancelled()
            destination.write_bytes(b"segment")
            return 7

        def run():
            try:
                self.downloader._download_hls_segments_parallel(
                    VideoItem("cancel", "https://example.test/list.m3u8", "https://example.test/"),
                    None, segments, {}, "ffmpeg",
                )
            except BaseException as error:
                errors.append(error)

        with mock.patch.object(module, "ThreadPoolExecutor", side_effect=create_pool), \
                mock.patch.object(self.downloader, "_download_hls_segment", side_effect=fetch), \
                mock.patch.object(self.downloader, "_remux_hls_segments") as remux:
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(window_full.wait(5))
                self.assertLessEqual(pools[0]._work_queue.qsize(), 4)
            finally:
                self.downloader.cancel()
                gate.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(submitted), 8)
        self.assertEqual(len(errors), 1)
        self.assertIn("cancelled", str(errors[0]))
        remux.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_normal_day_of_one_second_media_remains_supported(self):
        segments = self.downloader._parse_hls_segments(
            playlist(86_400, prefix="", duration=1), "https://example.test/list.m3u8", {},
        )
        self.assertEqual(len(segments), 86_400)
        self.assertEqual(sum(segment.duration for segment in segments), 86_400)

    def test_valid_two_track_playlist_has_bounded_queue_and_completes_in_order(self):
        video = self.downloader._parse_hls_segments(playlist(600, "v"), "https://example.test/v.m3u8", {})
        audio = self.downloader._parse_hls_segments(playlist(600, "a"), "https://example.test/a.m3u8", {})
        gate, window_full = threading.Event(), threading.Event()
        lock = threading.Lock()
        submitted, active, peaks, pools, results, errors = [], [0], [0], [], [], []

        class Pool(RealPool):
            def submit(pool_self, function, segment):
                with lock:
                    submitted.append(segment.url)
                    active[0] += 1
                    peaks[0] = max(peaks[0], active[0])
                    if len(submitted) == 8:
                        window_full.set()
                future = super().submit(function, segment)

                def completed(_future):
                    with lock:
                        active[0] -= 1

                future.add_done_callback(completed)
                return future

        def create_pool(**kwargs):
            pool = Pool(**kwargs)
            pools.append(pool)
            return pool

        def fetch(segment, _headers, *, destination):
            self.assertTrue(gate.wait(10))
            body = segment.url.rsplit("/", 1)[1].encode() + b"\n"
            destination.write_bytes(body)
            return len(body)

        def remux(_ffmpeg, concat, destination, *, audio_concat=None, **_kwargs):
            def content(path):
                return b"".join(Path(shlex.split(line)[1]).read_bytes()
                                for line in path.read_text().splitlines() if line.startswith("file "))
            self.assertEqual(content(concat), b"".join(f"v{index}.ts\n".encode() for index in range(600)))
            self.assertEqual(content(audio_concat), b"".join(f"a{index}.ts\n".encode() for index in range(600)))
            destination.write_bytes(b"complete AV")

        def run():
            try:
                results.append(self.downloader._download_hls_segments_parallel(
                    VideoItem("bounded", "https://example.test/v.m3u8", "https://example.test/"),
                    None, video, {}, "ffmpeg", audio_segments=audio,
                ))
            except BaseException as error:
                errors.append(error)

        with mock.patch.object(module, "ThreadPoolExecutor", side_effect=create_pool), \
                mock.patch.object(self.downloader, "_download_hls_segment", side_effect=fetch), \
                mock.patch.object(self.downloader, "_remux_hls_segments", side_effect=remux):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(window_full.wait(5))
                self.assertLessEqual(pools[0]._work_queue.qsize(), 4)
                self.assertEqual(len(submitted), 8)
                self.assertTrue(any("/v0.ts" in url for url in submitted))
                self.assertTrue(any("/a0.ts" in url for url in submitted))
            finally:
                gate.set()
                worker.join(15)
                if worker.is_alive():
                    self.downloader.cancel()
                    worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(submitted), 1200)
        self.assertLessEqual(peaks[0], 8)
        self.assertEqual(results[0].read_bytes(), b"complete AV")


class DownloadRedirectHTTPTests(unittest.TestCase):
    """Exercise requests' actual redirect handling with a loopback server."""

    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", "8192")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

            def do_GET(self):
                span = self.headers.get("Range")
                start, end = (map(int, span[6:].split("-")) if span else (0, 8191))
                if self.path.startswith("/redirect") or self.path.startswith("/alias"):
                    target = "/a" if self.path.startswith("/alias") or start == 0 else "/b"
                    self.send_response(302)
                    self.send_header("Location", target)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = (b"A" if self.path == "/a" else b"B") * (end - start + 1)
                self.send_response(206 if span else 200)
                self.send_header("ETag", '"1"')
                self.send_header("Content-Length", str(len(payload)))
                if span:
                    self.send_header("Content-Range", f"bytes {start}-{end}/8192")
                self.end_headers()
                self.wfile.write(payload)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def test_actual_direct_redirect_change_rejects_while_stable_target_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            for source in ("/redirect", "/alias-a"):
                downloader = ParallelDownloader(connections=4, min_segment_size=1024)
                try:
                    output = Path(directory) / "media.bin"
                    if source == "/redirect":
                        with self.assertRaisesRegex(RuntimeError, "resource URL changed"):
                            downloader.download(self.base + source, output)
                        self.assertEqual(list(Path(directory).iterdir()), [])
                    else:
                        result = downloader.download(self.base + source, output)
                        self.assertEqual(result.path.read_bytes(), b"A" * 8192)
                finally:
                    downloader.close()

    def test_actual_hls_redirect_change_rejects_and_aliases_to_one_target_succeed(self):
        with tempfile.TemporaryDirectory() as directory:
            for sources in (("/redirect", "/redirect"), ("/alias-a", "/alias-b")):
                downloader = VideoDownloader(Path(directory))
                try:
                    segments = [_HlsSegment(index, self.base + source, 1, None, None, index * 2048, 2048)
                                for index, source in enumerate(sources)]
                    self.assertEqual(downloader._download_hls_segment(segments[0], {}), b"A" * 2048)
                    if sources[0] == "/redirect":
                        with self.assertRaisesRegex(_HlsByteRangeError, "resource URL changed"):
                            downloader._download_hls_segment(segments[1], {})
                    else:
                        self.assertEqual(downloader._download_hls_segment(segments[1], {}), b"A" * 2048)
                finally:
                    downloader.close()


if __name__ == "__main__":
    unittest.main()
