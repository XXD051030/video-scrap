"""Regression tests for loading and saving the per-user settings file.

Run directly: ``python3 tests/test_settings.py`` (no pytest needed).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import settings


class AppSettingsPersistenceTests(unittest.TestCase):
    def test_parser_limit_errors_use_the_same_recovery_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with mock.patch.object(settings, "SETTINGS_PATH", path):
                for content in ('{"playback_buffer_seconds":' + "1" * 5000 + "}",
                                "[" * 1500 + "0" + "]" * 1500):
                    path.write_text(content, encoding="utf-8")
                    store = settings.SettingsStore()
                    self.assertEqual(store.get(), settings.AppSettings())
                    self.assertIn("using defaults", store.diagnostics[0])

    def test_invalid_utf8_is_backed_up_and_store_starts_with_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            original = b"\xff\xfe invalid settings"
            path.write_bytes(original)
            with mock.patch.object(settings, "SETTINGS_PATH", path):
                store = settings.SettingsStore()
                self.assertEqual(store.get(), settings.AppSettings())
                backup, = path.parent.glob("settings.corrupt-*.json")
                self.assertEqual(backup.read_bytes(), original)
                self.assertIn(str(backup), store.diagnostics[0])
                self.assertTrue(store.update(settings.AppSettings(playback_buffer_seconds=25)))
                self.assertEqual(backup.read_bytes(), original)
                self.assertEqual(settings.AppSettings.load().playback_buffer_seconds, 25)

    def test_failed_store_save_returns_error_but_keeps_session_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with mock.patch.object(settings, "SETTINGS_PATH", path):
                store = settings.SettingsStore(settings.AppSettings())
                self.assertTrue(store.update(store.get()))
                previous = path.read_bytes()
                with mock.patch.object(settings.os, "replace", side_effect=OSError("disk error")):
                    self.assertFalse(store.update(settings.AppSettings(playback_buffer_seconds=35)))
                self.assertIn("disk error", store.last_save_error)
                self.assertEqual(store.get().playback_buffer_seconds, 35)
                self.assertEqual(path.read_bytes(), previous)
                self.assertTrue(store.update(store.get()))
                self.assertEqual(store.last_save_error, "")

    def test_save_replaces_file_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "settings.json"
            with mock.patch.object(settings, "SETTINGS_PATH", path):
                value = settings.AppSettings(
                    playback_buffer_seconds=42, theme=settings.THEME_LIGHT
                )
                value.save()

                self.assertEqual(settings.AppSettings.load(), value)
                self.assertEqual(json.loads(path.read_text("utf-8")), {
                    "playback_buffer_seconds": 42,
                    "theme": settings.THEME_LIGHT,
                })
                self.assertEqual(list(path.parent.iterdir()), [path])

    def test_load_falls_back_for_non_finite_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with mock.patch.object(settings, "SETTINGS_PATH", path):
                for value in ("1e309", "Infinity", "-Infinity"):
                    with self.subTest(value=value):
                        path.write_text(
                            '{"playback_buffer_seconds": ' + value + '}',
                            encoding="utf-8",
                        )
                        self.assertEqual(
                            settings.AppSettings.load(), settings.AppSettings()
                        )

    def test_failed_replace_preserves_previous_file_and_removes_temp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            previous = '{"playback_buffer_seconds": 12, "theme": "dark"}'
            path.write_text(previous, encoding="utf-8")

            with mock.patch.object(settings, "SETTINGS_PATH", path), mock.patch.object(
                settings.os, "replace", side_effect=OSError("disk error")
            ) as replace:
                settings.AppSettings(playback_buffer_seconds=34).save()

            replace.assert_called_once()
            self.assertEqual(path.read_text("utf-8"), previous)
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
