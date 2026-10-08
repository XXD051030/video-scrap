"""Loopback integration checks for explicitly PNG-wrapped HLS previews.

Run directly with ``build/.venv/bin/python tests/test_wrapped_hls_proxy.py``.
All manifests, PNG envelopes, and media bytes are synthetic. HTTP servers bind
only to 127.0.0.1 and every client disables environment proxy configuration.
"""

from __future__ import annotations

import re
import struct
import sys
import threading
import time
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from unittest.mock import patch
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.media_proxy import (  # noqa: E402
    PLAYBACK_HLS_PREFETCH_CONNECTIONS,
    MediaProxyServer,
)


PNG = b"\x89PNG\r\n\x1a\n"
TS = b"\x47\x40\x00\x10" + bytes(range(184))


def _chunk(kind: bytes, body: bytes = b"") -> bytes:
    crc = zlib.crc32(body, zlib.crc32(kind)) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)


def _png(payload: bytes, *, compressed: bool = True) -> bytes:
    flag, body = (b"\x01", zlib.compress(payload)) if compressed else (b"\x00", payload)
    return PNG + _chunk(b"roUd", flag + body) + _chunk(b"IEND")


def _playlist(segment_paths: list[str], duration: int = 10) -> bytes:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{duration}"]
    for path in segment_paths:
        lines += [f"#EXTINF:{duration},", path]
    lines.append("#EXT-X-ENDLIST")
    return ("\n".join(lines) + "\n").encode("ascii")


def _media_urls(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def _attribute_url(text: str, prefix: str) -> str:
    line = next(line for line in text.splitlines() if line.startswith(prefix))
    match = re.search(r'URI="([^"]+)"', line)
    assert match is not None
    return match.group(1)


@dataclass
class Route:
    payload: bytes = b""
    status: int = 200
    content_type: str = "image/png"
    location: str | None = None
    gate: threading.Event | None = None
    segment: bool = False


class LocalOrigin:
    """Record actual wire requests and hold segment responses when needed."""

    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = routes
        self.records: list[dict[str, object]] = []
        self.condition = threading.Condition()
        self.active_segments = 0
        self.maximum_segments = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):  # noqa: N802
                self.respond(body=True)

            def do_HEAD(self):  # noqa: N802
                self.respond(body=False)

            def respond(self, *, body):
                with owner.condition:
                    route = owner.routes.get(self.path, Route(b"unexpected path", status=404))
                    owner.records.append({
                        "method": self.command,
                        "path": self.path,
                        "range": self.headers.get("Range"),
                        "accept_encoding": self.headers.get("Accept-Encoding"),
                        "referer": self.headers.get("Referer"),
                    })
                    if route.segment:
                        owner.active_segments += 1
                        owner.maximum_segments = max(owner.maximum_segments, owner.active_segments)
                    owner.condition.notify_all()
                try:
                    if route.gate is not None:
                        route.gate.wait(timeout=8)
                    self.send_response(route.status)
                    self.send_header("Content-Type", route.content_type)
                    self.send_header("Content-Length", str(len(route.payload)))
                    if route.location is not None:
                        self.send_header("Location", route.location)
                    self.end_headers()
                    if body:
                        self.wfile.write(route.payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with owner.condition:
                        if route.segment:
                            owner.active_segments -= 1
                        owner.condition.notify_all()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        with self.condition:
            for route in self.routes.values():
                if route.gate is not None:
                    route.gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def requests_for(self, path: str) -> list[dict[str, object]]:
        with self.condition:
            return [record.copy() for record in self.records if record["path"] == path]

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 5) -> bool:
        deadline = time.monotonic() + timeout
        with self.condition:
            while not predicate():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(min(remaining, 0.02))
        return True


class WrappedHlsProxyTests(unittest.TestCase):
    def origin(self, routes: dict[str, Route]) -> LocalOrigin:
        origin = LocalOrigin(routes)
        self.addCleanup(origin.stop)
        return origin

    def proxy(self, *, buffer_seconds: int = 0) -> MediaProxyServer:
        proxy = MediaProxyServer(buffer_seconds=buffer_seconds).start()
        proxy._session.trust_env = False
        self.addCleanup(proxy.stop)
        return proxy

    @staticmethod
    def fetch(url: str, *, method: str = "GET", headers: dict[str, str] | None = None):
        with requests.Session() as client:
            client.trust_env = False
            return client.request(method, url, headers=headers, timeout=(3, 8))

    def register(self, proxy: MediaProxyServer, origin: LocalOrigin, path: str = "/api/hls/fixture") -> str:
        return proxy.register(
            origin.base + path,
            referer="https://videos.example.test/v/fixture",
            is_hls=True,
            hls_png_wrapped=True,
        )

    def assert_full_origin_gets(self, origin: LocalOrigin) -> None:
        self.assertTrue(origin.records)
        for record in origin.records:
            self.assertEqual(record["method"], "GET")
            self.assertIsNone(record["range"])
            self.assertEqual(record["accept_encoding"], "identity")

    def test_manifest_get_and_head_unwrap_and_use_redirect_final_base(self) -> None:
        origin = self.origin({
            "/api/hls/fixture": Route(status=302, location="/redirected/media/list.png"),
            "/redirected/media/list.png": Route(_png(_playlist(["segments/first.png"]))),
            "/redirected/media/segments/first.png": Route(_png(TS), segment=True),
        })
        proxy = self.proxy()
        local = self.register(proxy, origin)
        response = self.fetch(local)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/vnd.apple.mpegurl")
        self.assertEqual(int(response.headers["Content-Length"]), len(response.content))
        self.assertTrue(response.text.startswith("#EXTM3U\n"))
        segment_url = _media_urls(response.text)[0]
        segment = proxy._lookup(urlparse(segment_url).path)
        self.assertEqual(segment.upstream, origin.base + "/redirected/media/segments/first.png")
        self.assertTrue(segment.hls_png_wrapped)

        head = self.fetch(local, method="HEAD")
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")
        self.assertEqual(head.headers["Content-Type"], response.headers["Content-Type"])
        self.assertEqual(head.headers["Content-Length"], response.headers["Content-Length"])
        self.assertEqual(len(origin.requests_for("/api/hls/fixture")), 2)
        self.assertEqual(len(origin.requests_for("/redirected/media/list.png")), 2)
        self.assertEqual(len(origin.requests_for("/redirected/media/segments/first.png")), 0)
        self.assert_full_origin_gets(origin)

    def test_segment_head_and_ranges_are_served_from_complete_logical_cache(self) -> None:
        origin = self.origin({
            "/api/hls/fixture": Route(_png(_playlist(["/segments/first.png"]))),
            "/segments/first.png": Route(_png(TS * 3), segment=True),
        })
        proxy = self.proxy()
        manifest = self.fetch(self.register(proxy, origin))
        segment_url = _media_urls(manifest.text)[0]
        segment = proxy._lookup(urlparse(segment_url).path)
        logical = TS * 3

        head = self.fetch(segment_url, method="HEAD")
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")
        self.assertEqual(int(head.headers["Content-Length"]), len(logical))
        self.assertEqual(head.headers["Accept-Ranges"], "bytes")
        self.assertEqual(segment.total_size, len(logical))
        self.assertEqual(segment.cache_path.read_bytes(), logical)
        self.assertEqual(segment.cached_bytes(), len(logical))

        full = self.fetch(segment_url)
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.content, logical)
        for span, start, end in (("bytes=4-19", 4, 19), ("bytes=-11", len(logical) - 11, len(logical) - 1)):
            with self.subTest(span=span):
                part = self.fetch(segment_url, headers={"Range": span})
                self.assertEqual(part.status_code, 206)
                self.assertEqual(part.content, logical[start:end + 1])
                self.assertEqual(part.headers["Content-Range"], f"bytes {start}-{end}/{len(logical)}")
        outside = self.fetch(segment_url, headers={"Range": f"bytes={len(logical)}-"})
        self.assertEqual(outside.status_code, 416)
        self.assertEqual(outside.headers["Content-Range"], f"bytes */{len(logical)}")
        self.assertEqual(len(origin.requests_for("/segments/first.png")), 1)
        self.assert_full_origin_gets(origin)

        cache_dir = proxy.cache_dir
        proxy.stop()
        self.assertTrue(segment.closed)
        self.assertFalse(segment.cache_path.exists())
        self.assertFalse(cache_dir.exists())

    def test_prefetch_and_simultaneous_player_gets_share_one_complete_fetch(self) -> None:
        gate = threading.Event()
        origin = self.origin({
            "/api/hls/fixture": Route(_png(_playlist(["/segments/shared.png"]))),
            "/segments/shared.png": Route(_png(TS), gate=gate, segment=True),
        })
        proxy = self.proxy(buffer_seconds=190)
        self.addCleanup(gate.set)
        manifest = self.fetch(self.register(proxy, origin))
        segment_url = _media_urls(manifest.text)[0]
        self.assertTrue(origin.wait_for(lambda: len(origin.requests_for("/segments/shared.png")) == 1))
        entered = threading.Condition()
        calls = 0
        original = proxy._ensure_wrapped_cache

        def record_player_entry(entry):
            nonlocal calls
            with entered:
                calls += 1
                entered.notify_all()
            return original(entry)

        with patch.object(proxy, "_ensure_wrapped_cache", side_effect=record_player_entry), ThreadPoolExecutor(max_workers=4) as clients:
            futures = [clients.submit(self.fetch, segment_url, headers={"Range": "bytes=1-8"}) for _ in range(4)]
            with entered:
                arrived = entered.wait_for(lambda: calls == 4, timeout=3)
            upstream_count_before_release = len(origin.requests_for("/segments/shared.png"))
            gate.set()
            self.assertTrue(arrived)
            self.assertEqual(upstream_count_before_release, 1)
            for future in futures:
                response = future.result(timeout=5)
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, TS[1:9])
        self.assertEqual(len(origin.requests_for("/segments/shared.png")), 1)
        segment = proxy._lookup(urlparse(segment_url).path)
        self.assertEqual(segment.cache_path.read_bytes(), TS)
        self.assert_full_origin_gets(origin)

    def test_failed_or_corrupt_wrapper_is_not_cached_and_can_be_retried(self) -> None:
        bad = bytearray(_png(TS))
        bad[-13] ^= 1  # Corrupt roUd's CRC, retaining a complete PNG container.
        origin = self.origin({
            "/api/hls/fixture": Route(_png(_playlist(["/segments/bad.png", "/segments/unavailable.png"]))),
            "/segments/bad.png": Route(bytes(bad), segment=True),
            "/segments/unavailable.png": Route(b"unavailable", status=503, content_type="text/plain", segment=True),
        })
        proxy = self.proxy()
        manifest = self.fetch(self.register(proxy, origin))
        for segment_url in _media_urls(manifest.text):
            with self.subTest(url=segment_url):
                entry = proxy._lookup(urlparse(segment_url).path)
                response = self.fetch(segment_url)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(entry.cached_bytes(), 0)
                self.assertIsNone(entry.total_size)
                self.assertEqual(entry.cache_path.read_bytes(), b"")
                path = urlparse(entry.upstream).path
                with origin.condition:
                    origin.routes[path] = Route(_png(TS), segment=True)
                retry = self.fetch(segment_url)
                self.assertEqual(retry.status_code, 200)
                self.assertEqual(retry.content, TS)
                self.assertEqual(entry.cache_path.read_bytes(), TS)
                self.assertEqual(len(origin.requests_for(path)), 2)
        self.assert_full_origin_gets(origin)

    def test_wrapped_master_child_manifests_keys_maps_and_plain_segments(self) -> None:
        master = b'''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Main",URI="audio-stream"
#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=100,URI="iframes.png"
#EXT-X-STREAM-INF:BANDWIDTH=1000,AUDIO="audio"
# A harmless comment does not turn the following variant into a segment.
video.png
'''
        media = b'''#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="keys/key.png"
#EXT-X-MAP:URI="init.png"
#EXTINF:10,
segment.png
#EXT-X-ENDLIST
'''
        key, init = b"0123456789abcdef", b"synthetic initialization bytes"
        origin = self.origin({
            "/catalog/master.png": Route(_png(master)),
            "/catalog/video.png": Route(_png(media)),
            "/catalog/audio-stream": Route(_png(_playlist(["audio.png"]))),
            "/catalog/iframes.png": Route(_png(_playlist(["iframe-segment.png"]))),
            "/catalog/keys/key.png": Route(_png(key, compressed=False)),
            "/catalog/init.png": Route(_png(init)),
            "/catalog/segment.png": Route(TS, content_type="video/mp2t", segment=True),
        })
        proxy = self.proxy()
        master_local = self.register(proxy, origin, "/catalog/master.png")
        response = self.fetch(master_local)
        self.assertEqual(response.status_code, 200)
        master_entry = proxy._lookup(urlparse(master_local).path)
        self.assertEqual(master_entry.hls_segment_entries, [])
        children = [
            _media_urls(response.text)[0],
            _attribute_url(response.text, "#EXT-X-MEDIA:"),
            _attribute_url(response.text, "#EXT-X-I-FRAME-STREAM-INF:"),
        ]
        for child_url in children:
            entry = proxy._lookup(urlparse(child_url).path)
            self.assertTrue(entry.hls_png_wrapped)
            self.assertTrue(entry.force_manifest)
            self.assertTrue(child_url.endswith(".m3u8"))
            child = self.fetch(child_url)
            self.assertEqual(child.status_code, 200)
            self.assertEqual(child.headers["Content-Type"], "application/vnd.apple.mpegurl")
            self.assertTrue(child.text.startswith("#EXTM3U\n"))
            if entry.upstream.endswith("video.png"):
                video_text = child.text

        for local, expected in (
            (_attribute_url(video_text, "#EXT-X-KEY:"), key),
            (_attribute_url(video_text, "#EXT-X-MAP:"), init),
            (_media_urls(video_text)[0], TS),
        ):
            entry = proxy._lookup(urlparse(local).path)
            self.assertTrue(entry.hls_png_wrapped)
            self.assertFalse(entry.force_manifest)
            response = self.fetch(local)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, expected)
            self.assertEqual(entry.cache_path.read_bytes(), expected)
        self.assert_full_origin_gets(origin)

    def test_manifest_unregistered_during_fetch_cannot_create_children_or_cache(self) -> None:
        proxy = self.proxy()
        local = proxy.register(
            "https://media.example.test/api/hls/fixture", is_hls=True,
            hls_png_wrapped=True,
        )
        entry = proxy._lookup(urlparse(local).path)

        def cancelled_fetch(fetched_entry):
            self.assertIs(fetched_entry, entry)
            proxy.unregister(local)
            return _playlist(["child.png"]), "https://media.example.test/final/list.png"

        with patch.object(proxy, "_fetch_wrapped_payload", side_effect=cancelled_fetch):
            response = self.fetch(local)
        self.assertEqual(response.status_code, 502)
        self.assertTrue(entry.closed)
        self.assertEqual(entry.children, [])
        self.assertEqual(entry.hls_child_by_upstream, {})
        self.assertEqual(entry.hls_segment_entries, [])
        self.assertEqual(proxy._entries, {})
        self.assertEqual(proxy._token_by_entry, {})
        self.assertEqual(list(proxy.cache_dir.iterdir()), [])

    def test_closed_parent_registration_cleans_unregistered_temp_file(self) -> None:
        proxy = self.proxy()
        local = proxy.register(
            "https://media.example.test/api/hls/fixture", is_hls=True,
            hls_png_wrapped=True,
        )
        parent = proxy._lookup(urlparse(local).path)
        proxy.unregister(local)
        self.assertEqual(list(proxy.cache_dir.iterdir()), [])
        with self.assertRaisesRegex(RuntimeError, "closed"):
            proxy.register(
                "https://media.example.test/child.png", parent=parent,
                hls_png_wrapped=True,
            )
        self.assertEqual(parent.children, [])
        self.assertEqual(proxy._entries, {})
        self.assertEqual(proxy._token_by_entry, {})
        self.assertEqual(list(proxy.cache_dir.iterdir()), [])

    def test_zero_buffer_does_not_fetch_segments_before_the_player(self) -> None:
        origin = self.origin({
            "/api/hls/fixture": Route(_png(_playlist(["/segments/first.png", "/segments/second.png"]))),
            "/segments/first.png": Route(_png(TS), segment=True),
            "/segments/second.png": Route(_png(TS), segment=True),
        })
        proxy = self.proxy(buffer_seconds=0)
        local = self.register(proxy, origin)
        self.assertEqual(origin.records, [])
        response = self.fetch(local)
        self.assertEqual(response.status_code, 200)
        entry = proxy._lookup(urlparse(local).path)
        self.assertFalse(origin.wait_for(lambda: any(record["path"].startswith("/segments/") for record in origin.records), timeout=0.15))
        self.assertTrue(all(segment.cached_bytes() == 0 for segment in entry.hls_segment_entries))
        first, second = _media_urls(response.text)
        self.assertEqual(self.fetch(first).content, TS)
        self.assertEqual(len(origin.requests_for("/segments/first.png")), 1)
        self.assertEqual(len(origin.requests_for("/segments/second.png")), 0)
        self.assertEqual(proxy._lookup(urlparse(second).path).cached_bytes(), 0)
        self.assert_full_origin_gets(origin)

    def test_190_second_prefetch_caches_logical_bytes_with_bounded_concurrency(self) -> None:
        gate = threading.Event()
        paths = [f"/segments/{index}.png" for index in range(20)]
        payloads = [TS + bytes([index]) for index in range(20)]
        routes = {"/api/hls/fixture": Route(_png(_playlist(paths, duration=10)))}
        routes.update({path: Route(_png(payload), gate=gate, segment=True) for path, payload in zip(paths, payloads)})
        origin = self.origin(routes)
        proxy = self.proxy(buffer_seconds=190)
        self.addCleanup(gate.set)
        local = self.register(proxy, origin)
        self.assertEqual(self.fetch(local).status_code, 200)
        entry = proxy._lookup(urlparse(local).path)
        self.assertTrue(origin.wait_for(lambda: origin.active_segments >= 2))
        origin.wait_for(lambda: origin.active_segments >= PLAYBACK_HLS_PREFETCH_CONNECTIONS, timeout=0.25)
        self.assertLessEqual(origin.maximum_segments, PLAYBACK_HLS_PREFETCH_CONNECTIONS)
        self.assertGreaterEqual(origin.maximum_segments, 2)
        gate.set()
        self.assertTrue(origin.wait_for(lambda: all(segment.cached_bytes() == len(payload) for segment, payload in zip(entry.hls_segment_entries[:19], payloads[:19]))))
        for index, segment in enumerate(entry.hls_segment_entries[:19]):
            self.assertEqual(segment.cache_path.read_bytes(), payloads[index])
            self.assertEqual(segment.total_size, len(payloads[index]))
            self.assertEqual(len(origin.requests_for(paths[index])), 1)
        self.assertEqual(len(origin.requests_for(paths[19])), 0)
        self.assertEqual(entry.hls_segment_entries[19].cached_bytes(), 0)
        self.assertLessEqual(origin.maximum_segments, PLAYBACK_HLS_PREFETCH_CONNECTIONS)
        self.assert_full_origin_gets(origin)
        cache_dir = proxy.cache_dir
        proxy.stop()
        self.assertFalse(cache_dir.exists())
        self.assertTrue(all(segment.closed for segment in entry.hls_segment_entries))


if __name__ == "__main__":
    unittest.main(verbosity=2)
