"""Offline checks for HLS preview selection and bounded prefetch traffic.

Run with ``.venv/bin/python tests/test_hls_preview_master.py``. The integration
fixture serves only loopback HTTP; no YouTube or other internet access is used.
"""

from __future__ import annotations

import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.media_proxy import MediaProxyServer, _select_hls_master_variant


MASTER_URL = "https://example.test/hls/master.m3u8"
SELECTED_URL = "https://example.test/hls/video.m3u8"


def playlist_urls(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


class RecordingRequest:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers[name] = value

    def end_headers(self):
        pass


class ManifestResponse:
    def __init__(self, status, text, url=MASTER_URL):
        self.status_code = status
        self.text = text
        self.url = url
        self.headers = {"Content-Type": "application/vnd.apple.mpegurl"}
        self.closed = False

    def close(self):
        self.closed = True


class LocalHlsOrigin:
    """Tiny origin recording manifests and segment traffic, including ranges."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        self.condition = threading.Condition()
        origin = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):  # noqa: N802
                with origin.condition:
                    origin.requests.append(self.path)
                    origin.condition.notify_all()
                status, content_type, payload = origin.routes.get(
                    self.path, (404, "text/plain", b"unexpected resource")
                )
                headers = {}
                span = self.headers.get("Range")
                if status == 200 and span:
                    match = re.fullmatch(r"bytes=(\d+)-(\d*)", span)
                    if match:
                        start = int(match.group(1))
                        end = min(int(match.group(2) or len(payload) - 1),
                                  len(payload) - 1)
                        headers["Content-Range"] = (
                            f"bytes {start}-{end}/{len(payload)}"
                        )
                        payload = payload[start:end + 1]
                        status = 206
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def wait_for(self, paths, timeout=5):
        deadline = time.monotonic() + timeout
        with self.condition:
            while not set(paths).issubset(self.requests):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
        return True


class HlsPreviewMasterTests(unittest.TestCase):
    def proxy_entry(self, *, upstream=MASTER_URL, selected=None):
        proxy = MediaProxyServer()
        self.addCleanup(proxy.stop)
        # Unit checks do not need a server or a background sequential worker.
        with mock.patch("src.media_proxy._Prefetcher.start"):
            local = proxy.register(upstream, is_hls=True, hls_variant_url=selected)
        return proxy, proxy._lookup(urlparse(local).path)

    def test_selected_variant_retains_referenced_groups_and_global_tags(self):
        master = '''#EXTM3U
#EXT-X-VERSION:6
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-SESSION-KEY:METHOD=AES-128,URI="session.key"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="chosen-audio",NAME="English",URI="audio.m3u8"
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="chosen-camera",NAME="Main",URI="camera.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="chosen-subs",NAME="English",URI="subs.m3u8"
#EXT-X-MEDIA:TYPE=CLOSED-CAPTIONS,GROUP-ID="chosen-cc",NAME="CC",INSTREAM-ID="CC1"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="other-audio",NAME="Other",URI="other-audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1500000,AUDIO="chosen-audio",VIDEO="chosen-camera",SUBTITLES="chosen-subs",CLOSED-CAPTIONS="chosen-cc"
video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=9000000,AUDIO="other-audio"
other.m3u8
#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=100000,URI="iframes.m3u8"
'''
        filtered = _select_hls_master_variant(master, MASTER_URL, SELECTED_URL)
        self.assertEqual(playlist_urls(filtered), ["video.m3u8"])
        self.assertEqual(filtered.count("#EXT-X-STREAM-INF:"), 1)
        self.assertEqual(filtered.count("#EXT-X-MEDIA:"), 4)
        for tag in ("#EXT-X-VERSION:6", "#EXT-X-INDEPENDENT-SEGMENTS",
                    '#EXT-X-SESSION-KEY:METHOD=AES-128,URI="session.key"'):
            self.assertIn(tag, filtered)
        for value in ("other-audio", "other.m3u8", "iframes.m3u8"):
            self.assertNotIn(value, filtered)

    def test_same_video_playlist_uses_highest_bandwidth_audio_pairing(self):
        master = '''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="low",NAME="Low",URI="low.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="high",NAME="High",URI="high.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1000000,AUDIO="low"
video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1100000,AUDIO="high"
video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=9000000
different-video.m3u8
'''
        filtered = _select_hls_master_variant(master, MASTER_URL, SELECTED_URL)
        self.assertEqual(playlist_urls(filtered), ["video.m3u8"])
        self.assertIn('BANDWIDTH=1100000,AUDIO="high"', filtered)
        self.assertIn('GROUP-ID="high"', filtered)
        self.assertNotIn('GROUP-ID="low"', filtered)
        self.assertNotIn("different-video", filtered)

    def test_googlevideo_refreshed_signatures_keep_fresh_request_uri(self):
        stale = ("https://r1.googlevideo.com/api/manifest/id/clip/itag/96/"
                 "expire/2000/sig/old/lsig/old-l?signature=old-q&foo=1")
        fresh = stale.replace("/old/", "/new/").replace("old-l?", "new-l?") \
            .replace("signature=old-q", "signature=new-q")
        master = f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\n{fresh}\n"
        filtered = _select_hls_master_variant(master, MASTER_URL, stale)
        self.assertEqual(playlist_urls(filtered), [fresh])
        self.assertNotIn(stale, filtered)
        for changed in (
            fresh.replace("r1.googlevideo.com", "r2.googlevideo.com"),
            fresh.replace("/id/clip/", "/id/another/"),
            fresh.replace("/itag/96/", "/itag/95/"),
            fresh.replace("/expire/2000/", "/expire/2001/"),
            fresh.replace("foo=1", "foo=2"),
        ):
            with self.subTest(changed=changed):
                other = f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\n{changed}\n"
                with self.assertRaises(ValueError):
                    _select_hls_master_variant(other, MASTER_URL, stale)

    def test_other_hosts_require_exact_resolved_variant_url(self):
        master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nvideo.m3u8?sig=new\n"
        exact = SELECTED_URL + "?sig=new"
        self.assertEqual(playlist_urls(_select_hls_master_variant(
            master, MASTER_URL, exact)), ["video.m3u8?sig=new"])
        for host in ("example.test", "r1.googlevideo.com.example.test"):
            base = f"https://{host}/hls/master.m3u8"
            stale = f"https://{host}/hls/video.m3u8?sig=old"
            with self.subTest(host=host), self.assertRaises(ValueError):
                _select_hls_master_variant(master, base, stale)

    def test_missing_variant_or_media_group_fails_explicitly(self):
        masters = {
            "different video": "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nother.m3u8\n",
            "missing audio": '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000,AUDIO="missing"\nvideo.m3u8\n',
            "missing playlist": "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\n",
        }
        for name, master in masters.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                _select_hls_master_variant(master, MASTER_URL, SELECTED_URL)

    def test_master_playlist_uris_are_not_prefetch_segments(self):
        proxy, entry = self.proxy_entry()
        master = '''#EXTM3U
#EXT-X-SESSION-KEY:METHOD=AES-128,URI="key.key"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Audio",URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1000,AUDIO="audio"
video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000
other.m3u8
'''
        rewritten, segments, durations = proxy._rewrite_m3u8(master, MASTER_URL, entry)
        self.assertEqual(segments, [])
        self.assertEqual(durations, [])
        self.assertEqual(len(playlist_urls(rewritten)), 2)
        self.assertEqual(set(entry.hls_child_by_upstream), {
            "https://example.test/hls/key.key", "https://example.test/hls/audio.m3u8",
            SELECTED_URL, "https://example.test/hls/other.m3u8",
        })
        for local in playlist_urls(rewritten):
            self.assertIsNotNone(proxy._lookup(urlparse(local).path))
        with mock.patch("src.media_proxy._HlsPrefetcher") as worker:
            proxy._ensure_hls_prefetcher(entry)
        worker.assert_not_called()

    def test_media_playlist_rewrites_segments_keys_maps_and_preserves_ranges(self):
        proxy, entry = self.proxy_entry(upstream=SELECTED_URL)
        media = '''#EXTM3U
#EXT-X-MEDIA-SEQUENCE:12
#EXT-X-KEY:METHOD=AES-128,URI="key.key",IV=0x01
#EXT-X-MAP:URI="init.mp4",BYTERANGE="5@0"
#EXTINF:4.25,
#EXT-X-BYTERANGE:3@5
blob.m4s
#EXTINF:5,
#EXT-X-BYTERANGE:2
blob.m4s
#EXT-X-ENDLIST
'''
        rewritten, segments, durations = proxy._rewrite_m3u8(media, SELECTED_URL, entry)
        self.assertEqual(durations, [4.25, 5.0])
        self.assertEqual([segment.upstream for segment in segments],
                         ["https://example.test/hls/blob.m4s"] * 2)
        self.assertIs(segments[0], segments[1])
        self.assertEqual(playlist_urls(rewritten)[0], playlist_urls(rewritten)[1])
        for tag in ("#EXT-X-MEDIA-SEQUENCE:12", 'BYTERANGE="5@0"',
                    "#EXT-X-BYTERANGE:3@5", "#EXT-X-BYTERANGE:2",
                    "IV=0x01", "#EXT-X-ENDLIST"):
            self.assertIn(tag, rewritten)
        for local in re.findall(r'URI="([^"]+)"', rewritten):
            self.assertIsNotNone(proxy._lookup(urlparse(local).path))
        self.assertEqual(set(entry.hls_child_by_upstream), {
            "https://example.test/hls/key.key", "https://example.test/hls/init.mp4",
            "https://example.test/hls/blob.m4s",
        })

    def test_invalid_extinf_does_not_schedule_a_segment(self):
        proxy, entry = self.proxy_entry(upstream=SELECTED_URL)
        for duration in ("NaN", "-1", "3oops", "1.2.3", ""):
            with self.subTest(duration=duration):
                text = f"#EXTM3U\n#EXTINF:{duration},\nsegment.ts\n"
                rewritten, segments, durations = proxy._rewrite_m3u8(text, SELECTED_URL, entry)
                self.assertEqual(segments, [])
                self.assertEqual(durations, [])
                self.assertEqual(len(playlist_urls(rewritten)), 1)

    def test_upstream_manifest_errors_are_not_returned_as_success(self):
        proxy, entry = self.proxy_entry(selected=SELECTED_URL)
        for upstream_status, expected in ((302, 502), (403, 403), (404, 404), (503, 503)):
            with self.subTest(status=upstream_status):
                response = ManifestResponse(upstream_status, "#EXTM3U\n")
                request = RecordingRequest()
                with mock.patch.object(proxy._session, "get", return_value=response), \
                        mock.patch("src.media_proxy._HlsPrefetcher") as worker:
                    self.assertTrue(proxy._serve_manifest(request, entry))
                self.assertEqual(request.status, expected)
                self.assertIn(str(upstream_status).encode(), request.wfile.getvalue())
                self.assertEqual(entry.children, [])
                worker.assert_not_called()
                self.assertTrue(response.closed)

    def test_local_preview_fetches_only_chosen_video_audio_with_nonzero_buffer(self):
        # Sixteen alternatives reproduce the real master fanout without remote URLs.
        # Both the normal default and the user's 190-second preference are exercised.
        chosen = 7
        master = ["#EXTM3U", "#EXT-X-VERSION:6"]
        for index in range(16):
            master += [
                f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-{index}",NAME="Audio",URI="audio-{index}.m3u8"',
                f'#EXT-X-STREAM-INF:BANDWIDTH={1000 + index},AUDIO="audio-{index}"',
                f"video-{index}.m3u8",
            ]
        routes = {"/master.m3u8": (200, "application/vnd.apple.mpegurl",
                                  ("\n".join(master) + "\n").encode())}
        expected_segments = set()
        for kind in ("video", "audio"):
            segment_paths = [f"/{kind}-{chosen}-{index}.ts" for index in range(2)]
            expected_segments.update(segment_paths)
            text = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n" + "".join(
                f"#EXTINF:6,\n{path.lstrip('/')}\n" for path in segment_paths
            ) + "#EXT-X-ENDLIST\n"
            routes[f"/{kind}-{chosen}.m3u8"] = (
                200, "application/vnd.apple.mpegurl", text.encode()
            )
            for path in segment_paths:
                routes[path] = (200, "video/mp2t", (path + " payload").encode())

        for buffer_seconds in (None, 190):
            with self.subTest(buffer_seconds=buffer_seconds):
                origin = LocalHlsOrigin(routes)
                proxy = None
                client = requests.Session()
                # These loopback fixtures must never use a user's HTTP proxy.
                client.trust_env = False
                try:
                    store = None
                    if buffer_seconds is not None:
                        store = SimpleNamespace(
                            get=lambda: SimpleNamespace(playback_buffer_seconds=buffer_seconds),
                            subscribe=lambda *_args, **_kwargs: None,
                        )
                    proxy = MediaProxyServer(settings_store=store).start()
                    self.assertGreater(proxy.buffer_seconds(), 0)
                    local = proxy.register(
                        origin.url + "/master.m3u8", is_hls=True,
                        hls_variant_url=origin.url + f"/video-{chosen}.m3u8",
                    )
                    response = client.get(local, timeout=5)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.text.count("#EXT-X-STREAM-INF:"), 1)
                    self.assertEqual(response.text.count("#EXT-X-MEDIA:"), 1)
                    self.assertIn(f'AUDIO="audio-{chosen}"', response.text)
                    self.assertIn(f'GROUP-ID="audio-{chosen}"', response.text)
                    master_entry = proxy._lookup(urlparse(local).path)
                    self.assertEqual(master_entry.hls_segment_entries, [])
                    self.assertEqual(proxy._hls_prefetchers, {})
                    with origin.condition:
                        self.assertEqual(origin.requests, ["/master.m3u8"])

                    video_local, = playlist_urls(response.text)
                    audio_local, = re.findall(r'URI="([^"]+)"', response.text)
                    segment_locals = []
                    for child_local in (video_local, audio_local):
                        media = client.get(child_local, timeout=5)
                        self.assertEqual(media.status_code, 200)
                        self.assertEqual(len(playlist_urls(media.text)), 2)
                        segment_locals.extend(playlist_urls(media.text))
                    # A nonzero buffer fetches these segments before the player
                    # asks for them. The master itself must not fan out to the
                    # fifteen other video/audio alternatives.
                    self.assertTrue(origin.wait_for(expected_segments))
                    for segment_local in segment_locals:
                        segment = client.get(segment_local, timeout=5)
                        self.assertEqual(segment.status_code, 200)
                        upstream = proxy._lookup(urlparse(segment_local).path).upstream
                        self.assertEqual(segment.content, routes[urlparse(upstream).path][2])
                    self.assertEqual(len(proxy._hls_prefetchers), 2)
                    self.assertNotIn(proxy._token_for(master_entry), proxy._hls_prefetchers)
                    expected_paths = expected_segments | {
                        "/master.m3u8", f"/video-{chosen}.m3u8", f"/audio-{chosen}.m3u8",
                    }
                    with origin.condition:
                        self.assertEqual(set(origin.requests), expected_paths)
                finally:
                    client.close()
                    if proxy is not None:
                        proxy.stop()
                    origin.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
