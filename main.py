"""Application entry point."""

from __future__ import annotations

import atexit
import multiprocessing
from pathlib import Path
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.gui.main_window import MainWindow


def main() -> int:
    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication, QMessageBox

    from src.gui.main_window import MainWindow
    from src.settings import SETTINGS_PATH
    from src.single_instance import InstanceRole, SingleInstance

    app = QApplication(sys.argv)
    app.setApplicationName("Video Scraper")
    app.setStyle("Fusion")
    icon_path = Path(__file__).resolve().parent / "Logo.png"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))

    instance = SingleInstance(SETTINGS_PATH.parent, app)
    role = instance.start()
    if role == InstanceRole.SECONDARY:
        return 0
    if role == InstanceRole.ERROR:
        QMessageBox.critical(None, "Video Scraper", instance.error_message)
        return 1

    window = None
    try:
        window = MainWindow()
        atexit.register(_safe_stop_proxy, window)
        instance.activation_requested.connect(lambda: _activate_window(window))
        window.show()
        return app.exec()
    finally:
        if window is not None:
            # Usually closeEvent already ran. Also cover an event-loop exit
            # requested directly, retaining ownership throughout cleanup.
            if not window._closing:
                window.close()
            _finish_download_shutdown(window, app)
            _safe_stop_proxy(window)
        instance.close()


def _activate_window(window: MainWindow) -> None:
    from PyQt6.QtCore import Qt

    target = window.preview_panel.activation_window()
    if target.isMinimized():
        target.setWindowState(target.windowState() & ~Qt.WindowState.WindowMinimized)
    target.show()
    target.raise_()
    target.activateWindow()


def _safe_stop_proxy(window: MainWindow) -> None:
    try:
        window.media_proxy.stop()
    except Exception:
        pass


def _finish_download_shutdown(window: MainWindow, app) -> None:
    """Drain download supervisors even after the main event loop has exited.

    A rejected close schedules a timer while the application is running, but
    app.quit()/an exception can leave that timer without an event loop. Keep
    ownership and dispatch completion signals until the threads have exited.
    """
    workers = list(window.download_workers.values())
    for worker in workers:
        worker.cancel()

    def running(worker) -> bool:
        try:
            return worker.isRunning()
        except RuntimeError:
            # A finished QObject may already have received deferred deletion.
            return False

    while any(running(worker) for worker in workers):
        for worker in workers:
            if running(worker):
                worker.wait(100)
        app.processEvents()
    app.processEvents()
    window.close()


if __name__ == "__main__":
    # Frozen spawn/resource-tracker children must dispatch before Qt startup
    # or the single-instance check can turn a download into another app launch.
    multiprocessing.freeze_support()
    if len(sys.argv) == 3 and sys.argv[1] == "--smoke-test":
        from scripts.smoke_app import run_smoke_test

        sys.exit(run_smoke_test(sys.argv[2]))
    sys.exit(main())
