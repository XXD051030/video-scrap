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
import time
import shutil
from contextlib import contextmanager
from concurrent.futures import Future, wait, FIRST_COMPLETED
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urljoin, urlparse

import requests

from .browser_cookies import browser_cookiejar, build_scoped_session, scoped_headers
from .hls_payload import MAX_HLS_PAYLOAD_BYTES, decode_hls_payload
from .http_limits import (
    iter_limited_response, read_limited_response, abort_response,
    resource_identity_url, iter_continuous_response,
)
from .temp_ownership import create_owned_tempdir, owned_tempdir_io


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

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_SEGMENT_BYTES = 64 * 1024 * 1024
MAX_CACHE_BYTES = 512 * 1024 * 1024
MAX_ENTRY_CACHE_BYTES = 64 * 1024 * 1024
MAX_LIVE_RETIRED_ENTRIES = 32
MAX_MANIFEST_RESOURCES = 20000
CACHE_BLOCK_BYTES = 4096
LIVE_GRACE_SECONDS = 10.0
READ_DEADLINE_SECONDS = 120.0
# Bound decoded private bodies even when many slow player requests overlap.
MAX_WRAPPED_PAYLOAD_REQUESTS = 2
MAX_PROXY_ERROR_CHARS = 1024


class _BoundedHlsPool:
    """No queued backlog and no executor threads that can hold process exit."""

    def __init__(self):
        self._slots = threading.BoundedSemaphore(PLAYBACK_HLS_PREFETCH_CONNECTIONS)
        self._closed = False
        self._lock = threading.Lock()
        self._futures = set()

    def submit(self, function, *args):
        with self._lock:
            if self._closed or not self._slots.acquire(blocking=False):
                return None
            future = Future()
            self._futures.add(future)
        def execute():
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(function(*args))
                    except BaseException as error:
                        future.set_exception(error)
            finally:
                with self._lock:
                    self._futures.discard(future)
                self._slots.release()
        try:
            threading.Thread(target=execute, name="HlsPrefetch", daemon=True).start()
        except BaseException:
            with self._lock:
                self._futures.discard(future)
            self._slots.release()
            raise
        return future

    def close(self):
        with self._lock:
            self._closed = True
            futures = list(self._futures)
        for future in futures:
            future.cancel()


class _CacheBudget:
    def __init__(self, limit=MAX_CACHE_BYTES):
        self.limit = limit
        self.used = 0
        self.lock = threading.Lock()

    def reserve(self, count, directory):
        with self.lock:
            if self.used + count > self.limit:
                return False
            try:
                if shutil.disk_usage(directory).free < count + 64 * 1024 * 1024:
                    return False
            except OSError:
                return False
            self.used += count
            return True

    def release(self, count):
        with self.lock:
            self.used = max(0, self.used - count)


def _strong_etag(response):
    value = (response.headers.get("ETag") or "").strip()
    return value if re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]*"', value) else None


def _retry_after(response) -> float:
    value = (getattr(response, "headers", {}) or {}).get("Retry-After", "")
    try:
        return min(120.0, max(0.0, float(value)))
    except (ValueError, TypeError, OverflowError):
        try:
            from email.utils import parsedate_to_datetime
            return min(120.0, max(0.0, parsedate_to_datetime(value).timestamp() - time.time()))
        except (ValueError, TypeError, OverflowError):
            return 0.0


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
    if value is None:
        return None
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value.strip(), re.IGNORECASE)
    if match is None:
        raise ValueError("Only a valid single byte range is supported")
    a, b = match.groups()
    start = int(a) if a else None
    end = int(b) if b else None
    if (
        (start is None and (end is None or end == 0))
        or (start is not None and end is not None and end < start)
    ):
        raise ValueError("Unsatisfiable byte range")
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

    Each entry owns a sparse temp file, without keeping its handle open.
    Bytes fetched from upstream are written at their absolute offset, and
    a list of cached
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
        cache_budget: Optional[_CacheBudget] = None,
    ) -> None:
        self.upstream = upstream
        self.headers = headers
        self.referer = referer
        self.parent = parent
        self.extra_headers = dict(extra_headers or {})
        self.cookiejar = cookiejar
        self.children: List["_Entry"] = []
        self.cache_budget = cache_budget or _CacheBudget()
        self._allocated_bytes = 0
        self._allocated_blocks = set()
        self._response_lock = threading.RLock()
        self._resource_lock = threading.RLock()
        self._responses_lock = threading.Lock()
        self._responses = set()
        self.cancel_event = threading.Event()
        self.resource_etag = None
        self.resource_url = None
        self.resource_changed = False
        self.last_response_cacheable = True
        self.range_cache_supported = None
        self.retry_after = 0.0
        self.active_requests = 0
        self._superseded = False
        self.retired_at = None
        self.prefetch_generation = 0

        fd, path = tempfile.mkstemp(
            dir=str(cache_dir), prefix="vs_", suffix=".bin"
        )
        # A manifest can register thousands of segments before any is played.
        # Keep the stable, private path but open it only during cache I/O.
        os.close(fd)
        self.cache_path = Path(path)
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
        self.hls_segment_ranges = []
        self.hls_media_sequence = 0
        self.hls_player_sequence = None
        self.hls_live = False
        self._manifest_lock = threading.RLock()
        self.hls_index: Optional[int] = None
        # Latest segment index requested by a real player GET.
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

    def conditional_headers(self):
        return {"If-Match": self.resource_etag} if self.resource_etag else {}

    def accept_response(self, response):
        """Bind byte caches to a strong resource version before publishing data."""
        with self._resource_lock:
            if self.resource_changed:
                return False
            if response.status_code not in (200, 206):
                # Error bodies have their own validators. A transient origin
                # failure must not invalidate or bind the cached media entity.
                if response.status_code == 412 and self.resource_etag is not None:
                    self.resource_changed = True
                    self.discard_cache()
                return False
            tag = _strong_etag(response)
            final_url = resource_identity_url(response, self.upstream)
            if self.resource_etag is not None and (
                tag != self.resource_etag or final_url != self.resource_url
            ):
                self.resource_changed = True
                self.discard_cache()
                return False
            if tag is not None:
                self.resource_etag = tag
                self.resource_url = final_url
            self.last_response_cacheable = tag is not None
            self.range_cache_supported = tag is not None
            return True

    def track_response(self, response):
        with self._responses_lock:
            if self.closed:
                response.close()
                raise InterruptedError("Media request cancelled")
            self._responses.add(response)
        return response

    def untrack_response(self, response):
        with self._responses_lock:
            self._responses.discard(response)
        response.close()

    def discard_cache(self):
        with self._file_lock:
            if not self.closed:
                with self.cache_path.open("r+b") as stream:
                    stream.truncate(0)
            with self._intervals_lock:
                self._intervals = []
            self.cache_budget.release(self._allocated_bytes)
            self._allocated_bytes = 0
            self._allocated_blocks.clear()

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

    def write_at(self, offset: int, data: bytes, *, publish: bool = True) -> bool:
        if not data or self.closed:
            return
        with self._resource_lock, self._file_lock:
            if self.closed or self.resource_changed:
                return False
            blocks = set(range(offset // CACHE_BLOCK_BYTES,
                               (offset + len(data) - 1) // CACHE_BLOCK_BYTES + 1))
            new_blocks = blocks - self._allocated_blocks
            additional = len(new_blocks) * CACHE_BLOCK_BYTES
            if self._allocated_bytes + additional > MAX_ENTRY_CACHE_BYTES:
                return False
            if not self.cache_budget.reserve(additional, self.cache_path.parent):
                return False
            self._allocated_bytes += additional
            self._allocated_blocks.update(new_blocks)
            # r+b never recreates a removed cache. Holding the lock until
            # the handle is closed also lets unregister safely unlink it on
            # Windows, and prevents close racing the interval publication.
            with self.cache_path.open("r+b") as cache_file:
                cache_file.seek(offset)
                cache_file.write(data)
            if publish:
                self.merge_interval(offset, offset + len(data))
            return True

    def read_at(self, offset: int, length: int) -> bytes:
        if length <= 0 or self.closed:
            return b""
        with self._file_lock:
            if self.closed:
                return b""
            with self.cache_path.open("rb") as cache_file:
                cache_file.seek(offset)
                return cache_file.read(length)

    def update_player_offset(self, offset: int) -> None:
        if offset < 0:
            return
        self.last_player_offset = offset
        self.player_data_requested = True
        self.player_event.set()

    def close(self) -> None:
        with self._file_lock:
            if self.closed:
                return
            self.closed = True
            self.cancel_event.set()
            self.prefetch_generation += 1
            self.player_event.set()
            self.cache_budget.release(self._allocated_bytes)
            self._allocated_bytes = 0
            self._allocated_blocks.clear()
            self.hls_child_by_upstream.clear()
            try:
                self.cache_path.unlink()
            except OSError:
                pass
        with self._responses_lock:
            responses = list(self._responses)
            self._responses.clear()
        for response in responses:
            try:
                abort_response(response)
            except Exception:
                pass


class _Prefetcher(threading.Thread):
    """Cancelable, bounded byte prefetch with backoff after every failed fetch."""

    def __init__(self, entry, get_buffer_bytes, session, on_status=None):
        super().__init__(name=f"PrefetchSeq[{entry.cache_path.name}]", daemon=True)
        self.entry, self.get_buffer_bytes = entry, get_buffer_bytes
        self.session, self.on_status = session, on_status
        self.failures = 0

    def run(self):
        entry = self.entry
        while not entry.closed:
            if (not entry.player_data_requested or self.get_buffer_bytes() <= 0
                    or entry.range_cache_supported is False
                    or entry._allocated_bytes >= MAX_ENTRY_CACHE_BYTES
                    or entry.cache_budget.used >= entry.cache_budget.limit):
                entry.player_event.wait(1.0)
                entry.player_event.clear()
                continue
            anchor = entry.last_player_offset
            limit = anchor + min(self.get_buffer_bytes(), MAX_ENTRY_CACHE_BYTES)
            if entry.total_size is not None:
                limit = min(limit, entry.total_size)
            start = entry.skip_player_inflight(entry.first_uncached_offset(anchor))
            next_cached = entry.first_cached_start_after(start)
            if next_cached is not None:
                limit = min(limit, next_cached)
            if start >= limit:
                entry.player_event.wait(1.0)
                entry.player_event.clear()
                continue
            # One bounded range per request; changing the playback anchor
            # never leaves a whole movie download hidden in the background.
            end = min(limit, start + PREFETCH_CHUNK_SIZE) - 1
            try:
                progressed = self._fetch_into_cache(start, end)
            except Exception:
                progressed = False
            if progressed:
                self.failures = 0
            else:
                self.failures += 1
                delay = max(entry.retry_after, min(30.0, 0.5 * 2 ** min(self.failures - 1, 6)))
                if self.failures == 5 and self.on_status:
                    self.on_status("Buffering paused after repeated upstream failures; re-scrape if authentication expired.")
                entry.cancel_event.wait(delay)

    def _fetch_into_cache(self, start, end_inclusive):
        entry = self.entry
        with entry._response_lock:
            if entry.closed or entry.resource_changed:
                return False
            headers = dict(entry.headers)
            headers.update(entry.conditional_headers())
            headers.update({"Range": f"bytes={start}-{end_inclusive}", "Accept-Encoding": "identity"})
            response = None
            try:
                response = entry.track_response(self.session.get(
                    entry.upstream, headers=headers, stream=True, timeout=(5, 5),
                    allow_redirects=True, **entry.cookie_kwargs,
                ))
                entry.retry_after = _retry_after(response)
                if response.status_code not in (200, 206) or not _has_identity_encoding(response):
                    return False
                if not entry.accept_response(response):
                    return False
                # No strong validator means a later range cannot safely be
                # joined to this one. Serve on demand, without background cache.
                if _strong_etag(response) is None:
                    return False
                checked = None
                if response.status_code == 206:
                    checked = _validated_partial_range(response, start, end_inclusive, entry.total_size)
                    if checked is None:
                        return False
                    declared_end, total = checked
                    if total is not None:
                        entry.total_size = total
                    position = start
                else:
                    length = _parse_content_length(response.headers.get("Content-Length"))
                    if length is not None:
                        if entry.total_size is not None and entry.total_size != length:
                            return False
                        entry.total_size = length
                    position = 0
                    declared_end = end_inclusive
                    # Never discard gigabytes to satisfy a small seek-ahead
                    # request when Range is ignored. Let player demand decide.
                    if start > PREFETCH_CHUNK_SIZE:
                        return False
                initial = entry.cached_bytes()
                for chunk in iter_limited_response(
                    response, max(entry.total_size or 0, end_inclusive + 1, MAX_ENTRY_CACHE_BYTES),
                    20.0, lambda: entry.closed,
                ):
                    if entry.closed:
                        return False
                    if not chunk:
                        continue
                    safe = chunk[:max(0, declared_end + 1 - position)]
                    if not safe:
                        break
                    if not entry.write_at(position, safe):
                        return False
                    position += len(safe)
                    if position > declared_end:
                        break
                    if abs(entry.last_player_offset - start) > 4 * 1024 * 1024:
                        break
                return entry.cached_bytes() > initial
            except Exception:
                return False
            finally:
                if response is not None:
                    entry.untrack_response(response)


class _HlsPrefetcher(threading.Thread):
    """Small generation-aware HLS batches with no stale executor backlog."""

    def __init__(self, manifest_entry, get_buffer_seconds, session, on_status=None,
                 fetch_wrapped=None, pool=None):
        super().__init__(name=f"HlsPre[{manifest_entry.cache_path.name}]", daemon=True)
        self.entry, self.get_buffer_seconds = manifest_entry, get_buffer_seconds
        self.session, self.on_status = session, on_status
        self.fetch_wrapped = fetch_wrapped
        self.pool = pool or _BoundedHlsPool()
        self._owns_pool = pool is None
        self._last_announced_anchor = None
        self.failures = 0
        self._inflight = set()
        self._inflight_lock = threading.Lock()

    def run(self):
        entry = self.entry
        pending = {}
        try:
            while not entry.closed:
                target = max(0, self.get_buffer_seconds())
                with entry._manifest_lock:
                    generation = entry.prefetch_generation
                    segments = list(entry.hls_segment_entries)
                    durations = list(entry.hls_segment_durations)
                    ranges = list(entry.hls_segment_ranges)
                    player_index = entry.hls_player_segment_idx
                if (not segments or target <= 0
                        or entry.cache_budget.used >= entry.cache_budget.limit):
                    entry.player_event.wait(0.5)
                    entry.player_event.clear()
                    continue
                start = min(max(0, player_index), len(segments) - 1)
                total_duration = 0.0
                queue = []
                for index in range(start, len(segments)):
                    segment = segments[index]
                    interval = ranges[index] if index < len(ranges) else None
                    offset, end = interval if interval else (0, segment.total_size)
                    covered = segment.cached_run_end(offset)
                    if not segment.closed and (end is None or covered < end):
                        queue.append((segment, interval))
                    total_duration += max(0.5, durations[index] if index < len(durations) else 4.0)
                    if total_duration >= target or len(queue) >= PLAYBACK_HLS_PREFETCH_CONNECTIONS:
                        break
                for segment, interval in queue:
                    key = (id(segment), interval)
                    with self._inflight_lock:
                        if key in self._inflight:
                            continue
                        future = self.pool.submit(self._fetch_segment, segment, interval, generation)
                        if future is None:
                            break
                        self._inflight.add(key)
                        pending[future] = key
                if not pending:
                    entry.player_event.wait(0.5)
                    entry.player_event.clear()
                    continue
                progressed = False
                while pending and not entry.closed and generation == entry.prefetch_generation:
                    done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in done:
                        key = pending.pop(future)
                        with self._inflight_lock:
                            self._inflight.discard(key)
                        try:
                            progressed = bool(future.result()) or progressed
                        except Exception:
                            pass
                for future, key in list(pending.items()):
                    future.cancel()
                    # Running tasks keep their identity until actual completion.
                    def complete(_future, inflight_key=key):
                        with self._inflight_lock:
                            self._inflight.discard(inflight_key)
                    future.add_done_callback(complete)
                pending.clear()
                if progressed:
                    self.failures = 0
                elif generation == entry.prefetch_generation:
                    self.failures += 1
                    delay = max(entry.retry_after, min(30.0, 0.5 * 2 ** min(self.failures - 1, 6)))
                    if self.failures == 5:
                        self._emit("HLS buffering paused after repeated upstream failures; re-scrape if authentication expired.")
                    entry.cancel_event.wait(delay)
        finally:
            for future in pending:
                future.cancel()
            if self._owns_pool:
                self.pool.close()

    def _fetch_segment(self, segment, interval=None, generation=None):
        parent = self.entry
        def cancelled():
            return (segment.closed or parent.closed or
                    (generation is not None and generation != parent.prefetch_generation))
        start, end = interval if interval else (0, None)
        # Wrapped fetches acquire the global payload slot before the entry
        # lock, matching player requests and avoiding an inverted lock order.
        if segment.hls_png_wrapped:
            if self.fetch_wrapped is not None and not cancelled():
                return self.fetch_wrapped(segment, cancelled=cancelled) is None and not cancelled()
            return False
        with segment._response_lock:
            if cancelled():
                return False
            if end is not None and segment.cached_run_end(start) >= end:
                return True
            if interval is None and segment.total_size is not None and segment.cached_run_end(0) >= segment.total_size:
                return True
            headers = dict(segment.headers)
            headers.update(segment.conditional_headers())
            headers["Accept-Encoding"] = "identity"
            if interval is not None:
                headers["Range"] = f"bytes={start}-{end - 1}"
            response = None
            unpublished = False
            try:
                response = segment.track_response(self.session.get(
                    segment.upstream, headers=headers, stream=True, timeout=(5, 5),
                    allow_redirects=True, **segment.cookie_kwargs,
                ))
                parent.retry_after = _retry_after(response)
                if not _has_identity_encoding(response) or not segment.accept_response(response):
                    return False
                if interval is not None:
                    checked = _validated_partial_range(response, start, end - 1, segment.total_size)
                    if checked is None or _strong_etag(response) is None:
                        return False
                    declared_end, total = checked
                    if declared_end != end - 1:
                        return False
                    if total is not None:
                        segment.total_size = total
                    max_bytes = end - start
                else:
                    if response.status_code != 200:
                        return False
                    length = _parse_content_length(response.headers.get("Content-Length"))
                    if length is not None and length > MAX_SEGMENT_BYTES:
                        return False
                    max_bytes = MAX_SEGMENT_BYTES
                    # Full unvalidated bodies are published only after EOF;
                    # their complete cache never joins independent responses.
                    unpublished = _strong_etag(response) is None
                position = start
                for chunk in iter_limited_response(response, max_bytes, READ_DEADLINE_SECONDS, cancelled):
                    if not segment.write_at(position, chunk, publish=not unpublished):
                        raise ValueError("Playback cache budget reached")
                    position += len(chunk)
                expected = end if interval is not None else (
                    start + length if length is not None else position)
                if position != expected or position <= start:
                    raise ValueError("Incomplete HLS prefetch body")
                if unpublished:
                    segment.merge_interval(start, position)
                if interval is None:
                    segment.total_size = position
                return True
            except Exception:
                if unpublished:
                    segment.discard_cache()
                return False
            finally:
                if response is not None:
                    segment.untrack_response(response)

    def _emit(self, message):
        if self.on_status is not None:
            try:
                self.on_status(message)
            except Exception:
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
        self._lock = threading.RLock()
        self._cache_budget = _CacheBudget()
        self._hls_pool = _BoundedHlsPool()
        self._wrapped_payload_slots = threading.BoundedSemaphore(MAX_WRAPPED_PAYLOAD_REQUESTS)
        self._wrapped_payload_local = threading.local()
        self._unsubscribe_settings = None
        self._stopped = False
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._port: int = 0

        self._cache_dir = create_owned_tempdir("video_scraper_cache_")
        self._session = build_scoped_session(pool=32)
        self._settings_store = settings_store
        self._on_status = on_status
        self._buffer_seconds: int = DEFAULT_BUFFER_SECONDS
        if settings_store is not None:
            initial = settings_store.get()
            try:
                self._buffer_seconds = max(
                    0, int(initial.playback_buffer_seconds)
                )
            except (TypeError, ValueError, OverflowError, AttributeError):
                pass
            # Subscribe without firing the initial callback - the host
            # app's UI may not be ready to receive status messages yet.
            self._unsubscribe_settings = settings_store.subscribe(
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
        if self._stopped:
            raise RuntimeError("Media proxy is stopped")
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
        self._server.daemon_threads = True
        self._server.block_on_close = False
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="MediaProxyServer",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if callable(self._unsubscribe_settings):
            self._unsubscribe_settings()
            self._unsubscribe_settings = None
        # Mark every entry cancelled and close active HTTP responses before
        # waiting for any server or executor thread.
        with self._lock:
            workers = list(self._prefetchers.values()) + list(self._hls_prefetchers.values())
        self.clear()
        try:
            self._session.close()
        except Exception:
            pass
        self._hls_pool.close()
        deadline = time.monotonic() + 2.0
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
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
            with owned_tempdir_io(self._cache_dir):
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
        if self._stopped:
            return
        seconds = getattr(
            settings, "playback_buffer_seconds", DEFAULT_BUFFER_SECONDS
        )
        try:
            self._buffer_seconds = max(0, int(seconds))
        except (TypeError, ValueError, OverflowError):
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
        if self._stopped or self._on_status is None:
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
        browser_cookies: Optional[List[dict]] = None,
        is_dash: bool = False,
    ) -> str:
        if not upstream:
            raise ValueError("upstream URL required")
        if self._stopped:
            raise RuntimeError("Media proxy is stopped")
        ancestor, depth = parent, 0
        while ancestor is not None:
            depth += 1
            if depth > 16:
                raise ValueError("HLS playlist nesting exceeds the limit")
            ancestor = ancestor.parent
        if is_dash or _derive_ext(upstream) == "mpd":
            raise ValueError("DASH/MPD preview is not supported; download the video to play it locally.")
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
            cookiejar=(
                browser_cookiejar(browser_cookies, base=cookiejar)
                if browser_cookies else cookiejar
            ),
            cache_budget=self._cache_budget,
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
        if parent is None and not hls_png_wrapped and not is_hls and _derive_ext(upstream) != "m3u8":
            prefetcher = _Prefetcher(
                entry,
                get_buffer_bytes=self.buffer_bytes,
                session=self._session,
                on_status=self._emit,
            )
            with self._lock:
                self._prefetchers[token] = prefetcher
            prefetcher.start()

        ext = "m3u8" if is_hls else _derive_ext(upstream)
        if ext == "bin" and parent is not None:
            # FFmpeg's HLS demuxer rejects segment/manifest URLs whose
            # extension isn't on its allow-list, so a .jpg-disguised stream
            # would download but never play. Give it a media extension.
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

    def _retire_superseded_children(self, parent: _Entry) -> None:
        """Keep old role generations only while a player still uses them."""
        with self._lock:
            for child in list(parent.children):
                if (not child._superseded
                        or parent.hls_child_by_upstream.get(child.upstream) is child):
                    continue
                descendants = [child]
                while descendants:
                    current = descendants.pop()
                    if current.active_requests:
                        break
                    descendants.extend(current.children)
                else:
                    parent.children.remove(child)
                    token = self._token_by_entry.pop(id(child), None)
                    if token is not None:
                        self._entries.pop(token, None)
                        self._prefetchers.pop(token, None)
                        self._hls_prefetchers.pop(token, None)
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

        with self._lock:
            if entry.closed:
                request.send_response(410)
                request.end_headers()
                return
            entry.active_requests += 1
        try:
            self._handle_entry(request, entry, body, method)
        finally:
            with self._lock:
                entry.active_requests -= 1
                # A nested request can be the last user of a superseded
                # manifest. Reclaim that old generation after its body ends.
                ancestor = entry.parent
                while ancestor is not None:
                    self._retire_superseded_children(ancestor)
                    ancestor = ancestor.parent

    def _handle_entry(self, request, entry, body, method):
        # HEAD/probe requests do not advance the playback anchor.
        if body and method == "GET":
            self._note_player_segment(entry, request.headers.get("Range"))

        if entry.hls_png_wrapped:
            if self._likely_manifest(entry):
                self._serve_wrapped_manifest(request, entry, body=body)
                return
            try:
                # Keep the payload slot until private serving finishes. A slow
                # client cannot retain another decoded body outside this bound.
                with self._wrapped_payload_slot(entry):
                    try:
                        private_payload = self._ensure_wrapped_cache(entry)
                        try:
                            if private_payload is not None:
                                self._serve_wrapped_payload(request, entry, private_payload, body=body)
                            elif not body:
                                request.send_response(200)
                                request.send_header("Content-Type", self._guess_content_type(entry))
                                request.send_header("Content-Length", str(entry.total_size))
                                request.send_header("Accept-Ranges", "bytes")
                                request.end_headers()
                            else:
                                self._serve_range(request, entry)
                        finally:
                            private_payload = None
                    except Exception as exc:  # noqa: BLE001
                        # Error tracebacks can own decoded bytes too. Consume
                        # the error and finish its reply before releasing the
                        # slot, including when a slow client blocks that reply.
                        self._emit(f"Player wrapped-media error: {exc}")
                        try:
                            self._send_manifest_error(request, 502, str(exc), body=body)
                        except OSError:
                            pass
            except InterruptedError as exc:
                # Slot acquisition was cancelled before any body was fetched.
                self._emit(f"Player wrapped-media error: {exc}")
                self._send_manifest_error(request, 502, str(exc), body=body)
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

    def _note_player_segment(self, entry: _Entry, range_header=None) -> None:
        parent = entry.parent
        if parent is None:
            return
        try:
            requested = _parse_range_header(range_header)
        except ValueError:
            requested = None
        with parent._manifest_lock:
            candidates = [index for index, child in enumerate(parent.hls_segment_entries) if child is entry]
            if not candidates and entry.hls_index is not None:
                candidates = [entry.hls_index]
            if not candidates:
                return
            index = candidates[0]
            if requested is not None:
                for candidate in candidates:
                    interval = parent.hls_segment_ranges[candidate] if candidate < len(parent.hls_segment_ranges) else None
                    if interval and requested[0] is not None and interval[0] <= requested[0] < interval[1]:
                        index = candidate
                        break
            if parent.hls_player_segment_idx != index:
                parent.prefetch_generation += 1
            parent.hls_player_segment_idx = index
            parent.hls_player_sequence = parent.hls_media_sequence + index
        parent.player_event.set()

    def _likely_manifest(self, entry: _Entry) -> bool:
        if entry.force_manifest:
            return True
        path = urlparse(entry.upstream).path.lower()
        return path.endswith(".m3u8")

    # ----- manifest --------------------------------------------------------

    @contextmanager
    def _wrapped_payload_slot(self, entry, cancelled=None):
        depth = getattr(self._wrapped_payload_local, "depth", 0)
        acquired = False
        try:
            if depth == 0:
                while not self._wrapped_payload_slots.acquire(timeout=.1):
                    if self._stopped or entry.closed or (cancelled is not None and cancelled()):
                        raise InterruptedError("Wrapped media request cancelled")
                acquired = True
            if self._stopped or entry.closed or (cancelled is not None and cancelled()):
                raise InterruptedError("Wrapped media request cancelled")
            self._wrapped_payload_local.depth = depth + 1
            yield
        finally:
            self._wrapped_payload_local.depth = depth
            if acquired:
                self._wrapped_payload_slots.release()

    def _fetch_wrapped_payload(self, entry: _Entry, cancelled=None) -> Tuple[bytes, str]:
        with self._wrapped_payload_slot(entry, cancelled):
            return self._fetch_wrapped_payload_unlocked(entry, cancelled)

    def _fetch_wrapped_payload_unlocked(self, entry: _Entry, cancelled=None) -> Tuple[bytes, str]:
        """Fetch a full envelope; upstream byte offsets are not media offsets."""
        headers = dict(entry.headers)
        headers.pop("Range", None)
        headers["Accept-Encoding"] = "identity"
        max_wire_bytes = MAX_HLS_PAYLOAD_BYTES + 1024 * 1024
        with self._session.get(
            entry.upstream, headers=headers, stream=True, timeout=(5, 5),
            allow_redirects=True, **entry.cookie_kwargs,
        ) as response:
            if response.status_code != 200:
                raise ValueError(f"Wrapped media returned HTTP {response.status_code}")
            length = _parse_content_length(response.headers.get("Content-Length"))
            if length is not None and length > max_wire_bytes:
                raise ValueError("Wrapped HLS response exceeds the size limit")
            entry.track_response(response)
            try:
                wire = read_limited_response(response, max_wire_bytes, READ_DEADLINE_SECONDS,
                                             lambda: entry.closed or (cancelled is not None and cancelled()))
            finally:
                with entry._responses_lock:
                    entry._responses.discard(response)
            payload = decode_hls_payload(wire)
            if not payload or len(payload) > MAX_HLS_PAYLOAD_BYTES:
                raise ValueError("Empty or oversized wrapped HLS payload")
            return payload, response.url or entry.upstream

    def _ensure_wrapped_cache(self, entry: _Entry, cancelled=None, *, private_fallback=True) -> Optional[bytes]:
        # Player and prefetch requests share a single complete, validated
        # fetch. Failed/partial envelopes never enter the sparse byte cache.
        with self._wrapped_payload_slot(entry, cancelled), entry._response_lock, entry._wrapped_fetch_lock:
            if entry.closed:
                raise RuntimeError("Wrapped media request cancelled")
            if entry.total_size is not None and entry.cached_run_end(0) >= entry.total_size:
                return
            if cancelled is None:
                payload, _ = self._fetch_wrapped_payload(entry)
            else:
                payload, _ = self._fetch_wrapped_payload(entry, cancelled=cancelled)
            if entry.closed or (cancelled is not None and cancelled()):
                raise RuntimeError("Wrapped media request cancelled")
            if not entry.write_at(0, payload):
                # A cache admission failure must not turn a complete, valid
                # segment into a playback failure. The caller owns this bounded
                # private body and holds its payload slot while serving it.
                if private_fallback:
                    return payload
                # Completed prefetch Futures retain exception tracebacks.
                # Release this body before its slot can admit another one.
                payload = None
                raise ValueError("Playback cache budget reached")
            entry.total_size = len(payload)
            payload = None
            return None

    def _prefetch_wrapped_cache(self, entry, cancelled=None):
        # Completed Futures must not retain payloads through error tracebacks.
        # Consume failures while still holding the outermost payload slot.
        with self._wrapped_payload_slot(entry, cancelled):
            try:
                return self._ensure_wrapped_cache(entry, cancelled=cancelled, private_fallback=False)
            except Exception:
                return False

    def _serve_wrapped_payload(self, request, entry, payload, *, body):
        try:
            total = len(payload)
            try:
                requested = _parse_range_header(request.headers.get("Range"))
            except ValueError:
                self._send_range_error(request, total)
                return
            start, end = 0, total - 1
            if requested is not None:
                first, last = requested
                start = max(0, total - last) if first is None else first
                end = total - 1 if first is None or last is None else min(last, total - 1)
                if start >= total:
                    self._send_range_error(request, total)
                    return
            request.send_response(206 if requested is not None else 200)
            if requested is not None:
                request.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            request.send_header("Content-Type", self._guess_content_type(entry))
            request.send_header("Content-Length", str(end - start + 1))
            request.send_header("Accept-Ranges", "bytes")
            request.send_header("Cache-Control", "no-store")
            request.end_headers()
            if body:
                for offset in range(start, end + 1, CHUNK_SIZE):
                    if entry.closed:
                        return
                    try:
                        request.wfile.write(payload[offset:min(offset + CHUNK_SIZE, end + 1)])
                    except (BrokenPipeError, ConnectionResetError):
                        return
        finally:
            payload = None

    def _serve_wrapped_manifest(
        self, request: BaseHTTPRequestHandler, entry: _Entry, *, body: bool,
    ) -> None:
        with self._wrapped_payload_slot(entry):
            self._serve_wrapped_manifest_unlocked(request, entry, body=body)

    def _serve_wrapped_manifest_unlocked(self, request, entry, *, body):
        try:
            payload, final_url = self._fetch_wrapped_payload(entry)
            text = payload.decode("utf-8-sig")
            if not text.lstrip().startswith("#EXTM3U"):
                raise ValueError("Wrapped player response is not an HLS manifest")
            if entry.closed:
                raise RuntimeError("Wrapped manifest request cancelled")
            rewritten, _, _ = self._rewrite_m3u8(text, final_url, entry)
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
                stream=True,
                timeout=(5, 5),
                allow_redirects=True,
                **entry.cookie_kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc)[:MAX_PROXY_ERROR_CHARS]
            self._emit(f"Player manifest error: {message}")
            self._send_manifest_error(request, 502, f"Upstream error: {message}")
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
            final_url = upstream.url or entry.upstream
            entry.track_response(upstream)
            text = read_limited_response(upstream, MAX_MANIFEST_BYTES, 30.0, lambda: entry.closed).decode("utf-8-sig")
            if not text.lstrip().startswith("#EXTM3U"):
                self._send_manifest_error(request, 502, "Upstream response is not an HLS manifest; login or re-scraping may be required.")
                return True
            if entry.hls_variant_url:
                try:
                    text = _select_hls_master_variant(
                        text, final_url, entry.hls_variant_url
                    )
                except ValueError as exc:
                    self._emit(f"Player manifest error: {exc}")
                    self._send_manifest_error(request, 502, str(exc))
                    return True
            rewritten, _, _ = self._rewrite_m3u8(
                text, final_url, entry
            )
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
        except (ValueError, UnicodeError, TimeoutError, InterruptedError,
                requests.RequestException) as exc:
            message = str(exc)[:MAX_PROXY_ERROR_CHARS]
            self._emit(f"Player manifest error: {message}")
            self._send_manifest_error(request, 502, message)
            return True
        finally:
            with entry._responses_lock:
                entry._responses.discard(upstream)
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
                on_status=self._emit,
                fetch_wrapped=self._prefetch_wrapped_cache,
                pool=self._hls_pool,
            )
            self._hls_prefetchers[token] = worker
        worker.start()

    @staticmethod
    def _send_manifest_error(
        request: BaseHTTPRequestHandler, status: int, message: str,
        *, body: bool = True,
    ) -> None:
        # Keep upstream exception/error text bounded before encoding. UTF-8
        # needs at most four bytes per character, including replacement of
        # invalid surrogates; a malicious origin cannot inflate this reply.
        payload = message[:MAX_PROXY_ERROR_CHARS].encode("utf-8", errors="replace")
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

    def _serve_head(self, request: BaseHTTPRequestHandler, entry: _Entry) -> None:
        with entry._response_lock:
            self._serve_head_unlocked(request, entry)

    def _serve_head_unlocked(
        self, request: BaseHTTPRequestHandler, entry: _Entry
    ) -> None:
        headers = dict(entry.headers)
        headers.update(entry.conditional_headers())
        headers["Accept-Encoding"] = "identity"
        try:
            resp = self._session.request(
                method="HEAD",
                url=entry.upstream,
                headers=headers,
                timeout=(5, 5),
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
            entry.track_response(resp)
            if resp.status_code == 200 and not entry.accept_response(resp):
                self._send_manifest_error(request, 502, "Media changed upstream; re-scrape the video.", body=False)
                return
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
                entry.untrack_response(resp)
            except Exception:  # noqa: BLE001
                pass

    # ----- range serve -----------------------------------------------------

    def _serve_range(
        self, request: BaseHTTPRequestHandler, entry: _Entry
    ) -> None:
        if entry.resource_changed:
            self._send_manifest_error(request, 502, "Media changed upstream; re-scrape to start a fresh playback session.")
            return
        range_header = request.headers.get("Range")
        try:
            rng = _parse_range_header(range_header)
        except ValueError:
            self._send_range_error(request, entry.total_size)
            return

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
                    self._proxy_passthrough(request, entry, range_header=range_header)
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
                self._send_range_error(request, total)
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
            if not yielded or not entry.last_response_cacheable:
                return

    def _fetch_range(
        self, entry: _Entry, start: int, end_inclusive: Optional[int],
    ) -> Iterable[bytes]:
        with entry._response_lock:
            yield from self._fetch_range_unlocked(entry, start, end_inclusive)

    def _fetch_range_unlocked(
        self, entry: _Entry, start: int, end_inclusive: Optional[int]
    ) -> Iterable[bytes]:
        if entry.closed or entry.resource_changed:
            return
        headers = dict(entry.headers)
        headers.update(entry.conditional_headers())
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
                    timeout=(5, 5),
                    allow_redirects=True,
                    **entry.cookie_kwargs,
                )
            except Exception:  # noqa: BLE001
                return

            try:
                entry.track_response(resp)
                if not entry.accept_response(resp):
                    return
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
                reader = iter_limited_response if entry.parent is not None else iter_continuous_response
                for chunk in reader(
                    resp, MAX_SEGMENT_BYTES if entry.parent is not None else max(1, declared_end - start + 1 + CHUNK_SIZE),
                    READ_DEADLINE_SECONDS, lambda: entry.closed,
                ):
                    if entry.closed:
                        return
                    if not chunk:
                        continue
                    safe = chunk[:declared_end + 1 - pos]
                    if not safe:
                        return
                    if _strong_etag(resp) is not None:
                        entry.write_at(pos, safe)
                    pos += len(safe)
                    yield safe
                    if pos > declared_end:
                        return
            finally:
                try:
                    entry.untrack_response(resp)
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
        max_bytes = MAX_SEGMENT_BYTES if entry.parent is not None else max(
            end_inclusive + 1 if end_inclusive is not None else 0,
            entry.total_size or MAX_ENTRY_CACHE_BYTES,
        )
        reader = iter_limited_response if entry.parent is not None else iter_continuous_response
        for chunk in reader(resp, max_bytes, READ_DEADLINE_SECONDS, lambda: entry.closed):
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
            cache_chunk = chunk if end_inclusive is None else chunk[:max(0, end_inclusive + 1 - chunk_start)]
            if _strong_etag(resp) is not None:
                entry.write_at(chunk_start, cache_chunk)
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
            if end_inclusive is not None and chunk_end_excl > end_inclusive:
                return

    def _probe_total_size(self, entry: _Entry) -> None:
        with entry._response_lock:
            self._probe_total_size_unlocked(entry)

    def _probe_total_size_unlocked(self, entry: _Entry) -> None:
        if entry.total_size is not None:
            return
        headers = dict(entry.headers)
        headers.update(entry.conditional_headers())
        headers["Range"] = "bytes=0-0"
        headers["Accept-Encoding"] = "identity"
        try:
            resp = self._session.get(
                entry.upstream,
                headers=headers,
                stream=True,
                timeout=(5, 5),
                allow_redirects=True,
                **entry.cookie_kwargs,
            )
        except Exception:  # noqa: BLE001
            return
        try:
            entry.track_response(resp)
            if not entry.accept_response(resp):
                return
            checked = _validated_partial_range(resp, 0, 0)
            if checked is not None and checked[1] is not None:
                received = 0
                for chunk in iter_limited_response(resp, 2, 15.0, lambda: entry.closed, chunk_size=1):
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
                entry.untrack_response(resp)
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
        headers["Accept-Encoding"] = "identity"
        if range_header:
            headers["Range"] = range_header
        try:
            resp = self._session.get(
                entry.upstream,
                headers=headers,
                stream=True,
                timeout=(5, 5),
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
            entry.track_response(resp)
            request.send_response(resp.status_code)
            for key, value in resp.headers.items():
                if key.lower() in {"transfer-encoding", "connection", "keep-alive"}:
                    continue
                request.send_header(key, value)
            request.end_headers()
            # Preserve upstream representation bytes, even if the server
            # ignores identity and sends gzip. iter_content would decompress
            # while leaving Content-Encoding/Length/Range describing gzip.
            limit = MAX_SEGMENT_BYTES if entry.parent is not None else max(
                _parse_content_length(resp.headers.get("Content-Length")) or 0, 16 * 1024 ** 3,
            )
            reader = iter_limited_response if entry.parent is not None else iter_continuous_response
            for chunk in reader(resp, limit, READ_DEADLINE_SECONDS, lambda: entry.closed, raw=True):
                if not chunk:
                    continue
                try:
                    request.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
        finally:
            try:
                entry.untrack_response(resp)
            except Exception:  # noqa: BLE001
                pass

    # ----- helpers ---------------------------------------------------------

    @staticmethod
    def _send_range_error(request: BaseHTTPRequestHandler, total: Optional[int]) -> None:
        request.send_response(416)
        if total is not None:
            request.send_header("Content-Range", f"bytes */{total}")
        request.send_header("Content-Length", "0")
        request.send_header("Accept-Ranges", "bytes")
        request.end_headers()

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

    @contextmanager
    def _manifest_transaction(self, parent):
        with parent._manifest_lock:
            previous = set(parent.children)
            child_roles = {
                child: (child.force_manifest, child.proxy_ext, child.hls_index, child.retired_at)
                for child in previous
            }
            previous_lookup = dict(parent.hls_child_by_upstream)
            plan_fields = (
                "hls_segment_entries", "hls_segment_durations", "hls_segment_ranges",
                "hls_media_sequence", "hls_live", "hls_player_segment_idx",
                "hls_player_sequence", "prefetch_generation",
            )
            previous_plan = {
                field: list(value) if isinstance(value := getattr(parent, field), list) else value
                for field in plan_fields
            }
            try:
                yield
            except BaseException:
                with self._lock:
                    for child in list(parent.children):
                        if child in previous:
                            continue
                        parent.children.remove(child)
                        token = self._token_by_entry.pop(id(child), None)
                        if token is not None:
                            self._entries.pop(token, None)
                        self._dispose_entry(child)
                    parent.hls_child_by_upstream = previous_lookup
                    for child, role in child_roles.items():
                        child.force_manifest, child.proxy_ext, child.hls_index, child.retired_at = role
                    for field, value in previous_plan.items():
                        setattr(parent, field, value)
                raise
            else:
                # Only a committed replacement may retire the old resource.
                # A failed parse must retain its cache and active requests.
                with self._lock:
                    for child in previous_lookup.values():
                        if parent.hls_child_by_upstream.get(child.upstream) is not child:
                            child._superseded = True
                    self._retire_superseded_children(parent)

    def _rewrite_m3u8(self, text, manifest_url, manifest_entry):
        """Keep logical byte ranges beside shared URI entries for prefetch."""
        first_line = next((line.strip().lstrip("\ufeff") for line in text.splitlines() if line.strip()), "")
        if first_line != "#EXTM3U":
            raise ValueError("Upstream response is not an HLS manifest")
        if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise ValueError("HLS manifest exceeds the size limit")
        resource_count = sum(
            len(_M3U8_URI_ATTR_RE.findall(line)) if line.lstrip().startswith("#") else bool(line.strip())
            for line in text.splitlines()
        )
        if resource_count > MAX_MANIFEST_RESOURCES:
            raise ValueError("HLS manifest has too many resources")
        with self._manifest_transaction(manifest_entry):
            out, segments, durations, ranges = [], [], [], []
            touched, role_updates = set(), {}
            pending_duration, pending_variant, pending_range = None, False, None
            last_range_url, last_range_end = None, 0
            sequence = 0
            for raw_line in text.splitlines():
                line = raw_line.rstrip("\r")
                stripped = line.strip()
                if not stripped:
                    out.append(line)
                    continue
                upper = stripped.upper()
                if upper.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    try:
                        sequence = int(stripped.split(":", 1)[1])
                        if sequence < 0:
                            raise ValueError
                    except ValueError:
                        raise ValueError("Invalid HLS media sequence") from None
                if upper.startswith("#EXT-X-BYTERANGE:"):
                    value = stripped.split(":", 1)[1].strip()
                    match = re.fullmatch(r"([0-9]+)(?:@([0-9]+))?", value)
                    if match is None or int(match.group(1)) <= 0:
                        raise ValueError("Invalid HLS byte range")
                    pending_range = (int(match.group(1)), int(match.group(2)) if match.group(2) else None)
                if stripped.startswith("#"):
                    if upper.startswith("#EXT-X-STREAM-INF:"):
                        if pending_variant or pending_duration is not None:
                            raise ValueError("HLS playlist is missing a media URL")
                        pending_variant = True
                    elif upper.startswith("#EXTINF:"):
                        if pending_variant or pending_duration is not None:
                            raise ValueError("HLS playlist is missing a media URL")
                        pending_variant = False
                        pending_duration = None
                    match = _EXTINF_RE.match(stripped)
                    if upper.startswith("#EXTINF:") and match is None:
                        raise ValueError("Invalid HLS segment duration")
                    if match:
                        duration = float(match.group(1))
                        if not math.isfinite(duration):
                            raise ValueError("Invalid HLS segment duration")
                        pending_duration = duration
                    manifest_uri = upper.startswith((
                        "#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:", "#EXT-X-RENDITION-REPORT:",
                    ))
                    def wrap_attribute(match):
                        local, child = self._wrap_relative(match.group(1), manifest_url, manifest_entry, is_manifest=manifest_uri, role_updates=role_updates)
                        if child is not None:
                            touched.add(child.upstream)
                        return f'URI="{local}"'
                    out.append(_M3U8_URI_ATTR_RE.sub(wrap_attribute, line))
                else:
                    if not pending_variant and pending_duration is None:
                        raise ValueError("HLS media URL has no valid EXTINF or STREAM-INF")
                    absolute = urljoin(manifest_url, stripped)
                    interval = None
                    if pending_range is not None:
                        length, offset = pending_range
                        if offset is None:
                            if absolute != last_range_url:
                                raise ValueError("Implicit HLS range has no preceding range for the same resource")
                            offset = last_range_end
                        interval = (offset, offset + length)
                        last_range_url, last_range_end = absolute, offset + length
                    else:
                        last_range_url = None
                    local, child = self._wrap_relative(stripped, manifest_url, manifest_entry, is_manifest=pending_variant, role_updates=role_updates)
                    out.append(local)
                    if child is not None:
                        touched.add(child.upstream)
                        if pending_duration is not None:
                            segments.append(child)
                            durations.append(pending_duration)
                            ranges.append(interval)
                    pending_duration, pending_variant, pending_range = None, False, None
            if pending_duration is not None or pending_variant or pending_range is not None:
                raise ValueError("HLS playlist is missing a media URL")
            old_plan = (tuple(id(child) for child in manifest_entry.hls_segment_entries),
                        tuple(manifest_entry.hls_segment_ranges), manifest_entry.hls_media_sequence)
            new_plan = (tuple(id(child) for child in segments), tuple(ranges), sequence)
            roles_changed = any(
                (child.force_manifest, child.proxy_ext) != role
                for child, role in role_updates.items()
            )
            if old_plan != new_plan or roles_changed:
                manifest_entry.prefetch_generation += 1
            # Existing children receive their new role only after the complete
            # playlist is valid. A failed refresh cannot expose provisional
            # manifest handling to a concurrent player request.
            with self._lock:
                for child, (force_manifest, proxy_ext) in role_updates.items():
                    child.force_manifest, child.proxy_ext = force_manifest, proxy_ext
                    child.hls_index = None
            for index, segment in enumerate(segments):
                segment.hls_index = index
            manifest_entry.hls_segment_entries = segments
            manifest_entry.hls_segment_durations = durations
            manifest_entry.hls_segment_ranges = ranges
            manifest_entry.hls_media_sequence = sequence
            manifest_entry.hls_live = "#EXT-X-ENDLIST" not in text.upper()
            if manifest_entry.hls_player_sequence is not None:
                manifest_entry.hls_player_segment_idx = max(0, min(
                    manifest_entry.hls_player_sequence - sequence, max(0, len(segments) - 1),
                ))
            else:
                manifest_entry.hls_player_segment_idx = min(manifest_entry.hls_player_segment_idx, max(0, len(segments) - 1))
            self._retire_manifest_children(manifest_entry, touched)
            return "\n".join(out) + "\n", segments, durations

    def _retire_manifest_children(self, parent, current_urls):
        now = time.monotonic()
        with self._lock:
            stale = []
            for url, child in parent.hls_child_by_upstream.items():
                if url in current_urls:
                    child.retired_at = None
                else:
                    if child.retired_at is None:
                        child.retired_at = now
                    stale.append((url, child))
            stale.sort(key=lambda pair: pair[1].retired_at)
            excess = max(0, len(stale) - MAX_LIVE_RETIRED_ENTRIES)
            for index, (url, child) in enumerate(stale):
                descendants = [child]
                active = False
                while descendants:
                    descendant = descendants.pop()
                    if descendant.active_requests:
                        active = True
                        break
                    descendants.extend(descendant.children)
                if active:
                    continue
                if index < excess or now - child.retired_at >= LIVE_GRACE_SECONDS:
                    parent.hls_child_by_upstream.pop(url, None)
                    if child in parent.children:
                        parent.children.remove(child)
                    token = self._token_by_entry.pop(id(child), None)
                    if token is not None:
                        self._entries.pop(token, None)
                        self._prefetchers.pop(token, None)
                        self._hls_prefetchers.pop(token, None)
                    self._dispose_entry(child)

    def _wrap_relative(self, url, base_url, manifest_entry, *, is_manifest=False, role_updates=None):
        if not url:
            return url, None
        if url.startswith(self.base_url + "/play/"):
            return url, None
        absolute = urljoin(base_url, url)
        if not absolute.lower().startswith(("http://", "https://")):
            return url, None
        # Lookup and registration are one transaction, including nested calls
        # to register(). Concurrent manifest refreshes cannot duplicate entries.
        with self._lock:
            if manifest_entry.closed:
                raise InterruptedError("Manifest is closed")
            replaced = None
            child = manifest_entry.hls_child_by_upstream.get(absolute)
            if child is not None and not child.closed:
                proxy_ext = "m3u8" if is_manifest else _derive_ext(absolute)
                if proxy_ext == "bin":
                    proxy_ext = "ts"
                role = (is_manifest, proxy_ext)
                if role_updates is not None:
                    if child in role_updates and role_updates[child] != role:
                        raise ValueError("HLS resource has conflicting manifest and media roles")
                if (child.force_manifest, child.proxy_ext) == role:
                    if role_updates is not None:
                        role_updates[child] = role
                    token = self._token_by_entry.get(id(child))
                    if token is not None:
                        return f"{self.base_url}/play/{token}.{proxy_ext}", child
                replaced = child
                # A different role is a different representation. Stage a
                # fresh token/cache instead of mutating bytes still used by
                # an old player request. The manifest transaction can discard
                # the replacement and restore its lookup if validation fails.
            child_headers = scoped_headers(manifest_entry.extra_headers, manifest_entry.upstream, absolute)
            local = self.register(
                absolute, referer=manifest_entry.referer, extra_headers=child_headers,
                parent=manifest_entry, cookiejar=manifest_entry.cookiejar,
                is_hls=is_manifest, hls_png_wrapped=manifest_entry.hls_png_wrapped,
            )
            token = local.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            child = self._entries.get(token)
            if child is not None:
                manifest_entry.hls_child_by_upstream[absolute] = child
                if role_updates is not None:
                    role_updates[child] = (child.force_manifest, child.proxy_ext)
                else:
                    if replaced is not None:
                        replaced._superseded = True
                    self._retire_superseded_children(manifest_entry)
            return local, child
