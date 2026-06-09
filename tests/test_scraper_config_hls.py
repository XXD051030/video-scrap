"""Tests for inline player-config HLS extraction (tube-CMS sites like 91AV).

Run directly: ``python3 tests/test_scraper_config_hls.py`` (no pytest needed).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper import VideoScraper, _extract_config_hls, _is_preview_teaser


HASH = "aaaa1111bbbb2222cccc3333dddd4444"
PAGE = "https://91av1636.cc/video/14640"

# Mirrors the real page shape: an empty <video> player, preview <source>
# teasers, and a JSON config holding hash_id + a relative m3u8_path + the
# CDN "lines" host list. The playable manifest lives at /videos/<hash>/.
SAMPLE_HTML = (
    '<html><head><title>Some Clip - 91AV</title></head><body>'
    '<video id="video-player" preload="none" controls></video>'
    f'<source src="https://b2.bttss.cc/videos/{HASH}/preview.mp4">'
    '<script>window.__cfg = {"lines":[["line1","\\u7ebf\\u8def1","b2.bttss.cc"],'
    '["line2","\\u7ebf\\u8def2","ttcdn.cc"]],"cdn_host":null,'
    f'"online_video":{{"nid":"s9999","m3u8_path":"/home/video/{HASH}/play.m3u8",'
    f'"hash_id":"{HASH}","duration":100.0}}}};</script>'
    '</body></html>'
)


def test_extract_config_hls_builds_cdn_urls() -> None:
    urls = _extract_config_hls(SAMPLE_HTML, PAGE)
    assert f"https://b2.bttss.cc/videos/{HASH}/play.m3u8" in urls, urls
    assert f"https://ttcdn.cc/videos/{HASH}/play.m3u8" in urls, urls
    # No /home/video/ leakage from the raw config path.
    assert not any("/home/video/" in u for u in urls), urls


def test_extract_config_hls_no_match_returns_empty() -> None:
    assert _extract_config_hls("<html>nothing here</html>", PAGE) == []


def test_is_preview_teaser() -> None:
    assert _is_preview_teaser(f"https://b2.bttss.cc/videos/{HASH}/preview.mp4")
    assert _is_preview_teaser("https://x.cc/a/preview.webm")
    assert not _is_preview_teaser("https://x.cc/a/play.m3u8")
    assert not _is_preview_teaser("https://x.cc/a/real-video.mp4")


def test_scrape_html_drops_preview_and_surfaces_hls() -> None:
    # End-to-end of the pure-parsing parts (no network): feed the HTML through
    # the same helpers scrape() uses and assert the HLS wins, previews dropped.
    hls = _extract_config_hls(SAMPLE_HTML, PAGE)
    assert hls, "expected an HLS url from the config"
    item = VideoScraper()._item_from_hls(hls[0], PAGE, "Some Clip - 91AV", None)
    assert item.is_hls and item.is_direct
    assert item.ext == "m3u8"
    assert item.referer == PAGE


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
