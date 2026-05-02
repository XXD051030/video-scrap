"""Background worker threads for scraping and downloading.

Keeping these in QThread subclasses prevents the GUI from freezing during
network I/O.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import requests
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from ..downloader import DownloadProgress, VideoDownloader, build_request_headers
from ..scraper import VideoItem, VideoScraper


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
            resp = requests.get(self.url, headers=headers, timeout=15)
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
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.items = items
        self.output_dir = output_dir
        self.quality = quality
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        downloader = VideoDownloader(self.output_dir)
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
                self.item_finished.emit(index, str(final_path))
            except Exception as exc:  # noqa: BLE001
                self.item_failed.emit(index, str(exc))
        self.all_done.emit()
