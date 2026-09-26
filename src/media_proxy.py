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
from urllib.parse import urljoin, urlparse

import requests

from .net import build_session


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
_EXTINF_RE = re.compile(r"#EXTINF:([0-9.]+)", re.IGNORECASE)
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
    match = _CONTENT_RANGE_RE.search(value)
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2))
    total_str = match.group(3)
    total = int(total_str) if total_str.isdigit() else None
    return start, end, total


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
            if resp.status_code not in (200, 206):
                return

            if resp.status_code == 200:
                # Server ignored Range and sent the whole body. Cache it
                # from offset 0 (the body is the full file).
                cl = resp.headers.get("Content-Length")
                if cl:
                    try:
                        entry.total_size = int(cl)
                    except ValueError:
                        pass
                pos = 0
                for chunk in resp.iter_content(chunk_size=PREFETCH_CHUNK_SIZE):
                    if entry.closed:
                        return
                    if not chunk:
                        continue
                    entry.write_at(pos, chunk)
                    pos += len(chunk)
                return

            self._learn_total_size(resp)
            pos = start
            for chunk in resp.iter_content(chunk_size=PREFETCH_CHUNK_SIZE):
                if entry.closed:
                    return
                if not chunk:
                    continue
                entry.write_at(pos, chunk)
                pos += len(chunk)
                if pos > end_inclusive + 1:
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

    def _learn_total_size(self, resp: requests.Response) -> None:
        if self.entry.total_size is not None:
            return
        cr = _parse_content_range(resp.headers.get("Content-Range"))
        if cr and cr[2]:
            self.entry.total_size = cr[2]
            return
        cl = resp.headers.get("Content-Length")
        if cl and resp.status_code == 200:
            try:
                self.entry.total_size = int(cl)
            except ValueError:
                pass


class _HlsPrefetcher(threading.Thread):
    """Continuous HLS prefetcher anchored to the player's segment index."""

    def __init__(
        self,
        manifest_entry: _Entry,
        get_buffer_seconds: Callable[[], int],
        session: requests.Session,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(
            name=f"HlsPre[{manifest_entry.cache_path.name}]",
            daemon=True,
        )
        self.entry = manifest_entry
        self.get_buffer_seconds = get_buffer_seconds
        self.session = session
        self.on_status = on_status
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
        try:
            resp = self.session.get(
                segment.upstream,
                headers=segment.headers,
                stream=True,
                timeout=(5, 10),
                allow_redirects=True,
                **segment.cookie_kwargs,
            )
        except Exception:  # noqa: BLE001
            return
        try:
            if resp.status_code not in (200, 206):
                return
            cl = resp.headers.get("Content-Length")
            if cl and resp.status_code == 200:
                try:
                    segment.total_size = int(cl)
                except ValueError:
                    pass
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
        with self._lock:
            self._entries[token] = entry
            self._token_by_entry[id(entry)] = token
            if parent is not None:
                parent.children.append(entry)

        # Top-level entries (no parent) get a dedicated sequential
        # prefetcher. HLS segment entries are children of the manifest
        # and are filled in bulk by the manifest's _HlsPrefetcher.
        if parent is None:
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
        # Recursively clean up children first - their token entries need
        # to be removed from the map too. Snapshot under the lock so a
        # concurrent manifest rewrite can't slip a new child past us.
        with self._lock:
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
        entry.close()

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
        token = self._token_for(manifest_entry)
        if token is None:
            return
        with self._lock:
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
            )
            self._hls_prefetchers[token] = worker
        worker.start()

    # ----- HEAD ------------------------------------------------------------

    def _serve_head(
        self, request: BaseHTTPRequestHandler, entry: _Entry
    ) -> None:
        try:
            resp = self._session.request(
                method="HEAD",
                url=entry.upstream,
                headers=entry.headers,
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
            if cl and entry.total_size is None:
                try:
                    entry.total_size = int(cl)
                except ValueError:
                    pass
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
                if resp.status_code not in (200, 206):
                    return
                if entry.total_size is None:
                    cr = _parse_content_range(
                        resp.headers.get("Content-Range")
                    )
                    if cr and cr[2]:
                        entry.total_size = cr[2]
                    else:
                        cl = resp.headers.get("Content-Length")
                        if cl and resp.status_code == 200:
                            try:
                                entry.total_size = int(cl)
                            except ValueError:
                                pass

                if resp.status_code == 200:
                    # Server ignored the Range header. Stream the
                    # requested slice without writing the bytes at the
                    # wrong offset (the body starts at 0, not `start`).
                    yield from self._stream_slice_from_full_body(
                        resp, start, end_inclusive, entry
                    )
                    return

                pos = start
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if entry.closed:
                        return
                    if not chunk:
                        continue
                    entry.write_at(pos, chunk)
                    pos += len(chunk)
                    yield chunk
                    if end_inclusive is not None and pos > end_inclusive + 1:
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
            cr = _parse_content_range(resp.headers.get("Content-Range"))
            if cr and cr[2]:
                entry.total_size = cr[2]
                return
            cl = resp.headers.get("Content-Length")
            if cl and resp.status_code == 200:
                try:
                    entry.total_size = int(cl)
                except ValueError:
                    pass
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

        for raw_line in text.splitlines():
            line = raw_line.rstrip("\r")
            stripped = line.strip()
            if not stripped:
                out_lines.append(line)
                continue
            if stripped.startswith("#"):
                m = _EXTINF_RE.match(stripped)
                if m:
                    try:
                        pending_duration = float(m.group(1))
                    except ValueError:
                        pending_duration = None
                rewritten = _M3U8_URI_ATTR_RE.sub(
                    lambda mm: f'URI="{self._wrap_relative(mm.group(1), manifest_url, manifest_entry)[0]}"',
                    line,
                )
                out_lines.append(rewritten)
            else:
                wrapped, child_entry = self._wrap_relative(
                    stripped, manifest_url, manifest_entry
                )
                out_lines.append(wrapped)
                if child_entry is not None:
                    segment_entries.append(child_entry)
                    segment_durations.append(
                        pending_duration if pending_duration is not None else 4.0
                    )
                pending_duration = None
        return "\n".join(out_lines) + "\n", segment_entries, segment_durations

    def _wrap_relative(
        self,
        url: str,
        base_url: str,
        manifest_entry: _Entry,
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
        )
        leaf = local.rsplit("/", 1)[-1]
        token = leaf.rsplit(".", 1)[0]
        with self._lock:
            child = self._entries.get(token)
            if child is not None:
                manifest_entry.hls_child_by_upstream[absolute] = child
        return local, child
