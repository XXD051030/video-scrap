"""Offline Qt checks for the collapsible list and downward-growing logs.

Run directly: ``build/.venv/bin/python tests/test_collapsible_panels.py``.
The display rectangles are mocked so multiple-monitor edges can be checked
without moving windows on the user's desktop or making network requests.
"""

from __future__ import annotations

import sys
import tempfile
import time
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ui_layout import APP, _assert_inside, _fixture, _flush, _window

from PyQt6.QtCore import QRect, QUrl
from PyQt6.QtMultimedia import QMediaPlayer
from PyQt6.QtTest import QTest


def _settle():
    # Visibility and wrapped-caption size changes can schedule another turn.
    for _ in range(3):
        _flush()


def _wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert predicate(), "Qt media state did not arrive before the deadline"


def _assert_frame_inside(window, available):
    frame = window.frameGeometry()
    # Qt's offscreen backend offsets negative requested positions by its 2px
    # synthetic decoration. Keep the check strict on native desktop backends.
    tolerance = 2 if APP.platformName() == "offscreen" and (
        available.x() < 0 or available.y() < 0
    ) else 0
    bounds = available.adjusted(-tolerance, -tolerance, tolerance, tolerance)
    assert bounds.contains(frame), \
        f"Window frame {frame.getRect()} exceeds display {available.getRect()}"


def test_video_list_starts_expanded_without_remembering_last_collapse():
    with _window() as window:
        assert not window._video_list_collapsed
        assert window.video_list.isVisible()
        assert window.collapse_list_button.isVisible()
        assert not window.expand_list_button.isVisible()
        assert window.count_badge.text() == "0"
        window.collapse_list_button.click()
        _settle()
        assert window._video_list_collapsed
    with _window() as reopened:
        assert not reopened._video_list_collapsed
        assert reopened.video_list.isVisible()
        assert reopened.collapse_list_button.isVisible()
        assert not reopened.expand_list_button.isVisible()


def test_leftward_collapse_restores_custom_width_and_preserves_selection_jobs():
    with _window() as window:
        items = [_fixture(), _fixture()]
        window.video_list.set_videos(items)
        window._update_video_count_badge(len(items))
        window.video_list.setCurrentRow(1)
        window.video_splitter.setSizes([330, 950])
        window._download_queue[:] = [17, 19]
        window._download_tasks[17] = {"status": "queued", "video": items[0]}
        _settle()
        previous_left = window.video_splitter.sizes()[0]
        previous_preview_width = window.preview_panel.width()
        previous_window_size = window.size()
        list_widget, player = window.video_list, window.preview_panel.player
        pending = window.preview_panel._pending_playable_url
        queue, tasks = window._download_queue, window._download_tasks
        saved_tasks = dict(tasks)
        for _ in range(3):
            window.collapse_list_button.click()
            _settle()
            assert window._video_list_collapsed
            assert not window.video_list.isVisible()
            assert window.expand_list_button.isVisible()
            assert window.video_splitter.sizes()[0] == 52
            assert window.preview_panel.width() > previous_preview_width
            assert window.size() == previous_window_size
            assert window.video_list is list_widget
            assert window.video_list.videos() == items
            assert window.video_list.currentRow() == 1
            assert window.video_list.current_video() is items[1]
            assert window.preview_panel.player is player
            assert window.preview_panel._pending_playable_url == pending
            assert window._download_queue is queue and queue == [17, 19]
            assert window._download_tasks is tasks and tasks == saved_tasks
            window.expand_list_button.click()
            _settle()
            assert not window._video_list_collapsed
            assert window.video_list.isVisible()
            assert abs(window.video_splitter.sizes()[0] - previous_left) <= 2
            assert window.video_list.current_video() is items[1]
            assert window.preview_panel.player is player
            assert queue == [17, 19] and tasks == saved_tasks


def test_collapsed_count_updates_and_sidebar_icons_follow_both_themes():
    with _window() as window:
        window._apply_theme("dark", persist=False)
        dark_icons = [button.icon().pixmap(18, 18).toImage()
                      for button in (window.collapse_list_button, window.expand_list_button)]
        window.collapse_list_button.click()
        _settle()
        for count in (7, 123, 0):
            window._update_video_count_badge(count)
            _settle()
            assert window.count_badge.text() == str(count)
            assert window.collapsed_count_badge.text() == str(count)
            assert window.collapsed_count_badge.isVisible()
            _assert_inside(window.expand_list_button, window)
            _assert_inside(window.collapsed_count_badge, window)
        window._apply_theme("light", persist=False)
        _settle()
        for button, before in zip(
            (window.collapse_list_button, window.expand_list_button), dark_icons
        ):
            assert not button.icon().isNull()
            assert button.icon().pixmap(18, 18).toImage() != before, \
                "Sidebar arrow retained its dark-theme color"
        assert window._video_list_collapsed
        window.expand_list_button.click()
        _settle()
        assert window.video_list.isVisible() and window.count_badge.text() == "0"


def test_list_and_logs_toggles_preserve_real_paused_media_position_and_volume():
    with _window() as window, tempfile.TemporaryDirectory(prefix="panel-media-") as directory:
        item = _fixture()
        window.video_list.set_videos([item])
        panel = window.preview_panel
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
        panel.volume_slider.setValue(27)
        panel.mute_button.click()
        player, audio, video = panel.player, panel.audio_output, panel.video_widget
        source, position = player.source(), player.position()
        with mock.patch.object(window, "_logs_available_geometry", return_value=QRect(0, 0, 4000, 3000)):
            window.move(100, 100)
            _settle()
            for button in (window.collapse_list_button, window.logs_toggle_button,
                           window.expand_list_button, window.logs_toggle_button):
                button.click()
                _settle()
                assert panel.player is player and panel.audio_output is audio
                assert panel.video_widget is video and player.videoOutput() is video
                assert player.source() == source and player.position() == position
                assert player.playbackState() == QMediaPlayer.PlaybackState.PausedState
                assert panel.volume_slider.value() == 27
                assert abs(audio.volume() - 0.27) < 0.001 and audio.isMuted()
                assert window.video_list.current_video() is item
        panel.release_stream()


def test_logs_expand_downward_without_reducing_preview_when_display_has_room():
    with _window() as window:
        available = QRect(0, 0, 4000, 3000)
        with mock.patch.object(window, "_logs_available_geometry", return_value=available):
            window.move(100, 100)
            window.resize(1320, 820)
            window.log("Before opening logs")
            _settle()
            before = window.geometry()
            before_y = window.y()
            stage_height = window.preview_panel.media_stack_host.height()
            for _ in range(3):
                window.logs_toggle_button.click()
                _settle()
                assert window._logs_expanded and window.log_view.isVisible()
                assert window.height() > before.height()
                assert window.width() == before.width() and window.y() == before_y
                assert abs(window.preview_panel.media_stack_host.height() - stage_height) <= 2
                assert window._logs_window_growth == window.height() - before.height()
                _assert_inside(window.log_view, window)
                _assert_frame_inside(window, available)
                window.log("While logs are open")
                window.logs_toggle_button.click()
                _settle()
                assert not window._logs_expanded and not window.log_view.isVisible()
                assert abs(window.height() - before.height()) <= 2
                assert abs(window.preview_panel.media_stack_host.height() - stage_height) <= 2
                assert window._logs_window_growth == 0
            assert "Before opening logs" in window.log_view.toPlainText()
            assert "While logs are open" in window.log_view.toPlainText()
            window.clear_logs_button.click()
            assert window.log_view.toPlainText() == ""


def test_logs_move_window_up_near_bottom_on_displays_with_offset_origins():
    for origin_x, origin_y in ((-2200, 150), (900, -1800)):
        with _window() as window:
            available = QRect(origin_x, origin_y, 2000, 1600)
            with mock.patch.object(window, "_logs_available_geometry", return_value=available):
                window.resize(1320, 820)
                _settle()
                window.move(origin_x + 80, available.bottom() - window.frameGeometry().height() - 8)
                _settle()
                before_y, before_width, before_height = window.y(), window.width(), window.height()
                stage_height = window.preview_panel.media_stack_host.height()
                _assert_frame_inside(window, available)
                window.logs_toggle_button.click()
                _settle()
                assert window.y() < before_y, "Logs pushed the bottom beyond the display"
                assert window.width() == before_width and window.height() > before_height
                assert window.log_view.height() >= 140
                assert abs(window.preview_panel.media_stack_host.height() - stage_height) <= 2
                _assert_frame_inside(window, available)
                window.logs_toggle_button.click()
                _settle()
                assert abs(window.height() - before_height) <= 2
                assert window.width() == before_width
                _assert_frame_inside(window, available)


def test_height_limited_display_uses_compact_scrolling_logs_inside_screen():
    with _window() as window:
        available = QRect(800, -500, 1800, 850)
        with mock.patch.object(window, "_logs_available_geometry", return_value=available):
            window.resize(1320, 820)
            window.move(850, -490)
            _settle()
            before_width = window.width()
            stage_height = window.preview_panel.media_stack_host.height()
            window.logs_toggle_button.click()
            _settle()
            assert window._logs_expanded and window.log_view.isVisible()
            assert 64 <= window.log_view.height() < 140
            assert window.width() == before_width
            _assert_frame_inside(window, available)
            assert 100 <= window.preview_panel.media_stack_host.height() <= stage_height
            for widget in (window.log_view, window.download_button,
                           window.preview_panel.position_slider):
                _assert_inside(widget, window)
            for number in range(100):
                window.log(f"A scrolling log line {number}")
            _settle()
            assert window.log_view.verticalScrollBar().maximum() > 0
            window.logs_toggle_button.click()
            _settle()
            assert not window.log_view.isVisible()
            assert window.width() == before_width


def test_wrapped_metadata_and_audio_controls_allow_one_line_logs_on_short_display():
    with _window() as window:
        item = _fixture()
        window.video_list.set_videos([item])
        window.quality_combo.setCurrentText("audio only")
        window.resize(1024, window.minimumHeight())
        _settle()
        window.resize(1024, window.minimumHeight())
        window.move(150, 100)
        _settle()
        assert window.audio_format_row.isVisible()
        closed_height = window.height()
        stage_before = window.preview_panel.media_stack_host.height()
        frame_extra = window.frameGeometry().height() - closed_height
        available = QRect(100, 100, 2000, closed_height + 40 + frame_extra)
        with mock.patch.object(window, "_logs_available_geometry", return_value=available):
            window.logs_toggle_button.click()
            _settle()
            assert window._logs_expanded and window.log_view.isVisible()
            assert 32 <= window.log_view.height() < 64, \
                "Short display did not reduce logs below the usual compact height"
            _assert_frame_inside(window, available)
            assert window.width() == 1024
            assert window.height() <= closed_height + 40
            assert window.preview_panel.media_stack_host.height() >= 100
            assert window.video_list.current_video() is item
            for widget in (window.log_view, window.quality_combo, window.audio_format_combo,
                           window.download_button, window.choose_dir_button,
                           window.preview_panel.media_stack_host,
                           window.preview_panel.position_slider,
                           window.preview_panel.fullscreen_button):
                _assert_inside(widget, window)
            print(
                "Constrained display:",
                f"client {closed_height}->{window.height()}px,",
                f"stage {stage_before}->{window.preview_panel.media_stack_host.height()}px,",
                f"logs {window.log_view.height()}px",
            )
            window.logs_toggle_button.click()
            _settle()
            assert not window.log_view.isVisible()
            assert abs(window.height() - closed_height) <= 2
            assert window.video_list.current_video() is item


def test_maximized_logs_keep_window_maximized_without_tracking_normal_growth():
    with _window() as window:
        with mock.patch.object(window, "_logs_available_geometry", return_value=QRect(0, 0, 2400, 1800)):
            # Offscreen Qt has an 800px-wide display and initially maximizes
            # below the app's normal 1024px minimum. Let this harness fit the
            # synthetic screen so a later layout activation cannot repair an
            # unrelated width violation while logs are being tested.
            window.setMinimumWidth(min(
                window.minimumWidth(), window.screen().availableGeometry().width() - 4
            ))
            window.showMaximized()
            _settle()
            assert window.isMaximized()
            before = window.size()
            for expanded in (True, False, True, False):
                window.logs_toggle_button.click()
                _settle()
                assert window.isMaximized()
                assert window._logs_expanded is expanded
                assert window._logs_window_growth == 0
                assert window.size() == before
                assert window.log_view.isVisible() is expanded
            window.showNormal()
            _settle()


def test_closing_logs_keeps_user_resizing_and_subtracts_only_added_height():
    with _window() as window:
        with mock.patch.object(window, "_logs_available_geometry", return_value=QRect(0, 0, 4000, 3000)):
            window.move(100, 100)
            _settle()
            original_height = window.height()
            window.logs_toggle_button.click()
            _settle()
            added = window._logs_window_growth
            assert added > 0
            window.resize(window.width() + 100, window.height() + 65)
            _settle()
            user_width, user_height = window.width(), window.height()
            window.logs_toggle_button.click()
            _settle()
            assert window.width() == user_width, "Closing logs discarded the user's width change"
            assert abs(window.height() - (user_height - added)) <= 2
            assert abs(window.height() - (original_height + 65)) <= 2
            assert window._logs_window_growth == 0


if __name__ == "__main__":
    tests = [value for name, value in globals().copy().items()
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"Passed {len(tests)} collapsible panel checks")
