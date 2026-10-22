"""Conservative cleanup of marked, abandoned application temp directories.

Unmarked historical directories, symlinks, live owners, uncertain identities
and directories newer than a day are deliberately retained.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import uuid
from weakref import WeakValueDictionary

import psutil

MARKER = ".video-scrap-owner.json"
_LOCK = threading.Lock()
_DIRECTORY_LOCKS_GUARD = threading.Lock()
_DIRECTORY_LOCKS = WeakValueDictionary()
_PREFIXES = ("video_scraper_cache_", ".video-scrap-job-")


@contextmanager
def owned_tempdir_io(path: Path):
    """Keep this process's marker readers, updates and removal exclusive.

    Windows does not permit removing/replacing a marker while an ordinary
    reader holds it open. Per-directory locks let unrelated downloads finish
    while a stale-directory scan is inspecting an older owner's processes.
    Weak values avoid retaining a lock for every download for the app's life.
    """
    key = os.path.normcase(os.path.abspath(path))
    with _DIRECTORY_LOCKS_GUARD:
        lock = _DIRECTORY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _DIRECTORY_LOCKS[key] = lock
    with lock:
        yield


def remove_owned_tempdir(path: Path) -> None:
    with owned_tempdir_io(path):
        shutil.rmtree(path)


def _identity(pid):
    return {"pid": pid, "created": psutil.Process(pid).create_time()}


def create_owned_tempdir(prefix: str, directory=None) -> Path:
    if prefix not in _PREFIXES:
        raise ValueError("Unknown application temp directory kind")
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=directory))
    try:
        data = {"application": "video-scrap", "version": 1, "prefix": prefix,
                "created": time.time(), "owners": [_identity(os.getpid())], "groups": []}
        (path / MARKER).write_text(json.dumps(data), encoding="utf-8")
    except BaseException:
        shutil.rmtree(path)
        raise
    schedule_stale_cleanup(path.parent, prefix)
    return path


def add_temp_owner(path: Path, pid: int, *, group=None) -> None:
    with owned_tempdir_io(path):
        marker = path / MARKER
        data = json.loads(marker.read_text(encoding="utf-8"))
        data["owners"].append(_identity(pid))
        if group is not None:
            data["groups"].append(group)
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, marker)


def _inactive(data) -> bool:
    owners = data.get("owners")
    if not isinstance(owners, list) or not owners:
        return False
    for owner in owners:
        if not isinstance(owner, dict) or not isinstance(owner.get("pid"), int) or owner["pid"] < 1:
            return False
        try:
            created = float(owner["created"])
            if not math.isfinite(created) or created <= 0:
                return False
            process = psutil.Process(owner["pid"])
            if abs(process.create_time() - created) < .001 and process.status() != psutil.STATUS_ZOMBIE:
                return False
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, ValueError, TypeError, KeyError):
            return False
    groups = data.get("groups", [])
    if not isinstance(groups, list) or any(not isinstance(group, int) or group < 1 for group in groups):
        return False
    if os.name == "posix" and groups:
        for process in psutil.process_iter(("pid", "status")):
            try:
                if os.getpgid(process.pid) in groups and process.status() != psutil.STATUS_ZOMBIE:
                    return False
            except (ProcessLookupError, psutil.NoSuchProcess):
                continue
            except (PermissionError, psutil.Error):
                return False
    return True


def cleanup_stale_dirs(parent: Path, prefix: str, *, min_age=86400, now=None) -> list[Path]:
    if prefix not in _PREFIXES:
        raise ValueError("Unknown application temp directory kind")
    removed = []
    now = time.time() if now is None else now
    with _LOCK:
        for index, path in enumerate(Path(parent).glob(prefix + "*")):
            if index >= 256:
                break
            try:
                with owned_tempdir_io(path):
                    if path.is_symlink() or not path.is_dir():
                        continue
                    if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
                        continue
                    marker = path / MARKER
                    if marker.is_symlink() or marker.stat().st_size > 16384:
                        continue
                    data = json.loads(marker.read_text(encoding="utf-8"))
                    if not isinstance(data, dict) or data.get("application") != "video-scrap" or data.get("version") != 1 or data.get("prefix") != prefix:
                        continue
                    created = float(data["created"])
                    if not math.isfinite(created) or created <= 0 or now - created < min_age or not _inactive(data):
                        continue
                    # Move aside in the same filesystem before deletion so two
                    # concurrent cleanup scans cannot consume the same directory.
                    retired = path.with_name(prefix + "reaping-" + uuid.uuid4().hex)
                    path.rename(retired)
                    shutil.rmtree(retired)
                    removed.append(path)
            except (OSError, ValueError, TypeError, KeyError, RecursionError):
                continue
    return removed


def schedule_stale_cleanup(parent: Path, prefix: str) -> None:
    def clean():
        try:
            cleanup_stale_dirs(parent, prefix)
        except OSError:
            pass  # Cleanup must not stop startup or a new download.
    threading.Thread(target=clean, name="StaleMediaCleanup", daemon=True).start()
