"""Offline cache lifetime and file-descriptor regression checks.

Run with ``build/.venv/bin/python tests/test_media_proxy_cache_handles.py``.
The low-descriptor-limit check changes only an isolated child process.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.media_proxy import MediaProxyServer, _Entry


class CacheHandleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="proxy_cache_handles_")
        self.addCleanup(self.temp.cleanup)
        self.entry = _Entry(
            "https://example.test/segment.ts", {}, Path(self.temp.name),
        )
        self.addCleanup(self.entry.close)

    def test_sparse_overlapping_cache_io_keeps_verified_intervals(self) -> None:
        entry = self.entry
        entry.write_at(8, b"ijkl")
        entry.write_at(0, b"abcd")
        entry.write_at(3, b"defghij")
        self.assertEqual(entry.read_at(0, 20), b"abcdefghijkl")
        self.assertEqual(entry.read_at(9, 2), b"jk")
        self.assertEqual(entry._intervals, [(0, 12)])
        self.assertEqual(entry.cached_bytes(), 12)
        self.assertEqual(entry.cached_run_end(0), 12)

    def test_each_cache_operation_closes_its_handle(self) -> None:
        real_open = Path.open
        opened = []

        def recording_open(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            opened.append(handle)
            return handle

        with patch.object(Path, "open", recording_open):
            for index in range(10):
                payload = bytes([index])
                self.entry.write_at(index, payload)
                self.assertEqual(self.entry.read_at(index, 1), payload)
                self.assertTrue(all(handle.closed for handle in opened))
        self.assertEqual(len(opened), 20)

    def test_closed_cache_never_reopens_or_reappears(self) -> None:
        entry = self.entry
        entry.write_at(0, b"before close")
        entry.close()
        intervals = list(entry._intervals)
        with patch.object(Path, "open", side_effect=AssertionError("cache reopened")):
            entry.write_at(0, b"after close")
            self.assertEqual(entry.read_at(0, 100), b"")
            entry.close()
        self.assertEqual(entry._intervals, intervals)
        self.assertFalse(entry.cache_path.exists())

    def test_unregister_waits_for_inflight_io_then_removes_every_cache(self) -> None:
        proxy = MediaProxyServer(buffer_seconds=0)
        self.addCleanup(proxy.stop)
        url = proxy.register(
            "https://example.test/manifest.m3u8", is_hls=True,
            hls_png_wrapped=True,
        )
        parent = proxy._lookup(url)
        child_url = proxy.register("https://example.test/segment.ts", parent=parent)
        child = proxy._lookup(child_url)
        real_open = Path.open
        writing = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        @contextmanager
        def gated_open(path, *args, **kwargs):
            with real_open(path, *args, **kwargs) as handle:
                if path == child.cache_path:
                    writing.set()
                    if not release.wait(5):
                        raise TimeoutError("cache write was not released")
                yield handle

        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(Path, "open", gated_open):
                writer = pool.submit(child.write_at, 0, b"in-flight payload")
                self.assertTrue(writing.wait(5))
                disposal = pool.submit(proxy.unregister, url)
                release.set()
                writer.result(timeout=5)
                disposal.result(timeout=5)
        self.assertTrue(parent.closed)
        self.assertTrue(child.closed)
        child.write_at(0, b"late payload")
        self.assertEqual(child.read_at(0, 100), b"")
        self.assertEqual(proxy._entries, {})
        self.assertEqual(list(proxy.cache_dir.iterdir()), [])

    def test_concurrent_cache_io_and_close_cannot_recreate_file(self) -> None:
        entry = self.entry
        barrier = threading.Barrier(5)

        def access_cache(index):
            barrier.wait(timeout=5)
            for _ in range(100):
                entry.write_at(index * 8, bytes([index]) * 8)
                data = entry.read_at(index * 8, 8)
                if data:
                    self.assertEqual(data, bytes([index]) * 8)

        with ThreadPoolExecutor(max_workers=4) as pool:
            workers = [pool.submit(access_cache, index) for index in range(4)]
            barrier.wait(timeout=5)
            entry.close()
            for worker in workers:
                worker.result(timeout=5)
        self.assertTrue(entry.closed)
        self.assertFalse(entry.cache_path.exists())

    @unittest.skipIf(sys.platform == "win32", "RLIMIT_NOFILE is a POSIX API")
    def test_long_playlist_and_cache_io_under_isolated_64_descriptor_limit(self) -> None:
        script = r'''
import json
import os
import resource
from src.media_proxy import MediaProxyServer

proxy = MediaProxyServer(buffer_seconds=0)
try:
    root = proxy.register(
        "https://example.test/manifest.m3u8", is_hls=True,
        hls_png_wrapped=True,
    )
    parent = proxy._lookup(root)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    limit = min(64, hard) if hard != resource.RLIM_INFINITY else 64
    resource.setrlimit(resource.RLIMIT_NOFILE, (limit, hard))
    count = 2048
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2"]
    for index in range(count):
        lines.extend(("#EXTINF:2,", f"segment-{index}.ts"))
    lines.append("#EXT-X-ENDLIST")
    _, segments, durations = proxy._rewrite_m3u8(
        "\n".join(lines), parent.upstream, parent,
    )
    assert len(segments) == count
    assert durations == [2.0] * count
    assert len(proxy._entries) == count + 1
    for index, segment in enumerate(segments):
        payload = index.to_bytes(4, "big") + b"cached media"
        segment.write_at(17, payload)
        assert b"".join(proxy._iterate_bytes(segment, 17, 17 + len(payload) - 1)) == payload
    # Media cache I/O must leave room for sockets/other unrelated files.
    probes = []
    try:
        for _ in range(8):
            probes.append(os.open(os.devnull, os.O_RDONLY))
    finally:
        for fd in probes:
            os.close(fd)
    proxy.unregister(root)
    assert not proxy._entries
    assert not list(proxy.cache_dir.iterdir())
    for segment in segments:
        segment.write_at(0, b"late")
        assert segment.read_at(0, 100) == b""
        assert not segment.cache_path.exists()
    print(json.dumps({"limit": limit, "segments": count, "io_verified": count}))
finally:
    proxy.stop()
'''
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True, timeout=45, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertLessEqual(report["limit"], 64)
        self.assertEqual(report["segments"], 2048)
        self.assertEqual(report["io_verified"], 2048)


if __name__ == "__main__":
    unittest.main(verbosity=2)
