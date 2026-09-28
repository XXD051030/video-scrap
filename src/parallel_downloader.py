"""Multi-connection segmented downloader for direct media URLs.

Many CDNs throttle a single TCP connection but happily serve many in
parallel. This module probes ``Content-Length`` + ``Accept-Ranges``,
splits the file into N equal Range segments and pulls each one on its
own thread. Aggregate progress is reported to a callback in the same
shape as ``downloader.DownloadProgress`` so the GUI does not care which
backend is doing the work.

Falls back to a single-stream download if the server does not advertise
Range support.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

from .net import build_session


@dataclass
class _SegmentProgress:
    downloaded: int = 0


@dataclass
class ParallelDownloadResult:
    path: Path
    size: int
    elapsed: float


ProgressFn = Callable[[int, int, float], None]
"""``(downloaded_bytes, total_bytes, speed_bytes_per_sec)``"""


_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+)", re.IGNORECASE)


def _parse_content_range(value: str) -> Optional[Tuple[int, int, int]]:
    match = _CONTENT_RANGE_RE.fullmatch(value.strip())
    if match is None:
        return None
    start, end, total = map(int, match.groups())
    if end < start or total <= end:
        return None
    return start, end, total


def _publish_unique(part_path: Path, output_path: Path) -> Path:
    """Atomically publish a finished file without replacing an existing one."""
    index = 0
    while True:
        candidate = (
            output_path
            if index == 0
            else output_path.with_name(
                f"{output_path.stem} ({index}){output_path.suffix}"
            )
        )
        try:
            # The temporary file is in the same directory/filesystem. A hard
            # link fails atomically if another download claimed this name.
            os.link(part_path, candidate)
            return candidate
        except FileExistsError:
            index += 1
        except OSError:
            # Some removable and network filesystems do not support hard
            # links. Reserve the name exclusively, then atomically replace
            # only our own placeholder with the completed temporary file.
            try:
                fd = os.open(
                    str(candidate), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
            except FileExistsError:
                index += 1
                continue
            reserved = None
            try:
                reserved = os.fstat(fd)
                os.close(fd)
                os.replace(part_path, candidate)
            except OSError:
                if reserved is None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                try:
                    current = candidate.stat()
                    if reserved is None or (current.st_dev, current.st_ino) == (
                        reserved.st_dev,
                        reserved.st_ino,
                    ):
                        candidate.unlink()
                except OSError:
                    pass
                raise
            return candidate


def _open_unique_part(directory: Path) -> Tuple[int, Path]:
    """Create a private temporary name while retaining normal output mode."""
    for _ in range(10):
        part_path = directory / f".video-scrap-{secrets.token_hex(16)}.part"
        try:
            fd = os.open(str(part_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            return fd, part_path
        except FileExistsError:
            continue
    raise FileExistsError("Could not reserve a unique temporary download file")


class ParallelDownloader:
    """Range-based segmented downloader that writes pieces directly to disk."""

    def __init__(
        self,
        connections: int = 8,
        min_segment_size: int = 1 * 1024 * 1024,
        chunk_size: int = 256 * 1024,
        timeout: int = 30,
        lane_limiter: Optional[object] = None,
    ) -> None:
        self.connections = max(1, connections)
        self.min_segment_size = min_segment_size
        self.chunk_size = chunk_size
        self.timeout = timeout
        self.lane_limiter = lane_limiter
        self._cancel = threading.Event()
        self._session = build_session(pool=max(self.connections, 8))

    def cancel(self) -> None:
        self._cancel.set()

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass

    def download(
        self,
        url: str,
        output_path: Path,
        headers: Optional[Dict[str, str]] = None,
        progress: Optional[ProgressFn] = None,
    ) -> ParallelDownloadResult:
        headers = dict(headers or {})
        # Range offsets must refer to the bytes returned by iter_content.
        headers["Accept-Encoding"] = "identity"
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        size, accept_ranges = self._probe(url, headers)
        segments = self._plan(size, accept_ranges)

        # Open a single fd once and share across threads. ``os.pwrite``
        # writes at an absolute offset without using/modifying the fd's
        # file pointer, so concurrent writes are safe.
        fd, part_path = _open_unique_part(output_path.parent)
        if size > 0:
            try:
                os.ftruncate(fd, size)
            except OSError:
                pass

        per_segment = [_SegmentProgress() for _ in segments]
        start_ts = time.monotonic()
        last_report = [start_ts]
        report_lock = threading.Lock()

        def maybe_report(force: bool = False) -> None:
            if progress is None:
                return
            now = time.monotonic()
            with report_lock:
                if not force and (now - last_report[0]) < 0.25:
                    return
                last_report[0] = now
            done = sum(s.downloaded for s in per_segment)
            elapsed = max(now - start_ts, 1e-3)
            progress(done, size, done / elapsed)

        def worker(idx: int, span: Tuple[int, int]) -> None:
            start, end = span
            req_headers = dict(headers)
            if accept_ranges and size > 0:
                req_headers["Range"] = f"bytes={start}-{end}"
            acquired = False
            try:
                if self.lane_limiter is not None:
                    self.lane_limiter.acquire(lambda: self._cancel.is_set())
                    acquired = True
                with self._session.get(
                    url,
                    headers=req_headers,
                    stream=True,
                    timeout=self.timeout,
                ) as resp:
                    resp.raise_for_status()
                    response_length = resp.headers.get("Content-Length")
                    if response_length is not None:
                        try:
                            response_length = int(response_length)
                        except ValueError as exc:
                            raise RuntimeError("Invalid response length") from exc
                        if response_length < 0:
                            raise RuntimeError("Invalid response length")

                    if accept_ranges and size > 0:
                        expected_length = end - start + 1
                        actual_range = _parse_content_range(
                            resp.headers.get("Content-Range", "")
                        )
                        if resp.status_code != 206 or actual_range != (
                            start, end, size
                        ):
                            raise RuntimeError(
                                f"Range response mismatch for bytes={start}-{end}: "
                                f"HTTP {resp.status_code}, "
                                f"Content-Range {resp.headers.get('Content-Range', '')!r}"
                            )
                        if resp.headers.get("Content-Encoding", "").strip().lower() not in (
                            "", "identity"
                        ):
                            raise RuntimeError("Range response is content-encoded")
                    else:
                        if resp.status_code != 200:
                            raise RuntimeError(
                                f"Single-stream response was HTTP {resp.status_code}, not 200"
                            )
                        expected_length = size if size > 0 else response_length

                    if (
                        response_length is not None
                        and expected_length is not None
                        and response_length != expected_length
                    ):
                        raise RuntimeError(
                            f"Response length mismatch: expected {expected_length}, "
                            f"Content-Length {response_length}"
                        )

                    offset = start
                    received = 0
                    for chunk in resp.iter_content(chunk_size=self.chunk_size):
                        if self._cancel.is_set():
                            return
                        if not chunk:
                            continue
                        if (
                            expected_length is not None
                            and received + len(chunk) > expected_length
                        ):
                            raise RuntimeError("Response length exceeds expected bytes")
                        written = 0
                        while written < len(chunk):
                            count = os.pwrite(fd, chunk[written:], offset + written)
                            if count <= 0:
                                raise OSError("Short write while saving download")
                            written += count
                        offset += len(chunk)
                        received += len(chunk)
                        per_segment[idx].downloaded += len(chunk)
                        maybe_report()
                    if expected_length is not None and received != expected_length:
                        raise RuntimeError(
                            f"Response length mismatch: expected {expected_length}, "
                            f"received {received}"
                        )
            finally:
                if acquired and self.lane_limiter is not None:
                    self.lane_limiter.release()

        try:
            with ThreadPoolExecutor(max_workers=len(segments)) as pool:
                futures = [
                    pool.submit(worker, i, span)
                    for i, span in enumerate(segments)
                ]
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception:
                        # One segment failed: cancel so the sibling threads
                        # stop streaming immediately instead of each finishing
                        # its whole range before we surface the error.
                        self._cancel.set()
                        raise
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                part_path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            raise

        try:
            os.fsync(fd)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            part_path.unlink(missing_ok=True)
            raise

        try:
            if self._cancel.is_set():
                raise RuntimeError("Download cancelled")
            if size > 0 and part_path.stat().st_size != size:
                raise RuntimeError("Download length mismatch")
            published_path = _publish_unique(part_path, output_path)
        except Exception:
            part_path.unlink(missing_ok=True)
            raise
        try:
            part_path.unlink()
        except OSError:
            # The complete file is already published; a leftover .part can be
            # cleaned up later without marking this download as failed.
            pass

        elapsed = time.monotonic() - start_ts
        final_size = published_path.stat().st_size
        if progress is not None:
            progress(final_size, final_size, final_size / max(elapsed, 1e-3))
        return ParallelDownloadResult(
            path=published_path, size=final_size, elapsed=elapsed
        )

    def _probe(
        self, url: str, headers: Dict[str, str]
    ) -> Tuple[int, bool]:
        # HEAD is only advisory: confirm Range support with an actual GET.
        size = 0
        accept_ranges = False
        try:
            resp = self._session.head(
                url, headers=headers, allow_redirects=True, timeout=self.timeout
            )
            if resp.ok:
                size = int(resp.headers.get("Content-Length") or 0)
                accept_ranges = (
                    resp.headers.get("Accept-Ranges", "").lower() == "bytes"
                )
        except Exception:  # noqa: BLE001
            pass

        try:
            probe_headers = dict(headers)
            probe_headers["Range"] = "bytes=0-1"
            resp = self._session.get(
                url,
                headers=probe_headers,
                stream=True,
                timeout=self.timeout,
                allow_redirects=True,
            )
            with resp:
                if resp.status_code == 206:
                    reported_range = _parse_content_range(
                        resp.headers.get("Content-Range", "")
                    )
                    accept_ranges = bool(
                        reported_range
                        and reported_range[0] == 0
                        and reported_range[1] == min(1, reported_range[2] - 1)
                        and (
                            not resp.headers.get("Content-Length")
                            or int(resp.headers["Content-Length"])
                            == reported_range[1] + 1
                        )
                        and resp.headers.get("Content-Encoding", "").strip().lower()
                        in ("", "identity")
                    )
                    if accept_ranges and reported_range is not None:
                        size = reported_range[2]
                else:
                    accept_ranges = False
        except Exception:  # noqa: BLE001
            accept_ranges = False
        if not accept_ranges:
            # A 200 probe is not consumed. The full GET supplies its own length.
            size = 0
        return size, accept_ranges

    def _plan(self, size: int, accept_ranges: bool) -> List[Tuple[int, int]]:
        if size <= 0 or not accept_ranges:
            # Single stream, end=-1 marker (we will pass without Range header).
            return [(0, -1)]

        seg_count = self.connections
        if size // seg_count < self.min_segment_size:
            seg_count = max(1, size // self.min_segment_size)

        seg_size = size // seg_count
        spans: List[Tuple[int, int]] = []
        for i in range(seg_count):
            start = i * seg_size
            end = (start + seg_size - 1) if i < seg_count - 1 else (size - 1)
            spans.append((start, end))
        return spans
