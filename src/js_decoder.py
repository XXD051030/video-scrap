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
from dataclasses import dataclass
from typing import List
from urllib.parse import unquote


_STRING_ARRAY_RE = re.compile(
    r"const\s+(_[a-zA-Z0-9_]+)\s*=\s*(\[\s*\"(?:[^\"\\]|\\.)*\"(?:\s*,\s*\"(?:[^\"\\]|\\.)*\")*\s*\])\s*;",
)
_INT_ARRAY_RE = re.compile(
    r"const\s+(_[a-zA-Z0-9_]+)\s*=\s*(\[\s*\d+(?:\s*,\s*\d+)*\s*\])\s*;",
)
_IDENTIFIER = r"[A-Za-z_$][\w$]*"
_STRING_LITERAL = r'''(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')'''
_REPLACE_RE = re.compile(
    rf"\s*\.replace\(\s*({_STRING_LITERAL})\s*,\s*({_STRING_LITERAL})\s*\)"
)
_ASSIGNMENT_RE = re.compile(
    rf"(?P<target>{_IDENTIFIER})\s*=(?!=)\s*"
    rf"(?P<value>(?:{_STRING_LITERAL}|[^;\n\"'])+)(?:;|\n|$)"
)
_COMMENT_OR_STRING_RE = re.compile(
    rf"{_STRING_LITERAL}|//[^\n]*|/\*.*?\*/", re.DOTALL
)
_MEDIA_EXT_RE = re.compile(r"\.(mp4|webm|m3u8|mov|m4v|mkv|mpd)(?:\?|$)", re.IGNORECASE)
MAX_DECODER_CHARS = 2 * 1024 * 1024
MAX_DECLARATIONS = 4096
MAX_ARRAY_ELEMENTS = 8192
MAX_CANDIDATE_CHARS = 64 * 1024


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


@dataclass
class _ArrayPair:
    strings: List[str]
    indices: List[int]
    strings_name: str
    indices_name: str
    position: int


def _candidate_pairs(html: str) -> List[_ArrayPair]:
    """Find adjacent compatible arrays belonging to the same script block."""
    declarations = []
    for kind, pattern, parse in (("strings", _STRING_ARRAY_RE, _try_parse_string_array),
                                 ("ints", _INT_ARRAY_RE, _try_parse_int_array)):
        for match in pattern.finditer(html):
            if len(declarations) >= MAX_DECLARATIONS:
                return []
            value = parse(match.group(2))
            if value and len(value) <= MAX_ARRAY_ELEMENTS:
                declarations.append((match.start(), match.end(), kind, match.group(1), value))
    declarations.sort(key=lambda entry: entry[0])
    pairs: List[_ArrayPair] = []
    for index, (s_pos, s_end, kind, strings_name, strings) in enumerate(declarations):
        if kind != "strings":
            continue
        # Only adjacent declarations can form a pair. This replaces repeated
        # whole-document scans with a sorted, bounded neighbor lookup.
        neighbors = declarations[index + 1:index + 2] + declarations[max(0, index - 1):index]
        for i_pos, i_end, i_kind, indices_name, ints in neighbors:
            if i_kind != "ints" or len(strings) != len(ints) or abs(i_pos - s_pos) > 4000:
                continue
            low, high = sorted((s_pos, i_pos))
            if re.search(r"</?script\b", html[low:high], re.IGNORECASE):
                continue
            n = len(ints)
            if sorted(ints) != list(range(n)):
                continue
            pairs.append(_ArrayPair(strings, ints, strings_name, indices_name, max(s_end, i_end)))
            break
    return pairs


def _parse_js_string(literal: str) -> str | None:
    """Read only string literals, without evaluating JavaScript/Python code."""
    body = literal[1:-1]
    out: List[str] = []
    i = 0
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
    while i < len(body):
        char = body[i]
        i += 1
        if char != "\\":
            out.append(char)
            continue
        if i == len(body):
            return None
        char = body[i]
        i += 1
        if char in {"u", "x"}:
            size = 4 if char == "u" else 2
            digits = body[i:i + size]
            if len(digits) != size or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                return None
            out.append(chr(int(digits, 16)))
            i += size
        elif char in {"\r", "\n"}:
            if char == "\r" and body[i:i + 1] == "\n":
                i += 1
        elif char.isdigit() and char != "0":
            return None  # Legacy octal escapes are outside this decoder.
        else:
            out.append(escapes.get(char, char))
    return "".join(out)


def _js_string_replace(value: str, needle: str, replacement: str) -> str:
    """String-search JS replace changes one match and expands JS $ tokens."""
    index = value.find(needle)
    if index < 0:
        return value
    prefix, suffix = value[:index], value[index + len(needle):]
    replacement = re.sub(
        r"\$([$&`'])",
        lambda match: {"$": "$", "&": needle, "`": prefix, "'": suffix}[match[1]],
        replacement,
    )
    return prefix + replacement + suffix


def _apply_expression(expression: str, target: str, value: str) -> str | None:
    """Evaluate the supported self-assignment chain, rejecting extra code."""
    expression = expression.strip()
    decoded = re.match(rf"decodeURIComponent\(\s*{re.escape(target)}\s*\)", expression)
    if decoded:
        if re.search(r"%(?![0-9a-fA-F]{2})", value):
            return None
        try:
            value = unquote(value, errors="strict")
        except UnicodeDecodeError:
            return None
        position = decoded.end()
    else:
        identity = re.match(rf"{re.escape(target)}(?![\w$])", expression)
        if not identity:
            return None
        position = identity.end()
    while expression[position:].strip():
        match = _REPLACE_RE.match(expression, position)
        if not match:
            return None
        needle, replacement = (_parse_js_string(part) for part in match.groups())
        if needle is None or replacement is None:
            return None
        value = _js_string_replace(value, needle, replacement)
        position = match.end()
    return value


def _decode_pipeline(html: str, pair: _ArrayPair, joined: str) -> str | None:
    window = re.split(r"</script\b", html[pair.position:pair.position + 2000], maxsplit=1, flags=re.IGNORECASE)[0]
    window = _COMMENT_OR_STRING_RE.sub(
        lambda match: " " * len(match[0]) if match[0].startswith(("//", "/*")) else match[0],
        window,
    )
    # Stop before another obfuscation block, so its transforms cannot leak in.
    next_array = _STRING_ARRAY_RE.search(window)
    if next_array:
        window = window[:next_array.start()]
    # Keep literal contents available to parse supported replacement arguments,
    # but use an offset-preserving mask when identifying executable targets.
    # A property assignment such as window.url does not reassign local url.
    code_mask = _COMMENT_OR_STRING_RE.sub(lambda match: " " * len(match[0]), window)

    def is_variable_target(match: re.Match) -> bool:
        start, end = match.span("target")
        return (
            code_mask[start:end] == match["target"]
            and not code_mask[:start].rstrip().endswith(".")
        )

    assembly_pattern = re.compile(
        rf"(?P<target>{_IDENTIFIER})\s*\+=\s*{re.escape(pair.strings_name)}\s*\[\s*"
        rf"{re.escape(pair.indices_name)}\s*\.indexOf\s*\("
    )
    assembly = next((
        match for match in assembly_pattern.finditer(window) if is_variable_target(match)
    ), None)
    assignments = [
        match for match in _ASSIGNMENT_RE.finditer(window) if is_variable_target(match)
    ]
    if assembly:
        target = assembly["target"]
        assignments = [match for match in assignments if match.start() > assembly.start()]
    else:
        # Some compact pages omit the loop in the captured snippet. A single
        # unambiguous self-transform still establishes the result variable.
        targets = {
            match["target"] for match in assignments
            if re.match(rf"(?:decodeURIComponent\(\s*)?{re.escape(match['target'])}(?![\w$])", match["value"].strip())
            and ("decodeURIComponent" in match["value"] or ".replace(" in match["value"])
        }
        if len(targets) > 1:
            return None
        target = next(iter(targets), None)
    value = joined
    transformed = False
    for match in assignments:
        if target is None or match["target"] != target:
            continue
        value = _apply_expression(match["value"], target, value)
        if value is None:
            return None
        transformed = True
    if not transformed and not value.lower().startswith(("http://", "https://")):
        try:
            value = unquote(value, errors="strict")
        except UnicodeDecodeError:
            return None
    return value


def decode_obfuscated_urls(html: str) -> List[str]:
    """Return any media URLs that can be recovered from JS-array obfuscation."""
    if len(html) > MAX_DECODER_CHARS:
        return []  # Browser fallback can handle scripts outside this budget.
    out: List[str] = []
    for pair in _candidate_pairs(html):
        strings, ints = pair.strings, pair.indices
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
        if len(joined) > MAX_CANDIDATE_CHARS:
            continue
        candidate = _decode_pipeline(html, pair, joined)
        if candidate and candidate.lower().startswith(("http://", "https://")) and _MEDIA_EXT_RE.search(candidate):
            out.append(candidate)
    seen: set[str] = set()
    unique: List[str] = []
    for url in out:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique
