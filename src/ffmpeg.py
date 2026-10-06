"""Find FFmpeg consistently in source and packaged application runs."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
from typing import Optional


def _executable_path(candidate: str | Path) -> Optional[str]:
    try:
        path = Path(candidate)
        if path.is_file() and (sys.platform == "win32" or os.access(path, os.X_OK)):
            return str(path.resolve())
    except (OSError, ValueError):
        pass
    return None


def resolve_ffmpeg() -> Optional[str]:
    """Use an existing system tool before falling back to local binaries.

    Discovery does not change PATH or download anything. Build assets are
    platform-specific; imageio-ffmpeg supplies a binary on supported platforms.
    """
    system = shutil.which("ffmpeg")
    if system:
        executable = _executable_path(system)
        if executable:
            return executable

    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    candidates: list[Path] = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle_root:
        candidates.append(Path(bundle_root) / "bin" / name)
    project_root = Path(__file__).resolve().parents[1]
    if sys.platform == "win32":
        candidates.append(project_root / "build" / "windows-app-assets" / "bin" / name)
    elif sys.platform == "darwin":
        candidates.append(project_root / "build" / "app-assets" / "bin" / name)
    for candidate in candidates:
        executable = _executable_path(candidate)
        if executable:
            return executable

    try:
        import imageio_ffmpeg

        candidate = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, OSError, RuntimeError):
        return None
    return _executable_path(candidate) if candidate else None
