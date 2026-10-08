"""Local header-injecting HTTP proxy with on-disk cache and prefetch.

Qt's QMediaPlayer cannot easily attach custom HTTP headers to outgoing
requests, but a lot of CDNs reject playback unless a ``Referer`` (and
sometimes ``Origin``) header is present. This module starts a tiny
loopback HTTP server which the player can talk to instead. For every
request it forwards to the real upstream URL with the headers we want.

On top of that:

* Every byte fetched from upstream is mirrored into a per-token sparse
  temp file. Subsequent requests for the same byte range (typical when
  the user drags the seek slider back over already-played content) are
  served straight from disk.
* For non-HLS streams a background prefetcher keeps the cache filled
  ``buffer_seconds * bytes_per_second`` ahead of the player.
* For HLS streams a long-running prefetcher tracks which segment the
  player most recently asked for and refills the buffer ahead of that
  segment using a small thread pool, so seeking still lands inside
  cached territory.

Usage::

    proxy = MediaProxyServer(settings_store=store, on_status=log).start()
    local_url = proxy.register("https://cdn/video.mp4", referer="https://site/")
    player.setSource(QUrl(local_url))
    ...
    proxy.stop()

Security note: the server only listens on 127.0.0.1 and accepts requests
that include a registered random token, so other applications on the
machine cannot proxy arbitrary URLs through it.
"""

from __future__ import annotations

import math
import os
import re
import secrets
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urljoin, urlparse

import requests

from .net import build_session
from .hls_payload import MAX_HLS_PAYLOAD_BYTES, decode_hls_payload


M3U8_CONTENT_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/mpegurl",
    "vnd.apple.mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
}

# Extensions FFmpeg's HLS demuxer accepts for manifests and segments. We
# echo the upstream extension into the proxy URL so the demuxer doesn't
# reject our local URLs as "not in allowed_segment_extensions".
_KNOWN_MEDIA_EXTS = {
    "m3u8", "mpd",
    "ts", "m4s", "mp4", "m4a", "m4v", "mov", "webm", "mkv", "aac", "mp3",
    "vtt", "srt", "key",
}


_M3U8_URI_ATTR_RE = re.compile(r'URI="([^"]+)"', re.IGNORECASE)
_EXTINF_RE = re.compile(r"#EXTINF:([0-9]+(?:\.[0-9]+)?|\.[0-9]+)\s*,", re.IGNORECASE)
_CONTENT_RANGE_RE = re.compile(
    r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE
)
_UNSAFE_EXTRA_HEADERS = {
    "connection", "content-length", "cookie", "host", "proxy-authorization",
    "range", "transfer-encoding",
}
_CROSS_HOST_MEDIA_HEADERS = {
    "accept", "accept-encoding", "accept-language", "origin", "referer",
    "user-agent",
}


def _googlevideo_variant_identity(url: str) -> Optional[tuple]:
    """Compare a signed Googlevideo URL without changing its request URL.

    Fetching the same master again can refresh its signatures while retaining
    the video id, format, expiry and every other resource-identifying field.
    Only signature values may differ; other hosts use exact URL matching.
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if host != "googlevideo.com" and not host.endswith(".googlevideo.com"):
        return None
    signature_keys = {"sig", "lsig", "signature"}
    parts = parsed.path.split("/")
    index = 0
    while index + 1 < len(parts):
        if parts[index] in signature_keys:
            parts[index + 1] = "<signature>"
            index += 2
        else:
            index += 1
    query = tuple(sorted(
        (key, "<signature>" if key in signature_keys else value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    ))
    return (
        parsed.scheme, parsed.netloc, tuple(parts), parsed.params, query,
        parsed.fragment,
    )


def _select_hls_master_variant(
    text: str, manifest_url: str, variant_url: str,
) -> str:
    """Keep one requested video variant and its related media groups."""
    from .downloader import _parse_m3u8_attrs

    lines = text.splitlines()
    variants = []
    pending = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#EXT-X-STREAM-INF:"):
            if pending is not None:
                raise ValueError("HLS master contains a variant without a playlist URL")
            pending = index
        elif stripped and not stripped.startswith("#") and pending is not None:
            attrs = _parse_m3u8_attrs(lines[pending].split(":", 1)[1])
            variants.append((pending, index, urljoin(manifest_url, stripped), attrs))
            pending = None
    if pending is not None:
        raise ValueError("HLS master contains a variant without a playlist URL")

    matches = [variant for variant in variants if variant[2] == variant_url]
    if not matches:
        identity = _googlevideo_variant_identity(variant_url)
        if identity is not None:
            matches = [
                variant for variant in variants
                if _googlevideo_variant_identity(variant[2]) == identity
            ]
    if not matches:
        raise ValueError("Selected HLS preview variant is missing from the refreshed master")

    def bandwidth(variant) -> int:
        try:
            return int(variant[3].get("BANDWIDTH") or 0)
        except (TypeError, ValueError):
            return 0

    # The same video playlist can be paired with low/high audio groups.
    # Keep the higher-bandwidth pairing, never another video resource.
    selected = max(matches, key=bandwidth)
    selected_indexes = {selected[0], selected[1]}
    variant_indexes = {index for variant in variants for index in variant[:2]}
    media_types = {"AUDIO", "VIDEO", "SUBTITLES", "CLOSED-CAPTIONS"}
    referenced_groups = {
        (kind, selected[3][kind])
        for kind in media_types
        if selected[3].get(kind) and selected[3][kind] != "NONE"
    }
    retained_groups = set()
    output = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if index in variant_indexes and index not in selected_indexes:
            continue
        if stripped.startswith("#EXT-X-I-FRAME-STREAM-INF:"):
            continue
        if stripped.startswith("#EXT-X-MEDIA:"):
            attrs = _parse_m3u8_attrs(stripped.split(":", 1)[1])
            group = (attrs.get("TYPE"), attrs.get("GROUP-ID"))
            if group not in referenced_groups:
                continue
            retained_groups.add(group)
        output.append(line)
    if referenced_groups - retained_groups:
        raise ValueError("Selected HLS preview variant references a missing media group")
    return "\n".join(output) + "\n"

# Estimated playback bitrate when we don't know the actual one. ~16 Mbps,
# which comfortably covers most 1080p web video. This is only used to
# convert the user's "buffer (seconds)" preference into a byte budget
# for the byte-range prefetcher; the HLS prefetcher uses real EXTINF
# durations from the manifest instead.
DEFAULT_BYTES_PER_SECOND = 2 * 1024 * 1024

CHUNK_SIZE = 64 * 1024
PREFETCH_CHUNK_SIZE = 512 * 1024
DEFAULT_BUFFER_SECONDS = 60
PLAYBACK_HLS_PREFETCH_CONNECTIONS = 8

# Shared pool that all HLS prefetchers submit segment downloads into so
# parallelism is bounded across the whole proxy.
_HLS_POOL = ThreadPoolExecutor(
    max_workers=PLAYBACK_HLS_PREFETCH_CONNECTIONS,
    thread_name_prefix="HlsPrefetch",
)


def _derive_ext(url: str) -> str:
    """Pick a safe file extension to attach to a proxy token."""
    path = urlparse(url).path
    tail = path.rsplit("/", 1)[-1]
    if "." in tail:
        ext = tail.rsplit(".", 1)[-1].lower()
        if ext in _KNOWN_MEDIA_EXTS:
            return ext
    return "bin"


def _parse_range_header(value: Optional[str]) -> Optional[Tuple[Optional[int], Optional[int]]]:
    """Parse a single ``bytes=`` range header into (start, end_inclusive)."""
    if not value:
        return None
    value = value.strip()
    if not value.lower().startswith("bytes="):
        return None
    spec = value[len("bytes="):].split(",")[0].strip()
    if "-" not in spec:
        return None
    a, _, b = spec.partition("-")
    try:
        start = int(a) if a else None
        end = int(b) if b else None
    except ValueError:
        return None
    if start is None and end is None:
        return None
    return start, end


def _parse_content_range(value: Optional[str]) -> Optional[Tuple[int, int, Optional[int]]]:
    if not value:
        return None
    match = _CONTENT_RANGE_RE.fullmatch(value.strip())
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2))
    total_str = match.group(3)
    total = int(total_str) if total_str.isdigit() else None
    return start, end, total


def _has_identity_encoding(resp: requests.Response) -> bool:
    encoding = (resp.headers.get("Content-Encoding") or "identity").strip().lower()
    return encoding == "identity"


def _parse_content_length(value: Optional[str]) -> Optional[int]:
    if value is None or not value.isascii() or not value.isdigit():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _validated_partial_range(
    resp: requests.Response,
    requested_start: int,
    requested_end: Optional[int],
    known_total: Optional[int] = None,
) -> Optional[Tuple[int, Optional[int]]]:
    """Return the declared end/total only when a 206 matches our request."""
    if resp.status_code != 206 or not _has_identity_encoding(resp):
        return None
    content_range = _parse_content_range(resp.headers.get("Content-Range"))
    if content_range is None:
        return None
    start, end, total = content_range
    if start != requested_start or end < start:
        return None
    if requested_end is not None and end > requested_end:
        return None
    if total is not None and (total <= 0 or end >= total):
        return None
    if known_total is not None:
        if end >= known_total or (total is not None and total != known_total):
            return None
    return end, total


class _Entry:
    """One registered upstream URL plus its on-disk cache state.

    Each entry owns a sparse temp file. Bytes fetched from upstream are
    written at their absolute offset, and a list of cached
    ``[start, end_exclusive)`` intervals lets us answer "do we already
    have these bytes?" without scanning the file.
    """

    def __init__(
        self,
        upstream: str,
        headers: Dict[str, str],
        cache_dir: Path,
        referer: Optional[str] = None,
        parent: Optional["_Entry"] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        cookiejar: Optional[CookieJar] = None,
    ) -> None:
        self.upstream = upstream
        self.headers = headers
        self.referer = referer
        self.parent = parent
        self.extra_headers = dict(extra_headers or {})
        self.cookiejar = cookiejar
        self.children: List["_Entry"] = []

        fd, path = tempfile.mkstemp(
            dir=str(cache_dir), prefix="vs_", suffix=".bin"
        )
        self.cache_path = Path(path)
        self._file = os.fdopen(fd, "r+b")
        self._file_lock = threading.Lock()

        # Sorted, non-overlapping cached byte intervals [start, end_exclusive).
        self._intervals: List[Tuple[int, int]] = []
        self._intervals_lock = threading.Lock()

        self.total_size: Optional[int] = None
        self.closed = False

        # Latest offset the player asked for (used by the byte-range
        # prefetcher to stay just-ahead of playback).
        self.last_player_offset = 0
        self.player_event = threading.Event()
        # Set to True the first time the player issues a real GET. Until
        # then the prefetcher stays idle so merely *selecting* a video
        # in the list never consumes upstream bandwidth (which would
        # otherwise compete with parallel downloads of unrelated files).
        self.player_data_requested = False

        # The single in-flight player fetch (start, end_exclusive). The
        # byte-range prefetcher uses this to skip ranges the player is
        # already pulling so we don't waste upstream bandwidth.
        self.player_inflight: Optional[Tuple[int, int]] = None

        # ----- HLS-only metadata -----
        # On the manifest entry: the ordered list of segment entries
        # plus their EXTINF durations parsed from the manifest. On a
        # child segment entry: its index inside its parent's list.
        self.hls_segment_entries: List["_Entry"] = []
        self.hls_segment_durations: List[float] = []
        self.hls_index: Optional[int] = None
        # Highest segment index the player has requested so far.
        self.hls_player_segment_idx: int = 0
        # Reuse child segment entries across manifest re-fetches, keyed by
        # absolute upstream URL, so refreshing a live / no-store manifest
        # doesn't leak a fresh _Entry (+ temp file) per segment each time.
        self.hls_child_by_upstream: Dict[str, "_Entry"] = {}
        # Force manifest handling even when the URL extension / content-type
        # doesn't look like one (e.g. a .jpg-disguised m3u8 + signed token).
        self.force_manifest = False
        self.hls_variant_url: Optional[str] = None
        self.hls_png_wrapped = False
        self._wrapped_fetch_lock = threading.Lock()
        # The media extension chosen for this entry's proxy URL, kept stable
        # across manifest re-fetches so reused children don't revert to .bin.
        self.proxy_ext = "bin"

    @property
    def cookie_kwargs(self) -> dict:
        # requests builds a fresh cookie header for the actual URL (and for
        # each redirect), preserving CookieJar's domain/path/secure rules.
        return {"cookies": self.cookiejar} if self.cookiejar is not None else {}

    # ----- interval bookkeeping --------------------------------------------

    def merge_interval(self, start: int, end: int) -> None:
        if end <= start:
            return
        with self._intervals_lock:
            new = (start, end)
            merged: List[Tuple[int, int]] = []
            placed = False
            for s, e in self._intervals:
                if e < new[0]:
                    merged.append((s, e))
                elif s > new[1]:
                    if not placed:
                        merged.append(new)
                        placed = True
                    merged.append((s, e))
                else:
                    new = (min(s, new[0]), max(e, new[1]))
            if not placed:
                merged.append(new)
            self._intervals = merged

    def cached_run_end(self, offset: int) -> int:
        """End of the contiguous cached run that contains ``offset``.

        Returns ``offset`` itself if no cached interval covers it.
        """
        with self._intervals_lock:
            for s, e in self._intervals:
                if s <= offset < e:
                    return e
                if s > offset:
                    break
            return offset

    def cached_bytes(self) -> int:
        with self._intervals_lock:
            return sum(e - s for s, e in self._intervals)

    def first_cached_start_after(self, offset: int) -> Optional[int]:
        """First cached interval start strictly greater than ``offset``."""
        with self._intervals_lock:
            for s, e in self._intervals:
                if s > offset:
                    return s
        return None

    def first_uncached_offset(self, start: int) -> int:
        """Smallest uncached offset at or after ``start``."""
        with self._intervals_lock:
            cur = start
            for s, e in self._intervals:
                if e <= cur:
                    continue
                if s <= cur:
                    cur = max(cur, e)
                else:
                    return cur
            return cur

    # ----- in-flight player tracking ---------------------------------------

    def begin_player_fetch(self, start: int, end_excl: int) -> None:
        self.player_inflight = (start, end_excl)

    def end_player_fetch(self, start: int, end_excl: int) -> None:
        if self.player_inflight == (start, end_excl):
            self.player_inflight = None

    def skip_player_inflight(self, start: int) -> int:
        """Return an offset past any in-flight player fetch covering ``start``."""
        inflight = self.player_inflight
        if inflight is None:
            return start
        i_start, i_end = inflight
        if i_start <= start < i_end:
            return i_end
        return start

    # ----- I/O -------------------------------------------------------------

    def write_at(self, offset: int, data: bytes) -> None:
        if not data or self.closed:
            return
        with self._file_lock:
            if self.closed:
                return
            self._file.seek(offset)
            self._file.write(data)
            self._file.flush()
        self.merge_interval(offset, offset + len(data))

    def read_at(self, offset: int, length: int) -> bytes:
        if length <= 0 or self.closed:
            return b""
        with self._file_lock:
            if self.closed:
                return b""
            self._file.seek(offset)
            return self._file.read(length)

    def update_player_offset(self, offset: int) -> None:
        if offset < 0:
            return
        self.last_player_offset = offset
        self.player_data_requested = True
        self.player_event.set()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.player_event.set()
        self.hls_child_by_upstream.clear()
        with self._file_lock:
            try:
                self._file.close()
            except OSError:
                pass
            try:
                self.cache_path.unlink()
            except OSError:
                pass


class _Prefetcher(threading.Thread):
    """Background worker that keeps a non-HLS entry's cache filled."""

    def __init__(
        self,
        entry: _Entry,
        get_buffer_bytes: Callable[[], int],
        session: requests.Session,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(
            name=f"PrefetchSeq[{entry.cache_path.name}]",
            daemon=True,
        )
        self.entry = entry
        self.get_buffer_bytes = get_buffer_bytes
        self.session = session
        self.on_status = on_status

    def run(self) -> None:  # noqa: D401 (Thread API)
        entry = self.entry
        # Lazy: stay completely idle until the player actually asks for
        # the first byte. Until then this entry costs zero upstream
        # bandwidth, even though it's already registered.
        while not entry.closed and not entry.player_data_requested:
            entry.player_event.wait(timeout=2.0)
            entry.player_event.clear()
        # Brief grace so the player wins the first in-flight slot.
        if not entry.closed:
            entry.player_event.wait(timeout=0.2)
            entry.player_event.clear()

        while not entry.closed:
            buffer_bytes = max(0, self.get_buffer_bytes())
            if buffer_bytes <= 0:
                entry.player_event.wait(timeout=2.0)
                entry.player_event.clear()
                continue

            anchor = entry.last_player_offset
            limit = anchor + buffer_bytes
            if entry.total_size is not None:
                limit = min(limit, entry.total_size)

            gap_start = entry.first_uncached_offset(anchor)
            # Don't compete with a fetch the player is already streaming.
            gap_start = entry.skip_player_inflight(gap_start)

            if gap_start >= limit:
                entry.player_event.wait(timeout=1.5)
                entry.player_event.clear()
                continue

            next_cached = entry.first_cached_start_after(gap_start)
            fetch_end_excl = limit
            if next_cached is not None:
                fetch_end_excl = min(fetch_end_excl, next_cached)
            fetch_end = fetch_end_excl - 1
            if fetch_end < gap_start:
                continue

            try:
                self._fetch_into_cache(gap_start, fetch_end)
            except Exception:  # noqa: BLE001
                # Brief backoff so a permanently-broken upstream doesn't
                # spin the CPU.
                entry.player_event.wait(timeout=1.5)
                entry.player_event.clear()

    def _fetch_into_cache(self, start: int, end_inclusive: int) -> None:
        entry = self.entry
        headers = dict(entry.headers)
        headers["Range"] = f"bytes={start}-{end_inclusive}"
        headers["Accept-Encoding"] = "identity"
        try:
            resp = self.session.get(
                entry.upstream,
                headers=headers,
                stream=True,
                timeout=20,
                allow_redirects=True,
                **entry.cookie_kwargs,
            )
        except Exception:  # noqa: BLE001
            return

        try:
            if (
                resp.status_code not in (200, 206)
                or not _has_identity_encoding(resp)
            ):
                return

            if resp.status_code == 200:
                # Server ignored Range and sent the whole body. Cache it
                # from offset 0 (the body is the full file).
                cl_header = resp.headers.get("Content-Length")
                if cl_header is not None:
                    length = _parse_content_length(cl_header)
                    if length is None or (
                        entry.total_size is not None and length != entry.total_size
                    ):
                        return
                    entry.total_size = length
                pos = 0
                for chunk in resp.iter_content(chunk_size=PREFETCH_CHUNK_SIZE):
                    if entry.closed:
                        return
                    if not chunk:
                        continue
                    entry.write_at(pos, chunk)
                    pos += len(chunk)
                return

            checked = _validated_partial_range(
                resp, start, end_inclusive, entry.total_size
            )
            if checked is None:
                return
            declared_end, total = checked
            if entry.total_size is None and total is not None:
                entry.total_size = total
            pos = start
            for chunk in resp.iter_content(chunk_size=PREFETCH_CHUNK_SIZE):
                if entry.closed:
                    return
                if not chunk:
                    continue
                # A broken upstream can send more bytes than Content-Range
                # declared. Never cache beyond the verified interval.
                safe = chunk[:declared_end + 1 - pos]
                if not safe:
                    return
                entry.write_at(pos, safe)
                pos += len(safe)
                if pos > declared_end:
                    return
                # If the player suddenly needs bytes far ahead of us,
                # abandon this fetch so the main loop can re-plan.
                if entry.last_player_offset > pos + 4 * 1024 * 1024:
                    return
                # If a player request now overlaps where we're filling,
                # back off and let the player have the upstream bandwidth.
                inflight = entry.player_inflight
                if inflight is not None and inflight[0] <= pos < inflight[1]:
                    return
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass


class _HlsPrefetcher(threading.Thread):
    """Continuous HLS prefetcher anchored to the player's segment index."""

    def __init__(
        self,
        manifest_entry: _Entry,
        get_buffer_seconds: Callable[[], int],
        session: requests.Session,
        on_status: Optional[Callable[[str], None]] = None,
        fetch_wrapped: Optional[Callable[[_Entry], None]] = None,
    ) -> None:
        super().__init__(
            name=f"HlsPre[{manifest_entry.cache_path.name}]",
            daemon=True,
        )
        self.entry = manifest_entry
        self.get_buffer_seconds = get_buffer_seconds
        self.session = session
        self.on_status = on_status
        self.fetch_wrapped = fetch_wrapped
        # Suppress repeated chatter for the same player position.
        self._last_announced_anchor: Optional[int] = None

    def run(self) -> None:  # noqa: D401 (Thread API)
        entry = self.entry
        # Wait until the manifest has been parsed and segments registered.
        while not entry.closed and not entry.hls_segment_entries:
            entry.player_event.wait(timeout=0.5)
            entry.player_event.clear()

        while not entry.closed:
            target = max(0, self.get_buffer_seconds())
            if target <= 0:
                entry.player_event.wait(timeout=2.0)
                entry.player_event.clear()
                continue

            segments = list(entry.hls_segment_entries)
            durations = list(entry.hls_segment_durations)
            if not segments:
                entry.player_event.wait(timeout=2.0)
                entry.player_event.clear()
                continue

            start_idx = max(
                0, min(entry.hls_player_segment_idx, len(segments) - 1)
            )

            queue: List[Tuple[int, _Entry]] = []
            cumulative = 0.0
            last_idx = start_idx
            for idx in range(start_idx, len(segments)):
                seg_dur = durations[idx] if idx < len(durations) else 4.0
                if seg_dur < 0.5:
                    seg_dur = 0.5
                seg = segments[idx]
                if not seg.closed and seg.cached_run_end(0) == 0:
                    queue.append((idx, seg))
                cumulative += seg_dur
                last_idx = idx
                if cumulative >= target:
                    break

            if not queue:
                # All caught up; quietly wait for the player to advance.
                entry.player_event.wait(timeout=2.0)
                entry.player_event.clear()
                continue

            # Only announce a NEW prefetch batch when the player has
            # actually moved to a different segment - prevents the log
            # from filling with repetitive lines while playback advances
            # one segment at a time.
            if self._last_announced_anchor != start_idx:
                self._emit(
                    f"buffer: prefetching {len(queue)} HLS segment(s) "
                    f"(~{int(cumulative)}s, player at "
                    f"{start_idx}/{len(segments) - 1})"
                )
                self._last_announced_anchor = start_idx

            futures = {}
            for idx, seg in queue:
                if entry.closed:
                    break
                fut = _HLS_POOL.submit(self._fetch_segment, seg)
                futures[fut] = idx

            for fut in as_completed(futures):
                if entry.closed:
                    break
                try:
                    fut.result()
                except Exception:  # noqa: BLE001
                    pass
                # If the player has jumped well past our prefetch window,
                # bail and re-plan from the new position.
                new_idx = entry.hls_player_segment_idx
                if new_idx > last_idx:
                    break

    def _fetch_segment(self, segment: _Entry) -> None:
        if segment.closed or segment.cached_run_end(0) > 0:
            return
        if segment.hls_png_wrapped:
            if self.fetch_wrapped is not None:
                self.fetch_wrapped(segment)
            return
        headers = dict(segment.headers)
        headers["Accept-Encoding"] = "identity"
        try:
            resp = self.session.get(
                segment.upstream,
                headers=headers,
                stream=True,
                timeout=(5, 10),
                allow_redirects=True,
                **segment.cookie_kwargs,
            )
        except Exception:  # noqa: BLE001
            return
        try:
            # This request has no Range header; a partial body has no known
            # offset and must not be cached at byte zero.
            if resp.status_code != 200 or not _has_identity_encoding(resp):
                return
            cl = _parse_content_length(resp.headers.get("Content-Length"))
            if cl is not None:
                segment.total_size = cl
            pos = 0
            for chunk in resp.iter_content(chunk_size=PREFETCH_CHUNK_SIZE):
                if segment.closed:
                    return
                if not chunk:
                    continue
                segment.write_at(pos, chunk)
                pos += len(chunk)
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    def _emit(self, msg: str) -> None:
        if self.on_status is None:
            return
        try:
            self.on_status(msg)
        except Exception:  # noqa: BLE001
            pass


class MediaProxyServer:
    """Background HTTP server on 127.0.0.1 with token-based URL mapping."""

    def __init__(
        self,
        settings_store=None,
        on_status: Optional[Callable[[str], None]] = None,
        buffer_seconds: Optional[int] = None,
    ) -> None:
        self._entries: Dict[str, _Entry] = {}
        self._token_by_entry: Dict[int, str] = {}
        self._prefetchers: Dict[str, _Prefetcher] = {}
        self._hls_prefetchers: Dict[str, _HlsPrefetcher] = {}
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._port: int = 0

        self._cache_dir: Path = Path(
            tempfile.mkdtemp(prefix="video_scraper_cache_")
        )
        self._session = build_session(pool=32)
        self._settings_store = settings_store
        self._on_status = on_status
        self._buffer_seconds: int = DEFAULT_BUFFER_SECONDS
        if settings_store is not None:
            initial = settings_store.get()
            try:
                self._buffer_seconds = max(
                    0, int(initial.playback_buffer_seconds)
                )
            except (TypeError, ValueError, AttributeError):
                pass
            # Subscribe without firing the initial callback - the host
            # app's UI may not be ready to receive status messages yet.
            settings_store.subscribe(
                self._on_settings_changed, fire_initial=False
            )
        if buffer_seconds is not None:
            self._buffer_seconds = max(0, int(buffer_seconds))

    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> "MediaProxyServer":
        if self._server is not None:
            return self
        owner = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args, **_kwargs) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802 (stdlib API)
                owner._handle(self, body=True)

            def do_HEAD(self) -> None:  # noqa: N802
                owner._handle(self, body=False, method="HEAD")

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Allow", "GET, HEAD, OPTIONS")
                self.end_headers()

        # Bind to port 0 so the OS hands us a free port while the server
        # keeps the socket - no bind/close/re-open window for another
        # process to grab the port in between.
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="MediaProxyServer",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self.clear()
        try:
            for child in self._cache_dir.iterdir():
                try:
                    child.unlink()
                except OSError:
                    pass
            self._cache_dir.rmdir()
        except OSError:
            pass
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ settings

    def _on_settings_changed(self, settings) -> None:
        seconds = getattr(
            settings, "playback_buffer_seconds", DEFAULT_BUFFER_SECONDS
        )
        try:
            self._buffer_seconds = max(0, int(seconds))
        except (TypeError, ValueError):
            self._buffer_seconds = DEFAULT_BUFFER_SECONDS
        # Wake every prefetcher so they re-evaluate their target.
        with self._lock:
            entries = list(self._entries.values())
        for entry in entries:
            entry.player_event.set()
        self._emit(f"buffer target updated to {self._buffer_seconds}s")

    def buffer_bytes(self) -> int:
        return self._buffer_seconds * DEFAULT_BYTES_PER_SECOND

    def buffer_seconds(self) -> int:
        return self._buffer_seconds

    def _emit(self, message: str) -> None:
        if self._on_status is None:
            return
        try:
            self._on_status(message)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ register

    def register(
        self,
        upstream: str,
        referer: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        parent: Optional[_Entry] = None,
        is_hls: bool = False,
        cookiejar: Optional[CookieJar] = None,
        hls_variant_url: Optional[str] = None,
        hls_png_wrapped: bool = False,
    ) -> str:
        if not upstream:
            raise ValueError("upstream URL required")
        from .downloader import build_request_headers

        token = secrets.token_urlsafe(12)
        headers = build_request_headers(referer)
        # Player streams care more about media types than HTML.
        headers["Accept"] = "*/*"
        # A literal Cookie header ignores domain scoping. Host / Range and
        # transport headers must also be controlled by requests and by the
        # byte-range cache rather than by extractor-provided metadata.
        safe_extra_headers = {
            key: value
            for key, value in (extra_headers or {}).items()
            if isinstance(key, str) and key.lower() not in _UNSAFE_EXTRA_HEADERS
        }
        headers.update(safe_extra_headers)

        entry = _Entry(
            upstream=upstream,
            headers=headers,
            cache_dir=self._cache_dir,
            referer=referer,
            parent=parent,
            extra_headers=safe_extra_headers,
            cookiejar=cookiejar,
        )
        entry.force_manifest = is_hls
        entry.hls_variant_url = hls_variant_url
        entry.hls_png_wrapped = hls_png_wrapped
        with self._lock:
            if parent is not None and parent.closed:
                entry.close()
                raise RuntimeError("Cannot register media under a closed manifest")
            self._entries[token] = entry
            self._token_by_entry[id(entry)] = token
            if parent is not None:
                parent.children.append(entry)

        # Top-level entries (no parent) get a dedicated sequential
        # prefetcher. HLS segment entries are children of the manifest
        # and are filled in bulk by the manifest's _HlsPrefetcher.
        if parent is None and not hls_png_wrapped:
            prefetcher = _Prefetcher(
                entry,
                get_buffer_bytes=self.buffer_bytes,
                session=self._session,
                on_status=self._on_status,
            )
            with self._lock:
                self._prefetchers[token] = prefetcher
            prefetcher.start()

        ext = _derive_ext(upstream)
        if ext == "bin":
            # FFmpeg's HLS demuxer rejects segment/manifest URLs whose
            # extension isn't on its allow-list, so a .jpg-disguised stream
            # would download but never play. Give it a media extension.
            if is_hls:
                ext = "m3u8"
            elif parent is not None:
                # Unknown-extension child of a manifest -> treat as a segment.
                ext = "ts"
        entry.proxy_ext = ext
        return f"{self.base_url}/play/{token}.{ext}"

    def unregister(self, token_or_url: str) -> None:
        leaf = token_or_url.rsplit("/", 1)[-1]
        token = leaf.rsplit(".", 1)[0] if "." in leaf else leaf
        with self._lock:
            entry = self._entries.pop(token, None)
            if entry is not None:
                self._token_by_entry.pop(id(entry), None)
            self._prefetchers.pop(token, None)
            self._hls_prefetchers.pop(token, None)
        if entry is None:
            return
        self._dispose_entry(entry)

    def clear(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
            self._token_by_entry.clear()
            self._prefetchers.clear()
            self._hls_prefetchers.clear()
        for entry in entries:
            self._dispose_entry(entry)

    def _dispose_entry(self, entry: _Entry) -> None:
        # Close the parent under the registration lock before taking the
        # snapshot, so an in-flight rewrite cannot add children after it.
        with self._lock:
            entry.close()
            children = list(entry.children)
            entry.children.clear()
            for child in children:
                child_token = self._token_by_entry.pop(id(child), None)
                if child_token is not None:
                    self._entries.pop(child_token, None)
                    self._prefetchers.pop(child_token, None)
                    self._hls_prefetchers.pop(child_token, None)
        for child in children:
            self._dispose_entry(child)

    def _lookup(self, path: str) -> Optional[_Entry]:
        clean = urlparse(path).path
        if not clean.startswith("/play/"):
            return None
        leaf = clean[len("/play/"):]
        token = leaf.rsplit(".", 1)[0] if "." in leaf else leaf
        with self._lock:
            return self._entries.get(token)

    def _token_for(self, entry: _Entry) -> Optional[str]:
        with self._lock:
            return self._token_by_entry.get(id(entry))

    # ------------------------------------------------------------------ HTTP

    def _handle(
        self,
        request: BaseHTTPRequestHandler,
        body: bool,
        method: str = "GET",
    ) -> None:
        entry = self._lookup(request.path)
        if entry is None:
            request.send_response(404)
            request.end_headers()
            return

        # Track HLS player progress as soon as we see a segment request.
        self._note_player_segment(entry)

        if entry.hls_png_wrapped:
            if self._likely_manifest(entry):
                self._serve_wrapped_manifest(request, entry, body=body)
                return
            try:
                self._ensure_wrapped_cache(entry)
            except Exception as exc:  # noqa: BLE001
                self._emit(f"Player wrapped-media error: {exc}")
                self._send_manifest_error(request, 502, str(exc), body=body)
                return
            if not body:
                request.send_response(200)
                request.send_header("Content-Type", self._guess_content_type(entry))
                request.send_header("Content-Length", str(entry.total_size))
                request.send_header("Accept-Ranges", "bytes")
                request.end_headers()
            else:
                # The cache now holds logical media bytes. The existing range
                # path can serve them without probing or ranging the PNG.
                self._serve_range(request, entry)
            return

        # M3U8 manifests must be fetched fresh and rewritten so their
        # internal segment URLs come back through us.
        if method == "GET" and self._likely_manifest(entry):
            handled = self._serve_manifest(request, entry)
            if handled:
                return
            # Fell through (not actually a manifest) - continue with the
            # normal flow.

        if method == "HEAD":
            self._serve_head(request, entry)
            return

        self._serve_range(request, entry)

    def _note_player_segment(self, entry: _Entry) -> None:
        parent = entry.parent
        if parent is None or entry.hls_index is None:
            return
        if entry.hls_index > parent.hls_player_segment_idx:
            parent.hls_player_segment_idx = entry.hls_index
        # Always wake the HLS prefetcher so it re-anchors immediately.
        parent.player_event.set()

    def _likely_manifest(self, entry: _Entry) -> bool:
        if entry.force_manifest:
            return True
        path = urlparse(entry.upstream).path.lower()
        return path.endswith(".m3u8") or path.endswith(".mpd")

    # ----- manifest --------------------------------------------------------

    def _fetch_wrapped_payload(self, entry: _Entry) -> Tuple[bytes, str]:
        """Fetch a full envelope; upstream byte offsets are not media offsets."""
        headers = dict(entry.headers)
        headers.pop("Range", None)
        headers["Accept-Encoding"] = "identity"
        max_wire_bytes = MAX_HLS_PAYLOAD_BYTES + 1024 * 1024
        with self._session.get(
            entry.upstream, headers=headers, stream=True, timeout=(10, 30),
            allow_redirects=True, **entry.cookie_kwargs,
        ) as response:
            if response.status_code != 200:
                raise ValueError(f"Wrapped media returned HTTP {response.status_code}")
            length = _parse_content_length(response.headers.get("Content-Length"))
            if length is not None and length > max_wire_bytes:
                raise ValueError("Wrapped HLS response exceeds the size limit")
            chunks = []
            received = 0
            for chunk in response.iter_content(CHUNK_SIZE):
                if entry.closed:
                    raise RuntimeError("Wrapped media request cancelled")
                received += len(chunk)
                if received > max_wire_bytes:
                    raise ValueError("Wrapped HLS response exceeds the size limit")
                chunks.append(chunk)
            payload = decode_hls_payload(b"".join(chunks))
            if not payload or len(payload) > MAX_HLS_PAYLOAD_BYTES:
                raise ValueError("Empty or oversized wrapped HLS payload")
            return payload, response.url or entry.upstream

    def _ensure_wrapped_cache(self, entry: _Entry) -> None:
        # Player and prefetch requests share a single complete, validated
        # fetch. Failed/partial envelopes never enter the sparse byte cache.
        with entry._wrapped_fetch_lock:
            if entry.closed:
                raise RuntimeError("Wrapped media request cancelled")
            if entry.total_size is not None and entry.cached_run_end(0) >= entry.total_size:
                return
            payload, _ = self._fetch_wrapped_payload(entry)
            if entry.closed:
                raise RuntimeError("Wrapped media request cancelled")
            entry.write_at(0, payload)
            entry.total_size = len(payload)

    def _serve_wrapped_manifest(
        self, request: BaseHTTPRequestHandler, entry: _Entry, *, body: bool,
    ) -> None:
        try:
            payload, final_url = self._fetch_wrapped_payload(entry)
            text = payload.decode("utf-8-sig")
            if not text.lstrip().startswith("#EXTM3U"):
                raise ValueError("Wrapped player response is not an HLS manifest")
            if entry.closed:
                raise RuntimeError("Wrapped manifest request cancelled")
            rewritten, segments, durations = self._rewrite_m3u8(text, final_url, entry)
            for idx, segment in enumerate(segments):
                segment.hls_index = idx
            entry.hls_segment_entries = segments
            entry.hls_segment_durations = durations
            result = rewritten.encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            self._emit(f"Player wrapped-manifest error: {exc}")
            self._send_manifest_error(request, 502, str(exc), body=body)
            return
        request.send_response(200)
        request.send_header("Content-Type", "application/vnd.apple.mpegurl")
        request.send_header("Content-Length", str(len(result)))
        request.send_header("Cache-Control", "no-store")
        request.end_headers()
        if body:
            try:
                request.wfile.write(result)
            except (BrokenPipeError, ConnectionResetError):
                return
            self._ensure_hls_prefetcher(entry)

    def _serve_manifest(
        self, request: BaseHTTPRequestHandler, entry: _Entry
    ) -> bool:
        """Fetch + rewrite an HLS manifest. Returns False if not a manifest."""
        try:
            upstream = self._session.get(
                entry.upstream,
                headers=entry.headers,
                timeout=20,
                allow_redirects=True,
                **entry.cookie_kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            request.send_response(502)
            request.send_header("Content-Type", "text/plain; charset=utf-8")
            request.end_headers()
            try:
                request.wfile.write(f"Upstream error: {exc}".encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError):
                pass
            return True

        try:
            if upstream.status_code != 200:
                message = f"Upstream manifest returned HTTP {upstream.status_code}"
                self._emit(f"Player manifest error: {message}")
                self._send_manifest_error(
                    request,
                    upstream.status_code if upstream.status_code >= 400 else 502,
                    message,
                )
                return True
            content_type = (
                upstream.headers.get("Content-Type") or ""
            ).lower().split(";")[0].strip()
            final_url = upstream.url or entry.upstream
            text = upstream.text
            if not self._looks_like_m3u8(entry.upstream, content_type) and not (
                text.lstrip().startswith("#EXTM3U")
            ):
                # Not actually a manifest - hand back to the regular path.
                return False
            if entry.hls_variant_url:
                try:
                    text = _select_hls_master_variant(
                        text, final_url, entry.hls_variant_url
                    )
                except ValueError as exc:
                    self._emit(f"Player manifest error: {exc}")
                    self._send_manifest_error(request, 502, str(exc))
                    return True
            rewritten, segment_entries, segment_durations = self._rewrite_m3u8(
                text, final_url, entry
            )
            for idx, seg in enumerate(segment_entries):
                seg.hls_index = idx
            entry.hls_segment_entries = segment_entries
            entry.hls_segment_durations = segment_durations
            payload = rewritten.encode("utf-8")
            request.send_response(200)
            request.send_header(
                "Content-Type", "application/vnd.apple.mpegurl"
            )
            request.send_header("Content-Length", str(len(payload)))
            request.send_header("Cache-Control", "no-store")
            request.end_headers()
            try:
                request.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                return True

            self._ensure_hls_prefetcher(entry)
            return True
        finally:
            try:
                upstream.close()
            except Exception:  # noqa: BLE001
                pass

    def _ensure_hls_prefetcher(self, manifest_entry: _Entry) -> None:
        if not manifest_entry.hls_segment_entries:
            return
        token = self._token_for(manifest_entry)
        if token is None:
            return
        with self._lock:
            if manifest_entry.closed or self._entries.get(token) is not manifest_entry:
                return
            existing = self._hls_prefetchers.get(token)
            if existing is not None:
                # Manifest re-fetched; just nudge the existing worker so
                # it re-evaluates with the new segment list.
                manifest_entry.player_event.set()
                return
            worker = _HlsPrefetcher(
                manifest_entry,
                get_buffer_seconds=self.buffer_seconds,
                session=self._session,
                on_status=self._on_status,
                fetch_wrapped=self._ensure_wrapped_cache,
            )
            self._hls_prefetchers[token] = worker
        worker.start()

    @staticmethod
    def _send_manifest_error(
        request: BaseHTTPRequestHandler, status: int, message: str,
        *, body: bool = True,
    ) -> None:
        payload = message.encode("utf-8")
        request.send_response(status)
        request.send_header("Content-Type", "text/plain; charset=utf-8")
        request.send_header("Content-Length", str(len(payload)))
        request.end_headers()
        if not body:
            return
        try:
            request.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ----- HEAD ------------------------------------------------------------

    def _serve_head(
        self, request: BaseHTTPRequestHandler, entry: _Entry
    ) -> None:
        headers = dict(entry.headers)
        headers["Accept-Encoding"] = "identity"
        try:
            resp = self._session.request(
                method="HEAD",
                url=entry.upstream,
                headers=headers,
                timeout=15,
                allow_redirects=True,
                **entry.cookie_kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            request.send_response(502)
            request.send_header("Content-Type", "text/plain; charset=utf-8")
            request.end_headers()
            try:
                request.wfile.write(f"Upstream error: {exc}".encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        try:
            cl = resp.headers.get("Content-Length")
            if (
                entry.total_size is None
                and resp.status_code == 200
                and _has_identity_encoding(resp)
            ):
                length = _parse_content_length(cl)
                if length is not None:
                    entry.total_size = length
            request.send_response(resp.status_code)
            for key, value in resp.headers.items():
                if key.lower() in {"transfer-encoding", "connection", "keep-alive"}:
                    continue
                request.send_header(key, value)
            request.end_headers()
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    # ----- range serve -----------------------------------------------------

    def _serve_range(
        self, request: BaseHTTPRequestHandler, entry: _Entry
    ) -> None:
        rng = _parse_range_header(request.headers.get("Range"))

        # Make sure we know the total size. If we don't, ask upstream
        # cheaply via a 0-0 range request.
        if entry.total_size is None:
            self._probe_total_size(entry)

        total = entry.total_size

        # Decide effective [start, end] inclusive.
        if rng is None:
            start = 0
            end = (total - 1) if total is not None else None
            partial = False
        else:
            r_start, r_end = rng
            if r_start is None and r_end is not None:
                # Suffix range - we need total to satisfy this.
                if total is None:
                    self._proxy_passthrough(request, entry)
                    return
                start = max(0, total - r_end)
                end = total - 1
            else:
                start = r_start or 0
                if r_end is None:
                    end = (total - 1) if total is not None else None
                else:
                    end = r_end
            partial = True

        if total is not None:
            if start >= total:
                request.send_response(416)
                request.send_header("Content-Range", f"bytes */{total}")
                request.end_headers()
                return
            if end is None or end >= total:
                end = total - 1

        entry.update_player_offset(start)

        # Headers
        if partial and total is not None:
            request.send_response(206)
            request.send_header(
                "Content-Range", f"bytes {start}-{end}/{total}"
            )
        elif total is not None:
            request.send_response(200)
        else:
            # Total unknown - fall back to streaming pass-through.
            self._proxy_passthrough(
                request, entry, range_header=request.headers.get("Range")
            )
            return

        content_type = self._guess_content_type(entry)
        request.send_header("Content-Type", content_type)
        request.send_header("Accept-Ranges", "bytes")
        if end is not None:
            request.send_header("Content-Length", str(end - start + 1))
        request.send_header("Cache-Control", "no-store")
        request.end_headers()

        try:
            for chunk in self._iterate_bytes(entry, start, end):
                if not chunk:
                    continue
                try:
                    request.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
        finally:
            entry.player_event.set()

    def _iterate_bytes(
        self, entry: _Entry, start: int, end_inclusive: Optional[int]
    ) -> Iterable[bytes]:
        """Yield bytes for [start, end_inclusive]; mix cache + upstream."""
        pos = start
        end = end_inclusive
        while True:
            if end is not None and pos > end:
                return
            if entry.closed:
                return

            # Try cache first.
            run_end = entry.cached_run_end(pos)
            if run_end > pos:
                upper = run_end - 1
                if end is not None:
                    upper = min(upper, end)
                remaining = upper - pos + 1
                while remaining > 0:
                    take = min(remaining, CHUNK_SIZE)
                    data = entry.read_at(pos, take)
                    if not data:
                        break
                    yield data
                    pos += len(data)
                    remaining -= len(data)
                continue

            # Cache miss - fetch from upstream up to the next cached
            # interval (or the requested end), whichever is closer.
            next_cached = entry.first_cached_start_after(pos)
            fetch_end = end
            if next_cached is not None:
                limit = next_cached - 1
                fetch_end = limit if end is None else min(end, limit)
            if fetch_end is not None and fetch_end < pos:
                continue

            yielded = False
            for chunk in self._fetch_range(entry, pos, fetch_end):
                yielded = True
                yield chunk
                pos += len(chunk)
                if fetch_end is not None and pos > fetch_end:
                    break
            if not yielded:
                return

    def _fetch_range(
        self, entry: _Entry, start: int, end_inclusive: Optional[int]
    ) -> Iterable[bytes]:
        headers = dict(entry.headers)
        headers["Accept-Encoding"] = "identity"
        if end_inclusive is not None:
            headers["Range"] = f"bytes={start}-{end_inclusive}"
        else:
            headers["Range"] = f"bytes={start}-"

        # Mark this range as in-flight so the prefetcher doesn't
        # duplicate the work.
        if end_inclusive is not None:
            inflight_end = end_inclusive + 1
        elif entry.total_size is not None:
            inflight_end = entry.total_size
        else:
            inflight_end = start + 32 * 1024 * 1024
        entry.begin_player_fetch(start, inflight_end)

        try:
            try:
                resp = self._session.get(
                    entry.upstream,
                    headers=headers,
                    stream=True,
                    timeout=30,
                    allow_redirects=True,
                    **entry.cookie_kwargs,
                )
            except Exception:  # noqa: BLE001
                return

            try:
                if (
                    resp.status_code not in (200, 206)
                    or not _has_identity_encoding(resp)
                ):
                    return

                if resp.status_code == 200:
                    cl_header = resp.headers.get("Content-Length")
                    if cl_header is not None:
                        length = _parse_content_length(cl_header)
                        if length is None or (
                            entry.total_size is not None and length != entry.total_size
                        ):
                            return
                        entry.total_size = length
                    # Server ignored the Range header. Stream the
                    # requested slice without writing the bytes at the
                    # wrong offset (the body starts at 0, not `start`).
                    yield from self._stream_slice_from_full_body(
                        resp, start, end_inclusive, entry
                    )
                    return

                checked = _validated_partial_range(
                    resp, start, end_inclusive, entry.total_size
                )
                if checked is None:
                    return
                declared_end, total = checked
                if entry.total_size is None and total is not None:
                    entry.total_size = total
                pos = start
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if entry.closed:
                        return
                    if not chunk:
                        continue
                    safe = chunk[:declared_end + 1 - pos]
                    if not safe:
                        return
                    entry.write_at(pos, safe)
                    pos += len(safe)
                    yield safe
                    if pos > declared_end:
                        return
            finally:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            entry.end_player_fetch(start, inflight_end)

    def _stream_slice_from_full_body(
        self,
        resp: requests.Response,
        start: int,
        end_inclusive: Optional[int],
        entry: _Entry,
    ) -> Iterable[bytes]:
        pos_in_body = 0
        for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
            if entry.closed:
                return
            if not chunk:
                continue
            chunk_start = pos_in_body
            pos_in_body += len(chunk)
            chunk_end_excl = pos_in_body
            # The 200 body is the whole file from offset 0, so mirror it
            # into the cache as we stream; seeking back no longer forces a
            # full re-download.
            entry.write_at(chunk_start, chunk)
            if chunk_end_excl <= start:
                continue
            if end_inclusive is not None and chunk_start > end_inclusive:
                return
            slice_start = max(0, start - chunk_start)
            slice_end = len(chunk)
            if end_inclusive is not None:
                slice_end = min(slice_end, end_inclusive + 1 - chunk_start)
            if slice_end > slice_start:
                yield chunk[slice_start:slice_end]

    def _probe_total_size(self, entry: _Entry) -> None:
        if entry.total_size is not None:
            return
        headers = dict(entry.headers)
        headers["Range"] = "bytes=0-0"
        headers["Accept-Encoding"] = "identity"
        try:
            resp = self._session.get(
                entry.upstream,
                headers=headers,
                stream=True,
                timeout=15,
                allow_redirects=True,
                **entry.cookie_kwargs,
            )
        except Exception:  # noqa: BLE001
            return
        try:
            checked = _validated_partial_range(resp, 0, 0)
            if checked is not None and checked[1] is not None:
                received = 0
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    received += len(chunk)
                    if received > 1:
                        return
                if received == 1:
                    entry.total_size = checked[1]
                return
            cl = _parse_content_length(resp.headers.get("Content-Length"))
            if cl is not None and resp.status_code == 200 and _has_identity_encoding(resp):
                entry.total_size = cl
        except Exception:  # noqa: BLE001
            return
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    def _proxy_passthrough(
        self,
        request: BaseHTTPRequestHandler,
        entry: _Entry,
        range_header: Optional[str] = None,
    ) -> None:
        """Last-resort streaming when caching can't help (unknown size)."""
        headers = dict(entry.headers)
        if range_header:
            headers["Range"] = range_header
        try:
            resp = self._session.get(
                entry.upstream,
                headers=headers,
                stream=True,
                timeout=30,
                allow_redirects=True,
                **entry.cookie_kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            request.send_response(502)
            request.send_header("Content-Type", "text/plain; charset=utf-8")
            request.end_headers()
            try:
                request.wfile.write(f"Upstream error: {exc}".encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        try:
            request.send_response(resp.status_code)
            for key, value in resp.headers.items():
                if key.lower() in {"transfer-encoding", "connection", "keep-alive"}:
                    continue
                request.send_header(key, value)
            request.end_headers()
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                try:
                    request.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    # ----- helpers ---------------------------------------------------------

    def _guess_content_type(self, entry: _Entry) -> str:
        path = urlparse(entry.upstream).path.lower()
        if path.endswith(".mp4") or path.endswith(".m4v"):
            return "video/mp4"
        if path.endswith(".webm"):
            return "video/webm"
        if path.endswith(".mov"):
            return "video/quicktime"
        if path.endswith(".mkv"):
            return "video/x-matroska"
        if path.endswith(".ts"):
            return "video/mp2t"
        if path.endswith(".m4s"):
            return "video/iso.segment"
        if path.endswith(".m4a"):
            return "audio/mp4"
        if path.endswith(".aac"):
            return "audio/aac"
        if path.endswith(".mp3"):
            return "audio/mpeg"
        return "application/octet-stream"

    def _looks_like_m3u8(self, url: str, content_type: str) -> bool:
        if content_type in M3U8_CONTENT_TYPES:
            return True
        path = urlparse(url).path.lower()
        return path.endswith(".m3u8")

    def _rewrite_m3u8(
        self,
        text: str,
        manifest_url: str,
        manifest_entry: _Entry,
    ) -> Tuple[str, List[_Entry], List[float]]:
        """Rewrite every URL inside an HLS playlist to go through us.

        Returns the rewritten manifest text plus the ordered list of
        segment entries (and parsed durations) for the prefetcher.
        """
        out_lines: List[str] = []
        segment_entries: List[_Entry] = []
        segment_durations: List[float] = []
        pending_duration: Optional[float] = None
        pending_variant = False

        for raw_line in text.splitlines():
            line = raw_line.rstrip("\r")
            stripped = line.strip()
            if not stripped:
                out_lines.append(line)
                continue
            if stripped.startswith("#"):
                if stripped.startswith("#EXT-X-STREAM-INF:"):
                    pending_variant = True
                elif stripped.upper().startswith("#EXTINF:"):
                    pending_variant = False
                if stripped.upper().startswith("#EXTINF:"):
                    pending_duration = None
                m = _EXTINF_RE.match(stripped)
                if m:
                    try:
                        duration = float(m.group(1))
                        pending_duration = duration if math.isfinite(duration) else None
                    except ValueError:
                        pending_duration = None
                manifest_uri = manifest_entry.hls_png_wrapped and stripped.startswith(
                    ("#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:")
                )
                rewritten = _M3U8_URI_ATTR_RE.sub(
                    lambda mm: f'URI="{self._wrap_relative(mm.group(1), manifest_url, manifest_entry, is_manifest=manifest_uri)[0]}"',
                    line,
                )
                out_lines.append(rewritten)
            else:
                wrapped, child_entry = self._wrap_relative(
                    stripped, manifest_url, manifest_entry,
                    is_manifest=manifest_entry.hls_png_wrapped and pending_variant,
                )
                out_lines.append(wrapped)
                if child_entry is not None and pending_duration is not None:
                    segment_entries.append(child_entry)
                    segment_durations.append(pending_duration)
                pending_duration = None
                pending_variant = False
        return "\n".join(out_lines) + "\n", segment_entries, segment_durations

    def _wrap_relative(
        self,
        url: str,
        base_url: str,
        manifest_entry: _Entry,
        *, is_manifest: bool = False,
    ) -> Tuple[str, Optional[_Entry]]:
        if not url:
            return url, None
        if url.startswith(self.base_url):
            return url, None
        absolute = urljoin(base_url, url)
        if not absolute.lower().startswith(("http://", "https://")):
            return url, None
        # Reuse an existing child for this upstream URL. Without this, every
        # manifest re-fetch (live HLS, Cache-Control: no-store) registers a
        # brand-new _Entry + temp file for each segment and never frees the
        # previous batch.
        with self._lock:
            existing = manifest_entry.hls_child_by_upstream.get(absolute)
        if existing is not None and not existing.closed:
            if is_manifest:
                existing.force_manifest = True
                existing.proxy_ext = "m3u8"
            existing_token = self._token_for(existing)
            if existing_token is not None:
                return (
                    f"{self.base_url}/play/{existing_token}.{existing.proxy_ext}",
                    existing,
                )
        same_host = (
            urlparse(absolute).hostname == urlparse(manifest_entry.upstream).hostname
        )
        child_headers = (
            manifest_entry.extra_headers
            if same_host
            else {
                key: value
                for key, value in manifest_entry.extra_headers.items()
                if key.lower() in _CROSS_HOST_MEDIA_HEADERS
            }
        )
        local = self.register(
            absolute,
            referer=manifest_entry.referer,
            extra_headers=child_headers,
            parent=manifest_entry,
            cookiejar=manifest_entry.cookiejar,
            is_hls=is_manifest,
            hls_png_wrapped=manifest_entry.hls_png_wrapped,
        )
        leaf = local.rsplit("/", 1)[-1]
        token = leaf.rsplit(".", 1)[0]
        with self._lock:
            child = self._entries.get(token)
            if child is not None:
                manifest_entry.hls_child_by_upstream[absolute] = child
        return local, child
