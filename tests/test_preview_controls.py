"""Offline regression checks for fullscreen ownership and preview-only audio.

Run directly with ``.venv/bin/python tests/test_preview_controls.py``.
Actual video rendering across fullscreen switches is covered by smoke_app.py.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, Qt, QUrl
from PyQt6.QtMultimedia import QMediaPlayer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from src.gui.preview_panel import PreviewPanel
from src.gui.style import DARK, LIGHT, build_palette, build_stylesheet
from src.scraper import VideoItem


APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


def _flush_events() -> None:
    APP.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APP.processEvents()


def _dispose(panel: PreviewPanel) -> None:
    panel.shutdown()
    panel.close()
    panel.deleteLater()
    _flush_events()


def _selected_panel() -> PreviewPanel:
    panel = PreviewPanel()
    panel.resize(640, 480)
    panel.show()
    panel.show_video(VideoItem(
        title="Preview fixture",
        url="https://example.invalid/sample.mp4",
        source_url="https://example.invalid/watch",
        is_direct=True,
    ))
    _flush_events()
    return panel


def _wait_until(predicate, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert predicate(), "Qt media state did not arrive before the deadline"


def test_volume_is_local_and_mute_keeps_the_selected_level() -> None:
    first, second = PreviewPanel(), PreviewPanel()
    try:
        assert first.volume_slider.value() == 60
        assert abs(first.audio_output.volume() - 0.6) < 0.001
        assert not first.audio_output.isMuted()
        assert first.volume_slider.isEnabled()
        first.volume_slider.setValue(23)
        assert abs(first.audio_output.volume() - 0.23) < 0.001
        assert first.volume_label.text() == "23%"
        assert abs(second.audio_output.volume() - 0.6) < 0.001
        first.mute_button.click()
        assert first.audio_output.isMuted()
        assert first.volume_slider.value() == 23
        assert abs(first.audio_output.volume() - 0.23) < 0.001
        first.mute_button.click()
        assert not first.audio_output.isMuted()
        assert first.volume_slider.value() == 23
        first.mute_button.click()
        first.volume_slider.setValue(71)
        assert not first.audio_output.isMuted()
        assert not first.mute_button.isChecked()
        first.volume_slider.setValue(0)
        assert first.audio_output.volume() == 0
        # Changes made directly to QAudioOutput are also reflected in controls.
        first.audio_output.setVolume(0.38)
        first.audio_output.setMuted(True)
        assert first.volume_slider.value() == 38
        assert first.volume_label.text() == "38%"
        assert first.mute_button.isChecked()
        assert first.mute_button.text() == "Unmute"
        assert not second.audio_output.isMuted()
    finally:
        _dispose(first)
        _dispose(second)


def test_fullscreen_does_not_load_a_pending_source_or_move_metadata() -> None:
    panel = _selected_panel()
    try:
        identities = panel.player, panel.audio_output, panel.video_widget
        pending = panel._pending_playable_url
        panel.fullscreen_button.click()
        _flush_events()
        fullscreen = panel.activation_window()
        assert panel.is_fullscreen()
        assert fullscreen.isFullScreen()
        assert panel.player_host.parentWidget() is fullscreen
        assert panel.title_label.window() is panel
        assert panel.meta_label.window() is panel
        assert panel.player.source().isEmpty()
        assert not panel._source_loaded
        assert panel._pending_playable_url == pending
        assert (panel.player, panel.audio_output, panel.video_widget) == identities
        assert panel.player.videoOutput() is panel.video_widget
        assert panel.play_button.window() is fullscreen
        assert panel.volume_slider.window() is fullscreen
        panel.volume_slider.setValue(42)
        panel.fullscreen_button.click()
        _flush_events()
        assert not panel.is_fullscreen()
        assert panel.activation_window() is panel
        assert panel.player_host.parentWidget() is panel
        assert panel.volume_slider.value() == 42
        assert abs(panel.audio_output.volume() - 0.42) < 0.001
        assert sip.isdeleted(fullscreen)
    finally:
        _dispose(panel)


def test_empty_preview_enters_fullscreen_without_a_link_or_media() -> None:
    panel = PreviewPanel()
    panel.resize(640, 480)
    panel.show()
    try:
        assert panel.fullscreen_button.isEnabled()
        assert not panel.play_button.isEnabled()
        assert not panel.stop_button.isEnabled()
        assert not panel.position_slider.isEnabled()
        for _ in range(2):
            panel.fullscreen_button.click()
            _flush_events()
            assert panel.is_fullscreen()
            assert panel.thumbnail_label.window() is panel.activation_window()
            assert "Select a video" in panel.thumbnail_label.text()
            assert panel.player.source().isEmpty()
            assert not panel._source_loaded
            assert panel.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
            assert panel.volume_slider.isEnabled()
            QTest.keyClick(panel.fullscreen_button, Qt.Key.Key_Escape)
            _flush_events()
            assert not panel.is_fullscreen()
            assert panel.fullscreen_button.isEnabled()
            assert panel.player.source().isEmpty()
        QTest.mouseDClick(panel.thumbnail_label, Qt.MouseButton.LeftButton)
        _flush_events()
        assert panel.is_fullscreen()
    finally:
        _dispose(panel)


def test_unplayable_selection_and_media_error_keep_fullscreen_available() -> None:
    panel = PreviewPanel()
    panel.resize(640, 480)
    panel.show()
    try:
        panel.show_video(VideoItem(
            title="No preview format",
            url="https://example.invalid/page",
            source_url="https://example.invalid/page",
        ))
        assert panel._pending_playable_url is None
        assert not panel.play_button.isEnabled()
        assert panel.fullscreen_button.isEnabled()
        panel.fullscreen_button.click()
        _flush_events()
        assert panel.is_fullscreen()
        assert panel.player.source().isEmpty()
        panel._on_player_error(QMediaPlayer.Error.FormatError, "Unsupported fixture")
        assert panel.fullscreen_button.isEnabled()
        panel.fullscreen_button.click()
        _flush_events()
        assert not panel.is_fullscreen()
        assert panel.fullscreen_button.isEnabled()
        assert panel.player.source().isEmpty()
    finally:
        _dispose(panel)


def test_paused_source_position_and_player_survive_fullscreen_roundtrip() -> None:
    panel = _selected_panel()
    try:
        with tempfile.TemporaryDirectory(prefix="preview-controls-") as directory:
            path = Path(directory) / "silence.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(8000)
                wav.writeframes(bytes(8000 * 2 * 3))
            panel.player.setSource(QUrl.fromLocalFile(str(path)))
            panel.player.play()
            _wait_until(lambda: panel.player.duration() > 0)
            panel.player.pause()
            panel.player.setPosition(900)
            _wait_until(lambda: panel.player.position() == 900)
            source, position = panel.player.source(), panel.player.position()
            player, audio = panel.player, panel.audio_output
            for _ in range(3):
                panel.fullscreen_button.click()
                _flush_events()
                assert panel.player is player and panel.audio_output is audio
                assert panel.player.source() == source
                assert panel.player.position() == position
                assert panel.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
                panel.fullscreen_button.click()
                _flush_events()
                assert panel.player.source() == source
                assert panel.player.position() == position
                assert panel.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
            panel.release_stream()
    finally:
        _dispose(panel)


def test_escape_window_close_and_double_click_return_controls() -> None:
    panel = _selected_panel()
    try:
        panel.fullscreen_button.click()
        _flush_events()
        window = panel.activation_window()
        QTest.keyClick(panel.fullscreen_button, Qt.Key.Key_Escape)
        _flush_events()
        assert not panel.is_fullscreen()
        assert sip.isdeleted(window)
        QTest.mouseDClick(panel.thumbnail_label, Qt.MouseButton.LeftButton)
        _flush_events()
        assert panel.is_fullscreen()
        panel.activation_window().close()
        _flush_events()
        assert not panel.is_fullscreen()
        assert panel.player_host.parentWidget() is panel
        assert panel.fullscreen_button.text() == "Full screen"
    finally:
        _dispose(panel)


def test_clearing_selection_keeps_fullscreen_available_after_exit() -> None:
    panel = _selected_panel()
    try:
        panel.fullscreen_button.click()
        panel.show_video(None)
        assert panel.fullscreen_button.isEnabled()
        assert not panel.play_button.isEnabled()
        panel.fullscreen_button.click()
        _flush_events()
        assert not panel.is_fullscreen()
        assert panel.fullscreen_button.isEnabled()
        panel.fullscreen_button.click()
        _flush_events()
        assert panel.is_fullscreen()
        assert panel.player.source().isEmpty()
        assert not panel.play_button.isEnabled()
    finally:
        _dispose(panel)


def test_returning_to_thumbnail_does_not_focus_the_passive_label() -> None:
    panel = _selected_panel()
    try:
        for fullscreen in (False, True):
            panel._show_video_view()
            if fullscreen:
                panel.fullscreen_button.click()
                _flush_events()
            # The QWindowContainer inside QVideoWidget can acquire focus
            # when Qt reparents the native video surface into fullscreen.
            panel.video_widget.setFocus()
            _flush_events()
            focused = panel.activation_window().focusWidget()
            assert focused is panel.video_widget or panel.video_widget.isAncestorOf(focused)
            panel._show_thumbnail_view()
            _flush_events()
            assert panel.activation_window().focusWidget() is panel.fullscreen_button
            assert not panel.thumbnail_label.hasFocus()
            panel._exit_fullscreen()
            _flush_events()
        panel._set_controls_enabled(False)
        panel._show_video_view()
        panel.video_widget.setFocus()
        _flush_events()
        panel._show_thumbnail_view()
        _flush_events()
        assert panel.focusWidget() is panel.fullscreen_button
        assert not panel.thumbnail_label.hasFocus()
    finally:
        _dispose(panel)


def test_fullscreen_follows_application_theme_and_cleans_up_on_close() -> None:
    panel = _selected_panel()
    try:
        panel.fullscreen_button.click()
        window = panel.activation_window()
        for theme in (DARK, LIGHT):
            APP.setPalette(build_palette(theme))
            APP.setStyleSheet(build_stylesheet(theme))
            panel.apply_theme(theme)
            _flush_events()
            assert window.palette().color(window.backgroundRole()).name() == theme.bg
            assert panel.player_host.window() is window
        panel.close()
        _flush_events()
        assert not panel.is_fullscreen()
        assert sip.isdeleted(window)
        assert panel.player_host.parentWidget() is panel
        panel.show()
        panel.fullscreen_button.click()
        _flush_events()
        window = panel.activation_window()
        panel.shutdown()
        _flush_events()
        assert not panel.is_fullscreen()
        assert sip.isdeleted(window)
        assert panel.player.source().isEmpty()
    finally:
        _dispose(panel)
        APP.setStyleSheet("")


if __name__ == "__main__":
    tests = [value for name, value in globals().copy().items()
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"Passed {len(tests)} preview control checks")
