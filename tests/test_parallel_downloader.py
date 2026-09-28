"""Regression tests for direct downloads without network access.

Run directly: ``python3 tests/test_parallel_downloader.py`` (no pytest needed).
"""

from __future__ import annotations

import errno
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterator, Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import parallel_downloader


PAYLOAD = bytes(range(256)) * 128


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        headers: dict[str, str],
        body: bytes = b"",
        after_first_chunk: Optional[Callable[[], None]] = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self.body = body
        self.after_first_chunk = after_first_chunk

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def raise_for_status(self) -> None:
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        for offset in range(0, len(self.body), chunk_size):
            if offset and self.after_first_chunk is not None:
                callback = self.after_first_chunk
                self.after_first_chunk = None
                callback()
            yield self.body[offset : offset + chunk_size]


class FakeSession:
    def __init__(
        self,
        data: bytes,
        *,
        supports_ranges: bool,
        range_fault: Optional[str] = None,
        single_stream_short: bool = False,
        head_claims_ranges: Optional[bool] = None,
        head_size_override: Optional[int] = None,
        probe_missing_length: bool = False,
        stream_barrier: Optional[threading.Barrier] = None,
    ) -> None:
        self.data = data
        self.supports_ranges = supports_ranges
        self.range_fault = range_fault
        self.single_stream_short = single_stream_short
        self.head_claims_ranges = head_claims_ranges
        self.head_size_override = head_size_override
        self.probe_missing_length = probe_missing_length
        self.stream_barrier = stream_barrier
        self.requested_ranges: list[Optional[str]] = []
        self.after_first_download_chunk: Optional[Callable[[], None]] = None
        self._lock = threading.Lock()

    def head(self, *args: object, **kwargs: object) -> FakeResponse:
        headers = {
            "Content-Length": str(
                len(self.data)
                if self.head_size_override is None
                else self.head_size_override
            )
        }
        advertises_ranges = (
            self.supports_ranges
            if self.head_claims_ranges is None
            else self.head_claims_ranges
        )
        if advertises_ranges:
            headers["Accept-Ranges"] = "bytes"
        return FakeResponse(200, headers)

    def get(self, *args: object, **kwargs: object) -> FakeResponse:
        headers = kwargs.get("headers") or {}
        range_header = headers.get("Range")
        with self._lock:
            self.requested_ranges.append(range_header)
        if range_header is None and self.stream_barrier is not None:
            self.stream_barrier.wait(timeout=5)

        if self.supports_ranges and range_header is not None:
            unit, span = range_header.split("=", 1)
            start_text, end_text = span.split("-", 1)
            assert unit == "bytes"
            start, end = int(start_text), int(end_text)
            body = self.data[start : end + 1]
            actual_end = min(end, len(self.data) - 1)
            if range_header == "bytes=0-1":
                return FakeResponse(
                    206,
                    {
                        "Content-Length": str(len(body)),
                        "Content-Range": f"bytes 0-{actual_end}/{len(self.data)}",
                    },
                    body,
                )
            if self.range_fault == "ignored":
                return FakeResponse(
                    200,
                    {"Content-Length": str(len(self.data))},
                    self.data,
                )
            if self.range_fault == "short":
                body = body[:-1]
            if self.range_fault == "long":
                body += b"x"
            reported_start = start + 1 if self.range_fault == "wrong_start" else start
            reported_total = (
                len(self.data) + 1
                if self.range_fault == "wrong_total"
                else len(self.data)
            )
            response_headers = {
                "Content-Length": str(end - start + 1),
                "Content-Range": f"bytes {reported_start}-{end}/{reported_total}",
            }
            if self.range_fault == "encoded":
                response_headers["Content-Encoding"] = "gzip"
            return FakeResponse(
                206,
                response_headers,
                body,
            )

        if self.range_fault == "invalid_probe" and range_header is not None:
            return FakeResponse(
                206,
                {"Content-Length": "2", "Content-Range": "bytes 1-2/32768"},
                self.data[1:3],
            )

        if self.probe_missing_length and range_header == "bytes=0-1":
            return FakeResponse(200, {}, self.data)

        return FakeResponse(
            200,
            {"Content-Length": str(len(self.data))},
            self.data[:-1] if self.single_stream_short else self.data,
            after_first_chunk=self.after_first_download_chunk
            if range_header is None
            else None,
        )

    def close(self) -> None:
        pass


class ParallelDownloaderTests(unittest.TestCase):
    def assert_no_part_files(self, directory: Path) -> None:
        self.assertEqual(
            [path.name for path in directory.iterdir() if path.name.endswith(".part")],
            [],
        )

    def make_downloader(
        self, session: FakeSession
    ) -> parallel_downloader.ParallelDownloader:
        with mock.patch.object(parallel_downloader, "build_session", return_value=session):
            return parallel_downloader.ParallelDownloader(
                connections=4, min_segment_size=1024, chunk_size=1024
            )

    def test_range_segments_reassemble_exact_bytes(self) -> None:
        session = FakeSession(PAYLOAD, supports_ranges=True)
        downloader = self.make_downloader(session)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "range.bin"
            try:
                result = downloader.download("http://example.test/media", output)
            finally:
                downloader.close()

            self.assertEqual(result.path, output)
            self.assertEqual(result.size, len(PAYLOAD))
            self.assertEqual(output.read_bytes(), PAYLOAD)
            self.assert_no_part_files(output.parent)

        self.assertCountEqual(
            session.requested_ranges,
            [
                "bytes=0-1",
                "bytes=0-8191",
                "bytes=8192-16383",
                "bytes=16384-24575",
                "bytes=24576-32767",
            ],
        )

    def test_no_range_support_uses_single_stream(self) -> None:
        session = FakeSession(PAYLOAD, supports_ranges=False)
        downloader = self.make_downloader(session)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "single.bin"
            try:
                result = downloader.download("http://example.test/media", output)
            finally:
                downloader.close()

            self.assertEqual(result.path, output)
            self.assertEqual(result.size, len(PAYLOAD))
            self.assertEqual(output.read_bytes(), PAYLOAD)
            self.assert_no_part_files(output.parent)

        # A preliminary Range probe is allowed; the download itself has none.
        self.assertCountEqual(session.requested_ranges, ["bytes=0-1", None])

    def test_head_range_claim_is_confirmed_before_segmenting(self) -> None:
        session = FakeSession(
            PAYLOAD, supports_ranges=False, head_claims_ranges=True
        )
        downloader = self.make_downloader(session)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "media.bin"
            try:
                result = downloader.download("http://example.test/media", output)
            finally:
                downloader.close()

            self.assertEqual(result.path.read_bytes(), PAYLOAD)
            self.assertCountEqual(session.requested_ranges, ["bytes=0-1", None])

    def test_probe_200_without_length_uses_full_get_length(self) -> None:
        session = FakeSession(
            PAYLOAD,
            supports_ranges=False,
            head_claims_ranges=True,
            head_size_override=len(PAYLOAD) - 3,
            probe_missing_length=True,
        )
        downloader = self.make_downloader(session)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "media.bin"
            try:
                result = downloader.download("http://example.test/media", output)
            finally:
                downloader.close()

            self.assertEqual(result.path.read_bytes(), PAYLOAD)
            self.assertEqual(result.size, len(PAYLOAD))

    def test_one_byte_file_accepts_valid_206_probe(self) -> None:
        session = FakeSession(b"x", supports_ranges=True)
        downloader = self.make_downloader(session)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "one.bin"
            try:
                result = downloader.download("http://example.test/media", output)
            finally:
                downloader.close()

            self.assertEqual(result.path.read_bytes(), b"x")
            self.assertCountEqual(session.requested_ranges, ["bytes=0-1", "bytes=0-0"])

    def test_cancelled_single_stream_removes_partial_file(self) -> None:
        session = FakeSession(PAYLOAD, supports_ranges=False)
        downloader = self.make_downloader(session)
        session.after_first_download_chunk = downloader.cancel
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cancelled.bin"
            try:
                with self.assertRaisesRegex(RuntimeError, "Download cancelled"):
                    downloader.download("http://example.test/media", output)
            finally:
                downloader.close()

            self.assertFalse(output.exists())
            self.assert_no_part_files(output.parent)

    def test_invalid_range_responses_preserve_existing_file(self) -> None:
        for fault in (
            "ignored", "wrong_start", "wrong_total", "short", "long", "encoded"
        ):
            with self.subTest(fault=fault):
                session = FakeSession(PAYLOAD, supports_ranges=True, range_fault=fault)
                downloader = self.make_downloader(session)
                with tempfile.TemporaryDirectory() as directory:
                    output = Path(directory) / "existing.bin"
                    output.write_bytes(b"previous download")
                    try:
                        with self.assertRaisesRegex(RuntimeError, "Range|length"):
                            downloader.download("http://example.test/media", output)
                    finally:
                        downloader.close()

                    self.assertEqual(output.read_bytes(), b"previous download")
                    self.assert_no_part_files(output.parent)

    def test_short_disk_writes_are_retried(self) -> None:
        session = FakeSession(PAYLOAD, supports_ranges=True)
        downloader = self.make_downloader(session)
        original_write = parallel_downloader.os.pwrite

        def write_part(fd: int, data: bytes, offset: int) -> int:
            return original_write(fd, data[: max(1, len(data) // 2)], offset)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "range.bin"
            try:
                with mock.patch.object(parallel_downloader.os, "pwrite", write_part):
                    result = downloader.download("http://example.test/media", output)
            finally:
                downloader.close()

            self.assertEqual(result.path.read_bytes(), PAYLOAD)

    def test_short_single_stream_preserves_existing_file(self) -> None:
        session = FakeSession(
            PAYLOAD, supports_ranges=False, single_stream_short=True
        )
        downloader = self.make_downloader(session)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "existing.bin"
            output.write_bytes(b"previous download")
            try:
                with self.assertRaisesRegex(RuntimeError, "length"):
                    downloader.download("http://example.test/media", output)
            finally:
                downloader.close()

            self.assertEqual(output.read_bytes(), b"previous download")
            self.assert_no_part_files(output.parent)

    def test_invalid_range_probe_falls_back_to_single_stream(self) -> None:
        session = FakeSession(
            PAYLOAD, supports_ranges=False, range_fault="invalid_probe"
        )
        downloader = self.make_downloader(session)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "probe.bin"
            try:
                result = downloader.download("http://example.test/media", output)
            finally:
                downloader.close()

            self.assertEqual(result.path.read_bytes(), PAYLOAD)
            self.assertCountEqual(session.requested_ranges, ["bytes=0-1", None])

    def test_filesystem_without_hard_links_publishes_without_overwrite(self) -> None:
        session = FakeSession(PAYLOAD, supports_ranges=False)
        downloader = self.make_downloader(session)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "existing.bin"
            output.write_bytes(b"previous download")
            try:
                with mock.patch.object(
                    parallel_downloader.os,
                    "link",
                    side_effect=OSError(errno.EPERM, "hard links unavailable"),
                ):
                    result = downloader.download("http://example.test/media", output)
            finally:
                downloader.close()

            self.assertEqual(output.read_bytes(), b"previous download")
            self.assertEqual(result.path.name, "existing (1).bin")
            self.assertEqual(result.path.read_bytes(), PAYLOAD)
            self.assert_no_part_files(output.parent)

    def test_failed_fallback_publish_removes_only_its_placeholder(self) -> None:
        session = FakeSession(PAYLOAD, supports_ranges=False)
        downloader = self.make_downloader(session)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "existing.bin"
            output.write_bytes(b"previous download")
            try:
                with mock.patch.object(
                    parallel_downloader.os,
                    "link",
                    side_effect=OSError(errno.EPERM, "hard links unavailable"),
                ), mock.patch.object(
                    parallel_downloader.os,
                    "replace",
                    side_effect=OSError("replace failed"),
                ):
                    with self.assertRaisesRegex(OSError, "replace failed"):
                        downloader.download("http://example.test/media", output)
            finally:
                downloader.close()

            self.assertEqual(output.read_bytes(), b"previous download")
            self.assertFalse((output.parent / "existing (1).bin").exists())
            self.assert_no_part_files(output.parent)

    def test_fallback_reservation_setup_failure_removes_placeholder(self) -> None:
        original_close = parallel_downloader.os.close

        def close_then_fail(fd: int) -> None:
            original_close(fd)
            raise OSError(errno.EIO, "close failed")

        for failed_step, patch_target in (
            ("stat failed", mock.patch.object(
                parallel_downloader.os,
                "fstat",
                side_effect=OSError(errno.EIO, "stat failed"),
            )),
            ("close failed", mock.patch.object(
                parallel_downloader.os, "close", side_effect=close_then_fail
            )),
        ):
            with self.subTest(failed_step=failed_step):
                with tempfile.TemporaryDirectory() as directory:
                    part = Path(directory) / "private.part"
                    part.write_bytes(PAYLOAD)
                    output = Path(directory) / "existing.bin"
                    output.write_bytes(b"previous download")
                    with mock.patch.object(
                        parallel_downloader.os,
                        "link",
                        side_effect=OSError(errno.EPERM, "hard links unavailable"),
                    ), patch_target:
                        with self.assertRaisesRegex(OSError, failed_step):
                            parallel_downloader._publish_unique(part, output)

                    self.assertEqual(output.read_bytes(), b"previous download")
                    self.assertFalse((output.parent / "existing (1).bin").exists())
                    self.assertEqual(part.read_bytes(), PAYLOAD)

    def test_cleanup_failure_after_publish_is_still_success(self) -> None:
        session = FakeSession(PAYLOAD, supports_ranges=False)
        downloader = self.make_downloader(session)
        original_unlink = Path.unlink

        def fail_part_cleanup(path: Path, missing_ok: bool = False) -> None:
            if path.name.endswith(".part") and not missing_ok:
                raise OSError("temporary cleanup failed")
            original_unlink(path, missing_ok=missing_ok)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "existing.bin"
            output.write_bytes(b"previous download")
            try:
                with mock.patch.object(Path, "unlink", fail_part_cleanup):
                    result = downloader.download("http://example.test/media", output)
            finally:
                downloader.close()

            self.assertEqual(output.read_bytes(), b"previous download")
            self.assertEqual(result.path.name, "existing (1).bin")
            self.assertEqual(result.path.read_bytes(), PAYLOAD)
            leftovers = [p for p in output.parent.iterdir() if p.name.endswith(".part")]
            self.assertEqual(len(leftovers), 1)
            leftovers[0].unlink()

    def test_sequential_same_name_downloads_keep_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "Same title.mp4"
            results = []
            for data in (b"first video", b"second video"):
                session = FakeSession(data, supports_ranges=False)
                downloader = self.make_downloader(session)
                try:
                    results.append(downloader.download("http://example.test/media", output))
                finally:
                    downloader.close()

            self.assertEqual([result.path.name for result in results], [
                "Same title.mp4", "Same title (1).mp4"
            ])
            self.assertEqual(results[0].path.read_bytes(), b"first video")
            self.assertEqual(results[1].path.read_bytes(), b"second video")
            self.assert_no_part_files(Path(directory))

    def test_concurrent_same_name_downloads_do_not_share_part_file(self) -> None:
        barrier = threading.Barrier(2)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "Same title.mp4"
            downloads = [
                self.make_downloader(
                    FakeSession(data, supports_ranges=False, stream_barrier=barrier)
                )
                for data in (b"A" * 4096, b"B" * 4096)
            ]

            def run(
                downloader: parallel_downloader.ParallelDownloader,
            ) -> parallel_downloader.ParallelDownloadResult:
                try:
                    return downloader.download("http://example.test/media", output)
                finally:
                    downloader.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run, downloader) for downloader in downloads]
                results = [future.result() for future in futures]

            self.assertEqual({result.path.name for result in results}, {
                "Same title.mp4", "Same title (1).mp4"
            })
            self.assertEqual({result.path.read_bytes() for result in results}, {
                b"A" * 4096, b"B" * 4096
            })
            self.assert_no_part_files(Path(directory))

    def test_final_progress_runs_after_publish(self) -> None:
        session = FakeSession(PAYLOAD, supports_ranges=True)
        downloader = self.make_downloader(session)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "range.bin"
            snapshots: list[Optional[bytes]] = []

            def check_published(_done: int, _total: int, _speed: float) -> None:
                snapshots.append(output.read_bytes() if output.exists() else None)

            try:
                downloader.download(
                    "http://example.test/media", output, progress=check_published
                )
            finally:
                downloader.close()

            self.assertEqual(snapshots[-1], PAYLOAD)


if __name__ == "__main__":
    unittest.main()
