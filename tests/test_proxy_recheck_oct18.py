"""Adversarial proxy regressions for the October 18 independent recheck."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.media_proxy import MediaProxyServer, MAX_WRAPPED_PAYLOAD_REQUESTS, _BoundedHlsPool


class Request:
    def __init__(self, range_header=None, output=None):
        self.headers = {} if range_header is None else {"Range": range_header}
        self.output_headers = {}
        self.status = None
        self.wfile = output or io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.output_headers[name] = value

    def end_headers(self):
        pass


class Response:
    def __init__(self, *, body=b"abcdefgh", status=206, final_url=None, delay=0, stalled=False):
        self.status_code = status
        self.headers = {"Content-Length": str(len(body)), "ETag": '"1"'}
        if status == 206:
            self.headers["Content-Range"] = f"bytes 0-{len(body) - 1}/{len(body)}"
        self.url = final_url or "https://fixture.test/object.mp4"
        self.body, self.delay, self.stalled = body, delay, stalled
        self.closed = False
        self.entered = threading.Event()
        self.released = threading.Event()
        self.raw = self

    def iter_content(self, chunk_size=65536):
        if self.stalled:
            self.entered.set()
            if not self.released.wait(3):
                raise AssertionError("Stalled response was not interrupted")
            return
        for offset in range(0, len(self.body), 1):
            time.sleep(self.delay)
            yield self.body[offset:offset + 1]

    def stream(self, _chunk_size, decode_content=False):
        return self.iter_content()

    def close(self):
        self.closed = True
        self.released.set()


class ProxyRecheckTests(unittest.TestCase):
    def setUp(self):
        self.proxy = MediaProxyServer(buffer_seconds=0)
        self.addCleanup(self.proxy.stop)

    def entry(self, *, parent=None, wrapped=False):
        local = self.proxy.register(
            "https://fixture.test/object.mp4", parent=parent,
            hls_png_wrapped=wrapped,
        )
        return self.proxy._lookup(local)

    def manifest(self, *, wrapped=False):
        return self.proxy._lookup(self.proxy.register(
            "https://fixture.test/list.m3u8", is_hls=True, hls_png_wrapped=wrapped,
        ))

    def test_full_wrapped_cache_keeps_serving_repeat_head_ranges_and_seek(self):
        parent = self.manifest(wrapped=True)
        first = self.proxy._wrap_relative("first.png", parent.upstream, parent)[1]
        second = self.proxy._wrap_relative("second.png", parent.upstream, parent)[1]
        self.proxy._cache_budget.limit = 4096
        logical = b"logical segment bytes"
        with patch.object(self.proxy, "_fetch_wrapped_payload", return_value=(logical, second.upstream)) as fetch:
            for child, span, status, expected in (
                (first, None, 200, logical),
                (second, None, 200, logical),
                (second, "bytes=3-8", 206, logical[3:9]),
                (second, "bytes=-5", 206, logical[-5:]),
                (first, None, 200, logical),
            ):
                request = Request(span)
                self.proxy._handle_entry(request, child, True, "GET")
                self.assertEqual(request.status, status)
                self.assertEqual(request.wfile.getvalue(), expected)
                self.assertEqual(self.proxy._cache_budget.used, 4096)
            head = Request()
            self.proxy._handle_entry(head, second, False, "HEAD")
            self.assertEqual(head.status, 200)
            self.assertEqual(head.output_headers["Content-Length"], str(len(logical)))
            self.assertEqual(head.wfile.getvalue(), b"")
            outside = Request(f"bytes={len(logical)}-")
            self.proxy._handle_entry(outside, second, True, "GET")
            self.assertEqual(outside.status, 416)
            self.assertEqual(fetch.call_count, 6)
        self.assertEqual(first.cache_path.read_bytes(), logical)
        self.assertEqual(second.cache_path.read_bytes(), b"")
        self.assertEqual(second.cached_bytes(), 0)
        self.assertIsNone(second.total_size)

    def test_concurrent_cache_full_private_bodies_remain_bounded_during_slow_output(self):
        parent = self.manifest(wrapped=True)
        children = [self.proxy._wrap_relative(f"{i}.png", parent.upstream, parent)[1] for i in range(8)]
        self.proxy._cache_budget.limit = 0
        entered = threading.Condition()
        release = threading.Event()
        self.addCleanup(release.set)
        active = maximum = fetched = 0

        class SlowOutput(io.BytesIO):
            def write(output, data):
                nonlocal active, maximum
                with entered:
                    active += 1
                    maximum = max(maximum, active)
                    entered.notify_all()
                try:
                    if not release.wait(3):
                        raise AssertionError("Slow player output was not released")
                    return super().write(data)
                finally:
                    with entered:
                        active -= 1

        def fetch(child):
            nonlocal fetched
            with entered:
                fetched += 1
                entered.notify_all()
            return (b"complete logical segment", child.upstream)

        def serve(child):
            request = Request(output=SlowOutput())
            self.proxy._handle_entry(request, child, True, "GET")
            return request

        with patch.object(self.proxy, "_fetch_wrapped_payload", side_effect=fetch), ThreadPoolExecutor(max_workers=8) as clients:
            futures = [clients.submit(serve, child) for child in children]
            try:
                with entered:
                    self.assertTrue(entered.wait_for(lambda: active == MAX_WRAPPED_PAYLOAD_REQUESTS, timeout=2))
                time.sleep(.05)
                self.assertEqual(fetched, MAX_WRAPPED_PAYLOAD_REQUESTS)
                self.assertEqual(self.proxy._cache_budget.used, 0)
                self.assertTrue(all(child.cache_path.stat().st_size == 0 for child in children))
            finally:
                release.set()
            requests = [future.result(timeout=3) for future in futures]
        self.assertEqual(fetched, len(children))
        self.assertLessEqual(maximum, MAX_WRAPPED_PAYLOAD_REQUESTS)
        self.assertTrue(all(request.status == 200 for request in requests))
        self.assertTrue(all(request.wfile.getvalue() == b"complete logical segment" for request in requests))

    def test_prefetch_cache_rejection_does_not_keep_private_payload(self):
        parent = self.manifest(wrapped=True)
        child = self.proxy._wrap_relative("one.png", parent.upstream, parent)[1]
        self.proxy._cache_budget.limit = 0
        with patch.object(self.proxy, "_fetch_wrapped_payload", return_value=(b"complete", child.upstream)):
            self.assertFalse(self.proxy._prefetch_wrapped_cache(child))
        self.assertEqual(child.cached_bytes(), 0)
        self.assertEqual(child.cache_path.stat().st_size, 0)
        self.assertEqual(self.proxy._cache_budget.used, 0)

    def test_completed_failed_prefetch_futures_do_not_retain_decoded_bodies(self):
        parent = self.manifest(wrapped=True)
        children = [self.proxy._wrap_relative(f"{i}.png", parent.upstream, parent)[1] for i in range(8)]
        self.proxy._cache_budget.limit = 0
        pool = _BoundedHlsPool()
        self.addCleanup(pool.close)
        with patch.object(self.proxy, "_fetch_wrapped_payload", side_effect=lambda child: (b"x" * 131072, child.upstream)):
            # Keep every completed Future alive, as the supervisor can do
            # while waiting for another in-flight segment to finish.
            futures = [pool.submit(self.proxy._prefetch_wrapped_cache, child) for child in children]
            self.assertTrue(all(future is not None for future in futures))
            self.assertEqual([future.result(2) for future in futures], [False] * len(children))
        self.assertTrue(all(future.exception() is None for future in futures))
        self.assertEqual(self.proxy._cache_budget.used, 0)
        self.assertTrue(all(child.cache_path.stat().st_size == 0 for child in children))

    def test_decode_then_cache_write_errors_stay_bounded_during_slow_error_reply(self):
        parent = self.manifest(wrapped=True)
        children = [self.proxy._wrap_relative(f"{i}.png", parent.upstream, parent)[1] for i in range(8)]
        condition = threading.Condition()
        release = threading.Event()
        self.addCleanup(release.set)
        fetched = replying = 0
        original_error = self.proxy._send_manifest_error

        def fetch(child):
            nonlocal fetched
            with condition:
                fetched += 1
            return b"x" * 131072, child.upstream

        def slow_error(*args, **kwargs):
            nonlocal replying
            with condition:
                replying += 1
                condition.notify_all()
            if not release.wait(3):
                raise AssertionError("Slow error response was not released")
            return original_error(*args, **kwargs)

        def serve(child):
            request = Request()
            self.proxy._handle_entry(request, child, True, "GET")
            return request

        with patch.object(self.proxy, "_fetch_wrapped_payload", side_effect=fetch), \
             patch.object(type(children[0]), "write_at", side_effect=OSError("cache write failed")), \
             patch.object(self.proxy, "_send_manifest_error", side_effect=slow_error), \
             ThreadPoolExecutor(max_workers=8) as clients:
            futures = [clients.submit(serve, child) for child in children]
            try:
                with condition:
                    self.assertTrue(condition.wait_for(lambda: replying == MAX_WRAPPED_PAYLOAD_REQUESTS, timeout=2))
                time.sleep(.05)
                self.assertEqual(fetched, MAX_WRAPPED_PAYLOAD_REQUESTS)
            finally:
                release.set()
            requests = [future.result(3) for future in futures]
        self.assertEqual(fetched, len(children))
        self.assertTrue(all(request.status == 502 for request in requests))
        self.assertEqual(self.proxy._cache_budget.used, 0)

    def test_healthy_whole_media_exceeds_old_deadline_on_all_stream_paths(self):
        for status, passthrough in ((206, False), (200, False), (200, True)):
            with self.subTest(status=status, passthrough=passthrough):
                child = self.entry()
                child.total_size = 8
                response = Response(status=status)
                request = Request()
                clock = [0.0]

                def chunks(chunk_size=65536):
                    for byte in response.body:
                        clock[0] += .012
                        yield bytes([byte])

                response.iter_content = chunks
                with patch.object(self.proxy._session, "get", return_value=response), \
                     patch("src.media_proxy.READ_DEADLINE_SECONDS", .04), \
                     patch("src.http_limits.transfer_monotonic", side_effect=lambda: clock[0]):
                    if passthrough:
                        self.proxy._proxy_passthrough(request, child)
                    else:
                        self.proxy._serve_range(request, child)
                    self.assertGreater(clock[0], .04)
                self.assertEqual(request.status, 200)
                self.assertEqual(request.wfile.getvalue(), response.body)
                self.assertTrue(response.closed)

    def test_slow_player_backpressure_does_not_count_as_upstream_idle(self):
        class SlowOutput(io.BytesIO):
            def write(output, data):
                time.sleep(.055)
                return super().write(data)

        entry = self.entry()
        entry.total_size = 8
        response = Response(delay=.005)
        request = Request(output=SlowOutput())
        with patch.object(self.proxy._session, "get", return_value=response), \
             patch("src.media_proxy.READ_DEADLINE_SECONDS", .025):
            self.proxy._serve_range(request, entry)
        self.assertEqual(request.wfile.getvalue(), response.body)

    def test_whole_media_stall_times_out_and_cancellation_interrupts_blocked_read(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                entry = self.entry()
                entry.total_size = 8
                response = Response(stalled=True)
                errors = []

                def consume():
                    try:
                        list(self.proxy._fetch_range(entry, 0, 7))
                    except BaseException as error:
                        errors.append(error)

                with patch.object(self.proxy._session, "get", return_value=response), \
                     patch("src.media_proxy.READ_DEADLINE_SECONDS", 5 if cancel else .04):
                    worker = threading.Thread(target=consume, daemon=True)
                    worker.start()
                    self.assertTrue(response.entered.wait(1))
                    if cancel:
                        entry.close()
                    worker.join(1)
                    self.assertFalse(worker.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], InterruptedError if cancel else TimeoutError)
                self.assertTrue(response.closed)

    def test_bounded_hls_child_keeps_absolute_read_deadline(self):
        child = self.entry(parent=self.manifest())
        child.total_size = 8
        response = Response(delay=.012)
        with patch.object(self.proxy._session, "get", return_value=response), \
             patch("src.media_proxy.READ_DEADLINE_SECONDS", .04):
            with self.assertRaises(TimeoutError):
                b"".join(self.proxy._fetch_range(child, 0, 7))
        self.assertTrue(response.closed)

    def test_redirected_same_etag_different_final_object_cannot_join_ranges(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                entry = self.entry(parent=self.manifest() if nested else None)
                first = Response(body=b"AA", final_url="https://cdn.test/object-a.mp4")
                second = Response(body=b"BB", final_url="https://cdn.test/object-b.mp4")
                first.headers["Content-Range"] = "bytes 0-1/4"
                second.headers["Content-Range"] = "bytes 2-3/4"
                with patch.object(self.proxy._session, "get", side_effect=(first, second)) as get:
                    self.assertEqual(b"".join(self.proxy._fetch_range(entry, 0, 1)), b"AA")
                    self.assertEqual(b"".join(self.proxy._fetch_range(entry, 2, 3)), b"")
                self.assertEqual(get.call_args_list[1].kwargs["headers"]["If-Match"], '"1"')
                self.assertTrue(entry.resource_changed)
                self.assertEqual(entry.cached_bytes(), 0)
                self.assertEqual(entry.cache_path.stat().st_size, 0)

    def test_failed_refresh_restores_existing_roles_and_entire_plan(self):
        parent = self.manifest()
        good = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:10\n#EXTINF:2,\nclip.ts\n#EXT-X-ENDLIST\n"
        _, segments, _ = self.proxy._rewrite_m3u8(good, parent.upstream, parent)
        child = segments[0]
        self.proxy._note_player_segment(child)
        before = (parent.prefetch_generation, list(parent.hls_segment_entries),
                  list(parent.hls_segment_durations), list(parent.hls_segment_ranges),
                  parent.hls_media_sequence, parent.hls_live, parent.hls_player_sequence,
                  parent.hls_player_segment_idx)
        for bad in (
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nclip.ts\n#EXTINF:bad,\n",
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nclip.ts\n#EXTINF:2,\nnew.ts\n#EXTINF:bad,\n",
        ):
            with self.assertRaises(ValueError):
                self.proxy._rewrite_m3u8(bad, parent.upstream, parent)
            self.assertFalse(child.force_manifest)
            self.assertEqual(child.proxy_ext, "ts")
            self.assertEqual(child.hls_index, 0)
            after = (parent.prefetch_generation, list(parent.hls_segment_entries),
                     list(parent.hls_segment_durations), list(parent.hls_segment_ranges),
                     parent.hls_media_sequence, parent.hls_live, parent.hls_player_sequence,
                     parent.hls_player_segment_idx)
            self.assertEqual(after, before)
            self.assertEqual(parent.children, [child])
            self.assertEqual(list(parent.hls_child_by_upstream.values()), [child])
        # A commit-phase failure must also restore metadata already published
        # by the valid parse, including an existing child's changed role.
        with patch.object(self.proxy, "_retire_manifest_children", side_effect=RuntimeError("commit failed")):
            with self.assertRaisesRegex(RuntimeError, "commit failed"):
                self.proxy._rewrite_m3u8(
                    "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:20\n"
                    "#EXT-X-STREAM-INF:BANDWIDTH=1\nclip.ts\n"
                    "#EXTINF:3,\nnew.ts\n",
                    parent.upstream, parent,
                )
        after = (parent.prefetch_generation, list(parent.hls_segment_entries),
                 list(parent.hls_segment_durations), list(parent.hls_segment_ranges),
                 parent.hls_media_sequence, parent.hls_live, parent.hls_player_sequence,
                 parent.hls_player_segment_idx)
        self.assertEqual(after, before)
        self.assertFalse(child.force_manifest)
        self.assertEqual(child.proxy_ext, "ts")
        self.assertEqual(child.hls_index, 0)
        self.assertEqual(parent.children, [child])
        rewritten, segments, _ = self.proxy._rewrite_m3u8(good, parent.upstream, parent)
        self.assertEqual(segments, [child])
        self.assertIn(".ts\n", rewritten)

    def test_successful_refresh_can_change_existing_child_role_both_directions(self):
        parent = self.manifest()
        media = "#EXTM3U\n#EXTINF:2,\nclip.ts\n#EXT-X-ENDLIST\n"
        master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nclip.ts\n"
        _, children, _ = self.proxy._rewrite_m3u8(media, parent.upstream, parent)
        child = children[0]
        master_text, children, _ = self.proxy._rewrite_m3u8(master, parent.upstream, parent)
        self.assertEqual(children, [])
        manifest_child = parent.hls_child_by_upstream[child.upstream]
        self.assertIsNot(manifest_child, child)
        self.assertTrue(child.closed)
        self.assertTrue(manifest_child.force_manifest)
        self.assertEqual(manifest_child.proxy_ext, "m3u8")
        self.assertIsNone(manifest_child.hls_index)
        self.assertIn(".m3u8\n", master_text)
        media_text, children, _ = self.proxy._rewrite_m3u8(media, parent.upstream, parent)
        self.assertEqual(len(children), 1)
        media_child = children[0]
        self.assertIsNot(media_child, manifest_child)
        self.assertTrue(manifest_child.closed)
        self.assertFalse(media_child.force_manifest)
        self.assertEqual(media_child.proxy_ext, "ts")
        self.assertEqual(media_child.hls_index, 0)
        self.assertIn(".ts\n", media_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
