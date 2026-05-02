"""Main application window assembling URL bar, list, preview and downloads."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction, QIcon, QKeySequence
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QStyle,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..downloader import QUALITY_FORMATS, DownloadProgress
from ..media_proxy import MediaProxyServer
from ..scraper import VideoItem, build_items_from_urls
from ..settings import SettingsStore
from ..utils import format_bytes, is_valid_url
from .preview_panel import PreviewPanel
from .settings_dialog import SettingsDialog
from .video_list import VideoListWidget
from .workers import DownloadWorker, ScrapeWorker

try:
    from .js_renderer import JSPageScraper, WEBENGINE_AVAILABLE
except Exception:  # noqa: BLE001
    JSPageScraper = None  # type: ignore[assignment]
    WEBENGINE_AVAILABLE = False

try:
    from .interactive_scraper import InteractiveScrapeDialog
except Exception:  # noqa: BLE001
    InteractiveScrapeDialog = None  # type: ignore[assignment]


DEFAULT_DOWNLOAD_DIR = Path.cwd() / "downloads"


def _make_card(parent: Optional[QWidget] = None) -> QFrame:
    """Container that picks up the QFrame#Card stylesheet rule."""
    card = QFrame(parent)
    card.setObjectName("Card")
    card.setFrameShape(QFrame.Shape.NoFrame)
    return card


class MainWindow(QMainWindow):
    """Top-level window that wires the GUI together."""

    # Cross-thread bridge for status messages emitted by the media proxy
    # (its prefetch threads must not touch QWidgets directly).
    proxy_status = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Video Scraper")
        self.resize(1320, 820)
        self.setMinimumSize(1024, 640)
        self.setUnifiedTitleAndToolBarOnMac(True)

        self.scrape_worker: Optional[ScrapeWorker] = None
        self.download_worker: Optional[DownloadWorker] = None
        self.js_scraper: Optional[JSPageScraper] = None
        self.interactive_dialog: Optional["InteractiveScrapeDialog"] = None
        self._current_url: str = ""
        self._logs_expanded: bool = False
        self._total_count: int = 0
        self._selected_count: int = 0
        self.output_dir: Path = DEFAULT_DOWNLOAD_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.settings_store = SettingsStore()
        self.proxy_status.connect(self.log)
        self.media_proxy = MediaProxyServer(
            settings_store=self.settings_store,
            on_status=self.proxy_status.emit,
        ).start()

        self._build_ui()
        self._build_menu()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(12)

        root.addWidget(self._build_url_card())
        root.addWidget(self._build_splitter(), stretch=1)
        root.addWidget(self._build_download_card())
        root.addWidget(self._build_progress_row())
        root.addWidget(self._build_logs_section())

        self.setStatusBar(QStatusBar(self))
        self.statusBar().showMessage("Ready")

    def _build_url_card(self) -> QWidget:
        card = _make_card()
        layout = QHBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        icon_label = QLabel()
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogContentsView)
        icon_label.setPixmap(icon.pixmap(18, 18))
        layout.addWidget(icon_label)

        self.url_input = QLineEdit()
        self.url_input.setObjectName("UrlInput")
        self.url_input.setPlaceholderText(
            "Paste a page URL (YouTube, Bilibili, Twitter, generic webpage...)"
        )
        self.url_input.setClearButtonEnabled(True)
        self.url_input.returnPressed.connect(self.start_scrape)
        layout.addWidget(self.url_input, stretch=1)

        self.scrape_button = QPushButton("Scrape")
        self.scrape_button.setObjectName("Primary")
        self.scrape_button.setMinimumWidth(110)
        self.scrape_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scrape_button.clicked.connect(self.start_scrape)
        layout.addWidget(self.scrape_button)
        return card

    def _build_splitter(self) -> QWidget:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)

        # --- Left: discovered videos card ---
        left_card = _make_card()
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(12, 12, 12, 12)
        left_layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("Discovered videos")
        title.setProperty("role", "title")
        header.addWidget(title)

        self.count_badge = QLabel("0")
        self.count_badge.setProperty("role", "badge")
        self.count_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.count_badge)
        header.addStretch()

        self.select_all_button = QPushButton("Select all")
        self.select_all_button.setObjectName("Link")
        self.select_all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_all_button.clicked.connect(
            lambda: self.video_list.set_all_checked(True)
        )
        header.addWidget(self.select_all_button)

        self.deselect_all_button = QPushButton("Clear")
        self.deselect_all_button.setObjectName("Link")
        self.deselect_all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.deselect_all_button.clicked.connect(
            lambda: self.video_list.set_all_checked(False)
        )
        header.addWidget(self.deselect_all_button)
        left_layout.addLayout(header)

        self.video_list = VideoListWidget()
        self.video_list.selection_changed.connect(self._on_video_selected)
        self.video_list.selection_count_changed.connect(
            self._on_selection_count_changed
        )
        left_layout.addWidget(self.video_list, stretch=1)

        # --- Right: preview card ---
        right_card = _make_card()
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(12, 12, 12, 12)
        right_layout.setSpacing(8)
        self.preview_panel = PreviewPanel(proxy=self.media_proxy)
        self.preview_panel.player_message.connect(self.log)
        right_layout.addWidget(self.preview_panel)

        splitter.addWidget(left_card)
        splitter.addWidget(right_card)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([480, 820])
        return splitter

    def _build_download_card(self) -> QWidget:
        card = _make_card()
        layout = QHBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        quality_label = QLabel("Quality")
        quality_label.setProperty("role", "section")
        layout.addWidget(quality_label)

        self.quality_combo = QComboBox()
        for label in QUALITY_FORMATS.keys():
            self.quality_combo.addItem(label)
        self.quality_combo.setCurrentText("best")
        self.quality_combo.setMinimumWidth(120)
        layout.addWidget(self.quality_combo)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: #262b36;")
        sep.setFixedHeight(24)
        layout.addWidget(sep)

        save_label = QLabel("Save to")
        save_label.setProperty("role", "section")
        layout.addWidget(save_label)

        self.path_label = QLabel(self._truncate_path(self.output_dir))
        self.path_label.setProperty("role", "path")
        self.path_label.setToolTip(str(self.output_dir))
        self.path_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        layout.addWidget(self.path_label, stretch=1)

        self.choose_dir_button = QPushButton("Choose…")
        self.choose_dir_button.setObjectName("Ghost")
        self.choose_dir_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.choose_dir_button.clicked.connect(self.choose_output_dir)
        layout.addWidget(self.choose_dir_button)

        self.download_button = QPushButton("Download selected")
        self.download_button.setObjectName("Primary")
        self.download_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download_button.setMinimumWidth(170)
        self.download_button.clicked.connect(self.start_download)
        layout.addWidget(self.download_button)
        return card

    def _build_progress_row(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(0)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Idle")
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFixedHeight(14)
        layout.addWidget(self.progress_bar)
        return wrap

    def _build_logs_section(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.logs_toggle_button = QPushButton("Logs ▾")
        self.logs_toggle_button.setObjectName("Link")
        self.logs_toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.logs_toggle_button.clicked.connect(self._toggle_logs)
        header.addWidget(self.logs_toggle_button)
        header.addStretch()
        self.clear_logs_button = QPushButton("Clear")
        self.clear_logs_button.setObjectName("Link")
        self.clear_logs_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_logs_button.clicked.connect(lambda: self.log_view.clear())
        header.addWidget(self.clear_logs_button)
        layout.addLayout(header)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("Logs")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setPlaceholderText("Logs will appear here...")
        self.log_view.setFixedHeight(140)
        self.log_view.setVisible(False)
        layout.addWidget(self.log_view)
        return wrap

    def _build_menu(self) -> None:
        toolbar = QToolBar("Main toolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(toolbar.iconSize() * 0.9)
        self.addToolBar(toolbar)

        open_dir_action = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon),
            "Open downloads folder",
            self,
        )
        open_dir_action.triggered.connect(self._open_output_folder)
        toolbar.addAction(open_dir_action)

        if InteractiveScrapeDialog is not None:
            interactive_action = QAction(
                self.style().standardIcon(
                    QStyle.StandardPixmap.SP_BrowserReload
                ),
                "Scrape with browser (solve CAPTCHA / login)",
                self,
            )
            interactive_action.triggered.connect(
                self._start_interactive_from_toolbar
            )
            toolbar.addAction(interactive_action)

        prefs_action = QAction(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_FileDialogDetailedView
            ),
            "Preferences",
            self,
        )
        prefs_action.setShortcut(QKeySequence("Ctrl+,"))
        prefs_action.triggered.connect(self._open_preferences)
        toolbar.addAction(prefs_action)

        about_action = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation),
            "About",
            self,
        )
        about_action.setShortcut(QKeySequence.StandardKey.HelpContents)
        about_action.triggered.connect(self._show_about)
        toolbar.addAction(about_action)

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _truncate_path(path: Path, max_chars: int = 60) -> str:
        text = str(path)
        if len(text) <= max_chars:
            return text
        head = text[: max_chars // 2 - 2]
        tail = text[-(max_chars // 2 - 1) :]
        return f"{head}…{tail}"

    def _toggle_logs(self) -> None:
        self._logs_expanded = not self._logs_expanded
        self.log_view.setVisible(self._logs_expanded)
        arrow = "▴" if self._logs_expanded else "▾"
        self.logs_toggle_button.setText(f"Logs {arrow}")

    def _update_video_count_badge(self, count: int) -> None:
        self._total_count = count
        self._refresh_badge()

    def _on_selection_count_changed(self, selected: int) -> None:
        self._selected_count = selected
        self._refresh_badge()

    def _refresh_badge(self) -> None:
        total = getattr(self, "_total_count", 0)
        selected = getattr(self, "_selected_count", 0)
        if total == 0:
            self.count_badge.setText("0")
        elif selected == 0:
            self.count_badge.setText(str(total))
        else:
            self.count_badge.setText(f"{selected} / {total}")

    def log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    # ------------------------------------------------------------- scraping

    def start_scrape(self) -> None:
        url = self.url_input.text().strip()
        if not is_valid_url(url):
            QMessageBox.warning(
                self,
                "Invalid URL",
                "Please enter a valid http(s) URL before scraping.",
            )
            return
        if self.scrape_worker and self.scrape_worker.isRunning():
            return

        self.log_view.clear()
        self.video_list.set_videos([])
        self._update_video_count_badge(0)
        self.preview_panel.show_video(None)
        self.scrape_button.setEnabled(False)
        self.scrape_button.setText("Scraping…")
        self.statusBar().showMessage("Scraping...")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("Scraping...")
        self._current_url = url

        self.scrape_worker = ScrapeWorker(url, self)
        self.scrape_worker.log.connect(self.log)
        self.scrape_worker.finished_with_results.connect(self._on_scrape_done)
        self.scrape_worker.failed.connect(self._on_scrape_failed)
        self.scrape_worker.start()

    def _reset_scrape_button(self) -> None:
        self.scrape_button.setEnabled(True)
        self.scrape_button.setText("Scrape")

    def _on_scrape_done(self, videos: List[VideoItem]) -> None:
        self.video_list.set_videos(videos)
        self._update_video_count_badge(len(videos))
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        if videos:
            self.progress_bar.setFormat(f"Found {len(videos)} video(s)")
            self.statusBar().showMessage(f"Found {len(videos)} video(s)")
            self._reset_scrape_button()
            return

        if WEBENGINE_AVAILABLE and self._current_url:
            self.log("Static analysis found nothing - escalating to WebEngine render...")
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat("Rendering page in headless browser...")
            self.statusBar().showMessage("Rendering JS...")
            self._start_js_render(self._current_url)
            return

        self._reset_scrape_button()
        self.progress_bar.setFormat("No videos found")
        self.statusBar().showMessage("No videos found")
        if self._offer_interactive("Couldn't detect any videos on that page."):
            return
        QMessageBox.information(
            self,
            "No videos",
            "Couldn't detect any videos on that page.\n\n"
            "Tip: install PyQt6-WebEngine to enable the headless-browser fallback.",
        )

    def _start_js_render(self, url: str) -> None:
        if not WEBENGINE_AVAILABLE or JSPageScraper is None:
            return
        try:
            self.js_scraper = JSPageScraper(parent=self)
        except Exception as exc:  # noqa: BLE001
            self.log(f"WebEngine init failed: {exc}")
            self._on_js_render_failed(str(exc))
            return
        self.js_scraper.log.connect(self.log)
        self.js_scraper.finished.connect(self._on_js_render_done)
        self.js_scraper.failed.connect(self._on_js_render_failed)
        self.js_scraper.scrape(url)

    def _on_js_render_done(
        self,
        videos: list,
        iframes: list,
        page_title: str,
    ) -> None:
        items = build_items_from_urls(
            list(videos), list(iframes), self._current_url, page_title
        )
        self.video_list.set_videos(items)
        self._update_video_count_badge(len(items))
        self._reset_scrape_button()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        if items:
            self.progress_bar.setFormat(f"Found {len(items)} video(s) via WebEngine")
            self.statusBar().showMessage(f"Found {len(items)} video(s)")
        else:
            self.progress_bar.setFormat("No videos found")
            self.statusBar().showMessage("No videos found")
            if self._offer_interactive(
                "Couldn't detect any videos on that page, even after JS rendering."
            ):
                return
            QMessageBox.information(
                self,
                "No videos",
                "Couldn't detect any videos on that page, even after JS rendering.",
            )

    def _on_js_render_failed(self, message: str) -> None:
        self._reset_scrape_button()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("WebEngine failed")
        self.statusBar().showMessage("WebEngine failed")
        self.log(f"WebEngine failed: {message}")
        if self._offer_interactive(f"WebEngine fallback failed: {message}"):
            return
        QMessageBox.warning(self, "WebEngine fallback failed", message)

    # ------------------------------------------------------ interactive mode

    def _offer_interactive(self, reason: str) -> bool:
        """Prompt the user to open the interactive browser dialog.

        Returns True if the dialog was opened (caller should skip any
        further "no videos" message boxes).
        """
        if InteractiveScrapeDialog is None or not self._current_url:
            return False
        answer = QMessageBox.question(
            self,
            "Open interactive browser?",
            (
                f"{reason}\n\n"
                "Many sites hide their video behind a CAPTCHA or login. "
                "Open an interactive browser window so you can clear the "
                "challenge manually? Cookies are saved so you won't have "
                "to do it again next time."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._open_interactive_scraper(self._current_url)
        return True

    def _start_interactive_from_toolbar(self) -> None:
        url = self.url_input.text().strip()
        if not is_valid_url(url):
            QMessageBox.warning(
                self,
                "Invalid URL",
                "Please enter a valid http(s) URL first.",
            )
            return
        self._current_url = url
        self._open_interactive_scraper(url)

    def _open_interactive_scraper(self, url: str) -> None:
        if InteractiveScrapeDialog is None:
            QMessageBox.warning(
                self,
                "WebEngine unavailable",
                "PyQt6-WebEngine is not installed, so the interactive "
                "browser fallback can't run.",
            )
            return
        if self.interactive_dialog is not None:
            self.interactive_dialog.raise_()
            self.interactive_dialog.activateWindow()
            return
        try:
            dialog = InteractiveScrapeDialog(url, self)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Interactive browser failed to start: {exc}")
            QMessageBox.critical(
                self, "Interactive browser failed", str(exc)
            )
            return
        self.interactive_dialog = dialog
        dialog.log.connect(self.log)
        dialog.finished_with_results.connect(self._on_js_render_done)
        dialog.failed.connect(self._on_js_render_failed)
        dialog.finished.connect(self._on_interactive_closed)
        self.statusBar().showMessage("Solve verification in the browser window…")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("Waiting for interactive browser…")
        dialog.show()

    def _on_interactive_closed(self, _result: int) -> None:
        self.interactive_dialog = None
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("Idle")
        self._reset_scrape_button()

    def _on_scrape_failed(self, message: str) -> None:
        self._reset_scrape_button()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Scrape failed")
        self.statusBar().showMessage("Scrape failed")
        QMessageBox.critical(self, "Scrape failed", message)

    def _on_video_selected(self, video: Optional[VideoItem]) -> None:
        self.preview_panel.show_video(video)

    # ------------------------------------------------------------- download

    def choose_output_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Choose download folder", str(self.output_dir)
        )
        if directory:
            self.output_dir = Path(directory)
            self.path_label.setText(self._truncate_path(self.output_dir))
            self.path_label.setToolTip(str(self.output_dir))

    def start_download(self) -> None:
        items = self.video_list.checked_videos()
        if not items:
            QMessageBox.information(
                self,
                "Nothing selected",
                "Click a video in the list to select it. "
                "Hold ⌘ or Shift to pick multiple.",
            )
            return
        if self.download_worker and self.download_worker.isRunning():
            return

        self.output_dir.mkdir(parents=True, exist_ok=True)
        quality = self.quality_combo.currentText()
        self.download_button.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Starting download...")
        self.statusBar().showMessage("Downloading...")
        # Stop any background prefetch so it doesn't fight the download
        # for upstream bandwidth.
        try:
            self.media_proxy.set_download_active(True)
        except Exception:  # noqa: BLE001
            pass
        self.log(f"Downloading {len(items)} item(s) to {self.output_dir}")

        self.download_worker = DownloadWorker(
            items, self.output_dir, quality=quality, parent=self
        )
        self.download_worker.item_started.connect(self._on_dl_item_started)
        self.download_worker.item_progress.connect(self._on_dl_item_progress)
        self.download_worker.item_finished.connect(self._on_dl_item_finished)
        self.download_worker.item_failed.connect(self._on_dl_item_failed)
        self.download_worker.all_done.connect(self._on_dl_all_done)
        self.download_worker.start()

    def _on_dl_item_started(self, index: int, item: VideoItem) -> None:
        self.log(f"[{index + 1}] Starting: {item.title}")
        self.progress_bar.setFormat(
            f"[{index + 1}] {item.title[:60]}... starting"
        )
        self.progress_bar.setValue(0)

    def _on_dl_item_progress(
        self, index: int, progress: DownloadProgress
    ) -> None:
        if progress.status == "downloading" and progress.total_bytes > 0:
            pct = int(progress.downloaded_bytes * 100 / progress.total_bytes)
            self.progress_bar.setValue(pct)
            speed = format_bytes(progress.speed) + "/s" if progress.speed else "--"
            eta = f"{progress.eta}s" if progress.eta is not None else "--"
            self.progress_bar.setFormat(
                f"[{index + 1}] {pct}%  {speed}  ETA {eta}"
            )
        elif progress.status == "finished":
            self.progress_bar.setValue(100)
            self.progress_bar.setFormat(f"[{index + 1}] Post-processing...")

    def _on_dl_item_finished(self, index: int, path: str) -> None:
        self.log(f"[{index + 1}] Done -> {path}")

    def _on_dl_item_failed(self, index: int, message: str) -> None:
        self.log(f"[{index + 1}] FAILED: {message}")

    def _on_dl_all_done(self) -> None:
        self.download_button.setEnabled(True)
        self.progress_bar.setFormat("All downloads finished")
        self.progress_bar.setValue(100)
        self.statusBar().showMessage("All downloads finished")
        try:
            self.media_proxy.set_download_active(False)
        except Exception:  # noqa: BLE001
            pass
        QMessageBox.information(
            self,
            "Downloads complete",
            f"Files saved to {self.output_dir}",
        )

    def _open_output_folder(self) -> None:
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl

        self.output_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_dir)))

    def _show_about(self) -> None:
        QMessageBox.information(
            self,
            "About Video Scraper",
            "Video Scraper\n\n"
            "Scrape videos from any webpage, preview, and download.\n"
            "Built with PyQt6 + yt-dlp.",
        )

    def _open_preferences(self) -> None:
        dialog = SettingsDialog(self.settings_store, self)
        dialog.exec()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        for worker in (self.scrape_worker, self.download_worker):
            if worker and worker.isRunning():
                worker.requestInterruption()
                worker.quit()
                worker.wait(200)
        try:
            self.media_proxy.stop()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)
