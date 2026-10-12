"""Targeted shutdown, QThread ownership, and cleanup-error GUI regressions.

All checks run offline; the thread fixtures never start a media download.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEvent, QThread, pyqtSignal

import main
from src.gui import main_window
import test_download_controls as controls


APP = controls.APP


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        APP.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for the offline QThread fixture")
        time.sleep(0.005)
    APP.processEvents()


class _DrainThread(QThread):
    def __init__(self):
        super().__init__()
        self.release = threading.Event()
        self.entered = threading.Event()
        self.cancel_count = 0
        self.waits = []
        self.timer = None

    def run(self):
        self.entered.set()
        self.release.wait(3)

    def cancel(self):
        self.cancel_count += 1
        self.timer = threading.Timer(0.15, self.release.set)
        self.timer.start()

    def wait(self, timeout):
        self.waits.append(timeout)
        return super().wait(timeout)


def test_exit_cleanup_drains_threads_without_a_running_main_loop_even_if_already_closing():
    # Deliberately never call QApplication.exec(). Completion is dispatched
    # by the exit helper itself rather than a closeEvent timer.
    for closing in (False, True):
        workers = [_DrainThread(), _DrainThread()]
        for worker in workers:
            worker.start()
            assert worker.entered.wait(1)
        closed = []
        events = []
        def close_window():
            assert all(not worker.isRunning() for worker in workers)
            closed.append(True)
        def dispatch_events():
            events.append(True)
            APP.processEvents()
        window = SimpleNamespace(
            _closing=closing, download_workers=dict(enumerate(workers)), close=close_window,
        )
        try:
            main._finish_download_shutdown(window, SimpleNamespace(processEvents=dispatch_events))
            assert closed == [True] and events
            assert all(worker.cancel_count == 1 for worker in workers)
            assert all(worker.waits for worker in workers)
            assert all(not worker.isRunning() for worker in workers)
        finally:
            for worker in workers:
                worker.release.set()
                worker.wait(2000)
                if worker.timer is not None:
                    worker.timer.join()
                worker.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


class _DeletedThread(QThread):
    def __init__(self):
        super().__init__()
        self.cancel_count = 0
        self.wait_count = 0

    def cancel(self):
        self.cancel_count += 1

    def wait(self, timeout):
        self.wait_count += 1
        raise AssertionError("A deleted completed QThread must not be waited on")


def test_exit_cleanup_does_not_wait_for_an_already_deferred_deleted_qthread():
    worker = _DeletedThread()
    worker.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    try:
        worker.isRunning()
    except RuntimeError:
        pass
    else:
        raise AssertionError("Fixture did not receive its deferred QObject deletion")
    closed = []
    window = SimpleNamespace(
        _closing=True, download_workers={1: worker}, close=lambda: closed.append(True),
    )
    main._finish_download_shutdown(window, APP)
    assert worker.cancel_count == 1 and worker.wait_count == 0
    assert closed == [True]


class _EarlyDoneThread(QThread):
    item_started = pyqtSignal(int, object)
    item_progress = pyqtSignal(int, object)
    item_state = pyqtSignal(int, str)
    item_control_error = pyqtSignal(int, str)
    item_finished = pyqtSignal(int, str)
    item_failed = pyqtSignal(int, str)
    all_done = pyqtSignal()
    instances = []

    def __init__(self, items, output_dir, **kwargs):
        super().__init__(kwargs.get("parent"))
        self.items = items
        self.release = threading.Event()
        self.sent_done = threading.Event()
        self.delete_calls = 0
        self.instances.append(self)

    def run(self):
        self.item_started.emit(0, self.items[0])
        self.item_state.emit(0, "downloading")
        self.all_done.emit()
        self.sent_done.set()
        self.release.wait(5)

    def cancel(self, keep_partial=False):
        self.release.set()
        return True

    def pause(self):
        return False

    def resume(self):
        return False

    def deleteLater(self):
        self.delete_calls += 1
        return super().deleteLater()


def test_custom_all_done_does_not_release_or_delete_a_real_qthread_before_finished():
    _EarlyDoneThread.instances = []
    with controls._window() as window:
        with mock.patch.object(main_window, "DownloadWorker", _EarlyDoneThread):
            try:
                for index in range(3):
                    window.video_list.set_videos([controls._item(f"Offline thread {index}")])
                    window.start_download()
                a, b = _EarlyDoneThread.instances
                _wait_for(lambda: a.sent_done.is_set() and b.sent_done.is_set())
                assert a.isRunning() and b.isRunning()
                assert window.download_workers == {1: a, 2: b}
                assert window._download_slot_tasks == [1, 2]
                assert window._download_queue == [3]
                assert len(_EarlyDoneThread.instances) == 2
                assert a.delete_calls == b.delete_calls == 0
                a.release.set()
                assert a.wait(2000)
                _wait_for(lambda: len(_EarlyDoneThread.instances) == 3)
                c = _EarlyDoneThread.instances[2]
                assert window.download_workers == {2: b, 3: c}
                assert window._download_slot_tasks == [3, 2]
                assert a.delete_calls == 1 and b.delete_calls == c.delete_calls == 0
                assert window._download_queue == []
            finally:
                for worker in _EarlyDoneThread.instances:
                    worker.release.set()
                for worker in _EarlyDoneThread.instances:
                    try:
                        worker.wait(2000)
                    except RuntimeError:
                        pass
                APP.processEvents()


def test_cleanup_failure_during_cancel_remains_visible_and_counts_only_once():
    with controls._window() as window:
        a, b = controls._enqueue(window)
        controls._progress(a, 46)
        row = controls._row(window, 1)
        row["cancel"].click()
        assert row["detail"].text() == "Cancelling…"
        a.item_state.emit(0, "failed")
        a.item_control_error.emit(0, "Synthetic process cleanup refusal; private folder retained")
        a.item_control_error.emit(0, "Duplicate cleanup notification")
        a.item_failed.emit(0, "Cancelled")
        a.item_state.emit(0, "cancelled")
        a.item_finished.emit(0, str(window.output_dir / "uncommitted-fixture.mp4"))
        assert row["detail"].text() == "Cleanup failed"
        assert row["percent"].text() == "46%"
        assert window._download_failed_count == 1
        assert window._download_tasks[1]["control_failed"]
        assert window._download_tasks[1]["state"] == "failed"
        assert not window._download_tasks[1]["cancelled"]
        assert not row["pause"].isEnabled() and not row["cancel"].isEnabled()
        controls._progress(b, 79)
        assert controls._row(window, 2)["percent"].text() == "79%"
        a.all_done.emit()
        assert row["detail"].text() == "Cleanup failed" and row["percent"].text() == "46%"
        assert window._download_failed_count == 1 and 2 in window.download_workers
        assert "Could not clean up download" in window.log_view.toPlainText()


if __name__ == "__main__":
    tests = [value for name, value in globals().copy().items()
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"Passed {len(tests)} download shutdown checks")
