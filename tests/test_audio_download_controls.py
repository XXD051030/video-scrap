"""Offline checks for audio format controls, queued tasks, and workers.

Run directly: ``build/.venv/bin/python tests/test_audio_download_controls.py``.
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

from PyQt6.QtCore import QCoreApplication, QEvent, QPoint
from PyQt6.QtWidgets import QApplication

from src import settings
from src.downloader import DownloadProgress
from src.gui import main_window, workers
from src.scraper import VideoItem


APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


class _Proxy:
    def __init__(self, **_kwargs):
        pass

    def start(self):
        return self

    def stop(self):
        pass

    def unregister(self, _url):
        pass


def _flush():
    APP.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APP.processEvents()


@contextmanager
def _window():
    with tempfile.TemporaryDirectory(prefix="video-audio-controls-") as directory:
        root = Path(directory)
        with mock.patch.object(settings, "SETTINGS_PATH", root / "settings.json"), \
                mock.patch.object(main_window, "DEFAULT_DOWNLOAD_DIR", root / "downloads"), \
                mock.patch.object(main_window, "MediaProxyServer", _Proxy):
            window = main_window.MainWindow()
            window.show()
            _flush()
            try:
                yield window
            finally:
                # Test worker doubles do not own running threads.
                window.download_workers.clear()
                window.close()
                window.deleteLater()
                _flush()
                APP.setStyleSheet("")


def _fixture():
    return VideoItem(
        title="A long audio controls fixture title " * 12,
        url="https://example.invalid/clip.mp4",
        source_url="https://example.invalid/watch",
        is_direct=True,
        duration=90,
        width=1920,
        height=1080,
        ext="mp4",
    )


def _rect_in_window(widget, window):
    rect = widget.rect()
    rect.moveTopLeft(widget.mapTo(window, QPoint(0, 0)))
    return rect


def test_audio_format_visibility_defaults_and_selection_are_preserved():
    with _window() as window:
        assert window.audio_format_combo.currentData() == "original"
        assert window.audio_format_row.isHidden()
        assert [window.audio_format_combo.itemData(i) for i in range(3)] == [
            "original", "mp3", "m4a"
        ]
        window.quality_combo.setCurrentText("audio only")
        _flush()
        assert window.audio_format_combo.isVisible()
        window.audio_format_combo.setCurrentIndex(window.audio_format_combo.findData("mp3"))
        window.quality_combo.setCurrentText("720p")
        _flush()
        assert window.audio_format_row.isHidden()
        window.quality_combo.setCurrentText("audio only")
        _flush()
        assert window.audio_format_combo.currentData() == "mp3"


def test_audio_controls_fit_minimum_window_with_long_metadata_and_paths():
    with _window() as window:
        window.video_list.set_videos([_fixture()])
        window.path_label.setText(str(Path(tempfile.gettempdir()) / ("long-folder-" * 25)))
        window.url_input.setText("https://x.com/example/status/123")
        window.quality_combo.setCurrentText("audio only")
        for theme in ("dark", "light"):
            window._apply_theme(theme, persist=False)
            for expanded in (False, True, False):
                if window._logs_expanded != expanded:
                    window.logs_toggle_button.click()
                window.resize(window.minimumWidth(), 640)
                _flush()
                controls = (
                    window.quality_combo, window.audio_format_label,
                    window.audio_format_combo, window.download_button,
                    window.path_label, window.choose_dir_button,
                    window.preview_panel.media_stack_host,
                    window.preview_panel.position_slider,
                    window.preview_panel.fullscreen_button,
                    window.preview_panel.volume_slider,
                )
                for widget in controls:
                    assert widget.isVisible()
                    assert window.rect().contains(_rect_in_window(widget, window)), \
                        f"Control leaves the minimum window: {widget}"
                quality = _rect_in_window(window.quality_combo, window)
                audio = _rect_in_window(window.audio_format_combo, window)
                path = _rect_in_window(window.path_label, window)
                assert quality.bottom() < audio.top() < path.top()
                assert audio.bottom() < path.top()
                assert not _rect_in_window(window.audio_format_label, window).intersects(audio)
                assert not quality.intersects(_rect_in_window(window.download_button, window))
                assert window.preview_panel.media_stack_host.height() >= 100
                stage = _rect_in_window(window.preview_panel.media_stack_host, window)
                seek = _rect_in_window(window.preview_panel.position_slider, window)
                assert stage.bottom() < seek.top()


def test_queue_freezes_each_audio_format_before_the_worker_starts():
    with _window() as window:
        window.video_list.set_videos([_fixture()])
        window.quality_combo.setCurrentText("audio only")
        with mock.patch.object(window, "_pump_download_queue"):
            for audio_format in ("mp3", "m4a"):
                window.audio_format_combo.setCurrentIndex(
                    window.audio_format_combo.findData(audio_format)
                )
                window.start_download()
        window.quality_combo.setCurrentText("best")
        window.audio_format_combo.setCurrentIndex(0)
        with mock.patch.object(main_window, "DownloadWorker") as worker_class:
            worker_class.side_effect = lambda *_args, **_kwargs: mock.Mock()
            window._pump_download_queue()
            assert [call.kwargs["audio_format"] for call in worker_class.call_args_list] == [
                "mp3", "m4a"
            ]
            assert all(call.kwargs["quality"] == "audio only"
                       for call in worker_class.call_args_list)
            assert len(window.download_workers) == 2


def test_audio_processing_status_is_visible_before_completion():
    with _window() as window:
        window._download_tasks[1] = {"item": _fixture(), "quality": "audio only"}
        window.download_workers[1] = mock.Mock()
        window._on_dl_item_progress(
            1, 0, DownloadProgress(status="finished", message="Converting audio to MP3...")
        )
        detail = window._download_rows[0]["detail"]
        bar = window._download_rows[0]["bar"]
        assert detail.text() == "Converting audio to MP3..."
        assert detail.toolTip() == "Converting audio to MP3..."
        assert bar.minimum() == 0 and bar.maximum() == 0
        assert window._download_tasks[1]["progress_pct"] is None
        assert window.progress_bar.maximum() == 0
        window._on_dl_item_progress(1, 0, DownloadProgress(status="completed"))
        assert detail.text() == "Complete"
        assert bar.maximum() == 100 and bar.value() == 100
        assert window._download_tasks[1]["progress_pct"] == 100
        assert window.progress_bar.maximum() == 100 and window.progress_bar.value() == 100


def test_worker_passes_selected_audio_format_and_reports_the_final_path():
    with tempfile.TemporaryDirectory(prefix="video-audio-worker-") as directory:
        root = Path(directory)
        for audio_format in ("original", "mp3", "m4a"):
            final_path = root / f"audio.{audio_format}"
            with mock.patch.object(workers, "VideoDownloader") as downloader_class:
                downloader = downloader_class.return_value
                downloader.download.return_value = final_path
                worker = workers.DownloadWorker(
                    [_fixture()], root, quality="audio only", audio_format=audio_format
                )
                finished = []
                failed = []
                worker.item_finished.connect(lambda index, path: finished.append((index, path)))
                worker.item_failed.connect(lambda index, message: failed.append((index, message)))
                worker.run()
                assert downloader.download.call_args.kwargs["audio_format"] == audio_format
                assert downloader.download.call_args.kwargs["quality"] == "audio only"
                assert finished == [(0, str(final_path))]
                assert not failed
                downloader.close.assert_called_once()
                worker.deleteLater()
        _flush()


def test_worker_reports_missing_audio_as_failure_and_closes_downloader():
    with tempfile.TemporaryDirectory(prefix="video-audio-failure-") as directory:
        with mock.patch.object(workers, "VideoDownloader") as downloader_class:
            downloader = downloader_class.return_value
            downloader.download.side_effect = RuntimeError("This source has no audio track")
            worker = workers.DownloadWorker(
                [_fixture()], Path(directory), quality="audio only", audio_format="mp3"
            )
            finished = []
            failed = []
            worker.item_finished.connect(lambda *_args: finished.append(True))
            worker.item_failed.connect(lambda index, message: failed.append((index, message)))
            worker.run()
            assert not finished
            assert failed == [(0, "This source has no audio track")]
            downloader.close.assert_called_once()
            worker.deleteLater()
            _flush()


if __name__ == "__main__":
    tests = [value for name, value in globals().copy().items()
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"Passed {len(tests)} audio download control checks")
