"""Shared utility helpers."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse


def is_valid_url(url: str) -> bool:
    """Return True if the string looks like an http(s) URL."""
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def format_duration(seconds: Optional[float]) -> str:
    """Format seconds into HH:MM:SS / MM:SS, or '--:--' when unknown."""
    if seconds is None or seconds <= 0:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_bytes(num_bytes: Optional[float]) -> str:
    """Convert a byte count into a human readable string."""
    if num_bytes is None or num_bytes <= 0:
        return "--"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    return f"{size:.2f} {units[idx]}"


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff ]+")


def safe_filename(name: str, max_len: int = 120) -> str:
    """Sanitize an arbitrary string for safe filesystem usage."""
    if not name:
        return "untitled"
    cleaned = _SAFE_NAME_RE.sub("_", name).strip().strip(".")
    return (cleaned or "untitled")[:max_len]
