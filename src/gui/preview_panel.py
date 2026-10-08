"""Right-side preview panel: thumbnail, metadata and embedded player."""

from __future__ import annotations

from typing import Optional

from urllib.parse import urlparse

from PyQt6.QtCore import QEvent, QSize, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence, QMouseEvent, QPixmap, QShortcut
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
from .icons import make_icon
from .style import DARK, Theme
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


class _FullscreenWindow(QWidget):
    """A temporary host; closing it restores the embedded player."""

    exit_requested = pyqtSignal()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        event.ignore()
        self.exit_requested.emit()


class PreviewPanel(QWidget):
    """Display details and an embedded media player for a selected video."""

    player_message = pyqtSignal(str)
    playback_activity_changed = pyqtSignal(bool)

    def __init__(
        self,
        proxy: Optional[MediaProxyServer] = None,
        theme: Optional[Theme] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("PreviewPanel")
        self._current: Optional[VideoItem] = None
        self._thumb_worker: Optional[ThumbnailWorker] = None
        self._thumbnail_pixmap: Optional[QPixmap] = None
        self._proxy = proxy
        self._theme = theme or DARK
        self._accent = self._theme.accent
        self._active_proxy_url: Optional[str] = None
        self._pending_playable_url: Optional[str] = None
        self._pending_playable_headers: dict[str, str] = {}
        self._pending_is_hls = False
        self._pending_hls_variant_url: Optional[str] = None
        self._source_loaded = False
        self._fullscreen_window: Optional[_FullscreenWindow] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # Move this whole presentation container into fullscreen, retaining
        # the same video output and controls rather than loading another player.
        self.player_host = QWidget()
        self.player_host.setObjectName("PlayerHost")
        player_layout = QVBoxLayout(self.player_host)
        player_layout.setContentsMargins(0, 0, 0, 0)
        player_layout.setSpacing(10)
        layout.addWidget(self.player_host, stretch=1)

        # Stack the thumbnail and the video widget so they share the same
        # screen real estate. Whichever one is on top is what the user sees.
        self.media_stack_host = QWidget()
        self.media_stack_host.setObjectName("MediaStage")
        self.media_stack_host.setMinimumSize(360, 100)
        self.media_stack_host.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.media_stack = QStackedLayout(self.media_stack_host)
        self.media_stack.setStackingMode(QStackedLayout.StackingMode.StackOne)
        self.media_stack.setContentsMargins(0, 0, 0, 0)

        self.thumbnail_label = QLabel("Select a video to preview")
        self.thumbnail_label.setObjectName("ThumbHint")
        self.thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail_label.setFrameShape(QFrame.Shape.NoFrame)
        self.thumbnail_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored
        )

        self.video_widget = QVideoWidget()
        self.video_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        # Order matters: thumbnail at index 0 (default), video at index 1.
        self.media_stack.addWidget(self.thumbnail_label)
        self.media_stack.addWidget(self.video_widget)
        self.media_stack.setCurrentIndex(0)

        player_layout.addWidget(self.media_stack_host, stretch=1)
        self.media_stack_host.installEventFilter(self)
        self.thumbnail_label.installEventFilter(self)
        self.video_widget.installEventFilter(self)

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self.audio_output.setVolume(0.6)

        # A dedicated seek row gives scrubbing the width of the video stage.
        seek_controls = QHBoxLayout()
        seek_controls.setSpacing(12)
        self.position_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.position_slider.setObjectName("SeekSlider")
        self.position_slider.setAccessibleName("Playback position")
        self.position_slider.setRange(0, 0)
        self.position_slider.sliderMoved.connect(self.player.setPosition)
        seek_controls.addWidget(self.position_slider, stretch=1)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setObjectName("TimeLabel")
        seek_controls.addWidget(self.time_label)
        player_layout.addLayout(seek_controls)

        # Keep playback on the left, audio and fullscreen on the right.
        self.controls_host = QWidget()
        self.controls_host.setObjectName("PlayerControls")
        controls = QHBoxLayout(self.controls_host)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        self.play_button = QPushButton("Play")
        self._prepare_control_button(self.play_button)
        self.play_button.setMinimumWidth(80)
        self.play_button.clicked.connect(self._toggle_play)
        controls.addWidget(self.play_button)

        self.stop_button = QPushButton("Stop")
        self._prepare_control_button(self.stop_button)
        self.stop_button.clicked.connect(self._stop)
        controls.addWidget(self.stop_button)
        controls.addStretch(1)

        self.mute_button = QPushButton("Mute")
        self._prepare_control_button(self.mute_button)
        self.mute_button.setCheckable(True)
        self.mute_button.setToolTip("Mute preview audio only")
        self.mute_button.toggled.connect(self.audio_output.setMuted)
        controls.addWidget(self.mute_button)

        self.volume_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setObjectName("VolumeSlider")
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(60)
        self.volume_slider.setMinimumWidth(80)
        self.volume_slider.setMaximumWidth(100)
        self.volume_slider.setAccessibleName("Preview volume")
        self.volume_slider.setToolTip("Preview volume (does not change system volume)")
        self.volume_slider.valueChanged.connect(self._set_volume)
        controls.addWidget(self.volume_slider)
        self.volume_label = QLabel("60%")
        self.volume_label.setObjectName("TimeLabel")
        self.volume_label.setMinimumWidth(36)
        controls.addWidget(self.volume_label)

        self.fullscreen_button = QPushButton("Full screen")
        self._prepare_control_button(self.fullscreen_button)
        self.fullscreen_button.setToolTip("Full screen; press Escape to return")
        self.fullscreen_button.clicked.connect(self._toggle_fullscreen)
        controls.addWidget(self.fullscreen_button)
        player_layout.addWidget(self.controls_host)
        self.audio_output.volumeChanged.connect(self._on_volume_changed)
        self.audio_output.mutedChanged.connect(self._on_muted_changed)

        # Info block under the controls.
        self.info_host = QWidget()
        self.info_host.setObjectName("MediaInfo")
        info = QVBoxLayout(self.info_host)
        info.setSpacing(5)
        info.setContentsMargins(0, 2, 0, 0)

        self.title_label = QLabel("")
        self.title_label.setWordWrap(True)
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.title_label.setProperty("role", "title")
        self.title_label.setStyleSheet("font-size: 14px; font-weight: 600;")
        info.addWidget(self.title_label)

        self.meta_label = QLabel("")
        self.meta_label.setProperty("role", "muted")
        self.meta_label.setWordWrap(True)
        self.meta_label.setTextFormat(Qt.TextFormat.PlainText)
        self.meta_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        info.addWidget(self.meta_label)

        self.url_label = QLabel("")
        self.url_label.setObjectName("UrlLink")
        self.url_label.setOpenExternalLinks(True)
        self.url_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        self.url_label.setWordWrap(False)
        info.addWidget(self.url_label)
        layout.addWidget(self.info_host)

        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.playbackStateChanged.connect(self._on_playback_state_changed)
        self.player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.player.errorOccurred.connect(self._on_player_error)

        self._refresh_control_icons()
        self._limit_metadata_height()
        self._set_controls_enabled(False)
        self._show_thumbnail_view()

    @staticmethod
    def _prepare_control_button(button: QPushButton) -> None:
        button.setObjectName("PlayerControl")
        button.setProperty("role", "player")
        button.setFixedHeight(34)
        button.setIconSize(QSize(18, 18))
        button.setCursor(Qt.CursorShape.PointingHandCursor)

    def _refresh_control_icons(self) -> None:
        color = self._theme.text
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setIcon(make_icon("pause" if playing else "play", color))
        self.stop_button.setIcon(make_icon("stop", color))
        self.mute_button.setIcon(make_icon(
            "volume-muted" if self.audio_output.isMuted() else "volume", color
        ))
        self.fullscreen_button.setIcon(make_icon(
            "exit-fullscreen" if self.is_fullscreen() else "fullscreen", color
        ))

    def show_video(self, video: Optional[VideoItem]) -> None:
        self._stop()
        self.player.setSource(QUrl())
        self._release_proxy_url()
        self._stop_thumb_worker()
        self._thumbnail_pixmap = None
        self._current = video
        self._pending_playable_url = None
        self._pending_playable_headers = {}
        self._pending_is_hls = False
        self._pending_hls_variant_url = None
        self._source_loaded = False
        self._show_thumbnail_view()

        if video is None:
            self.title_label.setText("")
            self.title_label.setToolTip("")
            self.meta_label.setText("")
            self.meta_label.setToolTip("")
            self.url_label.setText("")
            self.url_label.setToolTip("")
            self.thumbnail_label.setPixmap(QPixmap())
            self.thumbnail_label.setText("Select a video to preview")
            self._set_controls_enabled(False)
            self.player.setSource(QUrl())
            return

        self.title_label.setText(video.title)
        self.title_label.setToolTip(video.title)
        meta_parts = []
        if video.duration is not None:
            meta_parts.append(format_duration(video.duration))
        if video.width and video.height:
            meta_parts.append(video.resolution)
        if video.ext:
            meta_parts.append(video.ext.upper())
        if video.uploader:
            meta_parts.append(video.uploader)
        if video.is_hls:
            meta_parts.append("HLS stream")
        elif video.is_direct:
            meta_parts.append("Direct link")
        else:
            meta_parts.append("yt-dlp extractor")
        self.meta_label.setText("  ·  ".join(meta_parts))
        self.meta_label.setToolTip(self.meta_label.text())

        self._render_url_label(video)

        self.thumbnail_label.setPixmap(QPixmap())
        self.thumbnail_label.setText("Loading thumbnail...")

        if video.thumbnail:
            self._thumb_worker = ThumbnailWorker(
                0,
                video.thumbnail,
                referer=video.referer,
                cookies=getattr(video, "x_cookiejar", None),
                parent=self,
            )
            self._thumb_worker.ready.connect(self._apply_thumbnail)
            self._thumb_worker.failed.connect(self._on_thumb_failed)
            self._thumb_worker.start()
        else:
            self.thumbnail_label.setText("(No thumbnail available)")

        selected = None
        if self._is_x_video(video):
            selected = self._best_x_preview_format(video)
            playable_url = selected.get("url") if selected else None
        elif video.is_direct:
            playable_url = video.url
            self._pending_is_hls = video.is_hls
        else:
            selected = self._best_preview_format(video)
            playable_url = selected.get("url") if selected else None
        if selected:
            raw_headers = (video.raw or {}).get("http_headers") or {}
            format_headers = selected.get("http_headers") or {}
            if isinstance(raw_headers, dict):
                self._pending_playable_headers.update(raw_headers)
            if isinstance(format_headers, dict):
                self._pending_playable_headers.update(format_headers)
            self._pending_is_hls = self._is_hls_format(selected)
            self._pending_hls_variant_url = selected.get("_preview_hls_variant_url")
        if playable_url:
            self._pending_playable_url = playable_url
            self._set_controls_enabled(True)
        else:
            self._set_controls_enabled(False)

    def release_stream(self) -> None:
        """Stop playback and release any local proxy stream."""
        self.player.stop()
        self.player.setSource(QUrl())
        self._release_proxy_url()
        self._source_loaded = False
        self._show_thumbnail_view()

    def is_playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def shutdown(self) -> None:
        self._exit_fullscreen()
        self.release_stream()
        self._stop_thumb_worker()

    def is_fullscreen(self) -> bool:
        return self._fullscreen_window is not None

    def activation_window(self) -> QWidget:
        """Return the visible player window when activating an existing app."""
        return self._fullscreen_window or self.window()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self._exit_fullscreen()
        super().closeEvent(event)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt API)
        if watched is self.media_stack_host and event.type() == QEvent.Type.Resize:
            self._resize_thumbnail()
        if (
            watched in (self.media_stack_host, self.thumbnail_label, self.video_widget)
            and event.type() == QEvent.Type.MouseButtonDblClick
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._toggle_fullscreen()
            return True
        return super().eventFilter(watched, event)

    def _toggle_fullscreen(self) -> None:
        if self.is_fullscreen():
            self._exit_fullscreen()
            return
        window = _FullscreenWindow(self, Qt.WindowType.Window)
        window.setWindowTitle("Video preview — Escape to return")
        window.setWindowIcon(self.window().windowIcon())
        window.exit_requested.connect(self._exit_fullscreen)
        window_layout = QVBoxLayout(window)
        window_layout.setContentsMargins(12, 12, 12, 12)
        window_layout.addWidget(self.player_host)
        shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), window)
        shortcut.activated.connect(self._exit_fullscreen)
        # Keep an explicit reference for the lifetime of the fullscreen host.
        window.escape_shortcut = shortcut
        self._fullscreen_window = window
        self.fullscreen_button.setText("Exit full screen")
        self._refresh_control_icons()
        screen = self.window().screen()
        if screen is not None:
            window.move(screen.availableGeometry().topLeft())
        window.showFullScreen()
        self.player_host.show()
        self.fullscreen_button.setFocus()

    def _exit_fullscreen(self) -> None:
        window = self._fullscreen_window
        if window is None:
            return
        self._fullscreen_window = None
        window.hide()
        window.layout().removeWidget(self.player_host)
        self.layout().insertWidget(0, self.player_host, stretch=1)
        self.player_host.show()
        self.fullscreen_button.setText("Full screen")
        self._refresh_control_icons()
        self._focus_player_control()
        window.deleteLater()

    def _focus_player_control(self) -> None:
        self.fullscreen_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _set_volume(self, value: int) -> None:
        self.audio_output.setVolume(value / 100)
        if value > 0 and self.audio_output.isMuted():
            self.audio_output.setMuted(False)

    def _on_volume_changed(self, volume: float) -> None:
        value = round(volume * 100)
        was_blocked = self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(value)
        self.volume_slider.blockSignals(was_blocked)
        self.volume_label.setText(f"{value}%")

    def _on_muted_changed(self, muted: bool) -> None:
        was_blocked = self.mute_button.blockSignals(True)
        self.mute_button.setChecked(muted)
        self.mute_button.blockSignals(was_blocked)
        self.mute_button.setText("Unmute" if muted else "Mute")
        self._refresh_control_icons()

    def apply_theme(self, theme: Theme) -> None:
        """Retint icons and the source link alongside the global stylesheet."""
        self._theme = theme
        self._accent = theme.accent
        self._refresh_control_icons()
        self._limit_metadata_height()
        if self._current is not None:
            self._render_url_label(self._current)

    def _limit_metadata_height(self) -> None:
        """Bound long captions while keeping their full contents in tooltips."""
        for label in (self.title_label, self.meta_label):
            label.ensurePolished()
            label.setMaximumHeight(label.fontMetrics().lineSpacing() * 2 + 2)

    def _render_url_label(self, video: VideoItem) -> None:
        host = urlparse(video.url).netloc or video.url
        self.url_label.setText(
            f'<a href="{video.url}" style="color: {self._accent};'
            ' text-decoration: none;">'
            f"{host}  ↗</a>"
        )
        self.url_label.setToolTip(video.url)

    @staticmethod
    def _is_x_video(video: VideoItem) -> bool:
        host = (urlparse(video.source_url).hostname or "").lower()
        return host in {"x.com", "twitter.com"} or host.endswith(
            (".x.com", ".twitter.com")
        )

    @staticmethod
    def _is_hls_format(fmt: dict) -> bool:
        protocol = str(fmt.get("protocol") or "").lower()
        ext = str(fmt.get("ext") or "").lower()
        path = urlparse(str(fmt.get("url") or "")).path.lower()
        return "m3u8" in protocol or ext == "m3u8" or path.endswith(".m3u8")

    @classmethod
    def _best_x_preview_format(cls, video: VideoItem) -> Optional[dict]:
        """Prefer a combined MP4; use HLS if X exposes no suitable MP4."""
        options = []
        for fmt in video.formats or []:
            url = fmt.get("url")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                continue
            vcodec = str(fmt.get("vcodec") or "").lower()
            acodec = str(fmt.get("acodec") or "").lower()
            if vcodec == "none":
                continue
            is_hls = cls._is_hls_format(fmt)
            ext = str(fmt.get("ext") or "").lower()
            is_mp4 = ext == "mp4" or urlparse(url).path.lower().endswith(".mp4")
            if not is_hls and not is_mp4:
                continue
            # Explicit audio is preferable to an unknown codec; a silent
            # MP4 remains usable when X supplies no format with audio.
            audio = 2 if acodec and acodec != "none" else (1 if not acodec else 0)
            if not is_hls and audio > 0:
                category = 3
            elif is_hls:
                category = 2
            else:
                category = 1
            try:
                height = int(fmt.get("height") or 0)
            except (TypeError, ValueError):
                height = 0
            options.append(((category, audio, height), fmt))
        return max(options, key=lambda option: option[0])[1] if options else None

    def _wrap_with_proxy(self, playable_url: str, video: VideoItem) -> Optional[str]:
        """Route URLs that need a Referer through the local proxy."""
        is_x_video = self._is_x_video(video)
        if self._proxy is None:
            if is_x_video:
                self.player_message.emit("X preview unavailable: media proxy is not running.")
                return None
            if video.hls_png_wrapped:
                self.player_message.emit("Preview unavailable: media proxy is not running.")
                return None
            return playable_url
        if not is_x_video and not (
            video.referer or self._pending_playable_headers or self._pending_is_hls
        ):
            return playable_url
        try:
            if is_x_video:
                local = self._proxy.register(
                    playable_url,
                    referer=video.referer,
                    extra_headers=self._pending_playable_headers,
                    cookiejar=getattr(video, "x_cookiejar", None),
                    is_hls=self._pending_is_hls,
                )
            else:
                variant_options = {}
                if self._pending_hls_variant_url:
                    variant_options["hls_variant_url"] = self._pending_hls_variant_url
                if video.hls_png_wrapped:
                    variant_options["hls_png_wrapped"] = True
                local = self._proxy.register(
                    playable_url,
                    referer=video.referer,
                    extra_headers=self._pending_playable_headers,
                    is_hls=self._pending_is_hls or video.is_hls,
                    **variant_options,
                )
        except Exception as exc:  # noqa: BLE001
            if is_x_video:
                self.player_message.emit(f"X preview setup failed: {exc}")
                return None
            if video.hls_png_wrapped:
                self.player_message.emit(f"Preview setup failed: {exc}")
                return None
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
        """Return the chosen preview URL, retaining direct-link behavior."""
        if video.is_direct:
            return video.url
        selected = self._best_preview_format(video)
        return selected.get("url") if selected else None

    @staticmethod
    def _is_youtube_video(video: VideoItem) -> bool:
        if str((video.raw or {}).get("extractor_key") or "").lower() == "youtube":
            return True
        for url in (video.source_url, video.referer, video.url):
            host = (urlparse(url or "").hostname or "").lower()
            if host in {"youtu.be", "youtube.com", "youtube-nocookie.com"} or host.endswith(
                (".youtube.com", ".youtube-nocookie.com")
            ):
                return True
        return False

    @classmethod
    def _best_preview_format(cls, video: VideoItem) -> Optional[dict]:
        """Choose a video-bearing format, favoring audio and common codecs."""
        formats = [fmt for fmt in video.formats or [] if isinstance(fmt, dict)]
        audio_by_manifest = {}
        if cls._is_youtube_video(video):
            for fmt in formats:
                manifest = fmt.get("manifest_url")
                if (
                    isinstance(manifest, str)
                    and manifest.startswith(("https://", "http://"))
                    and cls._is_hls_format(fmt)
                    and str(fmt.get("vcodec") or "").lower() == "none"
                    and str(fmt.get("acodec") or "").lower() != "none"
                    and not fmt.get("has_drm")
                ):
                    audio_by_manifest[manifest] = fmt

        candidates = []
        for fmt in formats:
            url = fmt.get("url")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                continue
            ext = str(fmt.get("ext") or "").lower()
            protocol = str(fmt.get("protocol") or "").lower()
            vcodec = str(fmt.get("vcodec") or "").lower()
            acodec = str(fmt.get("acodec") or "").lower()
            if "dash" in protocol or ext == "mpd" or fmt.get("has_drm"):
                continue
            if vcodec == "none":
                continue
            is_hls = cls._is_hls_format(fmt)
            if not is_hls and ext not in {"mp4", "webm", "mov", ""}:
                continue
            selected = fmt
            audio = audio_by_manifest.get(fmt.get("manifest_url"))
            if is_hls and acodec == "none" and audio is not None:
                # YouTube exposes the two tracks separately. Its shared HLS
                # master retains the audio-group relation that a video-only
                # playlist loses; Qt can play that relation through the proxy.
                selected = dict(
                    fmt, url=fmt["manifest_url"], acodec=audio.get("acodec"),
                    _preview_hls_variant_url=url,
                )
                acodec = str(selected.get("acodec") or "").lower()
            try:
                height = int(fmt.get("height") or 0)
            except (TypeError, ValueError):
                height = 0
            # AV1 may be offered as MP4 even when Qt has no usable decoder.
            # Prefer AVC without changing the user's download-format choice.
            codec = 2 if vcodec.startswith(("avc1", "h264")) else (
                0 if vcodec.startswith(("av01", "av1")) else 1
            )
            score = (
                int(acodec != "none"), codec, int(360 <= height <= 720),
                int(not is_hls), int(ext == "mp4"), height,
            )
            candidates.append((score, selected))
        return max(candidates, key=lambda option: option[0])[1] if candidates else None

    def _apply_thumbnail(self, _index: int, data: bytes) -> None:
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self._thumbnail_pixmap = None
            self.thumbnail_label.setText("(Failed to load thumbnail)")
            return
        self._thumbnail_pixmap = pixmap
        self._resize_thumbnail()

    def _resize_thumbnail(self) -> None:
        if self._thumbnail_pixmap is None:
            return
        pixmap = self._thumbnail_pixmap.scaled(
            max(1, self.media_stack_host.width() - 8),
            max(1, self.media_stack_host.height() - 8),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.thumbnail_label.setText("")
        self.thumbnail_label.setPixmap(pixmap)

    def _on_thumb_failed(self, _index: int, message: str) -> None:
        self._thumbnail_pixmap = None
        self.thumbnail_label.setText(f"(Thumbnail error: {message})")

    def _stop_thumb_worker(self) -> None:
        worker = self._thumb_worker
        self._thumb_worker = None
        if worker is None or not worker.isRunning():
            return
        worker.requestInterruption()
        worker.quit()
        if not worker.wait(1000):
            worker.terminate()
            worker.wait(1000)

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
            if not self._ensure_source_loaded():
                return
            # Make the video widget visible BEFORE calling play() - Qt's
            # FFmpeg media backend will not render frames into a hidden
            # widget, which previously left playback stuck in the
            # buffering state with the thumbnail still on top.
            self._show_video_view()
            self.player.play()

    def _stop(self) -> None:
        self.player.stop()

    def _ensure_source_loaded(self) -> bool:
        if self._source_loaded:
            return True
        if self._current is None or not self._pending_playable_url:
            return False
        wrapped = self._wrap_with_proxy(self._pending_playable_url, self._current)
        if not wrapped:
            return False
        self.player.setSource(QUrl(wrapped))
        self._source_loaded = True
        return True

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
            self.play_button.setText("Pause")
            self._show_video_view()
            self.playback_activity_changed.emit(True)
        elif state == QMediaPlayer.PlaybackState.PausedState:
            self.play_button.setText("Play")
            self.playback_activity_changed.emit(False)
            # Keep the video frame visible while paused.
        else:
            # Stopped - back to thumbnail.
            self.play_button.setText("Play")
            self._show_thumbnail_view()
            self.playback_activity_changed.emit(False)
        self._refresh_control_icons()

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
        # Reparenting the native video surface can leave keyboard focus in its
        # internal window container. Move it to an existing control before
        # changing pages: QStackedLayout would otherwise focus the incoming
        # QLabel and lazily construct a text control during player teardown.
        focused = self.player_host.window().focusWidget()
        if focused is self.video_widget or (
            focused is not None and self.video_widget.isAncestorOf(focused)
        ):
            self._focus_player_control()
        self.media_stack.setCurrentIndex(0)

    def _show_video_view(self) -> None:
        self.media_stack.setCurrentIndex(1)

    def _on_player_error(self, error, error_string: str = "") -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        message = error_string or str(error)
        if self._current is not None and self._is_x_video(self._current):
            message += " X media links can expire; paste the post link again to refresh it."
            self._thumbnail_pixmap = None
            self.thumbnail_label.setPixmap(QPixmap())
            self.thumbnail_label.setText("X preview failed. Re-scan the post and try again.")
        self.player_message.emit(f"Player error: {message}")
        # Reset back to thumbnail so the user understands playback failed.
        self._show_thumbnail_view()
