"""Application entry point."""

from __future__ import annotations

import atexit
from pathlib import Path
import sys

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from src.gui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Video Scraper")
    app.setStyle("Fusion")
    icon_path = Path(__file__).resolve().parent / "Logo.png"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))

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
    if len(sys.argv) == 3 and sys.argv[1] == "--smoke-test":
        from scripts.smoke_app import run_smoke_test

        sys.exit(run_smoke_test(sys.argv[2]))
    sys.exit(main())
