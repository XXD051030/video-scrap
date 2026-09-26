"""Offline checks for X/Twitter post extraction."""

from __future__ import annotations

import sys
from http.cookiejar import CookieJar
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from requests.cookies import create_cookie

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper import VideoScraper, is_x_post_url


POST = "https://x.com/example/status/1234567890123456789?s=20"


def _video(media_id: str, title: str = "A post", *, url: str | None = None) -> dict:
    return {
        "id": media_id,
        "title": title,
        "thumbnail": f"https://pbs.twimg.com/media/{media_id}.jpg",
        "formats": [
            {
                "url": url or f"https://video.twimg.com/ext_tw_video/{media_id}/clip.mp4",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "http_headers": {"Referer": "https://x.com/"},
            }
        ],
    }


class _FakeYDL:
    def __init__(self, info: dict | None, cookiejar: CookieJar | None = None) -> None:
        self.info = info
        self.cookiejar = cookiejar or CookieJar()
        self.extracted_url: str | None = None

    def __enter__(self) -> "_FakeYDL":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = False) -> dict | None:
        assert download is False
        self.extracted_url = url
        return self.info


def test_x_post_url_uses_exact_hosts_and_post_paths() -> None:
    assert is_x_post_url(POST)
    assert is_x_post_url("https://twitter.com/example/status/123/video/2")
    assert is_x_post_url("https://mobile.x.com/i/web/status/123")
    assert not is_x_post_url("https://notx.com/example/status/123")
    assert not is_x_post_url("https://x.com.evil.test/example/status/123")
    assert not is_x_post_url("https://x.com/example")
    assert not is_x_post_url("https://x.com/example/status/not-a-number")


def test_x_scrape_uses_only_ytdlp_and_keeps_real_playlist_indices() -> None:
    first = _video("video-one")
    duplicate = _video("video-one", url="https://video.twimg.com/other-quality.mp4")
    second = _video("video-two")
    second["playlist_index"] = 5
    audio = {
        "id": "audio-only",
        "formats": [{"url": "https://video.twimg.com/audio.m4a", "ext": "m4a", "vcodec": "none"}],
    }
    info = {
        "_type": "playlist",
        "id": "1234567890123456789",
        "title": "A post",
        "entries": [first, duplicate, second, audio, {"id": "photo"}],
    }
    ydl = _FakeYDL(info)

    with patch("src.scraper.yt_dlp.YoutubeDL", return_value=ydl) as youtube_dl, patch(
        "src.scraper.requests.get"
    ) as get:
        items = VideoScraper().scrape(POST)

    get.assert_not_called()
    assert len(items) == 2
    assert [item.x_playlist_index for item in items] == [1, 5]
    assert [item.url for item in items] == [POST, POST]
    assert len({item.title for item in items}) == 2
    assert items[0].raw is first
    assert items[0].formats is first["formats"]
    assert items[0].formats[0]["http_headers"] == {"Referer": "https://x.com/"}
    assert "cookiesfrombrowser" not in youtube_dl.call_args.args[0]


def test_x_video_share_suffix_is_removed_for_full_playlist_extraction() -> None:
    share_url = "https://x.com/example/status/1234567890123456789/video/2?lang=en"
    ydl = _FakeYDL(_video("video-one"))

    with patch("src.scraper.yt_dlp.YoutubeDL", return_value=ydl):
        items = VideoScraper().scrape(share_url)

    assert ydl.extracted_url == "https://x.com/example/status/1234567890123456789?lang=en"
    assert items[0].url == share_url
    assert items[0].x_playlist_index == 1


def test_x_browser_cookies_are_filtered_to_x_domains_in_memory() -> None:
    browser_jar = CookieJar()
    for domain in (".x.com", "api.twitter.com", "video.twimg.com", "example.com", "notx.com"):
        browser_jar.set_cookie(create_cookie(name="session", value=domain, domain=domain))
    ydl = _FakeYDL(_video("video-one"), cookiejar=browser_jar)

    with patch("src.scraper.yt_dlp.YoutubeDL", return_value=ydl) as youtube_dl:
        item = VideoScraper().scrape(POST, x_auth_browser="chrome")[0]

    assert youtube_dl.call_args.args[0]["cookiesfrombrowser"] == ("chrome",)
    assert item.x_auth_browser == "chrome"
    assert item.x_cookiejar is not browser_jar
    assert {cookie.domain for cookie in item.x_cookiejar} == {
        ".x.com", "api.twitter.com", "video.twimg.com"
    }
    assert "example.com" not in repr(item)


def test_x_scrape_reports_extraction_failure_without_html_fallback() -> None:
    class _FailingYDL(_FakeYDL):
        def extract_info(self, url: str, download: bool = False) -> dict:
            raise ValueError("login required")

    with patch("src.scraper.yt_dlp.YoutubeDL", return_value=_FailingYDL(None)), patch(
        "src.scraper.requests.get"
    ) as get:
        with TestCase().assertRaisesRegex(RuntimeError, "login required"):
            VideoScraper().scrape(POST)

    get.assert_not_called()


def test_x_scrape_rejects_posts_without_playable_video() -> None:
    ydl = _FakeYDL({"id": "1234567890123456789", "title": "Photo only"})

    with patch("src.scraper.yt_dlp.YoutubeDL", return_value=ydl):
        with TestCase().assertRaisesRegex(RuntimeError, "No playable video"):
            VideoScraper().scrape(POST)


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
    sys.exit(1 if failures else 0)
