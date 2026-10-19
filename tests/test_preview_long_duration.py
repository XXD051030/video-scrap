"""Qt slider coverage for the player's full signed 64-bit millisecond range."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from src.gui.preview_panel import PreviewPanel


APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
MAX_SLIDER = (1 << 31) - 1
THIRTY_DAYS = 30 * 24 * 60 * 60 * 1000


class LongDurationPreviewTests(unittest.TestCase):
    def setUp(self):
        self.panel = PreviewPanel()
        self.panel.resize(640, 480)
        self.panel.show()
        self.panel.position_slider.setEnabled(True)
        APP.processEvents()

    def tearDown(self):
        self.panel.shutdown()
        self.panel.close()
        self.panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()

    def test_duration_position_and_seek_cover_64_bit_milliseconds(self):
        panel = self.panel
        with mock.patch.object(panel.player, "setPosition") as seek:
            for duration in (0, 1, 7_200_123, MAX_SLIDER, MAX_SLIDER + 1,
                             THIRTY_DAYS, (1 << 63) - 1):
                with self.subTest(duration=duration):
                    panel.player.durationChanged.emit(duration)
                    self.assertEqual(panel._duration_ms, duration)
                    self.assertEqual(panel.position_slider.maximum(), min(duration, MAX_SLIDER))
                    for position in (0, duration // 2, max(0, duration - 1), duration):
                        panel.player.positionChanged.emit(position)
                        value = panel.position_slider.value()
                        self.assertGreaterEqual(value, 0)
                        self.assertLessEqual(value, MAX_SLIDER)
                        panel.position_slider.sliderMoved.emit(value)
                        restored = seek.call_args.args[0]
                        resolution = max(1, (duration + MAX_SLIDER - 1) // MAX_SLIDER)
                        self.assertLessEqual(abs(restored - position), resolution)
                        if duration <= MAX_SLIDER:
                            self.assertEqual(restored, position)
                    panel.position_slider.sliderMoved.emit(panel.position_slider.maximum())
                    self.assertEqual(seek.call_args.args[0], duration)

    def test_playback_updates_do_not_override_a_long_media_drag(self):
        panel, slider = self.panel, self.panel.position_slider
        panel._on_duration_changed(THIRTY_DAYS)
        with mock.patch.object(panel.player, "setPosition") as seek:
            y = slider.height() // 2
            QTest.mousePress(slider, Qt.MouseButton.LeftButton,
                             pos=QPoint(slider.width() * 9 // 10, y))
            self.assertTrue(slider.isSliderDown())
            first = slider.value()
            panel._on_position_changed(1000)
            self.assertEqual(slider.value(), first)
            QTest.mouseMove(slider, QPoint(slider.width() - 1, y))
            self.assertGreater(slider.value(), first)
            QTest.mouseRelease(slider, Qt.MouseButton.LeftButton,
                               pos=QPoint(slider.width() - 1, y))
            self.assertFalse(slider.isSliderDown())
            self.assertEqual(seek.call_args.args[0], THIRTY_DAYS)
            panel._on_position_changed(THIRTY_DAYS // 2)
            self.assertLess(abs(slider.value() - MAX_SLIDER // 2), 2)

    def test_duration_change_preserves_the_drag_target_without_seeking(self):
        panel, slider = self.panel, self.panel.position_slider
        panel._on_duration_changed(THIRTY_DAYS)
        slider.setSliderDown(True)
        slider.setSliderPosition(MAX_SLIDER * 3 // 4)
        target = panel._slider_to_position(slider.sliderPosition())
        with mock.patch.object(panel.player, "setPosition") as seek:
            panel._on_duration_changed(THIRTY_DAYS * 2)
            self.assertTrue(slider.isSliderDown())
            self.assertLessEqual(abs(panel._slider_to_position(slider.sliderPosition()) - target), 2)
            seek.assert_not_called()
        slider.setSliderDown(False)

    def test_fullscreen_keeps_long_duration_mapping_and_seek_endpoint(self):
        panel, slider = self.panel, self.panel.position_slider
        panel._on_duration_changed(THIRTY_DAYS)
        panel._on_position_changed(THIRTY_DAYS - 2000)
        player, value = panel.player, slider.value()
        for _ in range(2):
            panel.fullscreen_button.click()
            APP.processEvents()
            self.assertTrue(panel.is_fullscreen())
            self.assertIs(panel.player, player)
            self.assertEqual(slider.value(), value)
            self.assertEqual(slider.maximum(), MAX_SLIDER)
            with mock.patch.object(panel.player, "setPosition") as seek:
                slider.sliderMoved.emit(slider.maximum())
                seek.assert_called_once_with(THIRTY_DAYS)
            panel.fullscreen_button.click()
            APP.processEvents()
            self.assertFalse(panel.is_fullscreen())
            self.assertEqual(slider.value(), value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
