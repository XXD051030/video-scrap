"""Application entry point."""

from __future__ import annotations

import atexit
from pathlib import Path
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox

from src.gui.main_window import MainWindow
from src.settings import SETTINGS_PATH
from src.single_instance import InstanceRole, SingleInstance


def main() -> int:
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
            _safe_stop_proxy(window)
        instance.close()


def _activate_window(window: MainWindow) -> None:
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


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--smoke-test":
        from scripts.smoke_app import run_smoke_test

        sys.exit(run_smoke_test(sys.argv[2]))
    sys.exit(main())
