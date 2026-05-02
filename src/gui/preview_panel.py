"""Right-side preview panel: thumbnail, metadata and embedded player."""

from __future__ import annotations

from typing import Optional

from urllib.parse import urlparse

from PyQt6.QtCore import QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QMouseEvent, QPixmap
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStackedLayout,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
    QWidget,
)

from ..media_proxy import MediaProxyServer
from ..scraper import VideoItem
from ..utils import format_duration
from .style import Tokens
from .workers import ThumbnailWorker


class ClickableSlider(QSlider):
    """QSlider that jumps to the clicked position instead of paging.

    Clicking anywhere on the groove sets the value to the corresponding
    point and starts a drag, so the user can immediately keep scrubbing
    without releasing the mouse.
    """

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        if event.button() == Qt.MouseButton.LeftButton:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle_rect = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider,
                opt,
                QStyle.SubControl.SC_SliderHandle,
                self,
            )
            # If the click already landed on the handle let Qt's default
            # drag behaviour take over - that gives the most natural feel.
            if not handle_rect.contains(event.pos()):
                new_value = self._pixel_pos_to_value(event.pos().x())
                self.setValue(new_value)
                self.sliderMoved.emit(new_value)
                self.setSliderDown(True)
                event.accept()
                return
        super().mousePressEvent(event)

    def _pixel_pos_to_value(self, x: int) -> int:
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            opt,
            QStyle.SubControl.SC_SliderGroove,
            self,
        )
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            opt,
            QStyle.SubControl.SC_SliderHandle,
            self,
        )
        slider_min = groove.x()
        slider_max = groove.right() - handle.width() + 1
        pos = max(slider_min, min(x - handle.width() // 2, slider_max))
        span = max(1, slider_max - slider_min)
        return QStyle.sliderValueFromPosition(
            self.minimum(),
            self.maximum(),
            pos - slider_min,
            span,
            opt.upsideDown,
        )


class PreviewPanel(QWidget):
    """Display details and an embedded media player for a selected video."""

    player_message = pyqtSignal(str)

    def __init__(
        self,
        proxy: Optional[MediaProxyServer] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._current: Optional[VideoItem] = None
        self._thumb_worker: Optional[ThumbnailWorker] = None
        self._proxy = proxy
        self._active_proxy_url: Optional[str] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # Stack the thumbnail and the video widget so they share the same
        # screen real estate. Whichever one is on top is what the user sees.
        self.media_stack_host = QWidget()
        self.media_stack_host.setMinimumSize(360, 260)
        self.media_stack_host.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.media_stack_host.setStyleSheet(
            f"background-color: {Tokens.MEDIA_BG};"
            f" border-radius: {Tokens.RADIUS}px;"
            f" border: 1px solid {Tokens.BORDER};"
        )
        self.media_stack = QStackedLayout(self.media_stack_host)
        self.media_stack.setStackingMode(QStackedLayout.StackingMode.StackOne)
        self.media_stack.setContentsMargins(0, 0, 0, 0)

        self.thumbnail_label = QLabel("◐  Select a video to preview")
        self.thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail_label.setFrameShape(QFrame.Shape.NoFrame)
        self.thumbnail_label.setStyleSheet(
            f"color: {Tokens.TEXT_MUTED};"
            f" background-color: {Tokens.MEDIA_BG};"
            f" border-radius: {Tokens.RADIUS}px;"
            " font-size: 14px;"
        )
        self.thumbnail_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        self.video_widget = QVideoWidget()
        self.video_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.video_widget.setStyleSheet(
            f"background-color: {Tokens.MEDIA_BG};"
            f" border-radius: {Tokens.RADIUS}px;"
        )

        # Order matters: thumbnail at index 0 (default), video at index 1.
        self.media_stack.addWidget(self.thumbnail_label)
        self.media_stack.addWidget(self.video_widget)
        self.media_stack.setCurrentIndex(0)

        layout.addWidget(self.media_stack_host, stretch=1)

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self.audio_output.setVolume(0.6)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.play_button = QPushButton("▶  Play")
        self.play_button.setObjectName("Primary")
        self.play_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_button.setMinimumWidth(90)
        self.play_button.clicked.connect(self._toggle_play)
        controls.addWidget(self.play_button)

        self.stop_button = QPushButton("◼  Stop")
        self.stop_button.setObjectName("Ghost")
        self.stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_button.clicked.connect(self._stop)
        controls.addWidget(self.stop_button)

        self.position_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.sliderMoved.connect(self.player.setPosition)
        controls.addWidget(self.position_slider, stretch=1)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setStyleSheet(
            f"color: {Tokens.TEXT_MUTED};"
            ' font-family: "SF Mono", Menlo, Consolas, monospace;'
            " font-size: 12px;"
        )
        controls.addWidget(self.time_label)
        layout.addLayout(controls)

        # Info block under the controls.
        info = QVBoxLayout()
        info.setSpacing(4)
        info.setContentsMargins(2, 4, 2, 0)

        self.title_label = QLabel("")
        self.title_label.setWordWrap(True)
        self.title_label.setProperty("role", "title")
        self.title_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        info.addWidget(self.title_label)

        self.meta_label = QLabel("")
        self.meta_label.setProperty("role", "muted")
        self.meta_label.setWordWrap(True)
        info.addWidget(self.meta_label)

        self.url_label = QLabel("")
        self.url_label.setStyleSheet(
            f"color: {Tokens.ACCENT};"
            ' font-family: "SF Mono", Menlo, Consolas, monospace;'
            " font-size: 12px;"
        )
        self.url_label.setOpenExternalLinks(True)
        self.url_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        self.url_label.setWordWrap(False)
        info.addWidget(self.url_label)
        layout.addLayout(info)

        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.playbackStateChanged.connect(self._on_playback_state_changed)
        self.player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.player.errorOccurred.connect(self._on_player_error)

        self._set_controls_enabled(False)
        self._show_thumbnail_view()

    def show_video(self, video: Optional[VideoItem]) -> None:
        self._stop()
        self._release_proxy_url()
        self._current = video
        self._show_thumbnail_view()

        if video is None:
            self.title_label.setText("")
            self.meta_label.setText("")
            self.url_label.setText("")
            self.url_label.setToolTip("")
            self.thumbnail_label.setText("◐  Select a video to preview")
            self.thumbnail_label.setPixmap(QPixmap())
            self._set_controls_enabled(False)
            self.player.setSource(QUrl())
            return

        self.title_label.setText(video.title)
        meta_parts = [
            f"Duration: {format_duration(video.duration)}",
            f"Resolution: {video.resolution}",
        ]
        if video.ext:
            meta_parts.append(f"Format: {video.ext}")
        if video.uploader:
            meta_parts.append(f"Uploader: {video.uploader}")
        if video.is_hls:
            meta_parts.append("Source: HLS stream")
        elif video.is_direct:
            meta_parts.append("Source: direct link")
        else:
            meta_parts.append("Source: yt-dlp extractor")
        self.meta_label.setText("  •  ".join(meta_parts))

        host = urlparse(video.url).netloc or video.url
        self.url_label.setText(
            f'<a href="{video.url}" style="color: {Tokens.ACCENT};'
            ' text-decoration: none;">'
            f"{host}  ↗</a>"
        )
        self.url_label.setToolTip(video.url)

        self.thumbnail_label.setText("Loading thumbnail...")
        self.thumbnail_label.setPixmap(QPixmap())

        if video.thumbnail:
            self._thumb_worker = ThumbnailWorker(
                0, video.thumbnail, referer=video.referer, parent=self
            )
            self._thumb_worker.ready.connect(self._apply_thumbnail)
            self._thumb_worker.failed.connect(self._on_thumb_failed)
            self._thumb_worker.start()
        else:
            self.thumbnail_label.setText("(No thumbnail available)")

        playable_url = self._best_playable_url(video)
        if playable_url:
            wrapped = self._wrap_with_proxy(playable_url, video)
            self.player.setSource(QUrl(wrapped))
            self._set_controls_enabled(True)
        else:
            self._set_controls_enabled(False)

    def _wrap_with_proxy(self, playable_url: str, video: VideoItem) -> str:
        """Route URLs that need a Referer through the local proxy."""
        if self._proxy is None or not video.referer:
            return playable_url
        try:
            local = self._proxy.register(playable_url, referer=video.referer)
        except Exception:  # noqa: BLE001
            return playable_url
        self._active_proxy_url = local
        return local

    def _release_proxy_url(self) -> None:
        if self._proxy is not None and self._active_proxy_url:
            try:
                self._proxy.unregister(self._active_proxy_url)
            except Exception:  # noqa: BLE001
                pass
        self._active_proxy_url = None

    def _best_playable_url(self, video: VideoItem) -> Optional[str]:
        """Pick a URL Qt can play in-app.

        Direct URLs (mp4, webm, m3u8...) are returned as-is - the proxy
        rewrites HLS manifests so QMediaPlayer's FFmpeg backend handles
        them too. For yt-dlp results we score the available formats.
        """
        if video.is_direct:
            return video.url

        candidates = []
        hls_candidates = []
        for fmt in video.formats or []:
            url = fmt.get("url")
            if not url:
                continue
            ext = (fmt.get("ext") or "").lower()
            protocol = (fmt.get("protocol") or "").lower()
            vcodec = (fmt.get("vcodec") or "").lower()
            acodec = (fmt.get("acodec") or "").lower()
            if "dash" in protocol or ext == "mpd":
                continue
            if vcodec == "none" and acodec == "none":
                continue
            height = fmt.get("height") or 0
            score = 0
            if vcodec != "none" and acodec != "none":
                score += 100
            if 360 <= (height or 0) <= 720:
                score += 50
            if ext == "mp4":
                score += 10
            if "m3u8" in protocol or ext == "m3u8":
                hls_candidates.append((score, url))
            elif ext in ("mp4", "webm", "mov", ""):
                candidates.append((score, url))
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]
        if hls_candidates:
            hls_candidates.sort(reverse=True)
            return hls_candidates[0][1]
        return None

    def _apply_thumbnail(self, _index: int, data: bytes) -> None:
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self.thumbnail_label.setText("(Failed to load thumbnail)")
            return
        target_w = max(320, self.media_stack_host.width() - 8)
        target_h = max(180, self.media_stack_host.height() - 8)
        pixmap = pixmap.scaled(
            target_w,
            target_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.thumbnail_label.setText("")
        self.thumbnail_label.setPixmap(pixmap)

    def _on_thumb_failed(self, _index: int, message: str) -> None:
        self.thumbnail_label.setText(f"(Thumbnail error: {message})")

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.play_button.setEnabled(enabled)
        self.stop_button.setEnabled(enabled)
        self.position_slider.setEnabled(enabled)
        if not enabled:
            self.position_slider.setValue(0)
            self.time_label.setText("00:00 / 00:00")

    def _toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            # Make the video widget visible BEFORE calling play() - Qt's
            # FFmpeg media backend will not render frames into a hidden
            # widget, which previously left playback stuck in the
            # buffering state with the thumbnail still on top.
            self._show_video_view()
            self.player.play()

    def _stop(self) -> None:
        self.player.stop()

    def _on_position_changed(self, position: int) -> None:
        if not self.position_slider.isSliderDown():
            self.position_slider.setValue(position)
        duration = self.player.duration()
        self.time_label.setText(
            f"{format_duration(position / 1000)} / {format_duration(duration / 1000)}"
        )

    def _on_duration_changed(self, duration: int) -> None:
        self.position_slider.setRange(0, duration)

    def _on_playback_state_changed(self, state) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.play_button.setText("❚❚  Pause")
            self._show_video_view()
        elif state == QMediaPlayer.PlaybackState.PausedState:
            self.play_button.setText("▶  Play")
            # Keep the video frame visible while paused.
        else:
            # Stopped - back to thumbnail.
            self.play_button.setText("▶  Play")
            self._show_thumbnail_view()

    def _on_media_status_changed(self, status) -> None:
        # When media is loading or buffering, switch to the video surface
        # so the first frame appears as soon as it decodes; revert to the
        # thumbnail when there's nothing to play.
        if status in (
            QMediaPlayer.MediaStatus.NoMedia,
            QMediaPlayer.MediaStatus.InvalidMedia,
            QMediaPlayer.MediaStatus.EndOfMedia,
        ):
            self._show_thumbnail_view()

    def _show_thumbnail_view(self) -> None:
        self.media_stack.setCurrentIndex(0)

    def _show_video_view(self) -> None:
        self.media_stack.setCurrentIndex(1)

    def _on_player_error(self, error, error_string: str = "") -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        message = error_string or str(error)
        self.player_message.emit(f"Player error: {message}")
        # Reset back to thumbnail so the user understands playback failed.
        self._show_thumbnail_view()
