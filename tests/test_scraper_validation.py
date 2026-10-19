"""Offline regression coverage for audit B06/B13-B16/B20 classification."""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from http_fixtures import html_response

from src.scraper import (
    VideoItem, VideoScraper, _extract_next_data_ev_hls, _is_embed_iframe,
    build_items_from_urls,
)


REQUESTED = "https://start.example.test/watch/42"
FINAL = "https://final.example.test/dir/page"


def encrypted_player(video_url: str) -> dict:
    payload = json.dumps({"videoUrl": video_url}, separators=(",", ":"))
    encoded = base64.b64encode(bytes((b + 31) % 256 for b in payload.encode())).decode()
    return {"d": encoded, "k": 31}


def next_player(video_url: str) -> str:
    data = {"props": {"pageProps": {"ev": encrypted_player(video_url)}}}
    return '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'


class ScraperValidationTests(unittest.TestCase):
    def scrape(self, html: str, *, final: str = FINAL):
        calls = []

        def extract(url, _log):
            calls.append(url)
            return []

        with patch("src.scraper.ScopedCookieSession.get", return_value=html_response(text=html, url=final)), patch.object(
            VideoScraper, "_scrape_with_ytdlp", side_effect=extract
        ):
            items = VideoScraper().scrape(REQUESTED)
        return items, calls

    def test_redirect_resolves_relative_video_poster_and_iframe(self):
        items, calls = self.scrape(
            '<video src="clip.mp4" poster="cover.jpg"></video><iframe src="player/42"></iframe>'
        )
        self.assertEqual([item.url for item in items], [
            "https://final.example.test/dir/clip.mp4", "https://final.example.test/dir/player/42"
        ])
        self.assertEqual(calls, [REQUESTED, "https://final.example.test/dir/player/42"])
        self.assertTrue(all(item.source_url == REQUESTED and item.referer == FINAL for item in items))
        self.assertTrue(all(item.thumbnail == "https://final.example.test/dir/cover.jpg" for item in items))

    def test_html_base_changes_resources_but_not_referer(self):
        items, _ = self.scrape(
            '<base href="//cdn.example.test/assets/"><video src="film.mp4" poster="poster.jpg"></video>'
            '<iframe src="player/42"></iframe>'
        )
        self.assertEqual([item.url for item in items], [
            "https://cdn.example.test/assets/film.mp4", "https://cdn.example.test/assets/player/42"
        ])
        self.assertTrue(all(item.referer == FINAL for item in items))
        self.assertTrue(all(item.thumbnail == "https://cdn.example.test/assets/poster.jpg" for item in items))

    def test_relative_base_and_metadata_are_resolved(self):
        items, _ = self.scrape(
            '<base href="../media/"><meta property="og:video" content="film.mp4?x=1&amp;y=2">'
            '<meta property="og:image" content="poster.jpg">'
        )
        self.assertEqual(items[0].url, "https://final.example.test/media/film.mp4?x=1&y=2")
        self.assertEqual(items[0].thumbnail, "https://final.example.test/media/poster.jpg")

    def test_non_http_base_falls_back_to_document(self):
        items, _ = self.scrape('<base href="javascript:alert(1)"><video src="clip.mp4"></video>')
        self.assertEqual(items[0].url, "https://final.example.test/dir/clip.mp4")

    def test_blob_and_non_http_sources_do_not_create_fake_results(self):
        for value in (
            "blob:https://media.example.test/clip.mp4", "javascript:alert(1)",
            "file:///tmp/clip.mp4", "ftp://media.example.test/clip.mp4",
            "https://[broken/clip.mp4", "https://media.example.test:99999/clip.mp4",
            "https://user:secret@media.example.test/clip.mp4",
        ):
            with self.subTest(value=value):
                items, _ = self.scrape(f'<video src="{value}"></video>')
                self.assertEqual(items, [])

    def test_rendered_sources_reject_non_http_and_fragments(self):
        media = [
            "blob:https://media.example.test/id", "javascript:alert(1)",
            "https://media.example.test/seg.ts?key=fixture", "https://media.example.test/seg.m4s#part",
            "https://media.example.test/movie.mp4", "https://media.example.test/master.m3u8",
        ]
        items = build_items_from_urls(media, [], FINAL, "Fixture")
        self.assertEqual([item.url for item in items], media[-2:])
        self.assertFalse(items[0].is_hls)
        self.assertTrue(items[1].is_hls)

    def test_rendered_relative_urls_are_validated_and_resolved(self):
        items = build_items_from_urls(["clip.mp4", "//cdn.example.test/movie.webm"], [], FINAL, "Fixture")
        self.assertEqual([item.url for item in items], [
            "https://final.example.test/dir/clip.mp4", "https://cdn.example.test/movie.webm"
        ])

    def test_direct_dash_and_hls_are_distinct(self):
        items, _ = self.scrape(
            '<video><source src="stream.mpd?key=fixture"><source src="stream.m3u8#track"></video>'
        )
        self.assertTrue(items[0].is_dash)
        self.assertFalse(items[0].is_hls)
        self.assertTrue(items[1].is_hls)
        self.assertFalse(items[1].is_dash)
        self.assertTrue(all(item.is_direct for item in items))
        rendered = build_items_from_urls(["https://cdn.example.test/main.mpd"], [], FINAL, "Fixture")
        self.assertTrue(rendered[0].is_dash)
        self.assertFalse(rendered[0].is_hls)

    def test_query_string_does_not_change_protocol_classification(self):
        items = build_items_from_urls(["https://cdn.example.test/clip.mp4?next=other.m3u8"], [], FINAL, "Fixture")
        self.assertFalse(items[0].is_hls)
        self.assertFalse(items[0].is_dash)

    def test_preview_filename_is_retained_without_authoritative_player(self):
        items, _ = self.scrape('<video src="https://cdn.example.test/preview.mp4"></video>')
        self.assertEqual([item.url for item in items], ["https://cdn.example.test/preview.mp4"])

    def test_preview_filename_remains_valid_next_to_unrelated_main(self):
        items, _ = self.scrape('<video src="preview.mp4"></video><video src="film.mp4"></video>')
        self.assertEqual(len(items), 2)

    def test_redirect_context_reaches_relative_encrypted_player_without_html_media(self):
        payload = json.dumps({"videoUrl": "hls/fixture"}, separators=(",", ":"))
        encoded = base64.b64encode(bytes((b + 31) % 256 for b in payload.encode())).decode()
        html = (
            '<base href="../assets/"><script data-tsr-stream-part="1">'
            '$_TSR.router=($R=>$R[0]={ev:$R[1]={d:' + json.dumps(encoded) + ',k:31}})($R);</script>'
        )
        items, _ = self.scrape(html)
        self.assertEqual(items[0].url, "https://final.example.test/assets/hls/fixture")
        self.assertEqual(items[0].source_url, REQUESTED)
        self.assertEqual(items[0].referer, FINAL)

    def test_next_data_rejects_invalid_media_urls(self):
        for value in (
            "https://user:secret@media.example.test/main.m3u8",
            "https://media.example.test:99999/main.m3u8",
            "https://[broken/main.m3u8",
            "https://media.example.test/main.m3u8\nInjected: value",
            "https://media.example.test\\wrong/main.m3u8",
            "blob:https://media.example.test/main.m3u8",
            "ftp://media.example.test/main.m3u8",
        ):
            with self.subTest(value=value):
                html = next_player(value)
                self.assertIsNone(_extract_next_data_ev_hls(html))
                items, _ = self.scrape(html)
                self.assertEqual(items, [])

    def test_invalid_next_data_allows_tanstack_player_fallback(self):
        ev = encrypted_player("hls/fixture")
        html = (
            '<base href="../assets/">'
            + next_player("https://[broken/main.m3u8")
            + '<script data-tsr-stream-part="1">'
            '$_TSR.router=($R=>$R[0]={ev:$R[1]={d:' + json.dumps(ev["d"])
            + ',k:31}})($R);</script>'
        )
        items, _ = self.scrape(html)
        self.assertEqual([item.url for item in items], [
            "https://final.example.test/assets/hls/fixture"
        ])
        self.assertTrue(items[0].is_hls)
        self.assertTrue(items[0].hls_png_wrapped)
        self.assertEqual(items[0].source_url, REQUESTED)
        self.assertEqual(items[0].referer, FINAL)

    def test_embed_domains_use_hostname_boundary(self):
        for url in (
            "https://youtube.com.evil.example.test/ad", "https://evilyoutube.com/ad",
            "https://vimeo.com.evil.example.test/ad", "https://youtube.com@evil.example.test/ad",
            "javascript:https://youtube.com/embed/42", "https://[broken/embed/42",
        ):
            with self.subTest(url=url):
                self.assertFalse(_is_embed_iframe(url))
        for url in (
            "https://youtube.com/watch?v=42", "https://www.youtube.com/watch?v=42",
            "https://player.vimeo.com/video/42", "https://custom.example.test/player/42",
        ):
            with self.subTest(url=url):
                self.assertTrue(_is_embed_iframe(url))

    def test_fake_embed_is_not_followed(self):
        items, calls = self.scrape('<iframe src="https://youtube.com.evil.example.test/ad"></iframe>')
        self.assertEqual(items, [])
        self.assertEqual(calls, [REQUESTED])

    def test_browser_cookies_are_per_item_and_hidden_from_repr(self):
        first = VideoItem("One", "https://example.test/1", FINAL)
        second = VideoItem("Two", "https://example.test/2", FINAL)
        first.browser_cookies.append({"name": "session", "value": "secret", "domain": "example.test"})
        self.assertEqual(second.browser_cookies, [])
        self.assertNotIn("secret", repr(first))


if __name__ == "__main__":
    unittest.main(verbosity=2)
