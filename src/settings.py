"""Persistent application settings stored as JSON.

Settings live in the per-user config directory so they survive across
sessions. The store is thread-safe and supports listener callbacks so
components like the media proxy can reconfigure themselves on the fly
when the user changes a value in the Preferences dialog.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, List, Optional


PLAYBACK_BUFFER_MIN = 0
PLAYBACK_BUFFER_MAX = 300
PLAYBACK_BUFFER_DEFAULT = 60

THEME_DARK = "dark"
THEME_LIGHT = "light"
VALID_THEMES = (THEME_DARK, THEME_LIGHT)
THEME_DEFAULT = THEME_DARK


def _config_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "VideoScraper"
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(
            Path.home() / "AppData" / "Roaming"
        )
        return Path(base) / "VideoScraper"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "video-scraper"


SETTINGS_PATH = _config_dir() / "settings.json"


@dataclass
class AppSettings:
    """Plain-data container for everything we persist."""

    playback_buffer_seconds: int = PLAYBACK_BUFFER_DEFAULT
    theme: str = THEME_DEFAULT

    def normalised(self) -> "AppSettings":
        clamped = max(
            PLAYBACK_BUFFER_MIN,
            min(PLAYBACK_BUFFER_MAX, int(self.playback_buffer_seconds)),
        )
        theme = self.theme if self.theme in VALID_THEMES else THEME_DEFAULT
        return AppSettings(playback_buffer_seconds=clamped, theme=theme)

    @classmethod
    def load(cls) -> "AppSettings":
        try:
            data = json.loads(SETTINGS_PATH.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        kwargs = {}
        for field_name in cls.__dataclass_fields__:
            if field_name in data:
                kwargs[field_name] = data[field_name]
        try:
            return cls(**kwargs).normalised()
        except (TypeError, ValueError):
            return cls()

    def save(self) -> None:
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(
                json.dumps(asdict(self), indent=2), "utf-8"
            )
        except OSError:
            # Persisting is best-effort; failing to write must not crash the app.
            pass


class SettingsStore:
    """Thread-safe wrapper that broadcasts changes to subscribers."""

    def __init__(self, settings: Optional[AppSettings] = None) -> None:
        self._lock = threading.Lock()
        self._settings = (settings or AppSettings.load()).normalised()
        self._listeners: List[Callable[[AppSettings], None]] = []

    def get(self) -> AppSettings:
        with self._lock:
            return AppSettings(**asdict(self._settings))

    def update(self, settings: AppSettings, persist: bool = True) -> None:
        snapshot = settings.normalised()
        with self._lock:
            self._settings = AppSettings(**asdict(snapshot))
            listeners = list(self._listeners)
        if persist:
            snapshot.save()
        for cb in listeners:
            try:
                cb(AppSettings(**asdict(snapshot)))
            except Exception:  # noqa: BLE001
                # Listener bugs must not break the settings flow.
                pass

    def subscribe(
        self,
        cb: Callable[[AppSettings], None],
        fire_initial: bool = True,
    ) -> None:
        with self._lock:
            self._listeners.append(cb)
        if fire_initial:
            try:
                cb(self.get())
            except Exception:  # noqa: BLE001
                pass
