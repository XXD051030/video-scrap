"""Modal dialog for editing application preferences."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..settings import (
    PLAYBACK_BUFFER_MAX,
    PLAYBACK_BUFFER_MIN,
    AppSettings,
    SettingsStore,
)
from .style import Tokens


class SettingsDialog(QDialog):
    """Lets the user tweak the playback buffer (and any future settings)."""

    def __init__(self, store: SettingsStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self.setWindowTitle("Preferences")
        self.setModal(True)
        self.setMinimumWidth(420)

        current = store.get()

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(14)

        intro = QLabel(
            "Tune how much of the video the player keeps cached ahead of "
            "the current position. A larger buffer makes seeking smoother "
            "but uses more temporary disk space."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {Tokens.TEXT_MUTED};")
        root.addWidget(intro)

        form = QFormLayout()
        form.setContentsMargins(0, 4, 0, 4)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.buffer_spin = QSpinBox()
        self.buffer_spin.setRange(PLAYBACK_BUFFER_MIN, PLAYBACK_BUFFER_MAX)
        self.buffer_spin.setSingleStep(5)
        self.buffer_spin.setSuffix(" s")
        self.buffer_spin.setValue(int(current.playback_buffer_seconds))
        self.buffer_spin.setMinimumWidth(120)

        buffer_label = QLabel("Playback buffer")
        buffer_label.setStyleSheet("font-weight: 600;")
        form.addRow(buffer_label, self.buffer_spin)
        root.addLayout(form)

        hint = QLabel(
            f"Range {PLAYBACK_BUFFER_MIN}–{PLAYBACK_BUFFER_MAX} seconds. "
            "Set to 0 to disable read-ahead. Cache is stored in a temp "
            "folder and cleared when the app quits."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"color: {Tokens.TEXT_DIM}; font-size: 12px;"
        )
        root.addWidget(hint)

        root.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setObjectName("Primary")
            ok.setCursor(Qt.CursorShape.PointingHandCursor)
            ok.setText("Save")
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel is not None:
            cancel.setObjectName("Ghost")
            cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _on_accept(self) -> None:
        self._store.update(
            AppSettings(playback_buffer_seconds=self.buffer_spin.value())
        )
        self.accept()
