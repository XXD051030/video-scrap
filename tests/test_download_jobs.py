"""Real spawned download pause/resume/cancel checks using only loopback media.

Run ``build/.venv/bin/python tests/test_download_jobs.py``. FFmpeg is used only
to generate an ordinary synthetic HLS fixture and remux its downloaded result.
Windows process APIs need a native Windows run; these tests do not claim that
mocking an operating-system call verifies a Windows executable.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import multiprocessing
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.download_job import (
    DownloadCancelled, DownloadCleanupError, DownloadJob, _ProcessTree, _item_payload, _send_message,
)
from src.ffmpeg import resolve_ffmpeg
from src.scraper import VideoItem
from src.temp_ownership import (
    MARKER, add_temp_owner, cleanup_stale_dirs, create_owned_tempdir,
    remove_owned_tempdir,
)


@contextmanager
def blocked_marker_reader(thread_name):
    """Hold a real marker descriptor open at the Windows sharing boundary."""
    entered, release = threading.Event(), threading.Event()
    real_open = Path.open

    @contextmanager
    def hold(stream):
        with stream:
            entered.set()
            if not release.wait(10):
                raise RuntimeError("Test did not release the marker reader")
            yield stream

    def open_path(path, *args, **kwargs):
        stream = real_open(path, *args, **kwargs)
        if (path.name == MARKER and threading.current_thread().name == thread_name
                and kwargs.get("mode", args[0] if args else "r") == "r"):
            return hold(stream)
        return stream

    with mock.patch.object(Path, "open", open_path):
        try:
            yield entered, release
        finally:
            release.set()


def descendant_target(directory: str) -> None:
    """A root with a real child and grandchild, each writing a heartbeat."""
    root = Path(directory)
    grandchild_code = (
        "from pathlib import Path\nimport sys,time\np=Path(sys.argv[1])\n"
        "while True:\n p.write_text(str(time.monotonic()))\n time.sleep(.025)\n"
    )
    code = (
        "from pathlib import Path\nimport subprocess,sys,time\nroot=Path(sys.argv[1])\n"
        f"child=subprocess.Popen([sys.executable,'-c',{grandchild_code!r},str(root/'grandchild')])\n"
        "(root/'grandchild.pid').write_text(str(child.pid))\n"
        "while True:\n (root/'child').write_text(str(time.monotonic()))\n time.sleep(.025)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(root)])
    (root / "child.pid").write_text(str(child.pid))
    while True:
        (root / "root").write_text(str(time.monotonic()))
        time.sleep(0.025)


class DownloadJobTests(unittest.TestCase):
    def test_unicode_error_packet_fits_and_roundtrips_over_a_real_pipe(self):
        receiver, sender = multiprocessing.get_context("spawn").Pipe(duplex=False)
        try:
            message = {"kind": "error", "message": "🚀" * 400}
            _send_message(sender, message)
            packet = receiver.recv_bytes(4096)
            self.assertEqual(json.loads(packet), message)
            self.assertLess(len(packet), 4096)
        finally:
            sender.close()
            receiver.close()

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_tmp = tempfile.TemporaryDirectory(prefix="download-jobs-fixture-")
        cls.addClassCleanup(cls.fixture_tmp.cleanup)
        cls.fixture_dir = Path(cls.fixture_tmp.name)
        cls.payload = bytes(range(256)) * (24 * 1024)  # Six MiB, exact byte check.
        cls.quick = cls.payload[:256 * 1024]
        cls.ffmpeg = resolve_ffmpeg()
        cls.routes = {"/slow.mp4": cls.payload, "/quick.mp4": cls.quick}
        if cls.ffmpeg:
            subprocess.run([
                cls.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", "4", "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "18", "-g", "30", "-c:a", "aac", "-b:a", "96k",
                "-hls_time", "1", "-hls_list_size", "0",
                "-hls_segment_filename", str(cls.fixture_dir / "segment%02d.ts"),
                str(cls.fixture_dir / "media.m3u8"),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            for file in cls.fixture_dir.iterdir():
                cls.routes[f"/{file.name}"] = file.read_bytes()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_HEAD(self):  # noqa: N802
                self.serve(head=True)

            def do_GET(self):  # noqa: N802
                self.serve(head=False)

            def serve(self, *, head):
                payload = cls.routes.get(self.path)
                if payload is None:
                    self.send_error(404)
                    return
                start, end = 0, len(payload) - 1
                ranged = False
                if self.headers.get("Range"):
                    match = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers["Range"])
                    if not match:
                        self.send_error(416)
                        return
                    start = int(match.group(1))
                    end = int(match.group(2)) if match.group(2) else end
                    end = min(end, len(payload) - 1)
                    ranged = True
                self.send_response(206 if ranged else 200)
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header(
                    "Content-Type", "application/vnd.apple.mpegurl"
                    if self.path.endswith("m3u8") else "video/mp2t"
                    if self.path.endswith(".ts") else "video/mp4",
                )
                if ranged:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self.end_headers()
                if head:
                    return
                hls = self.path.endswith(".ts")
                step = 1024 if hls else 16384
                try:
                    for position in range(start, end + 1, step):
                        self.wfile.write(payload[position:min(position + step, end + 1)])
                        self.wfile.flush()
                        if self.path == "/slow.mp4" or hls:
                            time.sleep(0.003)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="download-jobs-output-")
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name).resolve()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.addCleanup(self.pool.shutdown)
        self.jobs = []
        self.addCleanup(self.cancel_jobs)

    def cancel_jobs(self) -> None:
        for job in self.jobs:
            job.cancel()

    def item(self, route="/slow.mp4", *, title="Test clip") -> VideoItem:
        url = self.base_url + route
        return VideoItem(
            title=title, url=url, source_url=url, is_direct=True,
            is_hls=route.endswith(".m3u8"),
            ext="m3u8" if route.endswith(".m3u8") else "mp4",
        )

    def start(self, route="/slow.mp4", *, title="Test clip", **kwargs):
        states = []
        progress = []
        lock = threading.Lock()

        def state_update(state):
            with lock:
                states.append(state)

        def progress_update(event):
            with lock:
                progress.append(event)

        job = DownloadJob(
            self.item(route, title=title), self.output,
            parallel_connections=1, on_state=state_update,
            on_progress=progress_update, **kwargs,
        )
        self.jobs.append(job)
        future = self.pool.submit(job.run)
        return job, future, states, progress

    def wait_for(self, predicate, *, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Timed out waiting for download state")
            time.sleep(0.02)

    def assert_clean(self):
        self.assertEqual(list(self.output.glob(".video-scrap-job-*")), [])
        self.assertEqual(list(self.output.rglob("*.part")), [])

    def test_cancel_before_run_is_sticky_and_creates_no_process_or_output(self):
        states = []
        job = DownloadJob(self.item(), self.output, on_state=states.append)
        started = time.monotonic()
        job.cancel()
        self.assertLess(time.monotonic() - started, 0.1)
        with self.assertRaises(DownloadCancelled):
            job.run()
        self.assertIsNone(job.pid)
        self.assertEqual(states, ["cancelling", "cancelled"])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_direct_pause_resume_preserves_process_and_exact_bytes(self):
        job, future, states, progress = self.start()
        self.wait_for(lambda: any(event.downloaded_bytes > 0 for event in progress))
        pid = job.pid
        started = time.monotonic()
        self.assertTrue(job.pause())
        self.assertLess(time.monotonic() - started, 0.1)
        self.wait_for(lambda: job.is_paused)
        self.assertEqual(psutil.Process(pid).status(), psutil.STATUS_STOPPED)
        snapshots = {
            path: path.stat().st_mtime_ns
            for path in self.output.rglob("*") if path.is_file()
        }
        count = len(progress)
        time.sleep(0.35)
        self.assertFalse(future.done())
        self.assertEqual(len(progress), count)
        self.assertEqual({path: path.stat().st_mtime_ns for path in snapshots}, snapshots)
        self.assertTrue(job.resume())
        result = future.result(timeout=20)
        self.assertEqual(result.read_bytes(), self.payload)
        self.assertEqual(job.pid, pid)
        self.assertIn("paused", states)
        self.assertEqual(states[-1], "completed")
        self.assertFalse(job.pause())
        self.assertFalse(job.resume())
        self.assertFalse(job.cancel())
        self.assertEqual(progress[-1].filename, str(result))
        self.assertFalse(psutil.pid_exists(pid))
        self.assert_clean()

    def test_paused_job_does_not_block_another_real_download(self):
        paused, first, _, progress = self.start(title="Paused")
        self.wait_for(lambda: any(event.downloaded_bytes > 0 for event in progress))
        self.assertTrue(paused.pause())
        self.wait_for(lambda: paused.is_paused)
        other, second, _, _ = self.start("/quick.mp4", title="Other")
        result = second.result(timeout=20)
        self.assertEqual(result.read_bytes(), self.quick)
        self.assertEqual(other.state, "completed")
        self.assertTrue(paused.is_paused)
        self.assertFalse(first.done())
        paused.cancel()
        with self.assertRaises(DownloadCancelled):
            first.result(timeout=10)
        self.assertEqual(list(self.output.glob("*.mp4")), [result])
        self.assert_clean()

    def test_pause_before_run_is_preserved_until_explicit_resume(self):
        states = []
        job = DownloadJob(
            self.item(), self.output, parallel_connections=1, on_state=states.append,
        )
        self.jobs.append(job)
        self.assertTrue(job.pause())
        future = self.pool.submit(job.run)
        self.wait_for(lambda: job.is_paused)
        time.sleep(0.1)
        self.assertFalse(future.done())
        self.assertTrue(job.resume())
        result = future.result(timeout=20)
        self.assertEqual(result.read_bytes(), self.payload)
        self.assertEqual(states[:2], ["pausing", "paused"])
        self.assert_clean()

    def test_cancel_while_spawn_starting_stays_cancelled(self):
        job, future, states, _ = self.start()
        job.cancel()
        with self.assertRaises(DownloadCancelled):
            future.result(timeout=10)
        self.assertEqual(states[-1], "cancelled")
        self.assertNotIn("completed", states)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_early_cancel_waits_for_its_open_marker_before_removing_staging(self):
        allow_creation_return = threading.Event()
        cleanup_entered = threading.Event()

        with blocked_marker_reader("StaleMediaCleanup") as (marker_open, release_marker):
            def create_then_wait(prefix, directory):
                work = create_owned_tempdir(prefix, directory)
                if not marker_open.wait(5) or not allow_creation_return.wait(5):
                    raise RuntimeError("Test did not reach early cancellation")
                return work

            def remove(work):
                cleanup_entered.set()
                return remove_owned_tempdir(work)

            with mock.patch("src.download_job.create_owned_tempdir", create_then_wait), mock.patch(
                "src.download_job.remove_owned_tempdir", remove,
            ):
                job, future, states, _ = self.start()
                try:
                    self.assertTrue(marker_open.wait(5))
                    self.assertTrue(job.cancel())
                    allow_creation_return.set()
                    self.assertTrue(cleanup_entered.wait(5))
                    # This also fails deterministically on POSIX if cleanup
                    # unlinks an open marker instead of waiting for its reader.
                    time.sleep(0.1)
                    self.assertFalse(future.done())
                    self.assertEqual(len(list(self.output.rglob(MARKER))), 1)
                finally:
                    allow_creation_return.set()
                    release_marker.set()
                with self.assertRaises(DownloadCancelled):
                    future.result(timeout=10)
        self.assertEqual(states[-1], "cancelled")
        self.assertNotIn("failed", states)
        self.assertIsNone(job.pid)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_marker_update_waits_for_reader_without_blocking_other_directories(self):
        with mock.patch("src.temp_ownership.schedule_stale_cleanup"):
            work = create_owned_tempdir(".video-scrap-job-", self.output)
            other_parent = self.output / "other"
            other_parent.mkdir()
            other = create_owned_tempdir(".video-scrap-job-", other_parent)
        scan_errors = []

        def scan():
            try:
                cleanup_stale_dirs(self.output, ".video-scrap-job-")
            except BaseException as exc:
                scan_errors.append(exc)

        with blocked_marker_reader("ControlledStaleCleanup") as (marker_open, release_marker):
            scanner = threading.Thread(target=scan, name="ControlledStaleCleanup")
            scanner.start()
            try:
                self.assertTrue(marker_open.wait(5))
                update = self.pool.submit(add_temp_owner, work, os.getpid())
                time.sleep(0.1)
                self.assertFalse(update.done())
                # A scan of one task must not serialize I/O for a different
                # task behind its process-identity checks or marker reader.
                unrelated = self.pool.submit(add_temp_owner, other, os.getpid())
                unrelated.result(timeout=2)
                self.assertEqual(len(json.loads((other / MARKER).read_text())["owners"]), 2)
                self.pool.submit(remove_owned_tempdir, other).result(timeout=2)
                self.assertFalse(other.exists())
            finally:
                release_marker.set()
                scanner.join(timeout=5)
            update.result(timeout=5)
        self.assertFalse(scanner.is_alive())
        self.assertEqual(scan_errors, [])
        self.assertEqual(len(json.loads((work / MARKER).read_text())["owners"]), 2)

    def test_concurrent_equal_titles_preserve_both_exact_outputs(self):
        first_job, first, _, _ = self.start("/quick.mp4", title="Equal title")
        second_job, second, _, _ = self.start("/quick.mp4", title="Equal title")
        outputs = {first.result(timeout=20), second.result(timeout=20)}
        self.assertEqual(len(outputs), 2)
        self.assertEqual({output.read_bytes() for output in outputs}, {self.quick})
        self.assertEqual(first_job.state, "completed")
        self.assertEqual(second_job.state, "completed")
        self.assert_clean()

    def test_cancel_running_direct_job_never_reports_completion(self):
        job, future, states, progress = self.start()
        self.wait_for(lambda: any(event.downloaded_bytes > 0 for event in progress))
        pid = job.pid
        started = time.monotonic()
        job.cancel()
        self.assertLess(time.monotonic() - started, 0.1)
        with self.assertRaises(DownloadCancelled):
            future.result(timeout=10)
        self.assertEqual(states[-1], "cancelled")
        self.assertNotIn("completed", states)
        self.assertFalse(any(event.status == "completed" for event in progress))
        self.assertFalse(psutil.pid_exists(pid))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_real_ytdlp_backend_pauses_and_resumes_without_restarting(self):
        states = []
        progress = []
        video = self.item()
        video.is_direct = False  # Exercise the real generic yt-dlp route.
        job = DownloadJob(
            video, self.output, parallel_connections=1,
            on_state=states.append, on_progress=progress.append,
        )
        self.jobs.append(job)
        future = self.pool.submit(job.run)
        self.wait_for(lambda: any(event.downloaded_bytes > 0 for event in progress))
        pid = job.pid
        self.assertTrue(job.pause())
        self.wait_for(lambda: job.is_paused)
        time.sleep(0.2)
        self.assertFalse(future.done())
        self.assertTrue(job.resume())
        result = future.result(timeout=25)
        self.assertEqual(result.read_bytes(), self.payload)
        self.assertEqual(job.pid, pid)
        self.assertIn("paused", states)
        self.assert_clean()

    def test_hls_pause_resume_then_valid_video_output(self):
        if not self.ffmpeg:
            self.skipTest("FFmpeg is required for the synthetic HLS fixture")
        job, future, states, progress = self.start("/media.m3u8")
        self.wait_for(lambda: any(event.downloaded_bytes > 0 for event in progress))
        self.assertTrue(job.pause())
        self.wait_for(lambda: job.is_paused)
        pid = job.pid
        files = {path: path.stat().st_mtime_ns for path in self.output.rglob("*") if path.is_file()}
        time.sleep(0.25)
        self.assertEqual({path: path.stat().st_mtime_ns for path in files}, files)
        self.assertTrue(job.resume())
        result = future.result(timeout=25)
        check = subprocess.run([
            self.ffmpeg, "-v", "error", "-nostdin", "-i", str(result),
            "-t", "0.1", "-f", "null", "-",
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("paused", states)
        self.assertEqual(job.pid, pid)
        self.assert_clean()

    def test_cancel_paused_hls_discards_segments_and_root(self):
        if not self.ffmpeg:
            self.skipTest("FFmpeg is required for the synthetic HLS fixture")
        job, future, states, progress = self.start("/media.m3u8")
        self.wait_for(lambda: any(event.downloaded_bytes > 0 for event in progress))
        self.assertTrue(job.pause())
        self.wait_for(lambda: job.is_paused)
        pid = job.pid
        job.cancel()
        with self.assertRaises(DownloadCancelled):
            future.result(timeout=10)
        self.assertEqual(states[-1], "cancelled")
        self.assertNotIn("completed", states)
        self.assertFalse(psutil.pid_exists(pid))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_failed_backend_cleans_its_private_staging(self):
        job, future, states, progress = self.start("/missing.mp4")
        with self.assertRaisesRegex(RuntimeError, "404"):
            future.result(timeout=20)
        self.assertEqual(states[-1], "failed")
        self.assertFalse(any(event.status == "completed" for event in progress))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_connections_capped_and_item_has_only_plain_required_fields(self):
        video = self.item()
        video.raw = {"not": "serialized"}
        video.formats = [{"opaque": object()}]
        video.x_cookiejar = object()
        payload = _item_payload(video)
        self.assertNotIn("raw", payload)
        self.assertNotIn("formats", payload)
        self.assertNotIn("x_cookiejar", payload)
        self.assertEqual(DownloadJob(video, self.output, parallel_connections=32).parallel_connections, 16)
        self.assertEqual(DownloadJob(video, self.output, parallel_connections=0).parallel_connections, 1)

    def test_result_validation_rejects_outside_and_symlink(self):
        work = self.output / ".video-scrap-job-validation"
        work.mkdir()
        outside = self.output / "outside.mp4"
        outside.write_bytes(b"test")
        with self.assertRaisesRegex(RuntimeError, "invalid"):
            DownloadJob._validate_result(str(outside), work)
        local = work / "local.mp4"
        local.write_bytes(b"test")
        link = work / "link.mp4"
        try:
            link.symlink_to(local)
        except (OSError, NotImplementedError):
            return
        with self.assertRaisesRegex(RuntimeError, "symlinked"):
            DownloadJob._validate_result(str(link), work)

    def test_cancel_during_publication_discards_only_this_result(self):
        from src.download_job import _publish_unique as real_publish
        job = DownloadJob(self.item("/quick.mp4"), self.output)
        self.jobs.append(job)
        older = self.output / "Test clip.mp4"
        older.write_bytes(b"previous download")

        def publish_then_cancel(source, destination):
            result = real_publish(source, destination)
            job.cancel()
            return result

        with mock.patch("src.download_job._publish_unique", side_effect=publish_then_cancel):
            with self.assertRaises(DownloadCancelled):
                job.run()
        self.assertEqual(older.read_bytes(), b"previous download")
        self.assertEqual(list(self.output.iterdir()), [older])

    def test_finalizing_rejects_pause_but_cancel_remains_available(self):
        job = DownloadJob(self.item(), self.output)
        job._state_to("finalizing")
        self.assertFalse(job.pause())
        job.cancel()
        self.assertEqual(job.state, "cancelling")
        with self.assertRaises(DownloadCancelled):
            job.run()

    def test_process_tree_pauses_resumes_and_kills_descendants(self):
        directory = self.output / "tree"
        directory.mkdir()
        context = multiprocessing.get_context("spawn")
        process = context.Process(target=descendant_target, args=(str(directory),))
        process.start()
        tree = _ProcessTree(process.pid)
        try:
            self.wait_for(lambda: (directory / "grandchild").exists())
            tree.discover()
            self.assertEqual(len(tree.known), 3)
            tree.suspend()
            values = {name: (directory / name).read_text() for name in ("root", "child", "grandchild")}
            time.sleep(0.2)
            self.assertEqual({name: (directory / name).read_text() for name in values}, values)
            self.assertEqual(len(tree.suspended), 3)
            tree.resume()
            self.wait_for(lambda: (directory / "grandchild").read_text() != values["grandchild"])
            tree.suspend()
            tree.terminate()
            process.join(timeout=5)
            self.assertFalse(process.is_alive())
            self.assertTrue(all(not tree.alive(member) for member in tree.known.values()))
        finally:
            tree.terminate()
            process.join(timeout=5)
            process.close()

    def test_partial_tree_suspension_rolls_back_and_never_stays_paused(self):
        directory = self.output / "rollback"
        directory.mkdir()
        context = multiprocessing.get_context("spawn")
        process = context.Process(target=descendant_target, args=(str(directory),))
        process.start()
        tree = _ProcessTree(process.pid)
        try:
            self.wait_for(lambda: (directory / "grandchild").exists())
            members = tree.discover()
            original = psutil.Process.suspend

            def suspend(member):
                if member.pid != process.pid:
                    raise psutil.AccessDenied(member.pid)
                return original(member)

            with mock.patch.object(psutil.Process, "suspend", suspend):
                with self.assertRaises(psutil.AccessDenied):
                    tree.suspend()
            self.assertEqual(tree.suspended, {})
            self.assertNotEqual(psutil.Process(process.pid).status(), psutil.STATUS_STOPPED)
            self.assertEqual(len(members), 3)
        finally:
            tree.terminate()
            process.join(timeout=5)
            process.close()

    def test_tree_cleanup_failure_still_attempts_join_and_closes_every_handle(self):
        work = self.output / ".video-scrap-job-cleanup-error"
        work.mkdir()
        states = []
        job = DownloadJob(self.item(), self.output, on_state=states.append)
        job._state = "cancelled"
        owner = mock.Mock(pid=101)
        tree = mock.Mock(root=owner, known={101: owner})
        tree.terminate.side_effect = RuntimeError("injected tree failure")
        tree.alive.return_value = False
        process = mock.Mock(pid=101)
        process.is_alive.return_value = False
        receiver = mock.Mock()
        reader = mock.Mock()
        reader.is_alive.return_value = False
        with self.assertRaisesRegex(DownloadCleanupError, "injected tree failure") as caught:
            job._cleanup(
                process=process, tree=tree, receiver=receiver, reader=reader,
                work_dir=work, published=None, completed=False,
            )
        process.join.assert_called_once_with(timeout=3)
        process.close.assert_called_once_with()
        receiver.close.assert_called_once_with()
        reader.join.assert_called_once_with(timeout=1)
        owner.kill.assert_not_called()
        self.assertFalse(work.exists())  # Exit was verified, so safe to remove.
        self.assertIsNone(caught.exception.retained_dir)
        self.assertEqual(states, ["failed"])
        self.assertEqual(job.state, "failed")

    def test_cleanup_live_owned_root_preserves_staging_and_reports_explicit_error(self):
        work = self.output / ".video-scrap-job-owner-still-alive"
        work.mkdir()
        (work / "data.part").write_bytes(b"must remain owned")
        states = []
        job = DownloadJob(self.item(), self.output, on_state=states.append)
        job._state = "completed"
        root = mock.Mock(pid=201)
        child = mock.Mock(pid=202)
        unrelated = mock.Mock(pid=999)
        root.kill.side_effect = psutil.AccessDenied(root.pid)
        child.kill.side_effect = psutil.AccessDenied(child.pid)
        tree = mock.Mock(root=root, known={201: root, 202: child})
        tree.terminate.side_effect = RuntimeError("injected tree failure")
        tree.alive.return_value = True
        process = mock.Mock(pid=201)
        process.is_alive.return_value = True
        process.kill.side_effect = PermissionError("root still alive")
        process.close.side_effect = ValueError("cannot close a live process")
        receiver = mock.Mock()
        reader = mock.Mock()
        reader.is_alive.return_value = False
        with mock.patch("src.download_job.psutil.Process") as global_lookup:
            with self.assertRaisesRegex(DownloadCleanupError, "private staging retained") as caught:
                job._cleanup(
                    process=process, tree=tree, receiver=receiver, reader=reader,
                    work_dir=work, published=None, completed=True,
                )
        global_lookup.assert_not_called()
        unrelated.kill.assert_not_called()
        root.kill.assert_called_once_with()
        child.kill.assert_called_once_with()
        process.kill.assert_called_once_with()
        process.join.assert_called_once_with(timeout=3)
        process.close.assert_called_once_with()
        receiver.close.assert_called_once_with()
        reader.join.assert_called_once_with(timeout=1)
        self.assertEqual((work / "data.part").read_bytes(), b"must remain owned")
        self.assertEqual(caught.exception.retained_dir, work)
        self.assertEqual(states, ["failed"])
        self.assertEqual(job.state, "failed")

    def test_staging_removal_error_remains_visible_and_retains_directory(self):
        work = self.output / ".video-scrap-job-file-still-locked"
        work.mkdir()
        (work / "data.part").write_bytes(b"must remain inspectable")
        states = []
        job = DownloadJob(self.item(), self.output, on_state=states.append)
        job._state = "cancelled"
        with mock.patch("src.temp_ownership.shutil.rmtree", side_effect=PermissionError(
            "another application still owns a file handle",
        )):
            with self.assertRaisesRegex(DownloadCleanupError, "PermissionError") as caught:
                job._cleanup(
                    process=None, tree=None, receiver=None, reader=None,
                    work_dir=work, published=None, completed=False,
                )
        self.assertEqual(caught.exception.retained_dir, work)
        self.assertEqual((work / "data.part").read_bytes(), b"must remain inspectable")
        self.assertEqual(states, ["failed"])

    def test_tree_root_kill_failure_cannot_claim_success_or_drop_suspension(self):
        # Fault injection checks cleanup bookkeeping, not a platform pause.
        root = mock.Mock(pid=301)
        root.kill.side_effect = psutil.AccessDenied(root.pid)
        tree = _ProcessTree.__new__(_ProcessTree)
        tree.root = root
        tree.known = {301: root}
        tree.suspended = {301: root}
        with mock.patch.object(tree, "suspend"), mock.patch.object(
            tree, "alive", return_value=True,
        ), mock.patch("src.download_job.psutil.wait_procs", return_value=([], [])), mock.patch(
            "src.download_job.time.monotonic", side_effect=[0.0, 2.0],
        ):
            with self.assertRaisesRegex(RuntimeError, "Could not terminate"):
                tree.terminate()
        self.assertEqual(tree.suspended, {301: root})


if __name__ == "__main__":
    multiprocessing.freeze_support()
    unittest.main()
