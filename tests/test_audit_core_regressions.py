"""Reproductions for the 2026-10-18 settings, scraping and ownership audit."""
from __future__ import annotations
import base64
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from http_fixtures import html_response
from src import settings
from src.download_job import DownloadJob
from src.js_decoder import decode_obfuscated_urls, MAX_DECODER_CHARS
from src.scraper import VideoItem, VideoScraper, HTML_MAX_BYTES, _extract_next_data_ev_hls
from src.temp_ownership import MARKER, cleanup_stale_dirs, create_owned_tempdir
from src.utils import format_duration, safe_filename, redact_log_secrets
from scripts.windows_version import write_version_resource
from scripts import build_macos


class SettingsOrderingTests(unittest.TestCase):
    def test_concurrent_save_and_notifications_commit_in_the_same_order(self):
        entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
        original = settings.AppSettings.save
        with tempfile.TemporaryDirectory() as directory, patch.object(settings, "SETTINGS_PATH", Path(directory) / "settings.json"):
            def save(value, errors=None):
                if value.playback_buffer_seconds == 10:
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError("Test did not release the first save")
                else:
                    second_entered.set()
                return original(value, errors)
            store = settings.SettingsStore(settings.AppSettings())
            seen = []
            store.subscribe(lambda value: seen.append(value.playback_buffer_seconds), False)
            with patch.object(settings.AppSettings, "save", save):
                a = threading.Thread(target=lambda: store.update(settings.AppSettings(10)))
                b = threading.Thread(target=lambda: store.update(settings.AppSettings(20)))
                a.start()
                self.assertTrue(entered.wait(1))
                b.start()
                self.assertFalse(second_entered.wait(.05))
                release.set()
                a.join(3); b.join(3)
                self.assertFalse(a.is_alive() or b.is_alive())
            self.assertEqual(store.get().playback_buffer_seconds, 20)
            self.assertEqual(settings.AppSettings.load().playback_buffer_seconds, 20)
            self.assertEqual(seen, [10, 20])

    def test_reentrant_listener_update_and_idempotent_unsubscribe(self):
        store = settings.SettingsStore(settings.AppSettings())
        seen = []
        def callback(value):
            seen.append(value.playback_buffer_seconds)
            if value.playback_buffer_seconds == 10:
                store.update(settings.AppSettings(20), persist=False)
        unsubscribe = store.subscribe(callback, False)
        store.subscribe(callback, False)  # No duplicate delivery.
        store.update(settings.AppSettings(10), persist=False)
        self.assertEqual(seen, [10, 20])
        unsubscribe(); unsubscribe()
        store.update(settings.AppSettings(30), persist=False)
        self.assertEqual(seen, [10, 20])

    def test_state_events_cannot_arrive_in_reverse_commit_order(self):
        job = DownloadJob(VideoItem("Offline", "https://a.test/v.mp4", "https://a.test"), Path("."))
        entered, release = threading.Event(), threading.Event()
        emitted = []
        job.on_state = emitted.append
        original = job._emit_state
        def delayed(state):
            if state == "pausing":
                entered.set(); release.wait(3)
            original(state)
        with patch.object(job, "_emit_state", delayed):
            thread = threading.Thread(target=job.pause)
            thread.start()
            self.assertTrue(entered.wait(1))
            self.assertTrue(job._state_to("paused"))
            release.set(); thread.join(3)
        self.assertEqual(emitted, ["pausing", "paused"])
        self.assertEqual(job.state, "paused")


class ScrapingBoundaryTests(unittest.TestCase):
    def test_direct_media_url_is_not_fetched_as_a_page(self):
        url = "https://cdn.example.test/large.mp4?token=private"
        with patch("src.scraper.ScopedCookieSession.get") as get, patch.object(VideoScraper, "_scrape_with_ytdlp") as extract:
            items = VideoScraper().scrape(url)
        self.assertEqual(items[0].url, url)
        self.assertTrue(items[0].is_direct)
        get.assert_not_called(); extract.assert_not_called()

    def test_extensionless_video_headers_skip_body_read(self):
        response = html_response(text="not read", url="https://cdn.test/media?id=1")
        response.headers["Content-Type"] = "video/mp4"
        with patch.object(response, "iter_content", side_effect=AssertionError("Media body must not be read")), patch("src.scraper.ScopedCookieSession.get", return_value=response):
            scan = VideoScraper()._scrape_html(response.url, lambda _: None)
        self.assertTrue(scan.items[0].is_direct)
        self.assertEqual(scan.html, "")

    def test_oversized_html_and_cancelled_scrape_fail_bounded(self):
        response = html_response(text="x", url="https://a.test/watch")
        response.headers["Content-Length"] = str(HTML_MAX_BYTES + 1)
        with patch("src.scraper.ScopedCookieSession.get", return_value=response), patch.object(response, "iter_content", side_effect=AssertionError("Reject before reading")):
            with self.assertRaises(ValueError):
                VideoScraper()._scrape_html(response.url, lambda _: None)
        cancelled = threading.Event(); cancelled.set()
        with patch("src.scraper.ScopedCookieSession.get") as get, self.assertRaises(InterruptedError):
            VideoScraper().scrape(response.url, cancel_event=cancelled)
        get.assert_not_called()

    def test_malformed_next_structures_do_not_suppress_tanstack(self):
        from test_tanstack_ev import _router_script, _ev_literal
        route = _router_script(_ev_literal({"videoUrl": "/api/hls/valid"}))
        for data in ([], {"props": "bad"}, {"props": {"pageProps": 42}}):
            page = '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>' + route
            self.assertIsNone(_extract_next_data_ev_hls(page))
            response = html_response(text=page, url="https://a.test/watch")
            with patch("src.scraper.ScopedCookieSession.get", return_value=response), patch.object(VideoScraper, "_scrape_with_ytdlp", return_value=[]):
                items = VideoScraper().scrape(response.url)
            self.assertEqual(items[0].url, "https://a.test/api/hls/valid")

    def test_extractor_boundaries_normalize_infinite_dimensions(self):
        original = {"url": "https://a.test/v.mp4", "height": math.inf, "tbr": math.nan}
        item = VideoItem("Test", original["url"], "https://a.test", width=math.inf, duration=math.nan, formats=[original, "bad"])
        self.assertIsNone(item.width); self.assertIsNone(item.duration)
        self.assertIsNone(item.formats[0]["height"])
        self.assertIsNone(item.formats[0]["tbr"])
        self.assertEqual(len(item.formats), 1)
        self.assertEqual(original["height"], math.inf)

    def test_many_obfuscation_blocks_are_bounded_and_independent(self):
        page = "".join(f'<script>const _s{i}=["https://a.test/{i}.mp4"];const _i{i}=[0];</script>' for i in range(1600))
        start = time.monotonic()
        self.assertEqual(len(decode_obfuscated_urls(page)), 1600)
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(decode_obfuscated_urls("x" * (MAX_DECODER_CHARS + 1)), [])


class OwnershipAndBuildTests(unittest.TestCase):
    def test_cleanup_only_marked_old_inactive_owned_directories(self):
        with tempfile.TemporaryDirectory() as directory, patch("src.temp_ownership.schedule_stale_cleanup"):
            parent = Path(directory)
            old = create_owned_tempdir("video_scraper_cache_", parent)
            live = create_owned_tempdir("video_scraper_cache_", parent)
            young = create_owned_tempdir("video_scraper_cache_", parent)
            unmarked = parent / "video_scraper_cache_legacy"; unmarked.mkdir()
            data = json.loads((old / MARKER).read_text())
            data["created"] = time.time() - 90000
            data["owners"] = [{"pid": os.getpid(), "created": __import__('psutil').Process().create_time() - 3600}]
            (old / MARKER).write_text(json.dumps(data))
            data["owners"] = [{"pid": os.getpid(), "created": __import__('psutil').Process().create_time()}]
            (live / MARKER).write_text(json.dumps(data))
            self.assertEqual(cleanup_stale_dirs(parent, "video_scraper_cache_"), [old])
            self.assertTrue(live.exists() and young.exists() and unmarked.exists())

    def test_cleanup_retains_malformed_owner_identity_even_for_an_old_marker(self):
        with tempfile.TemporaryDirectory() as directory, patch("src.temp_ownership.schedule_stale_cleanup"):
            parent = Path(directory)
            for value in (math.nan, math.inf, -1, None, "bad"):
                path = create_owned_tempdir("video_scraper_cache_", parent)
                data = json.loads((path / MARKER).read_text())
                data["created"] = time.time() - 90000
                data["owners"][0]["created"] = value
                (path / MARKER).write_text(json.dumps(data))
            self.assertEqual(cleanup_stale_dirs(parent, "video_scraper_cache_"), [])
            self.assertEqual(len(list(parent.glob("video_scraper_cache_*"))), 5)

    def test_safe_names_and_shared_logs_are_portable(self):
        from pathlib import PureWindowsPath
        for name in ("CON", "NUL", "COM1", "LPT9", "con.txt", "name. "):
            output = safe_filename(name)
            self.assertFalse(PureWindowsPath(output + ".mp4").is_reserved())
            self.assertEqual(output, output.rstrip(" ."))
        self.assertLessEqual(len(safe_filename("汉" * 120).encode()) + 20, 255)
        self.assertEqual(format_duration(math.inf), "--:--")
        logged = redact_log_secrets("Failed https://user:pass@safe.test/v.mp4?token=secret#private")
        self.assertNotIn("secret", logged); self.assertNotIn("pass", logged)
        self.assertIn("safe.test/v.mp4?REDACTED", logged)
        signed_path = redact_log_secrets("https://cdn.test/hls/sig/private-signature/lsig/private-key/index.m3u8")
        self.assertNotIn("private-signature", signed_path)
        self.assertNotIn("private-key", signed_path)
        self.assertIn("/sig/REDACTED/lsig/REDACTED/", signed_path)

    def test_macos_report_survives_missing_git_and_windows_version_uses_source(self):
        with patch.object(build_macos.subprocess, "check_output", side_effect=FileNotFoundError("git")):
            self.assertEqual(build_macos.git_state(), {"git_commit": None, "working_tree_dirty": None})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "src").mkdir()
            (root / "src/__init__.py").write_text('__version__="1.2.34"')
            resource = write_version_resource(root, root / "version.txt").read_text()
            self.assertIn("filevers=(1, 2, 34, 0)", resource)
            self.assertIn("'ProductVersion', '1.2.34'", resource)


if __name__ == "__main__":
    unittest.main(verbosity=2)
