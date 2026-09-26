"""Lightweight decoder for common in-page video URL obfuscation tricks.

Many self-hosted video portals hide their direct .mp4 / .m3u8 URLs by
encoding fragments into a JS array and re-assembling them with an index
permutation array, so that simple HTML scraping fails.

Pattern handled here::

    const _aaa = ["frag1", "frag2", ...];
    const _bbb = [9, 2, 7, 6, ...];   // permutation
    let s = '';
    for (let i = 0; i < _bbb.length; i++) {
        s += _aaa[_bbb.indexOf(i)];
    }
    s = decodeURIComponent(s);
    s = s.replace('_84c41c', '');     // optional cleanup
    v.querySelector('source').src = s;

We extract the two arrays via regex, replay the permutation in Python,
URL-decode, and apply any trailing string ``.replace`` calls. The result
is a list of likely media URLs found in the page.

This intentionally does NOT execute arbitrary JavaScript; it just
recognises this very common pattern.
"""

from __future__ import annotations

import json
import re
from typing import List, Tuple
from urllib.parse import unquote


_STRING_ARRAY_RE = re.compile(
    r"const\s+(_[a-zA-Z0-9_]+)\s*=\s*(\[\s*\"(?:[^\"\\]|\\.)*\"(?:\s*,\s*\"(?:[^\"\\]|\\.)*\")*\s*\])\s*;",
)
_INT_ARRAY_RE = re.compile(
    r"const\s+(_[a-zA-Z0-9_]+)\s*=\s*(\[\s*\d+(?:\s*,\s*\d+)*\s*\])\s*;",
)
_REPLACE_RE = re.compile(
    r"\.replace\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]*)['\"]\s*\)"
)
_MEDIA_EXT_RE = re.compile(r"\.(mp4|webm|m3u8|mov|m4v|mkv|mpd)(?:\?|$)", re.IGNORECASE)


def _try_parse_string_array(literal: str) -> List[str] | None:
    try:
        value = json.loads(literal)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return value


def _try_parse_int_array(literal: str) -> List[int] | None:
    try:
        value = json.loads(literal)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, list) or not all(isinstance(v, int) for v in value):
        return None
    return value


def _candidate_pairs(html: str) -> List[Tuple[List[str], List[int], int]]:
    """Find every (string_array, int_array) pair that share the same length.

    Returns triplets of (strings, ints, position_in_html) so that callers
    can apply nearby ``.replace`` cleanups.
    """
    string_arrays: List[Tuple[List[str], int]] = []
    for m in _STRING_ARRAY_RE.finditer(html):
        parsed = _try_parse_string_array(m.group(2))
        if parsed:
            string_arrays.append((parsed, m.start()))

    int_arrays: List[Tuple[List[int], int]] = []
    for m in _INT_ARRAY_RE.finditer(html):
        parsed = _try_parse_int_array(m.group(2))
        if parsed:
            int_arrays.append((parsed, m.start()))

    pairs: List[Tuple[List[str], List[int], int]] = []
    for strings, s_pos in string_arrays:
        for ints, i_pos in int_arrays:
            if len(strings) != len(ints):
                continue
            if abs(i_pos - s_pos) > 4000:
                continue
            n = len(ints)
            if sorted(ints) != list(range(n)):
                continue
            pairs.append((strings, ints, max(s_pos, i_pos)))
    return pairs


def _apply_nearby_replaces(html: str, anchor_pos: int, value: str) -> str:
    """Apply any ``.replace('a', 'b')`` calls that appear shortly after the
    obfuscation block - that's where typical cleanup lives.
    """
    window = html[anchor_pos : anchor_pos + 2000]
    for needle, repl in _REPLACE_RE.findall(window):
        value = value.replace(needle, repl)
    return value


def decode_obfuscated_urls(html: str) -> List[str]:
    """Return any media URLs that can be recovered from JS-array obfuscation."""
    out: List[str] = []
    for strings, ints, pos in _candidate_pairs(html):
        n = len(ints)
        positions: dict[int, int] = {}
        for index, value in enumerate(ints):
            # Match list.index()'s first-occurrence behaviour while avoiding
            # a full scan of the permutation for every fragment.
            positions.setdefault(value, index)
        try:
            joined = "".join(strings[positions[i]] for i in range(n))
        except KeyError:
            continue
        decoded_once = unquote(joined)
        candidates = {joined, decoded_once}
        for candidate in list(candidates):
            cleaned = _apply_nearby_replaces(html, pos, candidate)
            candidates.add(cleaned)
            candidates.add(unquote(cleaned))
        for candidate in candidates:
            if not candidate:
                continue
            if not candidate.lower().startswith(("http://", "https://")):
                continue
            if _MEDIA_EXT_RE.search(candidate):
                out.append(candidate)
    seen: set[str] = set()
    unique: List[str] = []
    for url in out:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique
