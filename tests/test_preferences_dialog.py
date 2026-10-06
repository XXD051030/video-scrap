"""Offline Preferences checks using real Qt controls and isolated settings.

Run directly: ``build/.venv/bin/python tests/test_preferences_dialog.py``.
These checks exercise Save and dismissal through the UI, including a theme
change made after the dialog opens. They never read or write user preferences.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QStyle,
    QStyleOptionSpinBox,
)

from src import settings
from src.gui.settings_dialog import SettingsDialog
from src.gui.style import build_palette, build_stylesheet, get_theme


APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


def _flush() -> None:
    APP.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APP.processEvents()


class PreferencesDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="preferences-ui-")
        self.path = Path(self.directory.name) / "settings.json"
        self.path_patch = mock.patch.object(settings, "SETTINGS_PATH", self.path)
        self.path_patch.start()
        self.store = settings.SettingsStore(settings.AppSettings(
            playback_buffer_seconds=60, theme=settings.THEME_DARK,
        ))
        self.store.get().save()
        self.before = self.path.read_bytes()
        self.notifications: list[settings.AppSettings] = []
        self.store.subscribe(self.notifications.append, fire_initial=False)
        self.dialogs: list[SettingsDialog] = []
        self.original_palette = APP.palette()
        self.original_stylesheet = APP.styleSheet()

    def tearDown(self) -> None:
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        _flush()
        APP.setStyleSheet(self.original_stylesheet)
        APP.setPalette(self.original_palette)
        self.path_patch.stop()
        self.directory.cleanup()

    def _open(self, theme: str = settings.THEME_DARK) -> SettingsDialog:
        colors = get_theme(theme)
        APP.setPalette(build_palette(colors))
        APP.setStyleSheet(build_stylesheet(colors))
        dialog = SettingsDialog(self.store)
        self.dialogs.append(dialog)
        dialog.show()
        _flush()
        self.assertTrue(dialog.isVisible())
        return dialog

    def _button(self, dialog: SettingsDialog, standard):
        boxes = dialog.findChildren(QDialogButtonBox)
        self.assertEqual(len(boxes), 1)
        button = boxes[0].button(standard)
        self.assertIsNotNone(button)
        return button

    def _edit(self, dialog: SettingsDialog, value: int) -> None:
        edit = dialog.buffer_spin.lineEdit()
        edit.setFocus()
        edit.selectAll()
        QTest.keyClicks(edit, str(value))
        _flush()

    def _save(self, dialog: SettingsDialog) -> None:
        QTest.mouseClick(
            self._button(dialog, QDialogButtonBox.StandardButton.Ok),
            Qt.MouseButton.LeftButton,
        )
        _flush()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertFalse(dialog.isVisible())

    def test_save_commits_typed_value_once_and_reopens_with_saved_value(self) -> None:
        dialog = self._open()
        self.assertEqual(dialog.buffer_spin.value(), 60)
        self._edit(dialog, 125)
        self._save(dialog)
        expected = settings.AppSettings(playback_buffer_seconds=125, theme="dark")
        self.assertEqual(self.store.get(), expected)
        self.assertEqual(settings.AppSettings.load(), expected)
        self.assertEqual(self.notifications, [expected])
        reopened = self._open()
        self.assertEqual(reopened.buffer_spin.value(), 125)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_save_preserves_theme_changed_after_dialog_opens(self) -> None:
        dialog = self._open()
        changed = self.store.get()
        changed.theme = settings.THEME_LIGHT
        self.store.update(changed)
        self.notifications.clear()
        self._edit(dialog, 80)
        self._save(dialog)
        expected = settings.AppSettings(playback_buffer_seconds=80, theme="light")
        self.assertEqual(self.store.get(), expected)
        self.assertEqual(settings.AppSettings.load(), expected)
        self.assertEqual(self.notifications, [expected])

    def test_enter_in_buffer_editor_saves_the_typed_value_once(self) -> None:
        dialog = self._open()
        self._edit(dialog, 85)
        QTest.keyClick(dialog.buffer_spin.lineEdit(), Qt.Key.Key_Return)
        _flush()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertFalse(dialog.isVisible())
        expected = settings.AppSettings(playback_buffer_seconds=85, theme="dark")
        self.assertEqual(self.store.get(), expected)
        self.assertEqual(settings.AppSettings.load(), expected)
        self.assertEqual(self.notifications, [expected])

    def test_cancel_leaves_existing_settings_and_listeners_untouched(self) -> None:
        dialog = self._open()
        self._edit(dialog, 195)
        QTest.mouseClick(
            self._button(dialog, QDialogButtonBox.StandardButton.Cancel),
            Qt.MouseButton.LeftButton,
        )
        _flush()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertFalse(dialog.isVisible())
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(self.store.get().playback_buffer_seconds, 60)
        self.assertEqual(self.notifications, [])

    def test_escape_dismisses_without_persisting_edits(self) -> None:
        dialog = self._open()
        self._edit(dialog, 200)
        QTest.keyClick(dialog.buffer_spin, Qt.Key.Key_Escape)
        _flush()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertFalse(dialog.isVisible())
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(self.store.get().playback_buffer_seconds, 60)
        self.assertEqual(self.notifications, [])

    def test_window_close_dismisses_without_persisting_edits(self) -> None:
        dialog = self._open()
        self._edit(dialog, 205)
        dialog.close()
        _flush()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertFalse(dialog.isVisible())
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(self.store.get().playback_buffer_seconds, 60)
        self.assertEqual(self.notifications, [])

    def test_keyboard_input_accepts_and_persists_both_buffer_endpoints(self) -> None:
        for value in (settings.PLAYBACK_BUFFER_MIN, settings.PLAYBACK_BUFFER_MAX):
            with self.subTest(value=value):
                dialog = self._open()
                self._edit(dialog, value)
                self._save(dialog)
                self.assertEqual(self.store.get().playback_buffer_seconds, value)
                self.assertEqual(settings.AppSettings.load().playback_buffer_seconds, value)

    def test_keyboard_and_arrow_clicks_keep_five_second_step_and_bounds(self) -> None:
        dialog = self._open()
        spin = dialog.buffer_spin
        self.assertEqual(spin.minimum(), 0)
        self.assertEqual(spin.maximum(), 300)
        self.assertEqual(spin.suffix(), " s")
        spin.setValue(0)
        QTest.keyClick(spin, Qt.Key.Key_Up)
        self.assertEqual(spin.value(), 5)
        QTest.keyClick(spin, Qt.Key.Key_Down)
        self.assertEqual(spin.value(), 0)
        QTest.keyClick(spin, Qt.Key.Key_Down)
        self.assertEqual(spin.value(), 0)
        spin.setValue(300)
        QTest.keyClick(spin, Qt.Key.Key_Up)
        self.assertEqual(spin.value(), 300)
        QTest.keyClick(spin, Qt.Key.Key_Down)
        self.assertEqual(spin.value(), 295)
        option = QStyleOptionSpinBox()
        spin.initStyleOption(option)
        for control, expected in (
            (QStyle.SubControl.SC_SpinBoxUp, 300),
            (QStyle.SubControl.SC_SpinBoxDown, 295),
        ):
            rect = spin.style().subControlRect(
                QStyle.ComplexControl.CC_SpinBox, option, control, spin,
            )
            self.assertTrue(rect.isValid(), "The visible stepper must have a click target")
            QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=rect.center())
            _flush()
            self.assertEqual(spin.value(), expected)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(self.notifications, [])

    def test_dark_and_light_dialogs_fit_header_card_and_footer(self) -> None:
        for theme in (settings.THEME_DARK, settings.THEME_LIGHT):
            with self.subTest(theme=theme):
                self.store = settings.SettingsStore(settings.AppSettings(
                    playback_buffer_seconds=60, theme=theme,
                ))
                self.store.subscribe(self.notifications.append, fire_initial=False)
                dialog = self._open(theme)
                dialog.resize(dialog.minimumSizeHint())
                _flush()
                save = self._button(dialog, QDialogButtonBox.StandardButton.Ok)
                cancel = self._button(dialog, QDialogButtonBox.StandardButton.Cancel)
                self.assertEqual(save.text(), "Save")
                self.assertEqual(save.objectName(), "Primary")
                self.assertEqual(save.height(), 38)
                self.assertEqual(cancel.height(), 38)
                labels = [label for label in dialog.findChildren(QLabel) if label.isVisible()]
                self.assertTrue(any(label.text() == "Preferences" for label in labels))
                self.assertTrue(any(label.text() == "Playback" for label in labels))
                self.assertTrue(any(not label.pixmap().isNull() for label in labels),
                                "The header should use the same line icon as the toolbar")
                for widget in [dialog.buffer_spin, save, cancel, *labels]:
                    self.assertTrue(widget.isVisible())
                    point = widget.mapTo(dialog, QPoint())
                    self.assertGreaterEqual(point.x(), 0)
                    self.assertGreaterEqual(point.y(), 0)
                    self.assertLessEqual(point.x() + widget.width(), dialog.width())
                    self.assertLessEqual(point.y() + widget.height(), dialog.height())
                spin_bottom = dialog.buffer_spin.mapTo(dialog, QPoint(0, dialog.buffer_spin.height())).y()
                self.assertGreaterEqual(save.mapTo(dialog, QPoint()).y(), spin_bottom)
                self.assertTrue(dialog.grab().save(str(Path(self.directory.name) / f"{theme}.png")))
                dialog.close()
                _flush()
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(self.notifications, [])


if __name__ == "__main__":
    unittest.main()
