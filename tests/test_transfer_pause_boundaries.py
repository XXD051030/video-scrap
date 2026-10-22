"""Accepted pauses do not consume finite HTTP/HLS transfer budgets."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
from src import download_job, downloader, transfer_clock
from src.browser_cookies import _CancelableAdapter
from src.http_limits import iter_limited_response
from src.scraper import VideoItem
from test_hls_integrity_audio import HlsIntegrityAudioTests

ORIGINAL_CHILD = download_job._download_child
ORIGINAL_SESSION = downloader.build_session


def shortened_child(*args):
    downloader.HLS_READ_DEADLINE_SECONDS = .3
    _CancelableAdapter.HEADER_DEADLINE_SECONDS = .2
    ORIGINAL_CHILD(*args)


def shortened_socket_child(*args):
    def session(*_args, **_kwargs):
        result = ORIGINAL_SESSION(*_args, **_kwargs)
        original_get = result.get
        def get(*params, **options):
            options["timeout"] = (1, .15)
            return original_get(*params, **options)
        result.get = get
        return result
    downloader.build_session = session
    shortened_child(*args)


class ActiveClockTests(unittest.TestCase):
    def tearDown(self):
        transfer_clock.configure_pause_offset(None)

    def test_pause_between_clock_and_offset_samples_cannot_create_expired_deadline(self):
        offset = SimpleNamespace(value=0)
        transfer_clock.configure_pause_offset(offset)
        samples = iter((100.0, 105.0))

        def sample():
            value = next(samples)
            if value == 100.0:
                offset.value = 5_000_000_000  # Supervisor compensates before resume.
            return value

        with patch.object(transfer_clock.time, "monotonic", side_effect=sample):
            self.assertEqual(transfer_clock.monotonic(), 100.0)

    def test_compensated_header_timer_keeps_remaining_active_budget(self):
        offset = SimpleNamespace(value=0)
        transfer_clock.configure_pause_offset(offset)
        fired = threading.Event()
        timer = transfer_clock.deadline_timer(.15, fired.set)
        # Model the same before-resume offset update without cross-process locks.
        offset.value = 200_000_000
        timer.start()
        try:
            self.assertFalse(fired.wait(.2))
            self.assertTrue(fired.wait(.3))
        finally:
            timer.cancel()
            timer.join(1)

    def test_active_stall_timer_remains_finite_and_cancel_revokes_it(self):
        transfer_clock.configure_pause_offset(SimpleNamespace(value=0))
        fired = threading.Event()
        timer = transfer_clock.deadline_timer(.03, fired.set)
        timer.start()
        self.assertTrue(fired.wait(.3))
        timer.join(1)
        fired.clear()
        timer = transfer_clock.deadline_timer(.03, fired.set)
        timer.start()
        timer.cancel()
        timer.join(1)
        self.assertFalse(fired.is_set())


class RealPausedHlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        HlsIntegrityAudioTests.setUpClass()
        cls.addClassCleanup(HlsIntegrityAudioTests.doClassCleanups)
        cls.payload = (HlsIntegrityAudioTests.fixtures / "video00.ts").read_bytes()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.entered, self.release = threading.Event(), threading.Event()
        self.phase = "headers"
        self.media_requests = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                media = self.path != "/list.m3u8"
                if media:
                    owner.media_requests += 1
                body = owner.payload if media else b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST\n"
                if media and owner.phase == "headers":
                    owner.entered.set()
                    owner.release.wait(3)
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if media and owner.phase == "body":
                        self.wfile.write(body[:1])
                        self.wfile.flush()
                        owner.entered.set()
                        owner.release.wait(3)
                        body = body[1:]
                    self.wfile.write(body)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.runner = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.runner.start()
        self.addCleanup(self.runner.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.release.set)
        self.url = f"http://127.0.0.1:{self.server.server_port}/list.m3u8"

    def job(self, name):
        return download_job.DownloadJob(
            VideoItem(name, self.url, self.url, is_direct=True, is_hls=True),
            self.output, parallel_connections=1,
        )

    def test_real_pause_outlasts_header_and_body_deadlines_then_completes(self):
        for phase in ("headers", "body"):
            with self.subTest(phase=phase):
                self.phase = phase
                self.entered.clear()
                self.release.clear()
                job = self.job(phase)
                with ThreadPoolExecutor(1) as pool, patch.object(download_job, "_download_child", shortened_child):
                    result = pool.submit(job.run)
                    try:
                        self.assertTrue(self.entered.wait(15))
                        self.assertTrue(job.pause())
                        deadline = time.monotonic() + 3
                        while not job.is_paused and time.monotonic() < deadline:
                            time.sleep(.01)
                        self.assertTrue(job.is_paused)
                        self.release.set()
                        time.sleep(.5)
                        self.assertTrue(job.resume())
                        path = result.result(15)
                        self.assertGreater(path.stat().st_size, 0)
                        self.assertEqual(job.state, "completed")
                    finally:
                        self.release.set()
                        job.cancel()
                self.assertFalse(list(self.output.glob(".video-scrap-job-*")))

    def test_unpaused_stalled_body_still_fails_without_publishing(self):
        self.phase = "body"
        job = self.job("stall")
        with ThreadPoolExecutor(1) as pool, patch.object(download_job, "_download_child", shortened_child):
            result = pool.submit(job.run)
            try:
                self.assertTrue(self.entered.wait(15))
                with self.assertRaisesRegex(RuntimeError, "deadline|budget"):
                    result.result(5)
                self.assertEqual(job.state, "failed")
            finally:
                self.release.set()
                job.cancel()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_socket_expiring_during_pause_retries_and_finishes_after_resume(self):
        self.phase = "body"
        job = self.job("socket-retry")
        with ThreadPoolExecutor(1) as pool, patch.object(download_job, "_download_child", shortened_socket_child):
            result = pool.submit(job.run)
            try:
                self.assertTrue(self.entered.wait(15))
                self.assertTrue(job.pause())
                deadline = time.monotonic() + 3
                while not job.is_paused and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(job.is_paused)
                time.sleep(.5)
                self.assertTrue(job.resume())
                time.sleep(.06)
                self.release.set()  # Data arrives after resume, not during pause.
                path = result.result(15)
                self.assertGreater(path.stat().st_size, 0)
                self.assertEqual(job.state, "completed")
                self.assertGreaterEqual(self.media_requests, 2)
            finally:
                self.release.set()
                job.cancel()


if __name__ == "__main__":
    unittest.main(verbosity=2)
