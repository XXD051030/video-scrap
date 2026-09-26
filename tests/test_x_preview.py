"""Offline checks for X preview format choice and media proxy credentials.

Run directly with ``.venv/bin/python tests/test_x_preview.py``.
"""

from __future__ import annotations

import os
import sys
import threading
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
from requests.cookies import create_cookie

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from src.gui.preview_panel import PreviewPanel
from src.media_proxy import MediaProxyServer
from src.scraper import VideoItem


def _x_item(formats: list[dict]) -> VideoItem:
    return VideoItem(
        title="Post video",
        url="https://x.com/example/status/123",
        source_url="https://x.com/example/status/123",
        formats=formats,
        referer="https://x.com/example/status/123",
    )


def test_x_prefers_combined_mp4_and_falls_back_to_hls() -> None:
    video_only = {
        "url": "https://video.twimg.com/high.mp4",
        "ext": "mp4",
        "protocol": "https",
        "vcodec": "h264",
        "acodec": "none",
        "height": 1080,
    }
    combined = {
        "url": "https://video.twimg.com/medium.mp4",
        "ext": "mp4",
        "protocol": "https",
        "vcodec": "h264",
        "acodec": "aac",
        "height": 720,
    }
    hls = {
        "url": "https://video.twimg.com/master.m3u8",
        "ext": "mp4",
        "protocol": "m3u8_native",
        "vcodec": "h264",
        "acodec": "aac",
        "height": 1080,
    }
    assert PreviewPanel._best_x_preview_format(_x_item([video_only, hls, combined])) == combined
    assert PreviewPanel._best_x_preview_format(_x_item([video_only, hls])) == hls
    assert PreviewPanel._is_hls_format(hls)


def test_x_preview_passes_selected_format_headers_and_hls_flag() -> None:
    class FakeProxy:
        registered = None

        def register(self, *args, **kwargs):
            self.registered = (args, kwargs)
            return "http://127.0.0.1:60000/play/token.m3u8"

        def unregister(self, _url):
            pass

    app = QApplication.instance() or QApplication([])
    proxy = FakeProxy()
    panel = PreviewPanel(proxy=proxy)
    cookiejar = CookieJar()
    fmt = {
        "url": "https://video.twimg.com/master.m3u8",
        "ext": "mp4",
        "protocol": "m3u8_native",
        "vcodec": "h264",
        "acodec": "aac",
        "http_headers": {"User-Agent": "format-agent", "Referer": "https://x.com/"},
    }
    item = _x_item([fmt])
    item.raw = {"http_headers": {"Accept-Language": "en"}}
    item.x_cookiejar = cookiejar
    try:
        panel.show_video(item)
        assert panel._pending_playable_url == fmt["url"]
        assert panel._wrap_with_proxy(panel._pending_playable_url, item)
        args, kwargs = proxy.registered
        assert args == (fmt["url"],)
        assert kwargs["is_hls"] is True
        assert kwargs["cookiejar"] is cookiejar
        assert kwargs["extra_headers"] == {
            "Accept-Language": "en",
            "User-Agent": "format-agent",
            "Referer": "https://x.com/",
        }
    finally:
        panel.shutdown()
        panel.close()
        # Keep QApplication alive until the Qt widgets have been destroyed.
        assert app is not None


def test_proxy_scopes_cookies_and_sensitive_headers_across_hls_children() -> None:
    seen: list[tuple[str, str, dict[str, str]]] = []
    seen_lock = threading.Lock()

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):  # noqa: N802
            with seen_lock:
                seen.append((self.path, self.headers.get("Host", ""), dict(self.headers)))
            if self.path == "/playlist.m3u8":
                port = self.server.server_address[1]
                payload = (
                    "#EXTM3U\n#EXT-X-TARGETDURATION:4\n"
                    f"#EXTINF:4,\nhttp://127.0.0.1:{port}/same.ts\n"
                    f"#EXTINF:4,\nhttp://localhost:{port}/other.ts\n"
                    "#EXT-X-ENDLIST\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path == "/redirect.mp4":
                port = self.server.server_address[1]
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{port}/redirected.mp4")
                self.end_headers()
                return
            payload = b"0123456789"
            byte_range = self.headers.get("Range")
            if byte_range:
                part = byte_range.removeprefix("bytes=")
                start_text, _, end_text = part.partition("-")
                start = int(start_text)
                end = min(int(end_text) if end_text else len(payload) - 1, len(payload) - 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                payload = payload[start : end + 1]
            else:
                self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    proxy = MediaProxyServer()
    proxy._buffer_seconds = 0  # No background prefetch noise in this test.
    proxy.start()
    port = upstream.server_address[1]
    cookiejar = CookieJar()
    cookiejar.set_cookie(create_cookie(name="auth_token", value="secret", domain="127.0.0.1"))
    try:
        local = proxy.register(
            f"http://127.0.0.1:{port}/playlist.m3u8",
            is_hls=True,
            cookiejar=cookiejar,
            extra_headers={
                "Authorization": "Bearer private",
                "Cookie": "unsafe=secret",
                "Host": "evil.invalid",
                "Range": "bytes=999-1000",
                "User-Agent": "preview-test",
            },
        )
        response = requests.get(local, timeout=5)
        response.raise_for_status()
        child_urls = [line for line in response.text.splitlines() if line.startswith("http")]
        assert len(child_urls) == 2
        for child_url in child_urls:
            child = requests.get(child_url, timeout=5)
            child.raise_for_status()
            assert child.content == b"0123456789"

        records = {path: headers for path, _host, headers in seen if path in {
            "/playlist.m3u8", "/same.ts", "/other.ts"
        }}
        assert "auth_token=secret" in records["/playlist.m3u8"].get("Cookie", "")
        assert "auth_token=secret" in records["/same.ts"].get("Cookie", "")
        assert "Cookie" not in records["/other.ts"]
        assert records["/same.ts"].get("Authorization") == "Bearer private"
        assert "Authorization" not in records["/other.ts"]
        assert all("unsafe=secret" not in record.get("Cookie", "") for record in records.values())
        assert all(record.get("Host") != "evil.invalid" for record in records.values())
        assert all(record.get("Range") != "bytes=999-1000" for record in records.values())

        redirected = proxy.register(
            f"http://127.0.0.1:{port}/redirect.mp4",
            cookiejar=cookiejar,
            extra_headers={"Authorization": "Bearer private"},
        )
        redirected_response = requests.get(redirected, timeout=5)
        redirected_response.raise_for_status()
        assert redirected_response.content == b"0123456789"
        redirected_headers = [
            headers for path, _host, headers in seen if path == "/redirected.mp4"
        ]
        assert redirected_headers
        assert all("Authorization" not in headers for headers in redirected_headers)
        assert all("Cookie" not in headers for headers in redirected_headers)
    finally:
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=1)


if __name__ == "__main__":
    test_x_prefers_combined_mp4_and_falls_back_to_hls()
    test_x_preview_passes_selected_format_headers_and_hls_flag()
    test_proxy_scopes_cookies_and_sensitive_headers_across_hls_children()
