"""Offline regression checks for preview formats and media proxy routing.

Run directly: ``build/.venv/bin/python tests/test_preview_formats.py``.
URLs are inert fixtures; these checks never open a network connection.
"""

from __future__ import annotations

import copy
import os
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication

from src.gui.preview_panel import PreviewPanel
from src.scraper import VideoItem


APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


def _item(formats: list[dict], **kwargs) -> VideoItem:
    options = {
        "title": "Preview format fixture",
        "url": "https://example.invalid/watch",
        "source_url": "https://example.invalid/watch",
        "formats": formats,
    }
    options.update(kwargs)
    return VideoItem(**options)


def _format(url: str, **kwargs) -> dict:
    fmt = {
        "url": url,
        "ext": "mp4",
        "protocol": "https",
        "vcodec": "avc1.4D401F",
        "acodec": "mp4a.40.2",
        "height": 720,
    }
    fmt.update(kwargs)
    return fmt


def _youtube_hls_formats() -> list[dict]:
    # Sanitized metadata shape from SeZdYd-TXlI: HLS 233/234 are audio
    # renditions, while HLS 232 is a silent variant of their shared master.
    master = "https://manifest.googlevideo.com/api/manifest/hls/master.m3u8"
    return [
        _format("https://media.example.invalid/av1-398.mp4", format_id="398",
                vcodec="av01.0.05M.08", acodec="none"),
        _format("https://media.example.invalid/avc-136.mp4", format_id="136",
                acodec="none"),
        _format("https://media.example.invalid/audio-140.m4a", format_id="140",
                ext="m4a", vcodec="none", height=None),
        _format("https://manifest.googlevideo.com/video-232.m3u8", format_id="232",
                protocol="m3u8_native", acodec="none", manifest_url=master),
        {
            "url": "https://manifest.googlevideo.com/audio-234.m3u8",
            "format_id": "234",
            "ext": "mp4",
            "protocol": "m3u8_native",
            "vcodec": "none",
            "manifest_url": master,
            # Actual extraction does not always report acodec here.
        },
    ]


class _Proxy:
    def __init__(self):
        self.registrations = []
        self.unregistered = []

    def register(self, *args, **kwargs):
        self.registrations.append((args, kwargs))
        return "http://127.0.0.1:60000/play/offline-fixture.m3u8"

    def unregister(self, url):
        self.unregistered.append(url)


@contextmanager
def _panel(proxy=None):
    panel = PreviewPanel(proxy=proxy)
    try:
        yield panel
    finally:
        panel.shutdown()
        panel.close()
        panel.deleteLater()
        APP.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()


def test_youtube_separate_hls_tracks_select_the_shared_master_without_mutation():
    formats = _youtube_hls_formats()
    original = copy.deepcopy(formats)
    video = _item(formats, source_url="https://www.youtube.com/watch?v=SeZdYd-TXlI",
                  raw={"extractor_key": "Youtube"})
    selected = PreviewPanel._best_preview_format(video)
    assert selected is not None
    assert selected["url"] == formats[3]["manifest_url"]
    assert PreviewPanel._is_hls_format(selected)
    assert formats == original, "Preview selection must not alter download formats"
    assert all(selected is not fmt for fmt in formats)
    with _panel() as panel:
        assert panel._best_playable_url(video) == selected["url"]


def test_combined_hls_beats_silent_and_audio_only_direct_formats():
    silent = _format("https://example.invalid/silent.mp4", acodec="none")
    # Some extractors label an audio-only direct format as MP4.
    audio = _format("https://example.invalid/audio.mp4", vcodec="none")
    combined_hls = _format("https://example.invalid/complete.m3u8",
                           protocol="m3u8_native", height=1080)
    selected = PreviewPanel._best_preview_format(_item([silent, audio, combined_hls]))
    assert selected == combined_hls


def test_complete_avc_preview_beats_complete_av1_preview():
    av1 = _format("https://example.invalid/av1.mp4", vcodec="av01.0.05M.08")
    avc = _format("https://example.invalid/avc.mp4", height=360)
    selected = PreviewPanel._best_preview_format(_item([av1, avc]))
    assert selected == avc


def test_normal_combined_mp4_remains_preferred_to_equivalent_hls():
    combined = _format("https://example.invalid/combined.mp4")
    hls = _format("https://example.invalid/combined.m3u8", protocol="m3u8_native")
    silent = _format("https://example.invalid/high.mp4", height=2160, acodec="none")
    selected = PreviewPanel._best_preview_format(_item([hls, silent, combined]))
    assert selected == combined


def test_audio_storyboards_dash_drm_and_unsupported_sources_are_rejected():
    formats = [
        _format("https://example.invalid/audio.mp4", vcodec="none"),
        _format("https://example.invalid/storyboard.mhtml", ext="mhtml",
                protocol="mhtml", vcodec="none", acodec="none"),
        _format("https://example.invalid/dash.mp4", protocol="http_dash_segments"),
        _format("https://example.invalid/master.mpd", ext="mpd"),
        _format("https://example.invalid/protected.mp4", has_drm=True),
        _format("ftp://example.invalid/video.mp4"),
        _format("https://example.invalid/watch.html", ext="html"),
        _format("", ext="mp4"),
    ]
    assert PreviewPanel._best_preview_format(_item(formats)) is None


def test_youtube_master_is_not_promoted_without_a_matching_audio_rendition():
    original = _youtube_hls_formats()
    variant = original[3]
    wrong_audio = copy.deepcopy(original[4])
    wrong_audio["manifest_url"] = "https://manifest.googlevideo.com/other-master.m3u8"
    direct_audio = _format("https://example.invalid/audio.m4a", ext="m4a", vcodec="none")
    for formats in ([variant], [variant, wrong_audio], [variant, direct_audio]):
        selected = PreviewPanel._best_preview_format(_item(
            formats, source_url="https://youtube.com/watch?v=fixture"
        ))
        assert selected is not None
        assert selected["url"] == variant["url"]


def test_youtube_identity_uses_extractor_or_real_source_and_referer_hosts():
    formats = _youtube_hls_formats()[3:]
    master = formats[0]["manifest_url"]
    for options in (
        {"raw": {"extractor_key": "Youtube"}},
        {"source_url": "https://www.youtube.com/watch?v=fixture"},
        {"source_url": "https://youtu.be/fixture"},
        {"referer": "https://www.youtube-nocookie.com/embed/fixture"},
    ):
        selected = PreviewPanel._best_preview_format(_item(formats, **options))
        assert selected is not None and selected["url"] == master
    for source in (
        "https://example.invalid/watch",
        "https://youtube.com.example.invalid/watch",
        "https://notyoutube.com/watch",
    ):
        selected = PreviewPanel._best_preview_format(_item(formats, source_url=source))
        assert selected is not None and selected["url"] == formats[0]["url"]


def test_generic_preview_forwards_selected_headers_and_extensionless_hls():
    selected = _format(
        "https://example.invalid/playlist/token", protocol="m3u8_native",
        http_headers={"User-Agent": "format-agent", "Referer": "https://example.invalid/"},
    )
    video = _item([selected], raw={"http_headers": {
        "User-Agent": "raw-agent", "Accept-Language": "en"
    }})
    proxy = _Proxy()
    with _panel(proxy) as panel:
        panel.show_video(video)
        assert panel._pending_playable_url == selected["url"]
        assert panel._pending_is_hls
        assert panel._wrap_with_proxy(panel._pending_playable_url, video)
        args, kwargs = proxy.registrations[-1]
        assert args == (selected["url"],)
        assert kwargs["is_hls"] is True
        assert kwargs["extra_headers"] == {
            "User-Agent": "format-agent",
            "Accept-Language": "en",
            "Referer": "https://example.invalid/",
        }
        assert panel.player.source().isEmpty(), "Choosing a preview must remain lazy"


def test_direct_link_preserves_its_url_and_hls_proxy_routing():
    ignored_format = _format("https://example.invalid/unrelated.mp4")
    video = _item(
        [ignored_format], url="https://example.invalid/direct-token", is_direct=True,
        is_hls=True, referer="https://example.invalid/page",
    )
    proxy = _Proxy()
    with _panel(proxy) as panel:
        panel.show_video(video)
        assert panel._best_playable_url(video) == video.url
        assert panel._pending_playable_url == video.url
        assert panel._wrap_with_proxy(video.url, video)
        args, kwargs = proxy.registrations[-1]
        assert args == (video.url,)
        assert kwargs["referer"] == video.referer
        assert kwargs["is_hls"] is True


def test_missing_preview_format_keeps_play_disabled_and_fullscreen_available():
    video = _item([])
    assert PreviewPanel._best_preview_format(video) is None
    with _panel() as panel:
        panel.show_video(video)
        assert panel._best_playable_url(video) is None
        assert panel._pending_playable_url is None
        assert not panel.play_button.isEnabled()
        assert panel.fullscreen_button.isEnabled()


if __name__ == "__main__":
    checks = [value for name, value in globals().copy().items()
              if name.startswith("test_") and callable(value)]
    for check in checks:
        check()
        print(f"PASS {check.__name__}")
    print(f"Passed {len(checks)} preview format checks")
