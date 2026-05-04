"""Video list widget with thumbnail rows."""

from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QIcon, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QListWidget,
    QListWidgetItem,
)

from ..scraper import VideoItem
from ..utils import format_duration
from .workers import ThumbnailWorker


class VideoListWidget(QListWidget):
    """Show scraped videos with thumbnails and basic metadata.

    The current item drives both the preview pane and the download action.
    """

    selection_changed = pyqtSignal(object)

    THUMB_SIZE = QSize(176, 99)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("VideoList")
        self.setIconSize(self.THUMB_SIZE)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setUniformItemSizes(False)
        self.setSpacing(0)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self._items: List[VideoItem] = []
        self._thumb_workers: List[ThumbnailWorker] = []
        self.currentItemChanged.connect(self._on_current_item_changed)

    def set_videos(self, videos: List[VideoItem]) -> None:
        self.clear()
        self._stop_workers()
        self._items = list(videos)

        for index, video in enumerate(self._items):
            text = self._format_text(video)
            item = QListWidgetItem(text)
            item.setSizeHint(QSize(0, self.THUMB_SIZE.height() + 22))
            item.setData(Qt.ItemDataRole.UserRole, index)
            placeholder = QPixmap(self.THUMB_SIZE)
            placeholder.fill(Qt.GlobalColor.transparent)
            item.setIcon(QIcon(placeholder))
            item.setToolTip(f"{video.title}\n{video.url}")
            self.addItem(item)

            if video.thumbnail:
                worker = ThumbnailWorker(
                    index, video.thumbnail, referer=video.referer, parent=self
                )
                worker.ready.connect(self._apply_thumbnail)
                worker.start()
                self._thumb_workers.append(worker)

        if self._items:
            self.setCurrentRow(0)

    def videos(self) -> List[VideoItem]:
        return list(self._items)

    def current_video(self) -> Optional[VideoItem]:
        row = self.currentRow()
        if row < 0 or row >= len(self._items):
            return None
        return self._items[row]

    def shutdown(self) -> None:
        self._stop_workers()

    def _format_text(self, video: VideoItem) -> str:
        duration = format_duration(video.duration)
        resolution = video.resolution
        line2_parts = [duration, resolution]
        if video.uploader:
            line2_parts.append(video.uploader)
        if video.is_direct:
            line2_parts.append("direct link")
        return f"{video.title}\n{'  •  '.join(line2_parts)}"

    def _apply_thumbnail(self, index: int, data: bytes) -> None:
        if index < 0 or index >= self.count():
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        pixmap = pixmap.scaled(
            self.THUMB_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        item = self.item(index)
        if item is not None:
            item.setIcon(QIcon(self._round_pixmap(pixmap, radius=6)))

    @staticmethod
    def _round_pixmap(source: QPixmap, radius: int = 6) -> QPixmap:
        """Return a copy of ``source`` with rounded corners on a transparent bg."""
        if source.isNull():
            return source
        rounded = QPixmap(source.size())
        rounded.fill(Qt.GlobalColor.transparent)
        painter = QPainter(rounded)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        path = QPainterPath()
        path.addRoundedRect(
            QRectF(0, 0, source.width(), source.height()), radius, radius
        )
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, source)
        painter.end()
        return rounded

    def _on_current_item_changed(self, _current, _previous) -> None:
        self.selection_changed.emit(self.current_video())

    def _stop_workers(self) -> None:
        for worker in self._thumb_workers:
            if worker.isRunning():
                worker.requestInterruption()
                worker.quit()
                if not worker.wait(1000):
                    worker.terminate()
                    worker.wait(1000)
        self._thumb_workers.clear()
