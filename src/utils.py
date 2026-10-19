"""Shared utility helpers."""

from __future__ import annotations

import re
import math
from typing import Optional
from urllib.parse import urlparse, urlsplit, urlunsplit


def is_valid_url(url: str) -> bool:
    """Return True if the string looks like an http(s) URL."""
    try:
        parsed = urlparse(url.strip())
        parsed.port
        return parsed.scheme in ("http", "https") and bool(parsed.hostname) and parsed.username is None and parsed.password is None
    except Exception:
        return False


def format_duration(seconds: Optional[float]) -> str:
    """Format seconds into HH:MM:SS / MM:SS, or '--:--' when unknown."""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError, OverflowError):
        return "--:--"
    if not math.isfinite(seconds) or seconds <= 0:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_bytes(num_bytes: Optional[float]) -> str:
    """Convert a byte count into a human readable string."""
    try:
        num_bytes = float(num_bytes)
    except (TypeError, ValueError, OverflowError):
        return "--"
    if not math.isfinite(num_bytes) or num_bytes <= 0:
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
    cleaned = _SAFE_NAME_RE.sub("_", str(name)).strip(" .")
    cleaned = (cleaned or "untitled")[:max(1, max_len)]
    # Reserve room for the extension and disambiguation suffix on filesystems
    # with 255-byte component limits. Never split a UTF-8 character.
    cleaned = cleaned.encode("utf-8")[:220].decode("utf-8", errors="ignore").rstrip(" .") or "untitled"
    stem = cleaned.split(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        cleaned = "_" + cleaned
    return cleaned


_LOG_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_LOG_PATH_SECRET_RE = re.compile(
    r"(/(?:sig|signature|lsig|token|access_token|auth|auth_key|secret|session|jwt)/)[^/]+",
    re.IGNORECASE,
)


def redact_log_secrets(text: str) -> str:
    """Logs are shareable: retain media location, discard URL credentials."""
    def redact(match):
        value = match.group(0)
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname or ""
            if ":" in hostname:
                hostname = f"[{hostname}]"
            port = f":{parsed.port}" if parsed.port is not None else ""
            path = _LOG_PATH_SECRET_RE.sub(r"\1REDACTED", parsed.path)
            return urlunsplit((parsed.scheme, hostname + port, path,
                               "REDACTED" if parsed.query else "", ""))
        except ValueError:
            return "[invalid URL redacted]"
    return _LOG_URL_RE.sub(redact, str(text))
