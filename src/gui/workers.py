"""Background worker threads for scraping and downloading.

Keeping these in QThread subclasses prevents the GUI from freezing during
network I/O.
"""

from __future__ import annotations

from http.cookiejar import CookieJar
from pathlib import Path
import threading
from typing import List, Optional

import requests
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from ..downloader import (
    DownloadLaneLimiter,
    DownloadProgress,
    build_request_headers,
)
from ..download_job import DownloadCancelled, DownloadCleanupError, DownloadJob
from ..net import build_session
from ..scraper import VideoItem, VideoScraper

# One pooled session shared by all thumbnail fetches (only a few run at a
# time) so repeated hits to the same image CDN reuse connections.
_THUMB_SESSION = build_session(pool=6)


class ScrapeWorker(QThread):
    """Run VideoScraper.scrape on a background thread."""

    log = pyqtSignal(str)
    finished_with_results = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(
        self,
        url: str,
        parent: Optional[QObject] = None,
        x_auth_browser: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        self.url = url
        self.x_auth_browser = x_auth_browser

    def run(self) -> None:
        scraper = VideoScraper()
        try:
            results = scraper.scrape(
                self.url,
                progress=self.log.emit,
                x_auth_browser=self.x_auth_browser,
            )
            self.finished_with_results.emit(results)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class ThumbnailWorker(QThread):
    """Download a thumbnail image and emit raw bytes.

    Sends a browser-like header set (with optional Referer) so CDNs that
    hot-link-protect their image hosts will still serve the file.
    """

    ready = pyqtSignal(int, bytes)
    failed = pyqtSignal(int, str)

    def __init__(
        self,
        index: int,
        url: str,
        referer: Optional[str] = None,
        parent: Optional[QObject] = None,
        cookies: Optional[CookieJar] = None,
    ) -> None:
        super().__init__(parent)
        self.index = index
        self.url = url
        self.referer = referer
        self.cookies = cookies

    def run(self) -> None:
        try:
            headers = build_request_headers(self.referer)
            headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
            resp = _THUMB_SESSION.get(
                self.url,
                headers=headers,
                cookies=self.cookies,
                timeout=15,
            )
            resp.raise_for_status()
            self.ready.emit(self.index, resp.content)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(self.index, str(exc))


class DownloadWorker(QThread):
    """Supervise isolated download jobs without blocking the GUI controls."""

    item_started = pyqtSignal(int, object)
    item_progress = pyqtSignal(int, object)
    item_finished = pyqtSignal(int, str)
    item_failed = pyqtSignal(int, str)
    item_state = pyqtSignal(int, str)
    item_control_error = pyqtSignal(int, str)
    all_done = pyqtSignal()

    def __init__(
        self,
        items: List[VideoItem],
        output_dir: Path,
        quality: str = "best",
        parallel_connections: int = 8,
        lane_limiter: Optional[DownloadLaneLimiter] = None,
        parent: Optional[QObject] = None,
        audio_format: str = "original",
    ) -> None:
        super().__init__(parent)
        self.items = items
        self.output_dir = output_dir
        self.quality = quality
        self.audio_format = audio_format
        self.parallel_connections = parallel_connections
        self.lane_limiter = lane_limiter
        self._cancelled = False
        self._pause_requested = False
        self._done = False
        self._phase = "starting"
        self._current_index = 0
        self._job: Optional[DownloadJob] = None
        self._job_lock = threading.RLock()

    def pause(self) -> bool:
        with self._job_lock:
            if self._cancelled or self._done or self._phase in {
                "finalizing", "completed", "failed", "cancelled",
            }:
                return False
            self._pause_requested = True
            job, index = self._job, self._current_index
        if job is not None:
            accepted = job.pause()
            if not accepted:
                with self._job_lock:
                    self._pause_requested = False
            return accepted
        self.item_state.emit(index, "pausing")
        return True

    def resume(self) -> bool:
        with self._job_lock:
            if self._cancelled or self._done or not self._pause_requested:
                return False
            self._pause_requested = False
            job, index = self._job, self._current_index
        if job is not None:
            return job.resume()
        self.item_state.emit(index, "resuming")
        return True

    def cancel(self, keep_partial: bool = False) -> bool:
        """Cancel unfinished output; pause is the operation that retains it.

        The legacy argument remains accepted for callers. GUI cancellation no
        longer exports a partial result: the complete private job is removed.
        Process shutdown happens in run(), never in this GUI-thread method.
        """
        with self._job_lock:
            if self._done or (
                self._phase == "completed" and self._current_index == len(self.items) - 1
            ):
                return False
            job, index = self._job, self._current_index
            if job is None:
                self._cancelled = True
                self._pause_requested = False
        if job is not None:
            if job.cancel() is False:
                return False
            with self._job_lock:
                self._cancelled = True
                self._pause_requested = False
        else:
            self.item_state.emit(index, "cancelling")
        return True

    def run(self) -> None:
        try:
            for index, item in enumerate(self.items):
                with self._job_lock:
                    self._current_index = index
                    self._phase = "starting"
                    cancelled = self._cancelled
                if cancelled:
                    self.item_state.emit(index, "cancelled")
                    self.item_failed.emit(index, "Cancelled")
                    break
                self.item_started.emit(index, item)

                def on_progress(progress: DownloadProgress, idx: int = index) -> None:
                    self.item_progress.emit(idx, progress)

                def on_state(state: str, idx: int = index) -> None:
                    with self._job_lock:
                        self._phase = state
                    self.item_state.emit(idx, state)

                job = DownloadJob(
                    item,
                    self.output_dir,
                    quality=self.quality,
                    audio_format=self.audio_format,
                    parallel_connections=min(16, self.parallel_connections),
                    on_progress=on_progress,
                    on_state=on_state,
                )
                with self._job_lock:
                    self._job = job
                    cancelled, paused = self._cancelled, self._pause_requested
                if cancelled:
                    job.cancel()
                elif paused:
                    job.pause()
                try:
                    final_path = job.run()
                    # A successful return means final publication was committed
                    # before any late cancellation request could be accepted.
                    self.item_finished.emit(index, str(final_path))
                except DownloadCancelled:
                    on_state("cancelled")
                    self.item_failed.emit(index, "Cancelled")
                except DownloadCleanupError as exc:
                    on_state("failed")
                    self.item_control_error.emit(index, str(exc))
                except Exception as exc:  # noqa: BLE001
                    with self._job_lock:
                        cancelled = self._cancelled
                    on_state("cancelled" if cancelled else "failed")
                    self.item_failed.emit(index, "Cancelled" if cancelled else str(exc))
                finally:
                    with self._job_lock:
                        self._job = None
                if self._cancelled:
                    break
        finally:
            with self._job_lock:
                self._done = True
            self.all_done.emit()
