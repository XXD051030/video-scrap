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
