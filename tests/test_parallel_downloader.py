"""Regression tests for direct downloads without network access.

Run directly: ``python3 tests/test_parallel_downloader.py`` (no pytest needed).
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
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
    def __init__(self, data: bytes, *, supports_ranges: bool) -> None:
        self.data = data
        self.supports_ranges = supports_ranges
        self.requested_ranges: list[Optional[str]] = []
        self.after_first_download_chunk: Optional[Callable[[], None]] = None
        self._lock = threading.Lock()

    def head(self, *args: object, **kwargs: object) -> FakeResponse:
        headers = {"Content-Length": str(len(self.data))}
        if self.supports_ranges:
            headers["Accept-Ranges"] = "bytes"
        return FakeResponse(200, headers)

    def get(self, *args: object, **kwargs: object) -> FakeResponse:
        headers = kwargs.get("headers") or {}
        range_header = headers.get("Range")
        with self._lock:
            self.requested_ranges.append(range_header)

        if self.supports_ranges and range_header is not None:
            unit, span = range_header.split("=", 1)
            start_text, end_text = span.split("-", 1)
            assert unit == "bytes"
            start, end = int(start_text), int(end_text)
            body = self.data[start : end + 1]
            return FakeResponse(
                206,
                {
                    "Content-Length": str(len(body)),
                    "Content-Range": f"bytes {start}-{end}/{len(self.data)}",
                },
                body,
            )

        return FakeResponse(
            200,
            {"Content-Length": str(len(self.data))},
            self.data,
            after_first_chunk=self.after_first_download_chunk
            if range_header is None
            else None,
        )

    def close(self) -> None:
        pass


class ParallelDownloaderTests(unittest.TestCase):
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
            self.assertFalse(output.with_suffix(".bin.part").exists())

        self.assertCountEqual(
            session.requested_ranges,
            [
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
            self.assertFalse(output.with_suffix(".bin.part").exists())

        # A preliminary Range probe is allowed; the download itself has none.
        self.assertCountEqual(session.requested_ranges, ["bytes=0-1", None])

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
            self.assertFalse(output.with_suffix(".bin.part").exists())


if __name__ == "__main__":
    unittest.main()
