"""Application identity and project homepage."""

from pathlib import Path
import sys

from PyQt6.QtCore import QEvent, QSize, Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from .. import __version__
from .icons import make_icon


GITHUB_URL = "https://github.com/XXD051030/video-scrap"


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Video Scraper")
        self.setObjectName("AboutDialog")
        self.setModal(True)
        self.setMinimumWidth(400)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 28, 28, 24)
        root.setSpacing(12)

        resources = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
        self.logo_label = QLabel()
        self.logo_label.setFixedSize(128, 128)
        self.logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo_label.setAccessibleName("Video Scraper logo")
        ratio = self.devicePixelRatioF()
        logo = QPixmap(str(resources / "Logo.png")).scaled(
            QSize(round(128 * ratio), round(128 * ratio)),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        logo.setDevicePixelRatio(ratio)
        self.logo_label.setPixmap(logo)
        root.addWidget(self.logo_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.title_label = QLabel("Video Scraper")
        self.title_label.setProperty("role", "aboutTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.title_label)
        self.version_label = QLabel(f"Version {__version__}")
        self.version_label.setProperty("role", "muted")
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.version_label)

        self.project_label = QLabel("github.com/XXD051030/video-scrap")
        self.project_label.setProperty("role", "dim")
        self.project_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.project_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.project_label)
        root.addSpacing(8)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.github_button = QPushButton("GitHub project")
        self.github_button.setObjectName("Primary")
        self.github_button.setToolTip(GITHUB_URL)
        self.github_button.setAccessibleName("Open the Video Scraper GitHub project")
        self.github_button.clicked.connect(self._open_github)
        self.close_button = QPushButton("Close")
        self.close_button.setObjectName("Ghost")
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)
        for button in (self.github_button, self.close_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedHeight(38)
            button.setIconSize(QSize(18, 18))
            buttons.addWidget(button)
        root.addLayout(buttons)
        self._refresh_icons()
        self.resize(420, self.sizeHint().height())

    def _open_github(self) -> None:
        if not QDesktopServices.openUrl(QUrl(GITHUB_URL)):
            QMessageBox.warning(self, "Could not open GitHub", f"Open this address in your browser:\n{GITHUB_URL}")

    def _refresh_icons(self) -> None:
        primary_text = self.palette().color(QPalette.ColorRole.HighlightedText).name()
        text = self.palette().color(QPalette.ColorRole.WindowText).name()
        self.github_button.setIcon(make_icon("link", primary_text))
        self.close_button.setIcon(make_icon("close", text))

    def changeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange) and hasattr(self, "close_button"):
            self._refresh_icons()
