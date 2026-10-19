"""Cancellation must stop publishing across backends and lifecycle boundaries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.downloader import DownloadProgress, VideoDownloader, _HlsFallbackError, _HlsSegment
from src.scraper import VideoItem


PAYLOAD = b"ordinary synthetic download payload"


def item(*, x: bool = False, hls: bool = False) -> VideoItem:
    url = ("https://x.com/example/status/1234567890/video/1" if x
           else "https://example.test/video.m3u8" if hls
           else "https://example.test/watch/ordinary")
    return VideoItem(title="Cancel sample", url=url, source_url=url,
                     is_direct=hls, is_hls=hls,
                     x_playlist_index=1 if x else None)


class DownloadCancellationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name)
        no_network = mock.patch.object(
            requests.Session, "request", side_effect=AssertionError("No network expected")
        )
        no_network.start()
        self.addCleanup(no_network.stop)
        ffmpeg = mock.patch("src.downloader.resolve_ffmpeg", return_value="/fake/ffmpeg")
        ffmpeg.start()
        self.addCleanup(ffmpeg.stop)

    def downloader(self):
        result = VideoDownloader(self.output, parallel_connections=1)
        self.addCleanup(result.close)
        return result

    def assert_clean(self, *files):
        self.assertEqual(set(self.output.iterdir()), set(files))

    def ydl(self, record, *, between_tracks=None, postprocessor=False, after_post=None):
        class SyntheticYDL:
            def __init__(self, options):
                self.options = options
                record["options"] = options

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def download(self, _urls):
                target = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
                with target.open("wb") as output:
                    for count in range(1, 4):
                        output.write(PAYLOAD)
                        output.flush()
                        record["chunks_written"] = count
                        for hook in self.options["progress_hooks"]:
                            hook({"status": "downloading", "filename": str(target),
                                  "downloaded_bytes": count * len(PAYLOAD),
                                  "total_bytes": 3 * len(PAYLOAD)})
                        if count == 1 and between_tracks is not None:
                            for hook in self.options["progress_hooks"]:
                                hook({"status": "finished", "filename": str(target)})
                            between_tracks()
                for hook in self.options["progress_hooks"]:
                    hook({"status": "finished", "filename": str(target)})
                if postprocessor:
                    for hook in self.options["postprocessor_hooks"]:
                        hook({"status": "started", "postprocessor": "FFmpegMerger",
                              "info_dict": {"filepath": str(target)}})
                    record["postprocessor_entered"] = True
                    for hook in self.options["postprocessor_hooks"]:
                        hook({"status": "finished", "postprocessor": "FFmpegMerger",
                              "info_dict": {"filepath": str(target)}})
                for hook in self.options["post_hooks"]:
                    hook(str(target))
                if after_post is not None:
                    after_post()
                record["returned"] = True
                return 0

        return SyntheticYDL

    def test_ytdlp_all_qualities_and_x_stop_without_publishing(self):
        for x in (False, True):
            for quality in ("best", "1080p", "audio only"):
                for keep_partial in (False, True):
                    with self.subTest(x=x, quality=quality, keep_partial=keep_partial):
                        downloader = self.downloader()
                        record = {}
                        cancelled = False

                        def progress(value):
                            nonlocal cancelled
                            if value.status == "downloading" and not cancelled:
                                cancelled = True
                                downloader.cancel(keep_partial=keep_partial)

                        with mock.patch("src.downloader.yt_dlp.YoutubeDL", self.ydl(record)):
                            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                                downloader.download(item(x=x), quality, progress)
                        self.assertTrue(downloader._cancelled)
                        self.assertEqual(record["chunks_written"], 1)
                        self.assertNotIn("returned", record)
                        self.assert_clean()

    def test_cancelled_before_api_never_starts_backend(self):
        for keep_partial in (False, True):
            with self.subTest(keep_partial=keep_partial):
                downloader = self.downloader()
                downloader.cancel(keep_partial=keep_partial)
                with mock.patch("src.downloader.yt_dlp.YoutubeDL") as backend:
                    with self.assertRaisesRegex(RuntimeError, "cancelled"):
                        downloader.download(item())
                backend.assert_not_called()
                self.assertTrue(downloader._cancelled)
                self.assert_clean()

    def test_cancel_immediately_before_stage_cannot_be_reset(self):
        downloader = self.downloader()
        entered, released = threading.Event(), threading.Event()
        actual = downloader._download_to_stage

        def gated(*args, **kwargs):
            entered.set()
            if not released.wait(5):
                raise AssertionError("Stage gate timed out")
            return actual(*args, **kwargs)

        with mock.patch.object(downloader, "_download_to_stage", side_effect=gated), \
                mock.patch("src.downloader.yt_dlp.YoutubeDL") as backend:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(downloader.download, item())
                try:
                    self.assertTrue(entered.wait(5))
                    downloader.cancel()
                finally:
                    released.set()
                with self.assertRaisesRegex(RuntimeError, "cancelled"):
                    future.result(5)
        backend.assert_not_called()
        self.assertTrue(downloader._cancelled)
        self.assert_clean()

    def test_successful_downloader_can_download_consecutive_items(self):
        downloader = self.downloader()
        record = {}
        with mock.patch("src.downloader.yt_dlp.YoutubeDL", self.ydl(record)):
            first = downloader.download(item())
            second = downloader.download(item())
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), PAYLOAD * 3)
        self.assertEqual(second.read_bytes(), PAYLOAD * 3)
        self.assertFalse(downloader._cancelled)
        self.assert_clean(first, second)

    def test_single_track_finish_does_not_finalize_before_remaining_download(self):
        downloader = self.downloader()
        updates = []
        track_boundary = []
        record = {}

        def between():
            track_boundary.extend(updates)

        with mock.patch("src.downloader.yt_dlp.YoutubeDL",
                        self.ydl(record, between_tracks=between, postprocessor=True)):
            result = downloader.download(item(), on_progress=updates.append)
        self.assertTrue(track_boundary)
        self.assertFalse(any(value.status == "finalizing" for value in track_boundary))
        self.assertTrue(any(value.status == "finalizing" and "Merging" in value.message
                            for value in updates))
        self.assertEqual(updates[-1].status, "completed")
        self.assert_clean(result)

    def test_postprocessor_started_cancellation_prevents_backend_processing(self):
        downloader = self.downloader()
        record = {}

        def progress(value):
            if value.status == "finalizing":
                downloader.cancel()

        with mock.patch("src.downloader.yt_dlp.YoutubeDL", self.ydl(record, postprocessor=True)):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                downloader.download(item(), on_progress=progress)
        self.assertNotIn("postprocessor_entered", record)
        self.assert_clean()

    def test_cancel_after_ytdlp_post_hook_still_rejects_complete_result(self):
        downloader = self.downloader()
        record = {}
        with mock.patch("src.downloader.yt_dlp.YoutubeDL",
                        self.ydl(record, after_post=lambda: downloader.cancel(keep_partial=True))):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                downloader.download(item())
        self.assertTrue(record["returned"])
        self.assert_clean()

    def test_cancel_at_publication_phase_cannot_publish_complete_file(self):
        for keep_partial in (False, True):
            with self.subTest(keep_partial=keep_partial):
                downloader = self.downloader()
                old = self.output / "Cancel sample.mp4"
                old.write_bytes(b"previous complete file")

                def progress(value):
                    if value.status == "finalizing" and value.message.startswith("Saving"):
                        downloader.cancel(keep_partial=keep_partial)

                with mock.patch("src.downloader.yt_dlp.YoutubeDL", self.ydl({})), \
                        mock.patch("src.downloader._publish_unique") as publish:
                    with self.assertRaisesRegex(RuntimeError, "cancelled"):
                        downloader.download(item(), on_progress=progress)
                publish.assert_not_called()
                self.assertEqual(old.read_bytes(), b"previous complete file")
                self.assert_clean(old)
                old.unlink()

    def test_audio_finalizing_cancellation_prevents_probe_or_conversion(self):
        downloader = self.downloader()

        def progress(value):
            if value.status == "finalizing":
                downloader.cancel()

        with mock.patch("src.downloader.yt_dlp.YoutubeDL", self.ydl({})), \
                mock.patch("src.downloader.subprocess.Popen") as child:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                downloader.download(item(), "audio only", progress, "mp3")
        child.assert_not_called()
        self.assert_clean()

    def patch_hls(self, stack, downloader, *, remux):
        stack.enter_context(mock.patch.object(
            downloader, "_fetch_hls_manifest", return_value=(
                "https://example.test/video.m3u8", "#EXTM3U\n", None)))
        stack.enter_context(mock.patch.object(
            downloader, "_parse_hls_segments", return_value=[
                _HlsSegment(0, "https://example.test/0.ts", 1, None, None)]))
        def fetch(_segment, _headers, *, destination):
            destination.write_bytes(PAYLOAD)
            return len(PAYLOAD)
        stack.enter_context(mock.patch.object(downloader, "_download_hls_segment", side_effect=fetch))
        return stack.enter_context(mock.patch.object(downloader, "_remux_hls_segments", side_effect=remux))

    def test_hls_cancel_before_merge_never_creates_process_or_falls_back(self):
        downloader = self.downloader()

        def progress(value):
            if value.status == "finalizing":
                downloader.cancel()

        with ExitStack() as stack:
            remux = self.patch_hls(stack, downloader, remux=lambda *_args: None)
            fallback = stack.enter_context(mock.patch("src.downloader.yt_dlp.YoutubeDL"))
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                downloader.download(item(hls=True), on_progress=progress)
        remux.assert_not_called()
        fallback.assert_not_called()
        self.assert_clean()

    def test_hls_cancel_after_merge_rejects_complete_file_even_with_keep_partial(self):
        for keep_partial in (False, True):
            with self.subTest(keep_partial=keep_partial):
                downloader = self.downloader()

                def remux(_ffmpeg, _concat, target, **_kwargs):
                    target.write_bytes(PAYLOAD)
                    downloader.cancel(keep_partial=keep_partial)

                with ExitStack() as stack:
                    self.patch_hls(stack, downloader, remux=remux)
                    fallback = stack.enter_context(mock.patch("src.downloader.yt_dlp.YoutubeDL"))
                    with self.assertRaisesRegex(RuntimeError, "cancelled"):
                        downloader.download(item(hls=True))
                fallback.assert_not_called()
                self.assert_clean()

    def test_hls_cancel_on_last_segment_keeps_only_an_explicit_partial_result(self):
        for keep_partial in (False, True):
            with self.subTest(keep_partial=keep_partial):
                downloader = self.downloader()
                updates = []

                def progress(value):
                    updates.append(value)
                    if value.message == "Segment 1/1":
                        downloader.cancel(keep_partial=keep_partial)

                def remux(_ffmpeg, _concat, target, *, allow_cancelled=False, **_kwargs):
                    self.assertTrue(allow_cancelled)
                    self.assertIn(".partial", target.name)
                    target.write_bytes(PAYLOAD)

                with ExitStack() as stack:
                    merge = self.patch_hls(stack, downloader, remux=remux)
                    fallback = stack.enter_context(mock.patch("src.downloader.yt_dlp.YoutubeDL"))
                    if keep_partial:
                        result = downloader.download(item(hls=True), on_progress=progress)
                        self.assertEqual(updates[-1].status, "partial")
                        self.assert_clean(result)
                        result.unlink()
                    else:
                        with self.assertRaisesRegex(RuntimeError, "cancelled"):
                            downloader.download(item(hls=True), on_progress=progress)
                        merge.assert_not_called()
                        self.assert_clean()
                fallback.assert_not_called()

    def test_native_hls_cancel_does_not_retry_ffmpeg_or_publish(self):
        downloader = self.downloader()
        record = {}

        def progress(value):
            if value.downloaded_bytes:
                downloader.cancel()

        with mock.patch.object(downloader, "_download_hls_parallel", side_effect=
                               _HlsFallbackError("needs native backend", item(hls=True).url)), \
                mock.patch("src.downloader.yt_dlp.YoutubeDL", self.ydl(record)), \
                mock.patch.object(downloader, "_download_hls_ffmpeg") as ffmpeg:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                downloader.download(item(hls=True), on_progress=progress)
        ffmpeg.assert_not_called()
        self.assert_clean()

    def test_ffmpeg_hls_cancellation_terminates_real_child_and_removes_partial_stage(self):
        downloader = self.downloader()
        actual_popen = subprocess.Popen
        children = []

        def create(command, **kwargs):
            target = command[-1]
            code = (f"import pathlib,sys,time\npathlib.Path({target!r}).write_bytes(b'partial')\n"
                    "sys.stderr.write('out_time_ms=1000000\\n');sys.stderr.flush()\n"
                    "time.sleep(30)\n")
            child = actual_popen([sys.executable, "-c", code], **kwargs)
            children.append(child)
            return child

        def progress(value):
            if value.downloaded_bytes:
                downloader.cancel()

        with mock.patch.object(downloader, "_download_hls_parallel", side_effect=
                               _HlsFallbackError("needs fallback", item(hls=True).url)), \
                mock.patch.object(downloader, "_download_hls_ytdlp", side_effect=RuntimeError("native unavailable")), \
                mock.patch.object(downloader, "_probe_hls_duration", return_value=10), \
                mock.patch("src.downloader.subprocess.Popen", side_effect=create):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                downloader.download(item(hls=True), on_progress=progress)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertIsNone(downloader._ffmpeg_proc)
        self.assert_clean()

    def test_verified_partial_is_allowed_without_progress_listener(self):
        for audio in (False, True):
            with self.subTest(audio=audio):
                downloader = self.downloader()

                def stage(_item, _quality, progress):
                    result = downloader.output_dir / "Cancel sample.partial.mp4"
                    result.write_bytes(PAYLOAD)
                    downloader.cancel(keep_partial=True)
                    progress(DownloadProgress(status="partial", filename=str(result)))
                    return result

                def extract(source, _format, _progress, *, allow_cancelled=False):
                    self.assertTrue(allow_cancelled)
                    result = source.with_suffix(".m4a")
                    result.write_bytes(PAYLOAD)
                    return result

                with mock.patch.object(downloader, "_download_to_stage", side_effect=stage), \
                        mock.patch.object(downloader, "_extract_audio", side_effect=extract):
                    result = downloader.download(item(hls=True), "audio only" if audio else "best")
                self.assertIn(".partial", result.name)
                self.assertEqual(result.read_bytes(), PAYLOAD)
                self.assert_clean(result)
                result.unlink()

    def test_cancellation_during_process_creation_cannot_escape_registration(self):
        downloader = self.downloader()
        actual_popen = subprocess.Popen
        children = []

        def create(command, *args, **kwargs):
            proc = actual_popen(command, *args, **kwargs)
            children.append(proc)
            # Reentrant cancellation deliberately lands between Popen and
            # registration; the context must check again and reap the child.
            downloader.cancel()
            return proc

        with mock.patch("src.downloader.subprocess.Popen", side_effect=create):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                downloader._run_audio_ffmpeg([sys.executable, "-c", "import time; time.sleep(30)"])
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertIsNone(downloader._ffmpeg_proc)
        self.assert_clean()

    def test_registered_running_process_is_terminated_on_cancel(self):
        downloader = self.downloader()
        created = threading.Event()
        actual_popen = subprocess.Popen
        children = []

        def create(command, *args, **kwargs):
            proc = actual_popen(command, *args, **kwargs)
            children.append(proc)
            created.set()
            return proc

        with mock.patch("src.downloader.subprocess.Popen", side_effect=create):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(downloader._run_audio_ffmpeg,
                                     [sys.executable, "-c", "import time; time.sleep(30)"])
                self.assertTrue(created.wait(5))
                # Acquiring the same lock orders cancel after registration.
                downloader.cancel()
                with self.assertRaisesRegex(RuntimeError, "cancelled"):
                    future.result(5)
        self.assertIsNotNone(children[0].poll())
        self.assertIsNone(downloader._ffmpeg_proc)
        self.assert_clean()

    def test_partial_process_is_not_terminated_by_existing_cancel_flag(self):
        downloader = self.downloader()
        downloader.cancel(keep_partial=True)
        created = threading.Event()
        release = self.output / "release-child"
        actual_popen = subprocess.Popen
        children = []
        code = (f"import pathlib,time\np=pathlib.Path({str(release)!r})\n"
                "while not p.exists():\n time.sleep(.01)\n")

        def create(command, *args, **kwargs):
            proc = actual_popen(command, *args, **kwargs)
            children.append(proc)
            created.set()
            return proc

        with mock.patch("src.downloader.subprocess.Popen", side_effect=create):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(downloader._run_audio_ffmpeg,
                                     [sys.executable, "-c", code], allow_cancelled=True)
                try:
                    self.assertTrue(created.wait(5))
                    downloader.cancel()
                    self.assertIsNone(children[0].poll())
                finally:
                    release.touch()
                result, _stderr = future.result(5)
        self.assertEqual(result, 0)
        self.assertIsNone(downloader._ffmpeg_proc)
        self.assertFalse(downloader._ffmpeg_allow_cancelled)
        release.unlink()
        self.assert_clean()

    def test_failed_partial_process_still_cleans_up(self):
        downloader = self.downloader()
        downloader.cancel(keep_partial=True)
        actual_popen = subprocess.Popen
        with self.assertRaisesRegex(RuntimeError, "ffmpeg exited with 7"):
            with mock.patch("src.downloader.subprocess.Popen", side_effect=lambda *_a, **kwargs:
                            actual_popen(
                                [sys.executable, "-c", "raise SystemExit(7)"], **kwargs)):
                downloader._remux_hls_segments("/fake/ffmpeg", self.output / "list.txt",
                                               self.output / "result.mp4", allow_cancelled=True)
        self.assertIsNone(downloader._ffmpeg_proc)
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
