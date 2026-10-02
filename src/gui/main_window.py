"""Main application window assembling URL bar, list, preview and downloads."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import List, Optional

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QIcon, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
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

from ..downloader import DownloadLaneLimiter, QUALITY_FORMATS, DownloadProgress
from ..media_proxy import MediaProxyServer
from ..scraper import VideoItem, build_items_from_urls, is_x_post_url
from ..settings import SettingsStore
from ..utils import format_bytes, is_valid_url
from .preview_panel import PreviewPanel
from .settings_dialog import SettingsDialog
from .style import build_palette, build_stylesheet, get_theme
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


DEFAULT_DOWNLOAD_DIR = (
    Path.home() / "Downloads" / "Video Scraper"
    if getattr(sys, "frozen", False)
    else Path.cwd() / "downloads"
)
MAX_ACTIVE_DOWNLOADS = 2
DOWNLOAD_LANE_IDLE_BASE = 8
DOWNLOAD_LANE_IDLE_MAX = 32
DOWNLOAD_LANE_STEP = 8
DOWNLOAD_LANE_INCREASE_SEGMENTS = 80


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
        self.download_workers: dict[int, DownloadWorker] = {}
        self._download_queue: List[int] = []
        self._download_tasks: dict[int, dict] = {}
        self._next_download_id: int = 1
        self._lane_limiter = DownloadLaneLimiter(limit=DOWNLOAD_LANE_IDLE_BASE)
        self._download_lane_limit: int = DOWNLOAD_LANE_IDLE_BASE
        self._download_idle_lane_limit: int = DOWNLOAD_LANE_IDLE_BASE
        self._download_segment_successes: int = 0
        self._download_task_slots: dict[int, int] = {}
        self._download_slot_tasks: List[Optional[int]] = [
            None for _ in range(MAX_ACTIVE_DOWNLOADS)
        ]
        self._download_rows: list[dict[str, object]] = []
        self.stop_downloads_action: Optional[QAction] = None
        self.theme_action: Optional[QAction] = None
        self.js_scraper: Optional[JSPageScraper] = None
        self.interactive_dialog: Optional["InteractiveScrapeDialog"] = None
        self._current_url: str = ""
        self._current_x_auth_browser: Optional[str] = None
        self._logs_expanded: bool = False
        self._download_failed_count: int = 0
        self._closing: bool = False
        self.output_dir: Path = DEFAULT_DOWNLOAD_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.settings_store = SettingsStore()
        self._theme_name = self.settings_store.get().theme
        self._theme = get_theme(self._theme_name)
        self.proxy_status.connect(self.log)
        self.media_proxy = MediaProxyServer(
            settings_store=self.settings_store,
            on_status=self.proxy_status.emit,
        ).start()

        self._build_ui()
        self._build_menu()
        self._apply_theme(self._theme_name, persist=False)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(12)

        root.addWidget(self._build_url_card())
        root.addWidget(self._build_splitter(), stretch=1)
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
        self.url_input.textChanged.connect(self._update_x_auth_visibility)
        layout.addWidget(self.url_input, stretch=1)

        self.x_auth_label = QLabel("X session")
        self.x_auth_combo = QComboBox()
        self.x_auth_combo.addItem("Public", None)
        for label, browser in (
            ("Chrome", "chrome"),
            ("Safari", "safari"),
            ("Firefox", "firefox"),
            ("Edge", "edge"),
            ("Brave", "brave"),
        ):
            self.x_auth_combo.addItem(label, browser)
        self.x_auth_combo.setToolTip(
            "Choose a browser already signed in to X when this post requires login. "
            "The app does not save your X password."
        )
        self.x_auth_label.hide()
        self.x_auth_combo.hide()
        layout.addWidget(self.x_auth_label)
        layout.addWidget(self.x_auth_combo)

        self.scrape_button = QPushButton("Scrape")
        self.scrape_button.setObjectName("Primary")
        self.scrape_button.setMinimumWidth(110)
        self.scrape_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scrape_button.clicked.connect(self.start_scrape)
        layout.addWidget(self.scrape_button)
        return card

    def _update_x_auth_visibility(self, text: str) -> None:
        is_x = is_x_post_url(text.strip())
        self.x_auth_label.setVisible(is_x)
        self.x_auth_combo.setVisible(is_x)

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

        left_layout.addLayout(header)

        self.video_list = VideoListWidget()
        self.video_list.selection_changed.connect(self._on_video_selected)
        left_layout.addWidget(self.video_list, stretch=1)

        # --- Right: preview card ---
        right_card = _make_card()
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(12, 12, 12, 12)
        right_layout.setSpacing(8)
        self.preview_panel = PreviewPanel(
            proxy=self.media_proxy, theme=self._theme
        )
        self.preview_panel.player_message.connect(self.log)
        right_layout.addWidget(self.preview_panel, stretch=1)
        right_layout.addWidget(self._build_download_controls())

        splitter.addWidget(left_card)
        splitter.addWidget(right_card)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([480, 820])
        return splitter

    def _build_download_controls(self) -> QWidget:
        wrap = QWidget()
        layout = QHBoxLayout(wrap)
        layout.setContentsMargins(2, 6, 2, 0)
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
        sep.setObjectName("VSep")
        sep.setFrameShape(QFrame.Shape.NoFrame)
        sep.setFixedSize(1, 24)
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

        self.download_button = QPushButton("Download current")
        self.download_button.setObjectName("Primary")
        self.download_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download_button.setMinimumWidth(170)
        self.download_button.clicked.connect(self.start_download)
        layout.addWidget(self.download_button)
        return wrap

    def _build_progress_row(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(6)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Idle")
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFixedHeight(16)
        layout.addWidget(self.progress_bar)

        self._download_rows = []
        for _slot in range(MAX_ACTIVE_DOWNLOADS):
            row = QFrame()
            row.setObjectName("DownloadRow")
            row.setVisible(False)
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(10, 7, 10, 8)
            row_layout.setSpacing(5)

            meta = QHBoxLayout()
            meta.setSpacing(8)
            title = QLabel("Idle")
            title.setProperty("role", "downloadTitle")
            title.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
            )
            title.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            meta.addWidget(title, stretch=1)

            detail = QLabel("")
            detail.setProperty("role", "downloadMeta")
            detail.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            detail.setMinimumWidth(220)
            detail.setMaximumWidth(360)
            meta.addWidget(detail)
            row_layout.addLayout(meta)

            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setFixedHeight(8)
            row_layout.addWidget(bar)
            layout.addWidget(row)
            self._download_rows.append(
                {"row": row, "title": title, "detail": detail, "bar": bar}
            )
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
        toolbar.setIconSize(QSize(20, 20))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.addToolBar(toolbar)

        open_dir_action = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon),
            "Open downloads folder",
            self,
        )
        open_dir_action.triggered.connect(self._open_output_folder)
        toolbar.addAction(open_dir_action)

        self.stop_downloads_action = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop),
            "Stop all downloads",
            self,
        )
        self.stop_downloads_action.setToolTip(
            "Stop all downloads and merge available HLS segments"
        )
        self.stop_downloads_action.setEnabled(False)
        self.stop_downloads_action.triggered.connect(self.stop_all_downloads)
        toolbar.addAction(self.stop_downloads_action)

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

        spacer = QWidget()
        spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        toolbar.addWidget(spacer)

        self.theme_action = QAction("☀  Light mode", self)
        self.theme_action.triggered.connect(self._toggle_theme)
        toolbar.addAction(self.theme_action)
        theme_btn = toolbar.widgetForAction(self.theme_action)
        if theme_btn is not None:
            theme_btn.setObjectName("ThemeToggle")
            theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            theme_btn.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextOnly
            )

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _truncate_path(path: Path, max_chars: int = 60) -> str:
        text = str(path)
        if len(text) <= max_chars:
            return text
        head = text[: max_chars // 2 - 2]
        tail = text[-(max_chars // 2 - 1) :]
        return f"{head}…{tail}"

    @staticmethod
    def _truncate_text(text: str, max_chars: int = 80) -> str:
        if len(text) <= max_chars:
            return text
        return f"{text[: max_chars - 1]}…"

    def _toggle_logs(self) -> None:
        self._logs_expanded = not self._logs_expanded
        self.log_view.setVisible(self._logs_expanded)
        arrow = "▴" if self._logs_expanded else "▾"
        self.logs_toggle_button.setText(f"Logs {arrow}")

    def _update_video_count_badge(self, count: int) -> None:
        self.count_badge.setText(str(count))

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

        x_auth_browser = (
            self.x_auth_combo.currentData() if is_x_post_url(url) else None
        )
        self._current_x_auth_browser = x_auth_browser
        self.scrape_worker = ScrapeWorker(
            url, self, x_auth_browser=x_auth_browser
        )
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

        if is_x_post_url(self._current_url):
            self._reset_scrape_button()
            self.progress_bar.setFormat("No X videos found")
            self.statusBar().showMessage("No X videos found")
            suggestion = (
                " If it plays in a browser where you are signed in, choose "
                "that browser in X session and scrape again."
                if self._current_x_auth_browser is None
                else " Check that the selected browser is signed in to X."
            )
            QMessageBox.information(
                self,
                "No X videos",
                "No accessible video was found in this X post." + suggestion,
            )
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
        if is_x_post_url(self._current_url):
            self.log(f"X extraction failed: {message}")
            QMessageBox.warning(
                self,
                "X video extraction failed",
                message + (
                    "\n\nIf this post plays in a browser where you are signed "
                    "in, select that browser in X session and try again."
                    if self._current_x_auth_browser is None
                    else ""
                ),
            )
            return
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
        item = self.video_list.current_video()
        if item is None:
            QMessageBox.information(
                self,
                "Nothing selected",
                "Click a video in the list before downloading.",
            )
            return

        self.output_dir.mkdir(parents=True, exist_ok=True)
        quality = self.quality_combo.currentText()
        task_id = self._next_download_id
        self._next_download_id += 1
        self._download_tasks[task_id] = {
            "item": item,
            "quality": quality,
            "output_dir": self.output_dir,
            "failed": False,
        }
        self._download_queue.append(task_id)
        self.log(f"[D{task_id}] Queued: {item.title}")
        self._pump_download_queue()

    def stop_all_downloads(self) -> None:
        active = len(self.download_workers)
        queued = len(self._download_queue)
        if active == 0 and queued == 0:
            return

        if self.stop_downloads_action is not None:
            self.stop_downloads_action.setEnabled(False)
        for task_id in self._download_queue:
            self._download_tasks.pop(task_id, None)
        self._download_queue.clear()
        self._record_download_pressure()

        for task_id, worker in list(self.download_workers.items()):
            task = self._download_tasks.get(task_id)
            if task is not None:
                task["stopping"] = True
            self._set_download_row(
                task_id,
                status="Stopping; merging available segments...",
                indeterminate=True,
            )
            worker.cancel(keep_partial=True)
            worker.requestInterruption()

        self._lane_limiter.wake()
        self.log(
            f"Stopping {active} active download(s); cleared {queued} queued task(s)"
        )
        self.statusBar().showMessage("Stopping downloads...")
        self._update_download_summary()

    def _pump_download_queue(self) -> None:
        self._recalculate_download_lanes()
        while (
            len(self.download_workers) < MAX_ACTIVE_DOWNLOADS
            and self._download_queue
        ):
            task_id = self._download_queue.pop(0)
            task = self._download_tasks.get(task_id)
            if task is None:
                continue
            item = task["item"]
            worker = DownloadWorker(
                [item],
                task["output_dir"],
                quality=task["quality"],
                parallel_connections=DOWNLOAD_LANE_IDLE_MAX,
                lane_limiter=self._lane_limiter,
                parent=self,
            )
            self.download_workers[task_id] = worker
            task["worker"] = worker
            worker.item_started.connect(
                lambda index, started_item, tid=task_id: self._on_dl_item_started(
                    tid, index, started_item
                )
            )
            worker.item_progress.connect(
                lambda index, progress, tid=task_id: self._on_dl_item_progress(
                    tid, index, progress
                )
            )
            worker.item_finished.connect(
                lambda index, path, tid=task_id: self._on_dl_item_finished(
                    tid, index, path
                )
            )
            worker.item_failed.connect(
                lambda index, message, tid=task_id: self._on_dl_item_failed(
                    tid, index, message
                )
            )
            worker.all_done.connect(
                lambda tid=task_id: self._on_dl_all_done(tid)
            )
            worker.start()
        self._refresh_download_activity()

    def _refresh_download_activity(self) -> None:
        active = len(self.download_workers)
        queued = len(self._download_queue)
        has_downloads = active > 0 or queued > 0
        if self.stop_downloads_action is not None:
            self.stop_downloads_action.setEnabled(has_downloads)
        self._recalculate_download_lanes()
        if has_downloads:
            self.statusBar().showMessage(
                f"Downloads: {active} active, {queued} queued"
            )
        else:
            self.statusBar().showMessage("Ready")
        self._update_download_summary()

    def _assign_download_slot(self, task_id: int) -> int:
        existing = self._download_task_slots.get(task_id)
        if existing is not None:
            return existing
        for slot, current in enumerate(self._download_slot_tasks):
            if current is None:
                self._download_slot_tasks[slot] = task_id
                self._download_task_slots[task_id] = slot
                return slot
        self._download_slot_tasks[0] = task_id
        self._download_task_slots[task_id] = 0
        return 0

    def _release_download_slot(self, task_id: int) -> None:
        slot = self._download_task_slots.pop(task_id, None)
        if slot is not None and self._download_slot_tasks[slot] == task_id:
            self._download_slot_tasks[slot] = None

    def _set_download_row(
        self,
        task_id: int,
        title: Optional[str] = None,
        status: Optional[str] = None,
        value: Optional[int] = None,
        indeterminate: bool = False,
    ) -> None:
        if not self._download_rows:
            return
        slot = self._assign_download_slot(task_id)
        widgets = self._download_rows[slot]
        row = widgets["row"]
        title_label = widgets["title"]
        detail_label = widgets["detail"]
        bar = widgets["bar"]

        row.setVisible(True)
        if title is not None:
            title_label.setText(self._truncate_text(title, 88))
            title_label.setToolTip(title)
        if status is not None:
            detail_label.setText(self._truncate_text(status, 48))
            detail_label.setToolTip(status)
        if indeterminate:
            bar.setRange(0, 0)
        else:
            bar.setRange(0, 100)
            if value is not None:
                bar.setValue(max(0, min(100, value)))

    def _update_download_summary(self) -> None:
        active = len(self.download_workers)
        queued = len(self._download_queue)
        if active == 0 and queued == 0:
            if self.progress_bar.maximum() == 0:
                self.progress_bar.setRange(0, 100)
            if self.progress_bar.format().startswith("Downloads"):
                self.progress_bar.setValue(0)
                self.progress_bar.setFormat("Idle")
            return

        known: list[int] = []
        for task_id in self.download_workers:
            task = self._download_tasks.get(task_id, {})
            value = task.get("progress_pct")
            if isinstance(value, int):
                known.append(value)

        if known:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(sum(known) // len(known))
        else:
            self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat(
            f"Downloads: {active} active, {queued} queued"
        )

    def _recalculate_download_lanes(self) -> None:
        limit = self._download_idle_lane_limit
        self._lane_limiter.set_limit(limit)
        if limit != self._download_lane_limit:
            self._download_lane_limit = limit
            if self.download_workers:
                self.log(f"Download lanes adjusted to {limit} (network stable)")

    def _record_segment_downloaded(self) -> None:
        if self._download_idle_lane_limit >= DOWNLOAD_LANE_IDLE_MAX:
            return
        self._download_segment_successes += 1
        if self._download_segment_successes < DOWNLOAD_LANE_INCREASE_SEGMENTS:
            return

        self._download_segment_successes = 0
        self._download_idle_lane_limit = min(
            DOWNLOAD_LANE_IDLE_MAX,
            self._download_idle_lane_limit + DOWNLOAD_LANE_STEP,
        )
        self._recalculate_download_lanes()

    def _record_download_pressure(self) -> None:
        self._download_segment_successes = 0
        if self._download_idle_lane_limit <= DOWNLOAD_LANE_IDLE_BASE:
            return
        self._download_idle_lane_limit = DOWNLOAD_LANE_IDLE_BASE
        self._recalculate_download_lanes()

    def _on_dl_item_started(self, task_id: int, index: int, item: VideoItem) -> None:
        self.log(f"[D{task_id}] Starting: {item.title}")
        task = self._download_tasks.get(task_id)
        if task is not None:
            task["progress_pct"] = 0
        self._set_download_row(
            task_id,
            title=f"D{task_id}  {item.title}",
            status="Starting",
            value=0,
        )
        self._update_download_summary()

    def _on_dl_item_progress(
        self, task_id: int, index: int, progress: DownloadProgress
    ) -> None:
        task = self._download_tasks.get(task_id)
        if progress.status == "downloading" and progress.total_bytes > 0:
            pct = int(progress.downloaded_bytes * 100 / progress.total_bytes)
            if task is not None:
                task["progress_pct"] = pct
            if progress.message:
                detail = f"{pct}%  {progress.message}"
            else:
                speed = format_bytes(progress.speed) + "/s" if progress.speed else "--"
                eta = f"{progress.eta}s" if progress.eta is not None else "--"
                detail = f"{pct}%  {speed}  ETA {eta}"
            self._set_download_row(task_id, status=detail, value=pct)
        elif progress.status == "downloading":
            if task is not None:
                task["progress_pct"] = None
            self._set_download_row(
                task_id,
                status=progress.message or "Downloading...",
                indeterminate=True,
            )
        elif progress.status == "finished":
            if task is not None:
                task["progress_pct"] = 100
            self._set_download_row(task_id, status="Post-processing", value=100)
        elif progress.status == "completed":
            if task is not None:
                task["progress_pct"] = 100
                if progress.message:
                    task["completion_message"] = progress.message
            self._set_download_row(
                task_id,
                status=progress.message or "Complete",
                value=100,
            )
            if progress.message.startswith("HLS timings:"):
                self.log(f"[D{task_id}] {progress.message}")
        elif progress.status == "partial":
            if task is not None:
                task["partial"] = True
                if progress.total_bytes > 0:
                    task["progress_pct"] = int(
                        progress.downloaded_bytes * 100 / progress.total_bytes
                    )
            self._set_download_row(
                task_id,
                status=progress.message or "Partial saved",
                value=100,
            )

        if (
            progress.status == "downloading"
            and progress.message.startswith("Segment ")
        ):
            self._record_segment_downloaded()
        elif (
            progress.status == "downloading"
            and progress.message.startswith("Parallel HLS failed")
        ):
            self._record_download_pressure()
        self._update_download_summary()

    def _on_dl_item_finished(self, task_id: int, index: int, path: str) -> None:
        task = self._download_tasks.get(task_id)
        if task is not None:
            task["path"] = path
            task["progress_pct"] = 100
        if task and task.get("partial"):
            self._set_download_row(task_id, status="Partial saved", value=100)
            self.log(f"[D{task_id}] Partial saved -> {path}")
        else:
            self._set_download_row(
                task_id,
                status=(
                    task.get("completion_message")
                    if task and task.get("completion_message")
                    else "Complete"
                ),
                value=100,
            )
            self.log(f"[D{task_id}] Done -> {path}")

    def _on_dl_item_failed(self, task_id: int, index: int, message: str) -> None:
        self._download_failed_count += 1
        task = self._download_tasks.get(task_id)
        if task is not None:
            task["failed"] = True
            task["progress_pct"] = 0
        self._record_download_pressure()
        self._set_download_row(
            task_id,
            status="Stopped" if task and task.get("stopping") else "Failed",
            value=0,
        )
        self.log(f"[D{task_id}] FAILED: {message}")

    def _on_dl_all_done(self, task_id: int) -> None:
        worker = self.download_workers.pop(task_id, None)
        if worker is not None:
            worker.deleteLater()
        if self._closing:
            return
        task = self._download_tasks.get(task_id, {})
        if task.get("partial"):
            self._set_download_row(task_id, status="Partial saved", value=100)
        elif task.get("failed"):
            self._set_download_row(
                task_id,
                status="Stopped" if task.get("stopping") else "Failed",
                value=0,
            )
        else:
            self._set_download_row(task_id, status="Complete", value=100)
        self._release_download_slot(task_id)
        self._download_tasks.pop(task_id, None)
        self._pump_download_queue()

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

    # ------------------------------------------------------------- theming

    def _apply_theme(self, name: str, persist: bool = True) -> None:
        self._theme_name = name
        self._theme = get_theme(name)
        app = QApplication.instance()
        if app is not None:
            app.setPalette(build_palette(self._theme))
            app.setStyleSheet(build_stylesheet(self._theme))
        self.preview_panel.apply_theme(self._theme)
        self._update_theme_action_text()
        if persist:
            settings = self.settings_store.get()
            settings.theme = name
            self.settings_store.update(settings)

    def _toggle_theme(self) -> None:
        self._apply_theme(
            "light" if self._theme_name == "dark" else "dark",
            persist=True,
        )

    def _update_theme_action_text(self) -> None:
        if self.theme_action is None:
            return
        if self._theme_name == "dark":
            self.theme_action.setText("☀  Light mode")
            self.theme_action.setToolTip("Switch to light theme")
        else:
            self.theme_action.setText("☾  Dark mode")
            self.theme_action.setToolTip("Switch to dark theme")

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self._closing = True
        self.preview_panel.shutdown()
        self.video_list.shutdown()
        self._download_queue.clear()
        for worker in list(self.download_workers.values()):
            if worker.isRunning():
                worker.cancel()
                worker.requestInterruption()
        for task_id, worker in list(self.download_workers.items()):
            if worker.isRunning() and not worker.wait(5000):
                worker.terminate()
                worker.wait(1000)
            self.download_workers.pop(task_id, None)
        if self.scrape_worker and self.scrape_worker.isRunning():
            self.scrape_worker.requestInterruption()
            if not self.scrape_worker.wait(3000):
                self.scrape_worker.terminate()
                self.scrape_worker.wait(1000)
        try:
            self.media_proxy.stop()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)
