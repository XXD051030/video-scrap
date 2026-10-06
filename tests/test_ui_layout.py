"""Offline regressions for the desktop toolbar, menus, and compact layout.

Run directly: ``build/.venv/bin/python tests/test_ui_layout.py``.
These tests exercise real Qt widgets without browsing or starting downloads.
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QBuffer, QCoreApplication, QEvent, QIODevice, QPoint, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from src import settings
from src.gui import main_window
from src.scraper import VideoItem


APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


class _Proxy:
    """The constructor needs a proxy, but layout checks do not need a socket."""

    def __init__(self, **_kwargs):
        self.stopped = False

    def start(self):
        return self

    def stop(self):
        self.stopped = True

    def unregister(self, _url):
        pass


class _Window(main_window.MainWindow):
    def __init__(self):
        self.commands = []
        super().__init__()

    def _open_output_folder(self):
        self.commands.append("folder")

    def _start_interactive_from_toolbar(self):
        self.commands.append("browser")

    def _open_preferences(self):
        self.commands.append("preferences")

    def _show_about(self):
        self.commands.append("about")

    def stop_all_downloads(self):
        self.commands.append("stop")

    def start_scrape(self):
        self.commands.append("scrape")

    def start_download(self):
        self.commands.append("download")


def _flush():
    APP.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APP.processEvents()


@contextmanager
def _window():
    with tempfile.TemporaryDirectory(prefix="video-ui-layout-") as directory:
        root = Path(directory)
        with mock.patch.object(settings, "SETTINGS_PATH", root / "settings.json"), \
                mock.patch.object(main_window, "DEFAULT_DOWNLOAD_DIR", root / "downloads"), \
                mock.patch.object(main_window, "MediaProxyServer", _Proxy):
            window = _Window()
            window.show()
            _flush()
            try:
                yield window
            finally:
                window.close()
                assert window.media_proxy.stopped
                window.deleteLater()
                _flush()
                APP.setStyleSheet("")


def _fixture():
    return VideoItem(
        title="A long fixture title for checking readable metadata " * 5,
        url="https://example.invalid/" + "very-long-source-path/" * 20 + "clip.mp4",
        source_url="https://example.invalid/watch",
        duration=90,
        width=1920,
        height=1080,
        ext="mp4",
        uploader="A creator name with enough words to wrap the metadata " * 4,
        is_direct=True,
    )


def _thumbnail_png():
    image = QImage(1280, 720, QImage.Format.Format_RGB32)
    image.fill(QColor("#454545"))
    buffer = QBuffer()
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(buffer.data())


def _point_in_window(widget, window):
    return widget.mapTo(window, QPoint(0, 0))


def _assert_inside(widget, window):
    assert widget.isVisible(), f"Control hidden: {widget.objectName() or widget}"
    point = _point_in_window(widget, window)
    assert point.x() >= 0 and point.y() >= 0, f"Control starts outside: {widget}"
    assert point.x() + widget.width() <= window.width(), f"Control spills horizontally: {widget}"
    assert point.y() + widget.height() <= window.height(), f"Control spills vertically: {widget}"


def test_more_menu_and_toolbar_keep_commands_and_idle_stop_state():
    with _window() as window:
        toolbar = window.main_toolbar
        assert window.more_button.menu() is window.more_menu
        assert window.prefs_action in window.more_menu.actions()
        assert window.about_action in window.more_menu.actions()
        # Shortcuts must remain registered while the popup is closed.
        assert window.prefs_action in window.actions()
        assert window.about_action in window.actions()
        assert not window.prefs_action.shortcut().isEmpty()
        assert not window.about_action.shortcut().isEmpty()
        assert not window.stop_downloads_action.isEnabled()
        assert not toolbar.widgetForAction(window.stop_downloads_action).isEnabled()
        window.stop_downloads_action.trigger()
        assert "stop" not in window.commands
        toolbar.widgetForAction(window.open_dir_action).click()
        if window.browser_scrape_action is not None:
            toolbar.widgetForAction(window.browser_scrape_action).click()
        window.prefs_action.trigger()
        window.about_action.trigger()
        window.stop_downloads_action.setEnabled(True)
        toolbar.widgetForAction(window.stop_downloads_action).click()
        expected = ["folder"]
        if window.browser_scrape_action is not None:
            expected.append("browser")
        assert window.commands == expected + ["preferences", "about", "stop"]
        # Moving actions into More must not require opening the menu first.
        for action in (window.prefs_action, window.about_action):
            key = action.shortcut()[0]
            QTest.keyClick(window, key.key(), key.keyboardModifiers())
            _flush()
        assert window.commands[-2:] == ["preferences", "about"]
        window.more_menu.popup(window.more_button.mapToGlobal(QPoint(0, window.more_button.height())))
        _flush()
        assert window.more_menu.isVisible()
        assert window.more_menu.width() >= 220
        window.more_menu.close()


def test_input_and_download_controls_keep_independent_callbacks():
    with _window() as window:
        window.url_input.setText("https://example.invalid/watch")
        assert window.x_auth_combo.isHidden()
        assert window.x_auth_label.isHidden()
        window.scrape_button.click()
        window.url_input.returnPressed.emit()
        window.download_button.click()
        assert window.commands == ["scrape", "scrape", "download"]
        window.url_input.setText("https://x.com/example/status/123")
        assert window.x_auth_combo.isVisible()
        assert window.x_auth_label.isVisible()
        window.x_auth_combo.setCurrentIndex(window.x_auth_combo.findData("firefox"))
        assert window.x_auth_combo.currentData() == "firefox"
        window.url_input.setText("https://example.invalid/watch")
        assert window.x_auth_combo.isHidden()
        assert window.x_auth_combo.currentData() == "firefox"
        folder = str(Path(tempfile.gettempdir()) / ("long-folder-name-" * 20))
        with mock.patch.object(main_window.QFileDialog, "getExistingDirectory", return_value=folder):
            window.choose_dir_button.click()
        assert window.output_dir == Path(folder)
        assert window.path_label.toolTip() == folder
        # Choosing a directory and changing login selection do not start jobs.
        assert window.commands == ["scrape", "scrape", "download"]


def test_minimum_window_fits_long_metadata_paths_and_x_login_controls():
    with _window() as window:
        window.video_list.set_videos([_fixture()])
        # Exercise the decoded PNG path: a large bitmap must not impose its
        # previous scaled dimensions as the widget's new minimum size.
        window.preview_panel._apply_thumbnail(0, _thumbnail_png())
        folder = str(Path(tempfile.gettempdir()) / ("long-folder-name-" * 20))
        with mock.patch.object(main_window.QFileDialog, "getExistingDirectory", return_value=folder):
            window.choose_dir_button.click()
        for theme in ("dark", "light"):
            window._apply_theme(theme, persist=False)
            # Request the compact height after each width change. Qt may grow
            # it to the content minimum when two-line captions need more room.
            for width, height in ((1320, 820), (window.minimumWidth(), 640),
                                  (1320, 820), (window.minimumWidth(), 640)):
                window.resize(width, height)
                for url in ("https://example.invalid/watch", "https://x.com/example/status/123"):
                    window.url_input.setText(url)
                    _flush()
                    assert window.width() == width
                    assert window.height() >= height
                    assert window.height() >= window.minimumHeight()
                    for widget in (
                        window.url_input, window.scrape_button, window.more_button,
                        window.quality_combo, window.download_button,
                        window.path_label, window.choose_dir_button,
                        window.preview_panel.media_stack_host,
                        window.preview_panel.position_slider,
                        window.preview_panel.fullscreen_button,
                        window.preview_panel.volume_slider,
                    ):
                        _assert_inside(widget, window)
                    if "x.com" in url:
                        _assert_inside(window.x_auth_combo, window)
                    quality = _point_in_window(window.quality_combo, window)
                    path = _point_in_window(window.path_label, window)
                    assert path.y() >= quality.y() + window.quality_combo.height()
                    seek = _point_in_window(window.preview_panel.position_slider, window)
                    play = _point_in_window(window.preview_panel.play_button, window)
                    stage = _point_in_window(window.preview_panel.media_stack_host, window)
                    assert stage.y() + window.preview_panel.media_stack_host.height() <= seek.y(), \
                        "Media stage overlaps the seek row at the minimum window size"
                    assert seek.y() + window.preview_panel.position_slider.height() <= play.y()
                    assert window.preview_panel.media_stack_host.height() >= 100
                    thumbnail = window.preview_panel.thumbnail_label
                    pixmap = thumbnail.pixmap()
                    assert not pixmap.isNull()
                    scale = pixmap.devicePixelRatio()
                    assert pixmap.width() / scale <= thumbnail.width()
                    assert pixmap.height() / scale <= thumbnail.height()


def test_theme_switch_preserves_selection_pending_media_and_volume():
    with _window() as window:
        item = _fixture()
        window.video_list.set_videos([item])
        panel = window.preview_panel
        player, output, video = panel.player, panel.audio_output, panel.video_widget
        source = panel._pending_playable_url
        panel.volume_slider.setValue(27)
        for _ in range(3):
            initial = window._theme_name
            window.theme_action.trigger()
            _flush()
            assert window._theme_name != initial
            assert settings.AppSettings.load().theme == window._theme_name
            assert window.video_list.current_video() is item
            assert panel._current is item and panel._pending_playable_url == source
            assert panel.player is player and panel.audio_output is output and panel.video_widget is video
            assert panel.player.source().isEmpty() and not panel._source_loaded
            assert panel.volume_slider.value() == 27
            assert abs(panel.audio_output.volume() - 0.27) < 0.001
            assert not window.theme_action.icon().isNull()
            assert not window.scrape_button.icon().isNull()
            assert not panel.fullscreen_button.icon().isNull()


def test_expanded_logs_fit_without_covering_the_player_or_download_controls():
    with _window() as window:
        item = _fixture()
        window.video_list.set_videos([item])
        _flush()
        closed_minimum = window.minimumHeight()
        window.log("Layout regression message")
        for expanded in (True, False, True):
            window.logs_toggle_button.click()
            _flush()
            assert window._logs_expanded is expanded
            assert window.log_view.isVisible() is expanded
            if not expanded:
                assert window.minimumHeight() <= closed_minimum, \
                    "Collapsing logs keeps the expanded window minimum height"
            for width, height in (
                (window.minimumWidth(), window.minimumHeight()),
                (1320, max(820, window.minimumHeight())),
            ):
                window.resize(width, height)
                _flush()
                panel = window.preview_panel
                for widget in (panel.media_stack_host, panel.position_slider,
                               panel.fullscreen_button, window.download_button,
                               window.choose_dir_button, window.logs_toggle_button):
                    _assert_inside(widget, window)
                stage = _point_in_window(panel.media_stack_host, window)
                seek = _point_in_window(panel.position_slider, window)
                assert stage.y() + panel.media_stack_host.height() <= seek.y()
                if expanded:
                    _assert_inside(window.log_view, window)
                    download = _point_in_window(window.choose_dir_button, window)
                    logs = _point_in_window(window.log_view, window)
                    assert download.y() + window.choose_dir_button.height() <= logs.y()
                assert window.video_list.current_video() is item
                assert panel.player.source().isEmpty() and not panel._source_loaded
        assert "Layout regression message" in window.log_view.toPlainText()
        window.clear_logs_button.click()
        assert window.log_view.toPlainText() == ""


if __name__ == "__main__":
    tests = [value for name, value in globals().copy().items()
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"Passed {len(tests)} desktop UI checks")
