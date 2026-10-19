"""Persistent application settings stored as JSON.

Settings live in the per-user config directory so they survive across
sessions. The store is thread-safe and supports listener callbacks so
components like the media proxy can reconfigure themselves on the fly
when the user changes a value in the Preferences dialog.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from collections import deque
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
    def load(cls, diagnostics: Optional[List[str]] = None) -> "AppSettings":
        try:
            data = json.loads(SETTINGS_PATH.read_text("utf-8"))
        except FileNotFoundError:
            return cls()
        except (UnicodeError, ValueError, RecursionError) as exc:
            _record_invalid_settings(str(exc), diagnostics)
            return cls()
        except OSError as exc:
            if diagnostics is not None:
                diagnostics.append(f"Cannot read settings {SETTINGS_PATH}: {exc}. Using defaults.")
            return cls()
        if not isinstance(data, dict):
            _record_invalid_settings("Expected a JSON object", diagnostics)
            return cls()
        kwargs = {}
        for field_name in cls.__dataclass_fields__:
            if field_name in data:
                kwargs[field_name] = data[field_name]
        try:
            return cls(**kwargs).normalised()
        except (TypeError, ValueError, OverflowError) as exc:
            _record_invalid_settings(str(exc), diagnostics)
            return cls()

    def save(self, errors: Optional[List[str]] = None) -> bool:
        temp_path = None
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=SETTINGS_PATH.parent,
                prefix=f".{SETTINGS_PATH.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                temp_file.write(json.dumps(asdict(self), indent=2))
            os.replace(temp_path, SETTINGS_PATH)
            temp_path = None
            return True
        except OSError as exc:
            if errors is not None:
                errors.append(f"Cannot save settings to {SETTINGS_PATH}: {exc}")
            return False
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass


def _record_invalid_settings(reason: str, diagnostics: Optional[List[str]]) -> None:
    """Preserve damaged input before a later Save can replace it."""
    backup_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=SETTINGS_PATH.parent, prefix="settings.corrupt-", suffix=".json",
            delete=False,
        ) as backup:
            backup_path = Path(backup.name)
            with SETTINGS_PATH.open("rb") as source:
                shutil.copyfileobj(source, backup)
        message = f"Invalid settings ({reason}); using defaults. Backup: {backup_path}"
    except OSError as exc:
        if backup_path is not None:
            try:
                backup_path.unlink(missing_ok=True)
            except OSError:
                pass
        message = f"Invalid settings ({reason}); using defaults. Backup failed: {exc}"
    if diagnostics is not None:
        diagnostics.append(message)


class SettingsStore:
    """Serialize persistence and deliver snapshots in commit order.

    Callbacks run outside the data/transaction locks on the thread draining
    the notification queue. UI subscribers must marshal to their UI thread.
    A callback may read, unsubscribe, or update settings without deadlocking.
    """

    def __init__(self, settings: Optional[AppSettings] = None) -> None:
        self._lock = threading.Lock()
        self._transaction = threading.RLock()
        self._notifications = deque()
        self._dispatching = False
        self.diagnostics: List[str] = []
        self.last_save_error = ""
        self._settings = (settings or AppSettings.load(self.diagnostics)).normalised()
        self._listeners: List[Callable[[AppSettings], None]] = []

    def get(self) -> AppSettings:
        with self._lock:
            return AppSettings(**asdict(self._settings))

    def update(self, settings: AppSettings, persist: bool = True) -> bool:
        snapshot = settings.normalised()
        with self._transaction:
            errors: List[str] = []
            saved = snapshot.save(errors) if persist else True
            with self._lock:
                self._settings = AppSettings(**asdict(snapshot))
                self.last_save_error = errors[0] if errors else ""
                self._notifications.append((list(self._listeners), snapshot))
        self._drain_notifications()
        return saved

    def _drain_notifications(self) -> None:
        with self._lock:
            if self._dispatching:
                return
            self._dispatching = True
        try:
            while True:
                with self._lock:
                    if not self._notifications:
                        self._dispatching = False
                        return
                    listeners, snapshot = self._notifications.popleft()
                for cb in listeners:
                    with self._lock:
                        subscribed = cb in self._listeners
                    if not subscribed:
                        continue
                    try:
                        cb(AppSettings(**asdict(snapshot)))
                    except Exception:  # noqa: BLE001
                        pass
        except BaseException:
            with self._lock:
                self._dispatching = False
            raise

    def subscribe(
        self,
        cb: Callable[[AppSettings], None],
        fire_initial: bool = True,
    ) -> Callable[[], None]:
        with self._transaction:
            with self._lock:
                if cb not in self._listeners:
                    self._listeners.append(cb)
                    if fire_initial:
                        snapshot = AppSettings(**asdict(self._settings))
                        self._notifications.append(([cb], snapshot))
        self._drain_notifications()

        def unsubscribe() -> None:
            with self._lock:
                if cb in self._listeners:
                    self._listeners.remove(cb)

        return unsubscribe
