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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests


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


class ParallelDownloader:
    """Range-based segmented downloader that writes pieces directly to disk."""

    def __init__(
        self,
        connections: int = 8,
        min_segment_size: int = 1 * 1024 * 1024,
        chunk_size: int = 256 * 1024,
        timeout: int = 30,
    ) -> None:
        self.connections = max(1, connections)
        self.min_segment_size = min_segment_size
        self.chunk_size = chunk_size
        self.timeout = timeout
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def download(
        self,
        url: str,
        output_path: Path,
        headers: Optional[Dict[str, str]] = None,
        progress: Optional[ProgressFn] = None,
    ) -> ParallelDownloadResult:
        headers = dict(headers or {})
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        part_path = output_path.with_suffix(output_path.suffix + ".part")

        size, accept_ranges = self._probe(url, headers)
        segments = self._plan(size, accept_ranges)

        try:
            part_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

        # Open a single fd once and share across threads. ``os.pwrite``
        # writes at an absolute offset without using/modifying the fd's
        # file pointer, so concurrent writes are safe.
        fd = os.open(
            str(part_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o644,
        )
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
            with requests.get(
                url,
                headers=req_headers,
                stream=True,
                timeout=self.timeout,
            ) as resp:
                resp.raise_for_status()
                offset = start
                for chunk in resp.iter_content(chunk_size=self.chunk_size):
                    if self._cancel.is_set():
                        return
                    if not chunk:
                        continue
                    os.pwrite(fd, chunk, offset)
                    offset += len(chunk)
                    per_segment[idx].downloaded += len(chunk)
                    maybe_report()

        try:
            with ThreadPoolExecutor(max_workers=len(segments)) as pool:
                futures = [
                    pool.submit(worker, i, span)
                    for i, span in enumerate(segments)
                ]
                for fut in as_completed(futures):
                    fut.result()
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
        os.close(fd)

        if self._cancel.is_set():
            try:
                part_path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError("Download cancelled")

        os.replace(str(part_path), str(output_path))

        elapsed = time.monotonic() - start_ts
        final_size = output_path.stat().st_size
        if progress is not None:
            progress(final_size, final_size, final_size / max(elapsed, 1e-3))
        return ParallelDownloadResult(
            path=output_path, size=final_size, elapsed=elapsed
        )

    def _probe(
        self, url: str, headers: Dict[str, str]
    ) -> Tuple[int, bool]:
        # Prefer HEAD; some servers ignore Range info on HEAD, so fall back
        # to a tiny GET range request as a confirmation.
        size = 0
        accept_ranges = False
        try:
            resp = requests.head(
                url, headers=headers, allow_redirects=True, timeout=self.timeout
            )
            if resp.ok:
                size = int(resp.headers.get("Content-Length") or 0)
                accept_ranges = (
                    resp.headers.get("Accept-Ranges", "").lower() == "bytes"
                )
        except Exception:  # noqa: BLE001
            pass

        if not accept_ranges or size == 0:
            try:
                probe_headers = dict(headers)
                probe_headers["Range"] = "bytes=0-1"
                resp = requests.get(
                    url,
                    headers=probe_headers,
                    stream=True,
                    timeout=self.timeout,
                    allow_redirects=True,
                )
                with resp:
                    if resp.status_code == 206:
                        accept_ranges = True
                        cr = resp.headers.get("Content-Range") or ""
                        if "/" in cr:
                            try:
                                size = int(cr.rsplit("/", 1)[-1])
                            except ValueError:
                                pass
                        if size == 0:
                            size = int(resp.headers.get("Content-Length") or 0)
                    elif resp.ok:
                        size = int(resp.headers.get("Content-Length") or 0)
            except Exception:  # noqa: BLE001
                pass
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
