"""Offline regression checks for serialized TanStack player configuration.

Run directly with ``.venv/bin/python tests/test_tanstack_ev.py``. Every URL,
title, and encoded payload is synthetic; requests and yt-dlp are mocked.
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper import (  # noqa: E402
    VideoItem,
    VideoScraper,
    _extract_next_data_ev_hls,
    _extract_tanstack_ev_hls,
)


PAGE = "https://videos.example.test/v/fixture-42"
MAIN = "https://videos.example.test/api/hls/fixture-42"
AD = "https://ads.example.test/promotional-clip.mp4"
TITLE = "Example landscape recording"


def _encrypted(payload: object, key: int = 31) -> str:
    """Generate the inverse byte shift, with no real site's encoded data."""
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    shifted = bytes((byte + key) % 256 for byte in text.encode("ascii"))
    return base64.b64encode(shifted).decode("ascii")


def _ev_literal(payload: object, key: int = 31) -> str:
    return "{d:" + json.dumps(_encrypted(payload, key)) + ",k:" + str(key) + "}"


def _router_script(ev_literal: str, *, extra_js: str = "") -> str:
    # Match the serializer's assignments and loader-data position, rather than
    # putting a JSON object directly in a script with a made-up global name.
    return (
        '<script data-tsr-stream-part="1">'
        + "$_TSR.router=($R=>$R[0]={"
        + 'matches:$R[8]=[{i:"root"},{i:"/v/$id",'
        + "l:$R[11]={video:$R[12]={id:\"fixture-42\","
        + "name:\"Example landscape recording\"},"
        + "ev:$R[92]="
        + ev_literal
        + ",ads:$R[93]=[{videoUrl:\"https://ads.example.test/banner\"}]}}]"
        + "})($R);"
        + extra_js
        + "</script>"
    )


def _page_html(script: str, body: str = "") -> str:
    return (
        f"<html><head><title>{TITLE}</title>"
        '<meta property="og:image" content="https://videos.example.test/poster.jpg"></head>'
        + "<body>"
        + body
        + script
        + "</body></html>"
    )


def _scrape(html: str, ytdlp_items: list[VideoItem] | None = None) -> tuple[list[VideoItem], list[str]]:
    response = Mock(text=html, url=PAGE)
    logs: list[str] = []
    with patch("src.scraper.requests.get", return_value=response) as get, patch.object(
        VideoScraper, "_scrape_with_ytdlp", return_value=ytdlp_items or []
    ) as ytdlp:
        items = VideoScraper().scrape(PAGE, progress=logs.append)
    get.assert_called_once()
    assert get.call_args.args[0] == PAGE
    ytdlp.assert_called_once()
    assert ytdlp.call_args.args[0] == PAGE
    return items, logs


class TanStackEncryptedPlayerTests(unittest.TestCase):
    def test_serialized_router_loader_recovers_extensionless_relative_hls(self) -> None:
        html = _page_html(_router_script(_ev_literal({"videoUrl": "/api/hls/fixture-42"})))
        self.assertNotIn("__NEXT_DATA__", html)
        self.assertEqual(_extract_tanstack_ev_hls(html, PAGE), MAIN)

    def test_key_order_and_whitespace_do_not_change_the_payload(self) -> None:
        data = json.dumps(_encrypted({"videoUrl": "/api/hls/fixture-42"}))
        literals = (
            "{d:" + data + ",k:31}",
            "{k:31,d:" + data + "}",
            "{\n  d : " + data + " ,\n k : 31\n}",
            "{ k : 31 ,\n d : " + data + " }",
        )
        for literal in literals:
            with self.subTest(literal=literal):
                self.assertEqual(_extract_tanstack_ev_hls(_router_script(literal), PAGE), MAIN)

    def test_http_urls_are_resolved_without_requiring_a_media_extension(self) -> None:
        cases = {
            "/api/hls/fixture-42?token=fixture&expires=9": MAIN + "?token=fixture&expires=9",
            "../../api/hls/fixture-42": MAIN,
            "https://media.example.test/manifest/42": "https://media.example.test/manifest/42",
            "http://media.example.test/manifest/42": "http://media.example.test/manifest/42",
            "//media.example.test/manifest/42": "https://media.example.test/manifest/42",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                html = _router_script(_ev_literal({"videoUrl": value}))
                self.assertEqual(_extract_tanstack_ev_hls(html, PAGE), expected)

    def test_byte_shift_wraparound_and_unpadded_base64_are_supported(self) -> None:
        for key in (0, 31, 249, 255):
            with self.subTest(key=key):
                data = _encrypted({"videoUrl": "/api/hls/fixture-42"}, key).rstrip("=")
                ev = "{d:" + json.dumps(data) + ",k:" + str(key) + "}"
                self.assertEqual(_extract_tanstack_ev_hls(_router_script(ev), PAGE), MAIN)

    def test_other_javascript_is_not_evaluated(self) -> None:
        # A browser would run these statements. Static parsing must neither
        # execute them nor invoke Python eval/exec.
        html = _router_script(
            _ev_literal({"videoUrl": "/api/hls/fixture-42"}),
            extra_js='throw new Error("fixture must not execute");fetch("/unexpected");',
        )
        with patch("builtins.eval", side_effect=AssertionError("JavaScript must not be evaluated")), patch(
            "builtins.exec", side_effect=AssertionError("JavaScript must not be executed")
        ):
            self.assertEqual(_extract_tanstack_ev_hls(html, PAGE), MAIN)

    def test_non_router_scripts_and_unmarked_scripts_do_not_supply_ev(self) -> None:
        ev = _ev_literal({"videoUrl": "/api/hls/fixture-42"})
        cases = (
            "<html>no script</html>",
            "<script>window.player={ev:" + ev + "};</script>",
            '<script data-tsr-stream-part="1">window.analytics={ev:$R[92]=' + ev + "};</script>",
            _router_script(ev).replace(' data-tsr-stream-part="1"', ""),
            _router_script(ev).replace("$_TSR.router=", "$_TSR.router=="),
            '<script data-tsr-stream-part>$_TSR.router={};const re=/ev:{d:'
            + json.dumps(_encrypted({"videoUrl": "/api/hls/fixture-42"}))
            + ',k:31}/;</script>',
        )
        for html in cases:
            with self.subTest(html=html):
                self.assertIsNone(_extract_tanstack_ev_hls(html, PAGE))

    def test_malformed_encrypted_objects_are_rejected(self) -> None:
        data = json.dumps(_encrypted({"videoUrl": "/api/hls/fixture-42"}))
        literals = (
            '{}',
            '{d:"not base64 !!!",k:31}',
            '{d:"",k:31}',
            "{d:" + data + ',k:"31"}',
            "{d:" + data + ",k:null}",
            "{d:" + data + ",k:true}",
            "{d:" + data + ",k:31.5}",
            "{d:" + data + "}",
            "{k:31}",
            "{d:" + data + ",k:31",
            _ev_literal(["not", "a", "configuration"]),
            _ev_literal({"thumbVTTUrl": "/thumbs.vtt"}),
            _ev_literal({"videoUrl": 42}),
            _ev_literal({"videoUrl": ""}),
        )
        for literal in literals:
            with self.subTest(literal=literal):
                self.assertIsNone(_extract_tanstack_ev_hls(_router_script(literal), PAGE))

    def test_unsafe_schemes_and_embedded_credentials_are_rejected(self) -> None:
        urls = (
            "javascript:alert(1)",
            "data:application/vnd.apple.mpegurl;base64,fixture",
            "file:///tmp/fixture.m3u8",
            "ftp://media.example.test/fixture.m3u8",
            "https://user:password@media.example.test/manifest",
            "http://user@media.example.test/manifest",
            "//user:password@media.example.test/manifest",
            "https:///missing-host/manifest",
            "https://[malformed-host/manifest",
        )
        for value in urls:
            with self.subTest(value=value):
                html = _router_script(_ev_literal({"videoUrl": value}))
                self.assertIsNone(_extract_tanstack_ev_hls(html, PAGE))

    def test_malformed_script_does_not_hide_a_later_valid_route(self) -> None:
        html = _router_script('{d:"bad",k:31}') + _router_script(
            _ev_literal({"videoUrl": "/api/hls/fixture-42"})
        )
        self.assertEqual(_extract_tanstack_ev_hls(html, PAGE), MAIN)

    def test_scrape_prefers_real_stream_even_when_ytdlp_and_html_find_an_ad(self) -> None:
        html = _page_html(
            _router_script(_ev_literal({"videoUrl": "/api/hls/fixture-42"})),
            f'<video src="{AD}"></video>',
        )
        ytdlp_ad = VideoItem(
            title="Promotional clip", url=AD, source_url=PAGE,
            ext="mp4", is_direct=True, referer=PAGE,
        )
        items, _logs = _scrape(html, [ytdlp_ad])
        self.assertEqual([item.url for item in items], [MAIN, AD])
        main = items[0]
        self.assertEqual(main.title, TITLE)
        self.assertEqual(main.source_url, PAGE)
        self.assertEqual(main.referer, PAGE)
        self.assertEqual(main.ext, "m3u8")
        self.assertTrue(main.is_hls)
        self.assertTrue(main.is_direct)
        self.assertTrue(main.hls_png_wrapped)
        self.assertEqual(main.thumbnail, "https://videos.example.test/poster.jpg")
        self.assertFalse(items[1].is_hls)

    def test_duplicate_html_main_url_is_upgraded_and_moved_before_the_ad(self) -> None:
        html = _page_html(
            _router_script(_ev_literal({"videoUrl": "/api/hls/fixture-42"})),
            f'<video src="{AD}"></video>'
            '<video><source src="/api/hls/fixture-42"></video>',
        )
        items, _logs = _scrape(html)
        self.assertEqual([item.url for item in items], [MAIN, AD])
        self.assertEqual(sum(item.url == MAIN for item in items), 1)
        self.assertTrue(items[0].is_direct)
        self.assertTrue(items[0].is_hls)
        self.assertEqual(items[0].ext, "m3u8")
        self.assertEqual(items[0].referer, PAGE)
        self.assertEqual(items[0].title, TITLE)

    def test_non_router_ev_does_not_create_a_main_video_during_scrape(self) -> None:
        script = '<script data-tsr-stream-part="1">window.analytics={ev:$R[92]=' + _ev_literal(
            {"videoUrl": "/api/hls/fixture-42"}
        ) + "};</script>"
        items, _logs = _scrape(_page_html(script, f'<video src="{AD}"></video>'))
        self.assertEqual([item.url for item in items], [AD])
        self.assertEqual(items[0].ext, "mp4")

    def test_legacy_next_data_still_decodes_and_has_priority_over_ads(self) -> None:
        legacy_url = "https://media.example.test/hls/fixture/index.jpg?auth=fixture"
        data = {
            "props": {"pageProps": {
                "ev": {"d": _encrypted({"videoUrl": legacy_url}, 24), "k": 24},
                "video": {"name": "Legacy landscape recording"},
            }}
        }
        script = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + "</script>"
        html = _page_html(script, f'<video src="{AD}"></video>')
        self.assertIsNone(_extract_tanstack_ev_hls(html, PAGE))
        self.assertEqual(_extract_next_data_ev_hls(html), (legacy_url, "Legacy landscape recording"))
        items, _logs = _scrape(html)
        self.assertEqual([item.url for item in items], [legacy_url, AD])
        self.assertEqual(items[0].title, "Legacy landscape recording")
        self.assertEqual(items[0].ext, "m3u8")
        self.assertTrue(items[0].is_hls)
        self.assertEqual(items[0].referer, PAGE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
