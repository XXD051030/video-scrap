"""Offline regression checks for the public HTML scraping path.

Run directly: ``python3 tests/test_scraper_flow.py`` (no pytest needed).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.js_decoder import decode_obfuscated_urls
from src.scraper import VideoItem, VideoScraper


def test_js_decoder_permutation_decode_and_cleanup() -> None:
    parts = [
        "https%3A%2F%2Fmedia.",
        "example%2Fvideo_84c41c",
        ".mp4%3Fid%3D1",
    ]
    order = [2, 0, 1]
    strings = [parts[i] for i in order]
    script = (
        f"const _parts = {json.dumps(strings)}; "
        f"const _order = {json.dumps(order)}; "
        "url = decodeURIComponent(url).replace('_84c41c', '');"
    )

    assert "https://media.example/video.mp4?id=1" in set(
        decode_obfuscated_urls(script)
    )


def test_js_decoder_long_permutation() -> None:
    # A long permutation protects the indexing optimization without depending
    # on output order, which is deliberately unspecified by the decoder.
    expected = "https://media.example/video.mp4?token=" + "x" * 360
    parts = list(expected)
    order = list(reversed(range(len(parts))))
    script = (
        f"const _parts = {json.dumps([parts[i] for i in order])}; "
        f"const _order = {json.dumps(order)};"
    )

    assert expected in set(decode_obfuscated_urls(script))


def test_scrape_combines_html_embed_and_hidden_media() -> None:
    page = "https://site.example/watch/42"
    embed = "https://player.example/embed/42"
    hidden = "https://media.example/hidden.mp4?auth=ok"
    encoded = quote(hidden, safe="")
    parts = [encoded[:20], encoded[20:40], encoded[40:]]
    order = [1, 2, 0]
    script = (
        f"const _parts = {json.dumps([parts[i] for i in order])}; "
        f"const _order = {json.dumps(order)};"
    )
    html = (
        "<html><head><title>Example Clip</title></head><body>"
        '<video poster="/cover.jpg"><source src="/files/main.mp4"></video>'
        f'<iframe src="{embed}"></iframe>'
        '<iframe src="https://news.example/story"></iframe>'
        f"<script>{script}</script>"
        "</body></html>"
    )
    calls: list[str] = []

    def fake_ytdlp(url: str, _log: object) -> list[VideoItem]:
        calls.append(url)
        if url == embed:
            return [
                VideoItem(
                    title="Embedded Clip",
                    url="https://cdn.example/embedded.mp4",
                    source_url=embed,
                    is_direct=False,
                    referer=embed,
                )
            ]
        return []

    with patch("src.scraper.requests.get", return_value=Mock(text=html, url=page)), patch.object(
        VideoScraper, "_scrape_with_ytdlp", side_effect=fake_ytdlp
    ):
        items = VideoScraper().scrape(page)

    by_url = {item.url: item for item in items}
    assert set(by_url) == {
        "https://site.example/files/main.mp4",
        "https://cdn.example/embedded.mp4",
        hidden,
    }
    assert calls == [page, embed]
    assert by_url[hidden].is_direct
    assert by_url["https://cdn.example/embedded.mp4"].source_url == page
    assert all(item.thumbnail == "https://site.example/cover.jpg" for item in items)


def test_redirected_response_with_absolute_media_preserves_page_metadata() -> None:
    requested = "https://start.example/watch/42"
    final = "https://final.example/videos/42"
    media = "https://media.example/clip.mp4"
    html = f'<html><video src="{media}"></video></html>'
    response = Mock(text=html, url=final)

    with patch("src.scraper.requests.get", return_value=response), patch.object(
        VideoScraper, "_scrape_with_ytdlp", return_value=[]
    ):
        items = VideoScraper().scrape(requested)

    assert len(items) == 1
    assert items[0].url == media
    assert items[0].source_url == requested
    assert items[0].referer == requested
    # Relative media URLs after redirects have a known resolution issue; this
    # test deliberately does not bless that behavior as correct.


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'OK' if failures == 0 else 'FAILURES: ' + str(failures)}")
    sys.exit(1 if failures else 0)
