"""Modal dialog for editing application preferences."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QSize, Qt
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..settings import (
    PLAYBACK_BUFFER_MAX,
    PLAYBACK_BUFFER_MIN,
    SettingsStore,
)
from .icons import IconSpinBox, make_icon


class SettingsDialog(QDialog):
    """Lets the user tweak the playback buffer (and any future settings)."""

    def __init__(self, store: SettingsStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self.save_button = None
        self.cancel_button = None
        self.setWindowTitle("Preferences")
        self.setObjectName("PreferencesDialog")
        self.setModal(True)
        self.setMinimumWidth(480)

        current = store.get()

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 18)
        root.setSpacing(18)

        header = QHBoxLayout()
        header.setSpacing(12)
        self.header_icon = QLabel()
        self.header_icon.setObjectName("PreferencesIcon")
        self.header_icon.setFixedSize(28, 28)
        self.header_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.header_icon)
        headings = QVBoxLayout()
        headings.setSpacing(3)
        title = QLabel("Preferences")
        title.setProperty("role", "preferencesTitle")
        headings.addWidget(title)
        subtitle = QLabel("Playback and temporary cache")
        subtitle.setProperty("role", "muted")
        headings.addWidget(subtitle)
        header.addLayout(headings, stretch=1)
        root.addLayout(header)

        card = QFrame()
        card.setObjectName("PreferencesCard")
        body = QVBoxLayout(card)
        body.setContentsMargins(16, 16, 16, 16)
        body.setSpacing(14)
        section_title = QLabel("Playback")
        section_title.setProperty("role", "title")
        body.addWidget(section_title)
        intro = QLabel("Keep upcoming video ready for playback.")
        intro.setProperty("role", "muted")
        intro.setWordWrap(True)
        body.addWidget(intro)

        field = QHBoxLayout()
        field.setSpacing(16)
        labels = QVBoxLayout()
        labels.setSpacing(4)
        buffer_label = QLabel("Playback buffer")
        buffer_label.setProperty("role", "title")
        labels.addWidget(buffer_label)
        limits = QLabel(f"{PLAYBACK_BUFFER_MIN}–{PLAYBACK_BUFFER_MAX} seconds")
        limits.setProperty("role", "dim")
        labels.addWidget(limits)
        field.addLayout(labels, stretch=1)

        self.buffer_spin = IconSpinBox()
        self.buffer_spin.setRange(PLAYBACK_BUFFER_MIN, PLAYBACK_BUFFER_MAX)
        self.buffer_spin.setSingleStep(5)
        self.buffer_spin.setSuffix(" s")
        self.buffer_spin.setValue(int(current.playback_buffer_seconds))
        self.buffer_spin.setFixedSize(132, 38)
        self.buffer_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.buffer_spin.setAccessibleName("Playback buffer in seconds")
        field.addWidget(self.buffer_spin)
        body.addLayout(field)

        hint = QLabel("Larger buffers use more temporary disk space.\nSet to 0 to disable read-ahead.")
        hint.setWordWrap(True)
        hint.setProperty("role", "dim")
        body.addWidget(hint)
        root.addWidget(card)

        note = QHBoxLayout()
        note.setSpacing(8)
        self.cache_icon = QLabel()
        self.cache_icon.setFixedSize(18, 18)
        note.addWidget(self.cache_icon, alignment=Qt.AlignmentFlag.AlignTop)
        cache_note = QLabel("Temporary cache is cleared on normal app exit.")
        cache_note.setWordWrap(True)
        cache_note.setProperty("role", "dim")
        note.addWidget(cache_note, stretch=1)
        root.addLayout(note)

        root.addStretch(1)

        separator = QFrame()
        separator.setObjectName("HSep")
        separator.setFixedHeight(1)
        root.addWidget(separator)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.save_button = ok
        if ok is not None:
            ok.setObjectName("Primary")
            ok.setCursor(Qt.CursorShape.PointingHandCursor)
            ok.setText("Save")
            ok.setDefault(True)
        cancel = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button = cancel
        if cancel is not None:
            cancel.setObjectName("Ghost")
            cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        # The button box may polish its buttons before we assign style names.
        for button in (ok, cancel):
            if button is not None:
                button.setFixedHeight(38)
                button.setMinimumWidth(104)
                button.setIconSize(QSize(18, 18))
                button.style().unpolish(button)
                button.style().polish(button)
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)
        self._refresh_icons()
        self.resize(520, self.sizeHint().height())

    def _refresh_icons(self) -> None:
        text = self.palette().color(QPalette.ColorRole.WindowText).name()
        primary_text = self.palette().color(QPalette.ColorRole.HighlightedText).name()
        self.header_icon.setPixmap(make_icon("settings", text, 24).pixmap(QSize(24, 24), self.devicePixelRatioF()))
        self.cache_icon.setPixmap(make_icon("about", text, 16).pixmap(QSize(16, 16), self.devicePixelRatioF()))
        if self.save_button is not None:
            self.save_button.setIcon(make_icon("check", primary_text))
        if self.cancel_button is not None:
            self.cancel_button.setIcon(make_icon("close", text))

    def changeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange) and hasattr(self, "cache_icon"):
            self._refresh_icons()

    def _on_accept(self) -> None:
        settings = self._store.get()
        settings.playback_buffer_seconds = self.buffer_spin.value()
        self._store.update(settings)
        self.accept()
