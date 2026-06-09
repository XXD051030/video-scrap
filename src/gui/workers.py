"""Background worker threads for scraping and downloading.

Keeping these in QThread subclasses prevents the GUI from freezing during
network I/O.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import requests
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from ..downloader import (
    DownloadLaneLimiter,
    DownloadProgress,
    VideoDownloader,
    build_request_headers,
)
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

    def __init__(self, url: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.url = url

    def run(self) -> None:
        scraper = VideoScraper()
        try:
            results = scraper.scrape(self.url, progress=self.log.emit)
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
    ) -> None:
        super().__init__(parent)
        self.index = index
        self.url = url
        self.referer = referer

    def run(self) -> None:
        try:
            headers = build_request_headers(self.referer)
            headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
            resp = _THUMB_SESSION.get(self.url, headers=headers, timeout=15)
            resp.raise_for_status()
            self.ready.emit(self.index, resp.content)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(self.index, str(exc))


class DownloadWorker(QThread):
    """Download a list of VideoItems sequentially."""

    item_started = pyqtSignal(int, object)
    item_progress = pyqtSignal(int, object)
    item_finished = pyqtSignal(int, str)
    item_failed = pyqtSignal(int, str)
    all_done = pyqtSignal()

    def __init__(
        self,
        items: List[VideoItem],
        output_dir: Path,
        quality: str = "best",
        parallel_connections: int = 8,
        lane_limiter: Optional[DownloadLaneLimiter] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.items = items
        self.output_dir = output_dir
        self.quality = quality
        self.parallel_connections = parallel_connections
        self.lane_limiter = lane_limiter
        self._cancelled = False
        self._keep_partial = False
        self._downloader: Optional[VideoDownloader] = None

    def cancel(self, keep_partial: bool = False) -> None:
        self._cancelled = True
        self._keep_partial = self._keep_partial or keep_partial
        if self._downloader is not None:
            self._downloader.cancel(keep_partial=keep_partial)

    def run(self) -> None:
        downloader = VideoDownloader(
            self.output_dir,
            parallel_connections=self.parallel_connections,
            lane_limiter=self.lane_limiter,
        )
        self._downloader = downloader
        try:
            for index, item in enumerate(self.items):
                if self._cancelled:
                    break
                self.item_started.emit(index, item)

                def on_progress(progress: DownloadProgress, idx: int = index) -> None:
                    self.item_progress.emit(idx, progress)

                try:
                    final_path = downloader.download(
                        item,
                        quality=self.quality,
                        on_progress=on_progress,
                    )
                    if not self._cancelled or final_path.exists():
                        self.item_finished.emit(index, str(final_path))
                except Exception as exc:  # noqa: BLE001
                    if not self._cancelled:
                        self.item_failed.emit(index, str(exc))
                    elif self._keep_partial:
                        self.item_failed.emit(index, str(exc))
                    else:
                        # Plain stop (no partial kept): surface it as a stop
                        # so the row isn't later mislabeled "Complete".
                        self.item_failed.emit(index, "Stopped")
        finally:
            try:
                downloader.close()
            except Exception:  # noqa: BLE001
                pass
            self._downloader = None
            self.all_done.emit()
