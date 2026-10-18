"""Offline Qt regressions for audited input, task and recovery boundaries."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, QPoint, Qt, QUrl, pyqtSignal
from PyQt6.QtTest import QTest

import test_download_controls as controls
from src.downloader import DownloadProgress
from src.gui import main_window
from src.gui.preview_panel import ClickableSlider
from src.scraper import VideoItem


APP = controls.APP


class FakeScrapeWorker(QObject):
    log = pyqtSignal(str)
    finished_with_results = pyqtSignal(list)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, url, parent=None, **_kwargs):
        super().__init__(parent)
        self.url = url
        self.running = False

    def start(self):
        self.running = True

    def isRunning(self):
        return self.running


class FakeRenderer(QObject):
    log = pyqtSignal(str)
    finished = pyqtSignal(list, list, str)
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.disposed = False
        self.browser_cookies = []

    def scrape(self, url):
        self.page_url = url

    def dispose(self):
        self.disposed = True


def test_track_click_drag_release_and_playback_progress_resume():
    with controls._window() as window:
        slider = window.preview_panel.position_slider
        slider.setEnabled(True)
        slider.setRange(0, 100)
        slider.setValue(0)
        released = []
        slider.sliderReleased.connect(lambda: released.append(slider.value()))
        y = slider.height() // 2
        QTest.mousePress(slider, Qt.MouseButton.LeftButton,
                         pos=QPoint(slider.width() * 2 // 3, y))
        first = slider.value()
        assert slider.isSliderDown()
        QTest.mouseMove(slider, QPoint(slider.width() * 4 // 5, y))
        assert slider.value() > first
        QTest.mouseRelease(slider, Qt.MouseButton.LeftButton,
                           pos=QPoint(slider.width() * 4 // 5, y))
        assert not slider.isSliderDown() and len(released) == 1
        window.preview_panel._on_position_changed(20)
        assert slider.value() == 20


def test_volume_track_drag_and_hidden_drag_release():
    slider = ClickableSlider(Qt.Orientation.Horizontal)
    slider.resize(400, 30)
    slider.setRange(0, 100)
    slider.show()
    APP.processEvents()
    try:
        QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=QPoint(260, 15))
        QTest.mouseMove(slider, QPoint(320, 15))
        QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=QPoint(320, 15))
        assert slider.value() >= 75 and not slider.isSliderDown()
        QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=QPoint(100, 15))
        slider.hide()
        assert not slider.isSliderDown()
    finally:
        slider.close()
        slider.deleteLater()


def test_scrape_busy_guard_covers_enter_and_toolbar_until_renderer_finishes():
    with controls._window() as window, \
            mock.patch.object(main_window, "ScrapeWorker", FakeScrapeWorker), \
            mock.patch.object(main_window, "JSPageScraper", FakeRenderer), \
            mock.patch.object(main_window, "WEBENGINE_AVAILABLE", True):
        url = "https://origin-a.invalid/watch"
        window.url_input.setText(url)
        window.start_scrape()
        worker = window.scrape_worker
        worker.running = False
        worker.finished_with_results.emit([])
        renderer = window.js_scraper
        assert renderer is not None and window._scrape_active
        window.url_input.setText("https://origin-b.invalid/watch")
        window.url_input.returnPressed.emit()
        window._start_interactive_from_toolbar()
        assert window._current_url == url and window.scrape_worker is worker
        renderer.finished.emit(["https://origin-a.invalid/clip.mp4"], [], "A")
        assert window.video_list.current_video().source_url == url
        assert window.scrape_button.isEnabled() and not window._scrape_active
        assert renderer.disposed and window.js_scraper is None


def test_stale_renderer_result_cannot_change_new_task_or_reenable_button():
    with controls._window() as window, \
            mock.patch.object(main_window, "ScrapeWorker", FakeScrapeWorker), \
            mock.patch.object(main_window, "JSPageScraper", FakeRenderer), \
            mock.patch.object(main_window, "WEBENGINE_AVAILABLE", True):
        window.url_input.setText("https://origin-a.invalid/watch")
        window.start_scrape()
        old_worker = window.scrape_worker
        old_worker.running = False
        old_worker.finished_with_results.emit([])
        old_renderer = window.js_scraper
        # A result queued before a future cancellation/replacement must be inert.
        window._reset_scrape_button()
        window.url_input.setText("https://origin-b.invalid/watch")
        window.start_scrape()
        newer = window.scrape_worker
        old_renderer.finished.emit(["https://origin-a.invalid/clip.mp4"], [], "A")
        old_worker.failed.emit("late A error")
        assert window.video_list.count() == 0
        assert window._current_url == "https://origin-b.invalid/watch"
        assert not window.scrape_button.isEnabled() and window._scrape_active
        newer.running = False
        newer.finished_with_results.emit([controls._item("B")])
        assert window.video_list.current_video().title == "B"


def test_renderer_uses_captured_source_final_referer_and_cookie_snapshot():
    with controls._window() as window, \
            mock.patch.object(main_window, "JSPageScraper", FakeRenderer):
        source = "https://origin-a.invalid/watch"
        window._begin_scrape(source)
        window._start_js_render(source)
        renderer = window.js_scraper
        renderer.page_url = "https://final-a.invalid/watch"
        renderer.browser_cookies = [{"name": "sid", "value": "fixture",
                                    "domain": "final-a.invalid", "path": "/", "host_only": True}]
        window._current_url = "https://unrelated.invalid/watch"
        renderer.finished.emit(["https://final-a.invalid/clip.mp4"], [], "A")
        item = window.video_list.current_video()
        assert item.source_url == source and item.referer == renderer.page_url
        assert item.browser_cookies == renderer.browser_cookies
        assert item.browser_cookies[0] is not renderer.browser_cookies[0]


def test_synchronous_renderer_start_failure_releases_busy_state_and_page():
    class FailingRenderer(FakeRenderer):
        def scrape(self, url):
            raise RuntimeError("renderer startup failed")

    with controls._window() as window, \
            mock.patch.object(main_window, "JSPageScraper", FailingRenderer), \
            mock.patch.object(window, "_offer_interactive", return_value=False), \
            mock.patch.object(main_window.QMessageBox, "warning") as warning:
        window._begin_scrape("https://origin-a.invalid/watch")
        window._start_js_render(window._current_url)
        assert not window._scrape_active and window.scrape_button.isEnabled()
        assert window.js_scraper is None
        warning.assert_called_once()


def test_synchronous_worker_start_failure_does_not_lock_scrape_entry_points():
    class FailingWorker(FakeScrapeWorker):
        def start(self):
            raise RuntimeError("worker startup failed")

    with controls._window() as window, \
            mock.patch.object(main_window, "ScrapeWorker", FailingWorker), \
            mock.patch.object(main_window.QMessageBox, "critical") as warning:
        window.url_input.setText("https://origin-a.invalid/watch")
        window.start_scrape()
        assert not window._scrape_active and window.scrape_button.isEnabled()
        assert window.scrape_worker is None
        warning.assert_called_once()


def test_queued_download_can_be_removed_while_both_active_tasks_are_paused():
    with controls._window() as window:
        first, second = controls._enqueue(window, 4)
        first.item_state.emit(0, "paused")
        second.item_state.emit(0, "paused")
        assert window.queued_downloads_button.isVisible()
        actions = window.queued_downloads_menu.actions()
        assert len(actions) == 2 and "D3" in actions[0].text()
        actions[0].trigger()
        assert window._download_queue == [4] and 3 not in window._download_tasks
        assert len(controls._Worker.instances) == 2
        window.queued_downloads_menu.actions()[0].trigger()
        assert not window.queued_downloads_button.isVisible()
        assert window._download_queue == [] and first.calls == second.calls == []


def test_backend_fallback_state_restores_pause_but_stale_progress_does_not():
    with controls._window() as window:
        first, _ = controls._enqueue(window)
        first.item_state.emit(0, "finalizing")
        row = controls._row(window, 1)
        controls._progress(first, 30)
        assert not row["pause"].isEnabled()
        first.item_state.emit(0, "downloading")
        controls._progress(first, 40)
        assert window._download_tasks[1]["state"] == "downloading"
        assert row["pause"].isEnabled() and row["percent"].text() == "40%"
        row["pause"].click()
        assert first.calls == ["pause"]


def test_unavailable_directory_does_not_escape_startup_or_folder_actions():
    with controls._window() as outer:
        occupied = outer.output_dir / "occupied"
        occupied.write_text("not a directory")
        with mock.patch.object(main_window, "DEFAULT_DOWNLOAD_DIR", occupied), \
                mock.patch.object(main_window, "MediaProxyServer", controls._Proxy), \
                mock.patch.object(main_window.QMessageBox, "warning") as warning:
            window = main_window.MainWindow()
            try:
                assert "Download folder unavailable" in window.log_view.toPlainText()
                window.video_list.set_videos([controls._item()])
                window.start_download()
                window._open_output_folder()
                assert warning.call_count == 2 and not window._download_tasks
                recovered = occupied.parent / "recovered"
                with mock.patch.object(main_window.QFileDialog, "getExistingDirectory", return_value=str(recovered)):
                    window.choose_output_dir()
                assert window.output_dir == recovered and recovered.is_dir()
            finally:
                window.close()
                window.deleteLater()


def test_direct_dash_is_classified_and_preview_explains_download_still_available():
    with controls._window() as window:
        item = VideoItem(title="DASH", url="https://media.invalid/master.mpd",
                         source_url="https://media.invalid/watch", is_direct=True, is_dash=True)
        messages = []
        window.preview_panel.player_message.connect(messages.append)
        window.preview_panel.show_video(item)
        assert not window.preview_panel.play_button.isEnabled()
        assert window.preview_panel.fullscreen_button.isEnabled()
        assert window.preview_panel._best_playable_url(item) is None
        assert "download" in window.preview_panel.thumbnail_label.text().lower()
        assert any("MPD" in message for message in messages)


if __name__ == "__main__":
    tests = [value for name, value in globals().copy().items()
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"Passed {len(tests)} GUI audit regression checks")
