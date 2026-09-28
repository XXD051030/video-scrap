"""Offline regressions for distinct VideoDownloader output names.

Run directly with ``python3 tests/test_download_name_reservation.py``.
The fakes write real files, but never make a network request or run ffmpeg.
"""

from __future__ import annotations

import errno
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.downloader import VideoDownloader, _HlsSegment
from src.parallel_downloader import ParallelDownloadResult
from src.scraper import VideoItem


_payload = threading.local()


class FakeYoutubeDL:
    """Simulate yt-dlp's output and both of its completion hooks."""

    def __init__(self, opts: dict) -> None:
        self.opts = opts

    def __enter__(self) -> FakeYoutubeDL:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def download(self, _urls: list[str]) -> int:
        # Widen the overlap between independently started downloads. A serial
        # reservation implementation must also be allowed to complete.
        time.sleep(0.03)
        target = Path(self.opts["outtmpl"].replace("%(ext)s", "mp4"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_payload.value)
        self.opts["progress_hooks"][0](
            {"status": "finished", "filename": str(target)}
        )
        for hook in self.opts.get("post_hooks", []):
            hook(str(target))
        return 0


def item(title: str, *, x: bool = False, hls: bool = False) -> VideoItem:
    url = (
        "https://x.com/test/status/123456789/video/1"
        if x
        else "https://example.test/video.m3u8"
        if hls
        else "https://example.test/watch/1"
    )
    return VideoItem(
        title=title,
        url=url,
        source_url=url,
        is_direct=hls,
        is_hls=hls,
        x_playlist_index=1 if x else None,
    )


class DownloadNameReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output_dir = Path(directory.name)

    def run_ytdlp(self, video: VideoItem, data: bytes) -> Path:
        _payload.value = data
        downloader = VideoDownloader(self.output_dir)
        try:
            return downloader.download(video)
        finally:
            downloader.close()

    def assert_files(self, expected: dict[Path, bytes]) -> None:
        for path, data in expected.items():
            self.assertTrue(path.is_file(), path)
            self.assertEqual(path.read_bytes(), data, path)

    def test_generic_same_title_preserves_existing_outputs_and_skips_names(self) -> None:
        first = self.output_dir / "Clip.mp4"
        second = self.output_dir / "Clip (1).mp4"
        first.write_bytes(b"older one")
        second.write_bytes(b"older two")

        with mock.patch("src.downloader.yt_dlp.YoutubeDL", FakeYoutubeDL):
            result = self.run_ytdlp(item("Clip"), b"new video")
            later = self.run_ytdlp(item("Clip"), b"later video")

        self.assertEqual(len({first, second, result, later}), 4)
        self.assert_files(
            {
                first: b"older one",
                second: b"older two",
                result: b"new video",
                later: b"later video",
            }
        )

    def test_publish_to_volume_without_hard_links_keeps_existing_file(self) -> None:
        existing = self.output_dir / "Clip.mp4"
        existing.write_bytes(b"older video")
        with mock.patch("src.downloader.yt_dlp.YoutubeDL", FakeYoutubeDL), mock.patch(
            "src.parallel_downloader.os.link",
            side_effect=OSError(errno.EPERM, "hard links unavailable"),
        ):
            result = self.run_ytdlp(item("Clip"), b"new video")

        self.assertEqual(existing.read_bytes(), b"older video")
        self.assertEqual(result.name, "Clip (1).mp4")
        self.assertEqual(result.read_bytes(), b"new video")

    def test_generic_same_title_concurrent_downloads_keep_both_files(self) -> None:
        start = threading.Barrier(2)

        def run(data: bytes) -> Path:
            start.wait(timeout=5)
            return self.run_ytdlp(item("Shared"), data)

        with mock.patch("src.downloader.yt_dlp.YoutubeDL", FakeYoutubeDL):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run, data) for data in (b"alpha", b"beta")]
                results = [future.result(timeout=10) for future in futures]

        self.assertEqual(len(set(results)), 2)
        self.assertEqual({path.read_bytes() for path in results}, {b"alpha", b"beta"})

    def test_x_same_video_concurrent_downloads_keep_both_files(self) -> None:
        start = threading.Barrier(2)

        def run(data: bytes) -> Path:
            start.wait(timeout=5)
            return self.run_ytdlp(item("Same X post", x=True), data)

        with mock.patch("src.downloader.yt_dlp.YoutubeDL", FakeYoutubeDL):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run, data) for data in (b"first", b"second")]
                results = [future.result(timeout=10) for future in futures]

        self.assertEqual(len(set(results)), 2)
        self.assertEqual({path.read_bytes() for path in results}, {b"first", b"second"})

    def test_direct_download_reports_published_name(self) -> None:
        class FakeParallelDownloader:
            def __init__(self, **_kwargs) -> None:
                pass

            def download(self, _url: str = "", **kwargs) -> ParallelDownloadResult:
                target = kwargs["output_path"]
                data = _payload.value
                target.write_bytes(data)
                return ParallelDownloadResult(target, len(data), 0.1)

            def close(self) -> None:
                pass

        direct = VideoItem(
            title="Direct clip",
            url="https://example.test/media.mp4",
            source_url="https://example.test/page",
            is_direct=True,
            ext="mp4",
        )
        paths = []
        with mock.patch("src.downloader.ParallelDownloader", FakeParallelDownloader):
            for data in (b"first", b"second"):
                _payload.value = data
                updates = []
                downloader = VideoDownloader(self.output_dir)
                try:
                    paths.append(downloader.download(direct, on_progress=updates.append))
                finally:
                    downloader.close()
                self.assertEqual(updates[-1].filename, str(paths[-1]))

        self.assertNotEqual(paths[0], paths[1])
        self.assert_files({paths[0]: b"first", paths[1]: b"second"})

    def test_failed_ytdlp_download_releases_name_for_retry(self) -> None:
        class FailedYoutubeDL(FakeYoutubeDL):
            def download(self, _urls: list[str]) -> int:
                raise RuntimeError("simulated download failure")

        video = item("Retry")
        with mock.patch("src.downloader.yt_dlp.YoutubeDL", FailedYoutubeDL):
            with self.assertRaisesRegex(RuntimeError, "simulated download failure"):
                self.run_ytdlp(video, b"ignored")

        with mock.patch("src.downloader.yt_dlp.YoutubeDL", FakeYoutubeDL):
            result = self.run_ytdlp(video, b"successful retry")

        self.assertEqual(result, self.output_dir / "Retry.mp4")
        self.assert_files({result: b"successful retry"})

    def test_nonzero_ytdlp_result_never_publishes_partial_file(self) -> None:
        class FailedWithFile(FakeYoutubeDL):
            def download(self, urls: list[str]) -> int:
                super().download(urls)
                return 1

        _payload.value = b"unfinished video"
        downloader = VideoDownloader(self.output_dir)
        try:
            with mock.patch("src.downloader.yt_dlp.YoutubeDL", FailedWithFile):
                with self.assertRaisesRegex(RuntimeError, "yt-dlp download failed"):
                    downloader.download(item("Failed"))
        finally:
            downloader.close()

        self.assertFalse((self.output_dir / "Failed.mp4").exists())

    def test_nonzero_native_hls_result_does_not_publish_file(self) -> None:
        class FailedWithFile(FakeYoutubeDL):
            def download(self, urls: list[str]) -> int:
                super().download(urls)
                return 1

        _payload.value = b"unfinished HLS"
        downloader = VideoDownloader(self.output_dir)
        try:
            with mock.patch.object(
                downloader, "_download_hls_parallel",
                side_effect=RuntimeError("parallel failed"),
            ), mock.patch(
                "src.downloader.yt_dlp.YoutubeDL", FailedWithFile
            ), mock.patch("src.downloader.shutil.which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "Native HLS download failed"):
                    downloader.download(item("Failed HLS", hls=True))
        finally:
            downloader.close()

        self.assertFalse((self.output_dir / "Failed HLS.mp4").exists())

    def patch_hls(
        self,
        stack: ExitStack,
        downloader: VideoDownloader,
        *,
        segment_count: int,
        fetch,
        remux,
    ) -> None:
        segments = [
            _HlsSegment(index, f"https://example.test/{index}.ts", 1.0, None, None)
            for index in range(segment_count)
        ]
        stack.enter_context(
            mock.patch.object(
                downloader,
                "_fetch_hls_manifest",
                return_value=("https://example.test/list.m3u8", "#EXTM3U"),
            )
        )
        stack.enter_context(
            mock.patch.object(downloader, "_parse_hls_segments", return_value=segments)
        )
        stack.enter_context(mock.patch.object(downloader, "_download_hls_segment", side_effect=fetch))
        stack.enter_context(mock.patch.object(downloader, "_remux_hls_segments", side_effect=remux))

    def test_hls_partial_and_complete_names_do_not_collide_with_derived_title(self) -> None:
        """A's partial file and a complete video titled A.partial must coexist."""
        partial_job = VideoDownloader(self.output_dir, parallel_connections=2)
        full_job = VideoDownloader(self.output_dir, parallel_connections=1)
        first_segment = threading.Event()
        release_second = threading.Event()

        def fetch_partial(segment: _HlsSegment, _headers: dict) -> bytes:
            if segment.index == 0:
                first_segment.set()
                return b"first segment"
            if not release_second.wait(timeout=5):
                raise TimeoutError("second segment was not released")
            raise RuntimeError("stopped after the first segment")

        def remux_partial(_ffmpeg: str, _concat: Path, target: Path) -> None:
            target.write_bytes(b"partial from A")

        def remux_full(_ffmpeg: str, _concat: Path, target: Path) -> None:
            target.write_bytes(b"complete from A.partial")

        try:
            with ExitStack() as stack:
                stack.enter_context(mock.patch("src.downloader.shutil.which", return_value="/fake/ffmpeg"))
                self.patch_hls(
                    stack,
                    partial_job,
                    segment_count=2,
                    fetch=fetch_partial,
                    remux=remux_partial,
                )
                self.patch_hls(
                    stack,
                    full_job,
                    segment_count=1,
                    fetch=lambda _segment, _headers: b"full segment",
                    remux=remux_full,
                )
                with ThreadPoolExecutor(max_workers=2) as pool:
                    partial_future = pool.submit(partial_job.download, item("A", hls=True))
                    self.assertTrue(first_segment.wait(timeout=5))
                    full_future = pool.submit(full_job.download, item("A.partial", hls=True))
                    partial_job.cancel(keep_partial=True)
                    release_second.set()
                    partial = partial_future.result(timeout=10)
                    full = full_future.result(timeout=10)
        finally:
            release_second.set()
            partial_job.close()
            full_job.close()

        self.assertNotEqual(partial, full)
        self.assert_files(
            {partial: b"partial from A", full: b"complete from A.partial"}
        )

    def test_hls_failure_does_not_publish_internal_half_written_file(self) -> None:
        """If both HLS attempts fail, a half-written remux is not a finished file."""
        old = self.output_dir / "Broken.mp4"
        old.write_bytes(b"previous complete video")
        downloader = VideoDownloader(self.output_dir, parallel_connections=1)

        def fail_remux(_ffmpeg: str, _concat: Path, target: Path) -> None:
            target.write_bytes(b"incomplete remux")
            raise RuntimeError("simulated remux failure")

        class FailedYoutubeDL(FakeYoutubeDL):
            def download(self, _urls: list[str]) -> int:
                raise RuntimeError("simulated fallback failure")

        try:
            with ExitStack() as stack:
                stack.enter_context(mock.patch("src.downloader.shutil.which", return_value="/fake/ffmpeg"))
                stack.enter_context(mock.patch("src.downloader.yt_dlp.YoutubeDL", FailedYoutubeDL))
                stack.enter_context(
                    mock.patch.object(
                        downloader,
                        "_download_hls_ffmpeg",
                        side_effect=RuntimeError("simulated ffmpeg failure"),
                    )
                )
                self.patch_hls(
                    stack,
                    downloader,
                    segment_count=1,
                    fetch=lambda _segment, _headers: b"segment",
                    remux=fail_remux,
                )
                with self.assertRaisesRegex(RuntimeError, "simulated ffmpeg failure"):
                    downloader.download(item("Broken", hls=True))
        finally:
            downloader.close()

        self.assert_files({old: b"previous complete video"})
        self.assertEqual(list(self.output_dir.glob("*.mp4")), [old])


if __name__ == "__main__":
    unittest.main()
