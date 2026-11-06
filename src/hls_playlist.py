"""Text helpers for HLS (M3U8) playlists, shared by downloads and playback."""

from __future__ import annotations

import re
from typing import Dict

_ATTRIBUTE_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def parse_m3u8_attrs(value: str) -> Dict[str, str]:
    """Parse a tag's attribute list, such as ``BANDWIDTH=1,URI="a.m3u8"``."""
    attrs: Dict[str, str] = {}
    for match in _ATTRIBUTE_RE.finditer(value):
        raw = match.group(2).strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        attrs[match.group(1)] = raw
    return attrs
