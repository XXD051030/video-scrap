"""Reproductions for the 2026-10-18 proxy and transport boundary fixes."""
from __future__ import annotations

import io
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from src.browser_cookies import build_scoped_session
from src.http_limits import abort_response, iter_limited_response, read_limited_response
from src.media_proxy import (
    MediaProxyServer, _Entry, _Prefetcher, _HlsPrefetcher, _BoundedHlsPool,
    _CacheBudget, MAX_LIVE_RETIRED_ENTRIES, PLAYBACK_HLS_PREFETCH_CONNECTIONS,
)


class Response:
    def __init__(self, body=b"abcd", status=200, headers=None):
        self.body, self.status_code = body, status
        self.headers = dict(headers or {})
        self.url = "https://example.test/list.m3u8"
        self.closed = False
        self.read_chunks = 0
    def iter_content(self, chunk_size=65536):
        for offset in range(0, len(self.body), chunk_size):
            self.read_chunks += 1
            yield self.body[offset:offset + chunk_size]
    def close(self):
        self.closed = True
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        self.close()


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)
    def close(self):
        pass


class Request:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()
    def send_response(self, status): self.status = status
    def send_header(self, key, value): self.headers[key] = value
    def end_headers(self): pass


class ProxyAuditTests(unittest.TestCase):
    def setUp(self):
        self.proxy = MediaProxyServer(buffer_seconds=0)
        self.addCleanup(self.proxy.stop)
        url = self.proxy.register("https://example.test/list.m3u8", is_hls=True)
        self.parent = self.proxy._lookup(url)
    def segment(self, name="clip.ts"):
        _, child = self.proxy._wrap_relative(name, self.parent.upstream, self.parent)
        return child

    def test_changed_etag_cannot_join_same_length_ranges(self):
        entry = self.segment("clip.mp4")
        session = Session([
            Response(b"AA", 206, {"Content-Range": "bytes 0-1/4", "ETag": '"A"'}),
            Response(b"BB", 206, {"Content-Range": "bytes 2-3/4", "ETag": '"B"'}),
        ])
        self.proxy._session.close()
        self.proxy._session = session
        self.assertEqual(b"".join(self.proxy._fetch_range(entry, 0, 1)), b"AA")
        self.assertEqual(b"".join(self.proxy._fetch_range(entry, 2, 3)), b"")
        self.assertEqual(session.requests[1][1]["headers"]["If-Match"], '"A"')
        self.assertTrue(entry.resource_changed)
        self.assertEqual(entry.cached_bytes(), 0)

    def test_lost_or_weak_validator_rejects_bound_resource(self):
        for later in (None, 'W/"A"'):
            with self.subTest(later=later):
                entry = self.segment(str(later) + ".mp4")
                self.assertTrue(entry.accept_response(Response(headers={"ETag": '"A"'})))
                entry.write_at(0, b"AA")
                headers = {"ETag": later} if later else {}
                self.assertFalse(entry.accept_response(Response(headers=headers)))
                self.assertEqual(entry.cached_bytes(), 0)

    def test_error_response_validators_preserve_media_cache_and_allow_recovery(self):
        for status in (429, 503):
            with self.subTest(status=status):
                entry = self.segment(f"transient-{status}.mp4")
                self.proxy._session.close()
                self.proxy._session = Session([
                    Response(b"AA", 206, {"Content-Range": "bytes 0-1/4", "ETag": '"A"'}),
                    Response(b"<html>Retry later</html>", status, {"ETag": '"error-body"', "Retry-After": "1"}),
                    Response(b"BB", 206, {"Content-Range": "bytes 2-3/4", "ETag": '"A"'}),
                ])
                self.assertEqual(b"".join(self.proxy._fetch_range(entry, 0, 1)), b"AA")
                self.assertEqual(b"".join(self.proxy._fetch_range(entry, 2, 3)), b"")
                self.assertFalse(entry.resource_changed)
                self.assertEqual(entry.resource_etag, '"A"')
                self.assertEqual(entry.cached_bytes(), 2)
                self.assertEqual(b"".join(self.proxy._fetch_range(entry, 2, 3)), b"BB")
                self.assertEqual(entry.read_at(0, 4), b"AABB")
                self.assertEqual(len(self.proxy._session.requests), 3)
        unbound = self.segment("first-response-error.mp4")
        self.assertFalse(unbound.accept_response(Response(status=503, headers={"ETag": '"error-body"'})))
        self.assertIsNone(unbound.resource_etag)
        self.assertFalse(entry.accept_response(Response(status=412, headers={"ETag": '"A"'})))
        self.assertTrue(entry.resource_changed)
        self.assertEqual(entry.cached_bytes(), 0)

    def test_unvalidated_ranges_are_served_without_cache_join(self):
        entry = self.segment("unvalidated.mp4")
        self.proxy._session.close()
        self.proxy._session = Session([
            Response(b"AA", 206, {"Content-Range": "bytes 0-3/4"}),
            Response(b"BB", 206, {"Content-Range": "bytes 2-3/4"}),
        ])
        self.assertEqual(b"".join(self.proxy._iterate_bytes(entry, 0, 3)), b"AA")
        self.assertEqual(entry.cached_bytes(), 0)
        self.assertEqual(len(self.proxy._session.requests), 1)

    def test_cross_origin_children_drop_sensitive_custom_headers(self):
        self.parent.extra_headers = {"Authorization": "SYNTHETIC", "X-Token": "SYNTHETIC", "User-Agent": "test"}
        for url in ("http://example.test/segment.ts", "https://example.test:444/segment.ts", "https://other.test/segment.ts"):
            _, child = self.proxy._wrap_relative(url, self.parent.upstream, self.parent)
            self.assertNotIn("Authorization", child.headers)
            self.assertNotIn("X-Token", child.headers)
            self.assertEqual(child.headers["User-Agent"], "test")
        _, same = self.proxy._wrap_relative("https://EXAMPLE.TEST:443/same.ts", self.parent.upstream, self.parent)
        self.assertEqual(same.headers["Authorization"], "SYNTHETIC")

    def test_failed_sequence_prefetch_retries_with_delay(self):
        entry = self.segment("prefetch.mp4")
        entry.player_data_requested = True
        class FailingSession:
            calls = 0
            def get(inner, *_args, **_kwargs):
                inner.calls += 1
                return Response(status=403)
        session = FailingSession()
        worker = _Prefetcher(entry, lambda: 10, session)
        worker.start()
        time.sleep(.12)
        entry.close()
        worker.join(1)
        self.assertEqual(session.calls, 1)
        self.assertFalse(worker.is_alive())

    def test_failed_hls_prefetch_retries_with_delay(self):
        child = self.segment()
        self.parent.hls_segment_entries = [child]
        self.parent.hls_segment_durations = [2.0]
        class FailingSession:
            calls = 0
            def get(inner, *_args, **_kwargs):
                inner.calls += 1
                return Response(status=429, headers={"Retry-After": "3"})
        session = FailingSession()
        worker = _HlsPrefetcher(self.parent, lambda: 10, session)
        worker.start()
        time.sleep(.12)
        self.parent.close()
        worker.join(1)
        self.assertEqual(session.calls, 1)
        self.assertFalse(worker.is_alive())

    def test_range_ignored_prefetch_stops_at_target(self):
        entry = self.segment("range.mp4")
        response = Response(b"abcdefghij", headers={"ETag": '"A"', "Content-Length": "10"})
        session = Session([response])
        self.assertTrue(_Prefetcher(entry, lambda: 10, session)._fetch_into_cache(0, 1))
        self.assertEqual(entry.cached_bytes(), 2)
        self.assertEqual(entry.read_at(0, 10), b"ab")
        self.assertTrue(response.closed)

    def test_large_ignored_seek_prefetch_does_not_consume_body(self):
        child = self.segment("large.mp4")
        response = Response(b"abcdefghij", headers={"ETag": '"A"'})
        self.assertFalse(_Prefetcher(child, lambda: 10, Session([response]))._fetch_into_cache(1000000, 1000001))
        self.assertEqual(response.read_chunks, 0)
        self.assertEqual(child.cached_bytes(), 0)

    def test_logical_byteranges_use_correct_requests_without_duplicate_object_fetch(self):
        text = "#EXTM3U\n#EXTINF:2,\n#EXT-X-BYTERANGE:2@1\nblob.mp4\n#EXTINF:2,\n#EXT-X-BYTERANGE:2\nblob.mp4\n#EXT-X-ENDLIST\n"
        _, segments, _ = self.proxy._rewrite_m3u8(text, self.parent.upstream, self.parent)
        self.assertIs(segments[0], segments[1])
        self.assertEqual(self.parent.hls_segment_ranges, [(1, 3), (3, 5)])
        session = Session([
            Response(b"bc", 206, {"Content-Range": "bytes 1-2/10", "ETag": '"A"'}),
            Response(b"de", 206, {"Content-Range": "bytes 3-4/10", "ETag": '"A"'}),
        ])
        worker = _HlsPrefetcher(self.parent, lambda: 10, session)
        self.addCleanup(worker.pool.close)
        for interval in self.parent.hls_segment_ranges:
            self.assertTrue(worker._fetch_segment(segments[0], interval))
        self.assertEqual([row[1]["headers"]["Range"] for row in session.requests], ["bytes=1-2", "bytes=3-4"])
        self.assertEqual(segments[0].read_at(1, 4), b"bcde")
        self.assertEqual(segments[0].cached_bytes(), 4)

    def test_live_refresh_retires_old_children_with_bounded_grace(self):
        for start in range(100):
            text = f"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:{start}\n" + "".join(f"#EXTINF:2,\n{s}.ts\n" for s in range(start, start + 3))
            _, segments, durations = self.proxy._rewrite_m3u8(text, self.parent.upstream, self.parent)
            self.parent.hls_segment_entries, self.parent.hls_segment_durations = segments, durations
        self.assertLessEqual(len(self.parent.children), 3 + MAX_LIVE_RETIRED_ENTRIES)
        self.assertLessEqual(len(list(self.proxy.cache_dir.glob("vs_*.bin"))), 4 + MAX_LIVE_RETIRED_ENTRIES)
        with patch("src.media_proxy.time.monotonic", return_value=time.monotonic() + 20):
            self.proxy._rewrite_m3u8(text, self.parent.upstream, self.parent)
        self.assertEqual(len(self.parent.children), 3)

    def test_live_eviction_preserves_inflight_player_request(self):
        old = self.segment("old.ts")
        old.active_requests = 1
        old.retired_at = time.monotonic() - 20
        self.proxy._retire_manifest_children(self.parent, set())
        self.assertFalse(old.closed)
        old.active_requests = 0
        self.proxy._retire_manifest_children(self.parent, set())
        self.assertTrue(old.closed)

    def test_child_registration_is_atomic_under_refresh_race(self):
        barrier = threading.Barrier(9)
        results = []
        def wrap():
            barrier.wait()
            results.append(self.proxy._wrap_relative("race.ts", self.parent.upstream, self.parent)[1])
        workers = [threading.Thread(target=wrap) for _ in range(8)]
        for worker in workers: worker.start()
        barrier.wait()
        for worker in workers: worker.join(2)
        self.assertEqual(len(set(map(id, results))), 1)
        self.assertEqual(len(self.parent.children), 1)

    def test_concurrent_manifest_responses_publish_one_complete_prefetch_plan(self):
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                first_rewritten, second_done = threading.Event(), threading.Event()
                failures = []
                original_rewrite = self.proxy._rewrite_m3u8

                def payload():
                    sequence, name, offset, duration = (
                        (10, "first", 0, 2) if threading.current_thread().name == "FirstManifest"
                        else (20, "second", 100, 3)
                    )
                    return (f"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:{sequence}\n"
                            f"#EXTINF:{duration},\n#EXT-X-BYTERANGE:4@{offset}\n{name}.mp4\n").encode()

                def rewrite(*args):
                    result = original_rewrite(*args)
                    if threading.current_thread().name == "FirstManifest":
                        first_rewritten.set()
                        if not second_done.wait(3):
                            raise AssertionError("Second manifest did not finish")
                    return result

                def serve(second=False):
                    try:
                        if wrapped:
                            self.proxy._serve_wrapped_manifest(Request(), self.parent, body=True)
                        else:
                            self.proxy._serve_manifest(Request(), self.parent)
                    except BaseException as error:
                        failures.append(error)
                    finally:
                        if second:
                            second_done.set()

                with patch.object(self.proxy, "_rewrite_m3u8", side_effect=rewrite), \
                     patch.object(self.proxy, "_ensure_hls_prefetcher"), \
                     patch.object(self.proxy._session, "get", side_effect=lambda *args, **kwargs: Response(payload())), \
                     patch.object(self.proxy, "_fetch_wrapped_payload", side_effect=lambda *args: (payload(), self.parent.upstream)):
                    first = threading.Thread(target=serve, name="FirstManifest", daemon=True)
                    second = threading.Thread(target=lambda: serve(second=True), name="SecondManifest", daemon=True)
                    first.start()
                    self.assertTrue(first_rewritten.wait(3))
                    second.start()
                    first.join(3)
                    second.join(3)
                    self.assertFalse(first.is_alive())
                    self.assertFalse(second.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual([child.upstream.rsplit("/", 1)[-1] for child in self.parent.hls_segment_entries], ["second.mp4"])
                self.assertEqual(self.parent.hls_segment_durations, [3.0])
                self.assertEqual(self.parent.hls_segment_ranges, [(100, 104)])
                self.assertEqual(self.parent.hls_media_sequence, 20)

    def test_cache_byte_budget_never_overruns_and_releases_on_close(self):
        budget = _CacheBudget(4096)
        child = _Entry("https://example.test/file", {}, self.proxy.cache_dir, cache_budget=budget)
        try:
            self.assertTrue(child.write_at(0, b"123456"))
            self.assertFalse(child.write_at(4096, b"789012"))
            self.assertEqual(budget.used, 4096)
            self.assertEqual(child.cached_bytes(), 6)
            self.assertTrue(child.write_at(0, b"abcdef"))
            self.assertEqual(budget.used, 4096)
        finally:
            child.close()
        self.assertEqual(budget.used, 0)

    def test_backward_seek_updates_anchor_and_absolute_sequence(self):
        text = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:40\n" + "".join(f"#EXTINF:2,\n{i}.ts\n" for i in range(12))
        _, segments, durations = self.proxy._rewrite_m3u8(text, self.parent.upstream, self.parent)
        self.parent.hls_segment_entries, self.parent.hls_segment_durations = segments, durations
        self.proxy._note_player_segment(segments[10])
        self.proxy._note_player_segment(segments[1])
        self.assertEqual(self.parent.hls_player_segment_idx, 1)
        self.assertEqual(self.parent.hls_player_sequence, 41)

    def test_html_login_body_is_not_rewritten_as_manifest(self):
        response = Response(b"<html>Login required</html>", headers={"Content-Type": "text/html"})
        self.proxy._session.close()
        self.proxy._session = Session([response])
        request = Request()
        self.assertTrue(self.proxy._serve_manifest(request, self.parent))
        self.assertEqual(request.status, 502)
        self.assertEqual(self.parent.children, [])
        self.assertIn(b"not an HLS manifest", request.wfile.getvalue())
        self.assertTrue(response.closed)

    def test_marker_only_or_dangling_playlist_structure_is_rejected(self):
        for text in ("#EXTM3U\nLogin required\n", "#EXTM3U\n#EXTINF:3,\n",
                     "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n",
                     "#EXTM3U\n#EXT-X-BYTERANGE:2@0\n", "#EXTM3Uinvalid\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.proxy._rewrite_m3u8(text, self.parent.upstream, self.parent)
        self.assertEqual(self.proxy._rewrite_m3u8("#EXTM3U\n#EXT-X-TARGETDURATION:3\n", self.parent.upstream, self.parent)[1], [])

    def test_manifest_and_segment_budgets_reject_large_inputs(self):
        response = Response(b"#EXTM3U\n" * 30)
        self.proxy._session.close()
        self.proxy._session = Session([response])
        request = Request()
        with patch("src.media_proxy.MAX_MANIFEST_BYTES", 16):
            self.proxy._serve_manifest(request, self.parent)
        self.assertEqual(request.status, 502)
        child = self.segment()
        worker = _HlsPrefetcher(self.parent, lambda: 10, Session([Response(b"x" * 32)]))
        self.addCleanup(worker.pool.close)
        with patch("src.media_proxy.MAX_SEGMENT_BYTES", 16):
            self.assertFalse(worker._fetch_segment(child))
        self.assertEqual(child.cached_bytes(), 0)

    def test_stale_generation_prefetch_does_not_fetch(self):
        child = self.segment()
        session = Session([Response()])
        worker = _HlsPrefetcher(self.parent, lambda: 10, session)
        self.addCleanup(worker.pool.close)
        self.parent.prefetch_generation += 1
        self.assertFalse(worker._fetch_segment(child, generation=0))
        self.assertEqual(session.requests, [])

    def test_pool_binds_total_inflight_and_does_not_queue(self):
        pool = _BoundedHlsPool()
        release = threading.Event()
        self.addCleanup(release.set)
        self.addCleanup(pool.close)
        futures = [pool.submit(release.wait) for _ in range(PLAYBACK_HLS_PREFETCH_CONNECTIONS)]
        self.assertTrue(all(future is not None for future in futures))
        self.assertIsNone(pool.submit(release.wait))
        release.set()
        for future in futures: future.result(1)

    def test_pool_cannot_hold_interpreter_exit(self):
        code = "from src.media_proxy import _BoundedHlsPool; import threading; p=_BoundedHlsPool(); p.submit(threading.Event().wait); print('done',flush=True)"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "done")

    def test_stop_unsubscribes_once(self):
        calls = []
        self.proxy._unsubscribe_settings = lambda: calls.append(True)
        self.proxy.stop()
        self.proxy.stop()
        self.assertEqual(calls, [True])


class RedirectAdapter(BaseAdapter):
    def __init__(self, target):
        self.target, self.seen = target, []
    def send(self, request, **kwargs):
        self.seen.append(dict(request.headers))
        response = requests.Response()
        response.request, response.url = request, request.url
        response.headers = CaseInsensitiveDict()
        response._content = b"ok"
        response.raw = io.BytesIO(b"ok")
        if len(self.seen) == 1:
            response.status_code = 302
            response.headers["Location"] = self.target
        else:
            response.status_code = 200
        return response
    def close(self): pass


class HeaderScopeTests(unittest.TestCase):
    def test_real_requests_redirects_strip_custom_headers_at_each_origin_boundary(self):
        for target in ("https://other.test/v", "http://example.test/v", "https://example.test:444/v"):
            with self.subTest(target=target):
                session = build_scoped_session()
                adapter = RedirectAdapter(target)
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                session.trust_env = False
                try:
                    session.get("https://example.test/v", headers={"X-Token": "SYNTHETIC", "Authorization": "Bearer SYNTHETIC"})
                    self.assertEqual(len(adapter.seen), 2)
                    self.assertNotIn("X-Token", adapter.seen[1])
                    self.assertNotIn("Authorization", adapter.seen[1])
                finally:
                    session.close()


class ResponseLifecycleTests(unittest.TestCase):
    def test_watchdog_does_not_abort_response_that_finished_during_cancellation_check(self):
        entered, release = threading.Event(), threading.Event()
        watcher = []

        def cancelled():
            if threading.current_thread().name == "ResponseDeadline":
                watcher.append(threading.current_thread())
                entered.set()
                release.wait(3)
                return True
            return False

        self.addCleanup(release.set)
        response = Response()
        with patch("src.http_limits.abort_response") as abort:
            chunks = iter_limited_response(response, 10, 5, cancelled)
            self.assertEqual(next(chunks), b"abcd")
            self.assertTrue(entered.wait(1))
            self.assertEqual(list(chunks), [])
            release.set()
            watcher[0].join(1)
            self.assertFalse(watcher[0].is_alive())
            abort.assert_not_called()

    def test_completed_raw_response_cannot_abort_a_reused_connection(self):
        response = Response()
        reused_socket = Mock(spec=socket.socket)
        response.raw = Mock(closed=True)
        response.raw._connection.sock = reused_socket
        abort_response(response)
        reused_socket.shutdown.assert_not_called()
        self.assertFalse(response.closed)


class TransportBoundaryTests(unittest.TestCase):
    def server(self, mode):
        started, release = threading.Event(), threading.Event()
        class Handler(socketserver.BaseRequestHandler):
            def handle(inner):
                inner.request.settimeout(1)
                data = b""
                try:
                    while b"\r\n\r\n" not in data:
                        data += inner.request.recv(4096)
                    if mode == "normal":
                        body = b"x" * 160000
                        inner.request.sendall(f"HTTP/1.0 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
                        return
                    if mode == "headers":
                        inner.request.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                    else:
                        inner.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n")
                    started.set()
                    while not release.wait(.02):
                        inner.request.sendall(b"x")
                except OSError:
                    pass
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True
        server = Server(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(1)
        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.server_address[1]}/", started

    def session(self):
        session = build_scoped_session()
        session.trust_env = False
        self.addCleanup(session.close)
        return session

    def test_normal_http10_detached_response_is_not_truncated(self):
        url, _ = self.server("normal")
        response = self.session().get(url, timeout=(1, 1))
        self.assertEqual(len(response.content), 160000)

    def test_proxy_stop_interrupts_preheader_hls_fetch(self):
        url, started = self.server("headers")
        proxy = MediaProxyServer(buffer_seconds=0)
        self.addCleanup(proxy.stop)
        parent_url = proxy.register("https://example.test/list.m3u8", is_hls=True)
        parent = proxy._lookup(parent_url)
        segment_url = proxy.register(url, parent=parent)
        segment = proxy._lookup(segment_url)
        worker = _HlsPrefetcher(parent, lambda: 1, proxy._session, pool=proxy._hls_pool)
        future = proxy._hls_pool.submit(worker._fetch_segment, segment)
        self.assertTrue(started.wait(1))
        began = time.monotonic()
        proxy.stop()
        self.assertFalse(future.result(1))
        self.assertLess(time.monotonic() - began, 1)

    def test_dripping_headers_obey_absolute_header_deadline(self):
        url, _ = self.server("headers")
        session = self.session()
        session.get_adapter(url).HEADER_DEADLINE_SECONDS = .15
        began = time.monotonic()
        with self.assertRaises(requests.RequestException):
            session.get(url, timeout=(1, 1))
        self.assertLess(time.monotonic() - began, 1)

    def test_session_close_interrupts_preheader_request(self):
        url, started = self.server("headers")
        session = self.session()
        outcome = []
        def fetch():
            try: session.get(url, timeout=(1, 1))
            except Exception: outcome.append("stopped")
        worker = threading.Thread(target=fetch, daemon=True)
        worker.start()
        self.assertTrue(started.wait(1))
        began = time.monotonic()
        session.close()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, ["stopped"])
        self.assertLess(time.monotonic() - began, 1)

    def test_dripping_body_obeys_absolute_body_deadline(self):
        url, _ = self.server("body")
        response = self.session().get(url, stream=True, timeout=(1, 1))
        began = time.monotonic()
        with self.assertRaises(Exception):
            read_limited_response(response, 20000, .15)
        self.assertLess(time.monotonic() - began, 1)
        response.close()

    def test_cancellation_interrupts_dripping_body(self):
        url, _ = self.server("body")
        response = self.session().get(url, stream=True, timeout=(1, 1))
        cancelled = threading.Event()
        timer = threading.Timer(.15, cancelled.set)
        timer.start()
        self.addCleanup(timer.cancel)
        began = time.monotonic()
        with self.assertRaises(Exception):
            read_limited_response(response, 20000, 10, cancelled.is_set)
        self.assertLess(time.monotonic() - began, 1)
        response.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
