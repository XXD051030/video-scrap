"""Offline protocol/cookie regressions; no external media or login is used."""

from __future__ import annotations

import gzip
import io
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

import requests
from requests.adapters import BaseAdapter
from urllib3.response import HTTPResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.browser_cookies import browser_cookiejar, build_scoped_session
from src.media_proxy import MediaProxyServer, _Entry, _parse_range_header


class RecordingRequest:
    def __init__(self, range_header=None):
        self.headers = {"Range": range_header} if range_header is not None else {}
        self.status = None
        self.output_headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.output_headers[name] = value

    def end_headers(self):
        pass


class RecordingAdapter(BaseAdapter):
    """Exercise real requests preparation/redirects without any sockets."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.seen = []

    def send(self, request, **kwargs):
        self.seen.append((request.url, dict(request.headers)))
        status, headers, payload = self.routes.get(request.url, (200, {}, b"media"))
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response.url = request.url
        response.request = request
        response.raw = HTTPResponse(body=io.BytesIO(payload), preload_content=False)
        response.connection = self
        return response

    def close(self):
        pass


class ProxyProtocolTests(unittest.TestCase):
    def setUp(self):
        self.proxy = MediaProxyServer(buffer_seconds=0)
        self.addCleanup(self.proxy.stop)
        self.entry = _Entry("https://example.test/media.mp4", {}, self.proxy.cache_dir)
        self.addCleanup(self.entry.close)

    def test_normal_hls_marks_all_playlist_urls_without_extension(self):
        master_url = self.proxy.register("https://example.test/master", is_hls=True)
        parent = self.proxy._lookup(master_url)
        text = '''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="audio-track"
#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=100,URI="iframe-track"
#EXT-X-STREAM-INF:BANDWIDTH=1000,AUDIO="a"
video-track
'''
        rewritten, segments, durations = self.proxy._rewrite_m3u8(
            text, parent.upstream, parent,
        )
        self.assertEqual(segments, [])
        self.assertEqual(durations, [])
        self.assertEqual(len(parent.children), 3)
        for child in parent.children:
            self.assertTrue(child.force_manifest)
            self.assertTrue(self.proxy._likely_manifest(child))
            self.assertEqual(child.proxy_ext, "m3u8")
        self.assertEqual(rewritten.count(".m3u8"), 3)
        video = parent.hls_child_by_upstream["https://example.test/video-track"]
        media, segments, durations = self.proxy._rewrite_m3u8(
            "#EXTM3U\n#EXTINF:3,\nsegment\n#EXT-X-ENDLIST\n",
            video.upstream, video,
        )
        self.assertEqual(durations, [3.0])
        self.assertFalse(segments[0].force_manifest)
        self.assertEqual(segments[0].proxy_ext, "ts")

    def test_rendition_report_is_a_manifest_and_key_map_are_resources(self):
        text = '''#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="key"
#EXT-X-MAP:URI="init"
#EXT-X-RENDITION-REPORT:URI="other-track",LAST-MSN=4
#EXTINF:2,
segment
'''
        self.proxy._rewrite_m3u8(text, self.entry.upstream, self.entry)
        for path, expected in (("key", False), ("init", False),
                               ("other-track", True), ("segment", False)):
            child = self.entry.hls_child_by_upstream[f"https://example.test/{path}"]
            self.assertEqual(child.force_manifest, expected)

    def test_invalid_ranges_never_probe_or_emit_negative_lengths(self):
        invalid = ("bytes=8-3", "bytes=-0", "bytes=-", "bytes=--1",
                   "bytes=1--2", "bytes=1-2,4-5", "bytes=NaN-2", "")
        for total in (None, 16):
            for value in invalid:
                with self.subTest(total=total, value=value):
                    self.entry.total_size = total
                    request = RecordingRequest(value)
                    with patch.object(self.proxy, "_probe_total_size") as probe:
                        self.proxy._serve_range(request, self.entry)
                    probe.assert_not_called()
                    self.assertEqual(request.status, 416)
                    self.assertEqual(request.output_headers["Content-Length"], "0")
                    self.assertEqual(request.wfile.getvalue(), b"")
                    if total is not None:
                        self.assertEqual(request.output_headers["Content-Range"], "bytes */16")

    def test_ranges_preserve_valid_boundaries_and_suffix_clamping(self):
        self.assertIsNone(_parse_range_header(None))
        self.entry.total_size = 16
        self.entry.write_at(0, b"0123456789ABCDEF")
        for spec, expected, interval in (
            ("bytes=8-11", b"89AB", "bytes 8-11/16"),
            ("bytes=8-", b"89ABCDEF", "bytes 8-15/16"),
            ("bytes=-4", b"CDEF", "bytes 12-15/16"),
            ("bytes=-100", b"0123456789ABCDEF", "bytes 0-15/16"),
            ("bytes=14-100", b"EF", "bytes 14-15/16"),
        ):
            with self.subTest(spec=spec):
                request = RecordingRequest(spec)
                self.proxy._serve_range(request, self.entry)
                self.assertEqual(request.status, 206)
                self.assertEqual(request.output_headers["Content-Range"], interval)
                self.assertEqual(int(request.output_headers["Content-Length"]), len(expected))
                self.assertEqual(request.wfile.getvalue(), expected)

    def test_unsatisfiable_ranges_and_empty_resource(self):
        for total, spec in ((16, "bytes=16-"), (16, "bytes=99-100"),
                            (0, "bytes=0-"), (0, "bytes=-1")):
            with self.subTest(total=total, spec=spec):
                self.entry.total_size = total
                request = RecordingRequest(spec)
                self.proxy._serve_range(request, self.entry)
                self.assertEqual(request.status, 416)
                self.assertEqual(request.output_headers["Content-Length"], "0")

    def test_unknown_size_suffix_is_forwarded_unchanged(self):
        request = RecordingRequest("bytes=-64")
        with patch.object(self.proxy, "_probe_total_size"), \
                patch.object(self.proxy, "_proxy_passthrough") as passthrough:
            self.proxy._serve_range(request, self.entry)
        passthrough.assert_called_once_with(request, self.entry, range_header="bytes=-64")

    def test_passthrough_preserves_compressed_wire_representation(self):
        decoded = b"sample media" * 100
        wire = gzip.compress(decoded)
        adapter = RecordingAdapter({self.entry.upstream: (200, {
            "Content-Type": "video/mp4", "Content-Encoding": "gzip",
            "Content-Length": str(len(wire)),
        }, wire)})
        self.proxy._session.mount("https://", adapter)
        request = RecordingRequest()
        self.proxy._proxy_passthrough(request, self.entry)
        self.assertEqual(request.output_headers["Content-Encoding"], "gzip")
        self.assertEqual(int(request.output_headers["Content-Length"]), len(wire))
        self.assertEqual(request.wfile.getvalue(), wire)
        self.assertEqual(gzip.decompress(request.wfile.getvalue()), decoded)
        self.assertEqual(adapter.seen[0][1]["Accept-Encoding"], "identity")

    def test_passthrough_retains_suffix_range_and_partial_wire_headers(self):
        adapter = RecordingAdapter({self.entry.upstream: (206, {
            "Content-Range": "bytes 12-15/16", "Content-Length": "4",
        }, b"CDEF")})
        self.proxy._session.mount("https://", adapter)
        request = RecordingRequest("bytes=-4")
        self.proxy._proxy_passthrough(request, self.entry, range_header="bytes=-4")
        self.assertEqual(adapter.seen[0][1]["Range"], "bytes=-4")
        self.assertEqual(request.status, 206)
        self.assertEqual(request.output_headers["Content-Range"], "bytes 12-15/16")
        self.assertEqual(request.wfile.getvalue(), b"CDEF")

    def test_direct_dash_is_rejected_explicitly_without_unrewritten_mpd(self):
        for url, options in (("https://example.test/stream.mpd", {}),
                             ("https://example.test/manifest", {"is_dash": True})):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "DASH/MPD preview"):
                self.proxy.register(url, **options)


class BrowserCookieScopeTests(unittest.TestCase):
    def setUp(self):
        now = int(time.time())
        self.records = [
            {"name": "host", "value": "H", "domain": "example.test", "host_only": True},
            {"name": "domain", "value": "D", "domain": ".example.test", "host_only": False},
            {"name": "path", "value": "P", "domain": "example.test", "path": "/private"},
            {"name": "secure", "value": "S", "domain": "example.test", "secure": True},
            {"name": "expired", "value": "X", "domain": "example.test", "expires": now - 60},
        ]
        self.jar = browser_cookiejar(self.records)
        self.session = build_scoped_session()
        self.session.trust_env = False
        self.addCleanup(self.session.close)

    def cookie_names(self, url):
        adapter = RecordingAdapter()
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        response = self.session.get(url, cookies=self.jar)
        response.close()
        header = adapter.seen[-1][1].get("Cookie", "")
        return {part.split("=", 1)[0].strip() for part in header.split(";") if part}

    def test_domain_host_only_path_secure_and_expiration(self):
        cases = {
            "https://example.test/private/video": {"host", "domain", "path", "secure"},
            "http://example.test/private/video": {"host", "domain", "path"},
            "https://example.test/public": {"host", "domain", "secure"},
            "https://example.test/private-other": {"host", "domain", "secure"},
            "https://sub.example.test/private/video": {"domain"},
            "https://example.test.evil.test/private/video": set(),
            "https://another.test/private/video": set(),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(self.cookie_names(url), expected)

    def test_real_requests_redirects_recheck_each_hop_scope(self):
        urls = ["https://example.test/private/start", "https://sub.example.test/private/next",
                "http://example.test/public", "https://evil.test/final"]
        adapter = RecordingAdapter({
            url: (302, {"Location": urls[index + 1]}, b"")
            for index, url in enumerate(urls[:-1])
        })
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        response = self.session.get(urls[0], cookies=self.jar)
        response.close()
        expected = ({"host", "domain", "path", "secure"}, {"domain"}, {"host", "domain"}, set())
        self.assertEqual(len(adapter.seen), 4)
        for (url, headers), names in zip(adapter.seen, expected):
            header_names = {part.split("=", 1)[0].strip()
                            for part in headers.get("Cookie", "").split(";") if part}
            self.assertEqual(header_names, names, url)

    def test_incomplete_or_invalid_records_never_make_domainless_cookies(self):
        invalid = [{"name": "bad", "value": "X"}, {"name": "bad", "value": "X", "domain": ""},
                   {"name": "bad\r\nCookie", "value": "X", "domain": "example.test"},
                   {"name": "bad", "value": "X\nY", "domain": "example.test"},
                   {"name": "bad", "value": "X", "domain": "https://example.test"},
                   {"name": "bad", "value": "X", "domain": "example.test", "expires": "NaN"}]
        self.assertEqual(list(browser_cookiejar(invalid)), [])

    def test_missing_host_only_is_conservative_and_explicit_domain_can_share(self):
        self.jar = browser_cookiejar([
            {"name": "default", "value": "H", "domain": "example.test"},
            {"name": "explicit", "value": "D", "domain": "example.test", "host_only": False},
        ])
        self.assertEqual(self.cookie_names("https://example.test/"), {"default", "explicit"})
        self.assertEqual(self.cookie_names("https://sub.example.test/"), {"explicit"})

    def test_host_only_localhost_and_ip_cookies_stay_on_exact_host(self):
        for host in ("localhost", "127.0.0.1"):
            with self.subTest(host=host):
                self.jar = browser_cookiejar([{
                    "name": "local", "value": "H", "domain": host, "host_only": True,
                }])
                self.assertEqual(self.cookie_names(f"http://{host}/"), {"local"})
                self.assertEqual(self.cookie_names(f"http://child.{host}/"), set())

    def test_hls_children_use_the_snapshot_without_cross_host_cookie_broadcast(self):
        proxy = MediaProxyServer(buffer_seconds=0)
        self.addCleanup(proxy.stop)
        adapter = RecordingAdapter()
        proxy._session.mount("https://", adapter)
        local = proxy.register("https://example.test/master", is_hls=True,
                               browser_cookies=self.records)
        parent = proxy._lookup(local)
        text = '''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="https://sub.example.test/audio"
#EXT-X-STREAM-INF:BANDWIDTH=1000,AUDIO="a"
https://cdn.unrelated.test/video
'''
        proxy._rewrite_m3u8(text, parent.upstream, parent)
        for child in parent.children:
            self.assertIs(child.cookiejar, parent.cookiejar)
            proxy._session.get(child.upstream, **child.cookie_kwargs).close()
        self.assertEqual(adapter.seen[0][1].get("Cookie"), "domain=D")
        self.assertNotIn("Cookie", adapter.seen[1][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
