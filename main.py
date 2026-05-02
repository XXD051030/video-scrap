"""Application entry point."""

from __future__ import annotations

import atexit
import sys

from PyQt6.QtWidgets import QApplication

from src.gui.main_window import MainWindow
from src.gui.style import build_stylesheet


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Video Scraper")
    app.setStyle("Fusion")
    app.setStyleSheet(build_stylesheet())

    window = MainWindow()
    atexit.register(_safe_stop_proxy, window)
    window.show()
    return app.exec()


def _safe_stop_proxy(window: MainWindow) -> None:
    try:
        window.media_proxy.stop()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
