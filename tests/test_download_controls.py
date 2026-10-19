"""Offline Qt checks for per-download controls and task/slot state races."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, QPoint, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication

from src import settings
from src.downloader import DownloadProgress
from src.gui import main_window
from src.scraper import VideoItem


APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


class _Proxy:
    def __init__(self, **_kwargs):
        self.stopped = False

    def start(self):
        return self

    def unregister(self, _url):
        pass

    def stop(self):
        self.stopped = True


class _Worker(QObject):
    item_started = pyqtSignal(int, object)
    item_progress = pyqtSignal(int, object)
    item_state = pyqtSignal(int, str)
    item_control_error = pyqtSignal(int, str)
    item_finished = pyqtSignal(int, str)
    item_failed = pyqtSignal(int, str)
    all_done = pyqtSignal()
    finished = pyqtSignal()
    instances = []

    def __init__(self, items, output_dir, **kwargs):
        super().__init__(kwargs.get("parent"))
        self.items = items
        self.output_dir = output_dir
        self.kwargs = kwargs
        self.calls = []
        self.pause_accepted = True
        self.resume_accepted = True
        self.cancel_accepted = True
        self.running = False
        self.wait_result = True
        self.instances.append(self)

    def start(self):
        self.item_started.emit(0, self.items[0])
        self.item_state.emit(0, "downloading")

    def pause(self):
        self.calls.append("pause")
        return self.pause_accepted

    def resume(self):
        self.calls.append("resume")
        return self.resume_accepted

    def cancel(self, keep_partial=False):
        self.calls.append(("cancel", keep_partial))
        return self.cancel_accepted

    def requestInterruption(self):
        self.calls.append("interrupt")

    def isRunning(self):
        return self.running

    def wait(self, timeout):
        self.calls.append(("wait", timeout))
        return self.wait_result

    def terminate(self):
        raise AssertionError("Download QThread must never be terminated")


def _flush():
    APP.processEvents()


@contextmanager
def _window():
    with tempfile.TemporaryDirectory(prefix="video-download-controls-") as directory:
        root = Path(directory)
        _Worker.instances = []
        with mock.patch.object(settings, "SETTINGS_PATH", root / "settings.json"), \
                mock.patch.object(main_window, "DEFAULT_DOWNLOAD_DIR", root / "downloads"), \
                mock.patch.object(main_window, "MediaProxyServer", _Proxy), \
                mock.patch.object(main_window, "DownloadWorker", _Worker):
            window = main_window.MainWindow()
            window.show()
            _flush()
            try:
                yield window
            finally:
                window.download_workers.clear()
                window.close()
                window.deleteLater()
                _flush()
                APP.setStyleSheet("")


def _item(title="Ordinary fixture"):
    return VideoItem(title=title, url="https://example.invalid/clip.mp4",
                     source_url="https://example.invalid/watch", is_direct=True)


def _enqueue(window, count=2):
    for number in range(count):
        window.video_list.set_videos([_item(f"Ordinary fixture {number} " * 20)])
        window.start_download()
    _flush()
    return list(_Worker.instances)


def _progress(worker, percent):
    worker.item_progress.emit(0, DownloadProgress(
        status="downloading", downloaded_bytes=percent, total_bytes=100,
        speed=1024 * 1024, eta=20,
    ))


def _row(window, task_id):
    return window._download_rows[window._download_task_slots[task_id]]


def test_toolbar_has_no_global_stop_and_two_reusable_rows():
    with _window() as window:
        assert window.stop_downloads_action is None
        assert all(action.text() != "Stop downloads" for action in window.main_toolbar.actions())
        assert len(window._download_rows) == 2
        assert all(row["row"].isHidden() for row in window._download_rows)


def test_percentage_pause_cancel_order_fits_compact_black_and_white_themes():
    with _window() as window:
        a, b = _enqueue(window)
        assert a.kwargs["parallel_connections"] == b.kwargs["parallel_connections"] == 16
        _progress(a, 37)
        _progress(b, 68)
        for theme in ("dark", "light"):
            window._apply_theme(theme, persist=False)
            window.resize(1024, 820)
            _flush()
            assert window.width() == 1024
            for task_id in (1, 2):
                row = _row(window, task_id)
                rects = []
                for key in ("percent", "pause", "cancel"):
                    widget = row[key]
                    rect = widget.rect()
                    rect.moveTopLeft(widget.mapTo(window, QPoint(0, 0)))
                    assert widget.isVisible() and window.rect().contains(rect)
                    rects.append(rect)
                assert rects[0].right() < rects[1].left() < rects[2].left()
                assert abs(rects[0].center().y() - rects[1].center().y()) <= 1
                assert row["pause"].toolTip() == row["pause"].accessibleName()
                assert row["cancel"].toolTip() == row["cancel"].accessibleName()
                assert not row["pause"].icon().isNull() and not row["cancel"].icon().isNull()
            assert _row(window, 1)["percent"].text() == "37%"
            assert _row(window, 2)["percent"].text() == "68%"


def test_pause_waits_for_acknowledgement_and_freezes_only_its_own_progress():
    with _window() as window:
        a, b = _enqueue(window)
        _progress(a, 31)
        row = _row(window, 1)
        row["pause"].click()
        row["pause"].click()
        assert a.calls == ["pause"] and b.calls == []
        assert window._download_tasks[1]["state"] == "pausing"
        assert row["pause"].toolTip() == "Pause this download"
        assert not row["pause"].isEnabled()
        _progress(a, 42)
        a.item_progress.emit(0, DownloadProgress(status="completed"))
        assert row["percent"].text() == "31%" and row["detail"].text() == "Pausing…"
        _progress(b, 55)
        assert _row(window, 2)["percent"].text() == "55%"
        a.item_state.emit(0, "paused")
        assert row["detail"].text() == "Paused" and row["pause"].isEnabled()
        assert row["pause"].toolTip() == "Resume this download"
        assert "1 running, 1 paused, 0 queued" in window.progress_bar.format()
        row["pause"].click()
        assert a.calls == ["pause", "resume"] and b.calls == []
        assert window._download_tasks[1]["state"] == "resuming"
        assert not row["pause"].isEnabled()
        _progress(a, 43)
        assert row["percent"].text() == "31%"
        a.item_state.emit(0, "downloading")
        _progress(a, 44)
        assert row["percent"].text() == "44%" and row["pause"].isEnabled()


def test_rejected_pause_does_not_claim_the_download_is_paused():
    with _window() as window:
        a, _ = _enqueue(window)
        a.pause_accepted = False
        _row(window, 1)["pause"].click()
        assert window._download_tasks[1]["state"] == "downloading"
        assert _row(window, 1)["pause"].isEnabled()


def test_synchronous_backend_acknowledgement_does_not_return_to_pending_state():
    with _window() as window:
        a, _ = _enqueue(window)
        row = _row(window, 1)
        def pause_immediately():
            a.item_state.emit(0, "paused")
            return True
        def resume_immediately():
            a.item_state.emit(0, "resuming")
            a.item_state.emit(0, "downloading")
            return True
        a.pause = pause_immediately
        a.resume = resume_immediately
        row["pause"].click()
        assert window._download_tasks[1]["state"] == "paused"
        row["pause"].click()
        assert window._download_tasks[1]["state"] == "downloading"
        assert row["pause"].isEnabled()


def test_cancel_one_task_keeps_other_running_and_never_counts_as_failure():
    with _window() as window:
        a, b = _enqueue(window)
        _progress(a, 24)
        row = _row(window, 1)
        row["cancel"].click()
        row["cancel"].click()
        assert a.calls == [("cancel", False)] and b.calls == []
        assert row["detail"].text() == "Cancelling…"
        assert not row["pause"].isEnabled() and not row["cancel"].isEnabled()
        _progress(a, 99)
        a.item_state.emit(0, "downloading")
        assert row["percent"].text() == "24%" and row["detail"].text() == "Cancelling…"
        _progress(b, 63)
        assert _row(window, 2)["percent"].text() == "63%"
        a.item_failed.emit(0, "Cancelled by user")
        a.item_state.emit(0, "cancelled")
        a.all_done.emit()
        assert row["detail"].text() == "Cancelled" and row["percent"].text() == "24%"
        assert window._download_failed_count == 0
        assert 2 in window.download_workers and 1 not in window.download_workers


def test_two_paused_tasks_keep_slots_and_the_next_task_queued():
    with _window() as window:
        a, b = _enqueue(window, 3)
        for task_id, worker in ((1, a), (2, b)):
            _row(window, task_id)["pause"].click()
            worker.item_state.emit(0, "paused")
        assert len(_Worker.instances) == 2 and window._download_queue == [3]
        assert window._download_slot_tasks == [1, 2]
        assert "0 running, 2 paused, 1 queued" in window.progress_bar.format()
        _row(window, 1)["pause"].click()
        a.item_state.emit(0, "downloading")
        assert len(_Worker.instances) == 2 and window._download_queue == [3]


def test_reused_slot_controls_current_worker_and_ignores_old_late_signals():
    with _window() as window:
        a, b = _enqueue(window, 3)
        row = _row(window, 1)
        row["cancel"].click()
        a.item_state.emit(0, "cancelled")
        a.all_done.emit()
        c = _Worker.instances[2]
        assert window._download_slot_tasks == [3, 2]
        assert _row(window, 3) is row
        _progress(c, 12)
        saved = (row["title"].toolTip(), row["detail"].text(), row["percent"].text())
        _progress(a, 99)
        a.item_started.emit(0, _item("Stale task"))
        a.item_finished.emit(0, "/tmp/stale-task.mp4")
        a.item_failed.emit(0, "Late failure")
        a.item_state.emit(0, "completed")
        a.all_done.emit()
        assert (row["title"].toolTip(), row["detail"].text(), row["percent"].text()) == saved
        row["pause"].click()
        assert c.calls == ["pause"] and b.calls == []
        assert a.calls == [("cancel", False)]
        assert len(window._download_rows) == 2 and 1 not in window._download_tasks
        assert window._download_failed_count == 0


def test_finalizing_disables_pause_but_allows_cancel():
    with _window() as window:
        a, _ = _enqueue(window)
        a.item_state.emit(0, "finalizing")
        row = _row(window, 1)
        assert row["detail"].text() == "Finishing…"
        assert not row["pause"].isEnabled() and row["cancel"].isEnabled()
        _progress(a, 90)
        assert row["detail"].text() == "Finishing…"
        row["cancel"].click()
        assert a.calls == [("cancel", False)]


def test_completed_commit_wins_a_late_cancel_without_deleting_output():
    with _window() as window:
        a, _ = _enqueue(window)
        row = _row(window, 1)
        path = window.output_dir / "complete-fixture.mp4"
        path.write_bytes(b"complete ordinary fixture")
        row["cancel"].click()
        a.item_state.emit(0, "completed")
        a.item_finished.emit(0, str(path))
        a.all_done.emit()
        assert path.read_bytes() == b"complete ordinary fixture"
        assert row["detail"].text() == "Complete" and row["percent"].text() == "100%"
        assert not row["pause"].isEnabled() and not row["cancel"].isEnabled()
        assert window._download_failed_count == 0


def test_rejected_cancel_after_committed_output_does_not_claim_cancelling():
    with _window() as window:
        a, _ = _enqueue(window)
        a.cancel_accepted = False
        row = _row(window, 1)
        _progress(a, 98)
        row["cancel"].click()
        assert window._download_tasks[1]["state"] == "downloading"
        assert not window._download_tasks[1].get("cancel_requested")
        assert row["detail"].text() != "Cancelling…" and row["percent"].text() == "98%"
        a.item_state.emit(0, "completed")
        a.item_finished.emit(0, str(window.output_dir / "committed-fixture.mp4"))
        a.all_done.emit()
        assert row["detail"].text() == "Complete" and row["percent"].text() == "100%"


def test_hidden_cancel_all_clears_queue_and_never_requests_partial_merge():
    with _window() as window:
        a, b = _enqueue(window, 3)
        window.stop_all_downloads()
        assert window._download_queue == [] and 3 not in window._download_tasks
        assert a.calls == b.calls == [("cancel", False)]
        for task_id in (1, 2):
            row = _row(window, task_id)
            assert row["detail"].text() == "Cancelling…"
            assert not row["cancel"].isEnabled() and not row["pause"].isEnabled()


def test_failure_state_before_error_counts_once_and_keeps_failed_row():
    with _window() as window:
        a, _ = _enqueue(window)
        row = _row(window, 1)
        a.item_state.emit(0, "failed")
        a.item_failed.emit(0, "Synthetic network failure")
        a.item_failed.emit(0, "Duplicate failure")
        a.all_done.emit()
        assert row["detail"].text() == "Failed"
        assert window._download_failed_count == 1


def test_cancelled_or_silent_exit_never_becomes_complete_at_one_hundred():
    with _window() as window:
        a, b = _enqueue(window)
        row_a, row_b = _row(window, 1), _row(window, 2)
        row_a["cancel"].click()
        a.all_done.emit()
        b.all_done.emit()
        assert row_a["detail"].text() == "Cancelled" and row_a["percent"].text() == "0%"
        assert row_b["detail"].text() == "Stopped" and row_b["percent"].text() == "0%"
        assert window.progress_bar.format() == "Idle"


def test_close_requests_cancel_without_waiting_or_terminating_gui_threads():
    with _window() as window:
        a, b = _enqueue(window)
        a.running = b.running = True
        a.wait_result = b.wait_result = False
        event = QCloseEvent()
        window.closeEvent(event)
        assert not event.isAccepted()
        assert a.calls[:2] == [("cancel", False), "interrupt"]
        assert b.calls[:2] == [("cancel", False), "interrupt"]
        assert a.calls == b.calls == [("cancel", False), "interrupt"]
        assert window._closing and window._close_retry_pending
        a.running = b.running = False
        window._retry_close_after_downloads()
        assert window.media_proxy.stopped


if __name__ == "__main__":
    tests = [value for name, value in globals().copy().items()
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"Passed {len(tests)} per-download GUI checks")
