"""Current-page capture, keyboard seeking and cross-strategy media deduplication."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt, QUrl
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWebEngineCore import QWebEngineProfile
from src.gui.interactive_scraper import InteractiveScrapeDialog
from src.gui.preview_panel import PreviewPanel
from src.scraper import VideoItem, VideoScraper
from http_fixtures import html_response

APP = QApplication.instance() or QApplication(["current-page-regression"])
APP.setQuitOnLastWindowClosed(False)


def wait_for(predicate, seconds=8):
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        QTest.qWait(20)
    if not predicate():
        raise AssertionError("Qt condition timed out")


class KeyboardSeekTests(unittest.TestCase):
    def test_keyboard_seeks_real_player_and_programmatic_updates_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "minute.wav"
            with wave.open(str(path), "wb") as sound:
                sound.setnchannels(1)
                sound.setsampwidth(2)
                sound.setframerate(8000)
                sound.writeframes(b"\0\0" * 8000 * 60)
            panel = PreviewPanel()
            panel.resize(900, 500)
            panel.show()
            try:
                panel.show_video(VideoItem("Local", "https://fixture.invalid/minute.wav",
                                           "https://fixture.invalid/", is_direct=True))
                panel.player.setSource(QUrl.fromLocalFile(str(path)))
                wait_for(lambda: panel.player.duration() == 60000)
                panel.player.setPosition(20000)
                panel.position_slider.setFocus()
                QTest.keyClick(panel.position_slider, Qt.Key.Key_End)
                wait_for(lambda: panel.player.position() == 60000)
                QTest.keyClick(panel.position_slider, Qt.Key.Key_Home)
                wait_for(lambda: panel.player.position() == 0)
                panel.player.setPosition(20000)
                QTest.keyClick(panel.position_slider, Qt.Key.Key_PageUp)
                self.assertGreater(panel.player.position(), 20000)
                panel.player.setPosition(20000)
                point = panel.position_slider.rect().center()
                wheel = QWheelEvent(QPointF(point), QPointF(panel.position_slider.mapToGlobal(point)),
                                    QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                                    Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
                APP.sendEvent(panel.position_slider, wheel)
                self.assertGreater(panel.player.position(), 20000)
                position = panel.player.position()
                panel._on_position_changed(30000)
                self.assertEqual(panel.player.position(), position)
            finally:
                panel.shutdown()
                panel.close()
                panel.deleteLater()
                APP.processEvents()


class CurrentPageCaptureTests(unittest.TestCase):
    def setUp(self):
        self.reloads = 0
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path.endswith(".mp4"):
                    data, mime = b"fixture", "video/mp4"
                elif self.path == "/frame":
                    data = b"<script>fetch('/cross.mp4')</script>"
                    mime = "text/html"
                elif self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/new")
                    self.end_headers()
                    return
                elif self.path == "/reload":
                    owner.reloads += 1
                    data = f"<script>fetch('/capture{owner.reloads}.mp4')</script>".encode()
                    mime = "text/html"
                elif self.path == "/top":
                    data = f'<iframe src="http://127.0.0.1:{owner.other.server_port}/frame"></iframe>'.encode()
                    mime = "text/html"
                else:
                    name = "old" if self.path.startswith("/old") else "new"
                    data = f'<title>{name}</title><video preload="none" src="/{name}.mp4"></video>'.encode()
                    mime = "text/html"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        self.servers = []
        for _ in range(2):
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.servers.append((server, thread))
        self.server, self.other = (row[0] for row in self.servers)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.profile = QWebEngineProfile(APP)
        self.profile._media_cookies = {}
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.dispose()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
        self.profile.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def dialog(self, route):
        with patch("src.gui.interactive_scraper.get_shared_profile", return_value=self.profile):
            dialog = InteractiveScrapeDialog(self.base + route)
        dialog.show()
        self.dialogs.append(dialog)
        return dialog

    def test_navigation_and_late_callbacks_import_only_current_page(self):
        dialog = self.dialog("/old")
        wait_for(lambda: self.base + "/old.mp4" in dialog._captured_media)
        old_generation = dialog._page_generation
        dialog._page.load(QUrl(self.base + "/new"))
        wait_for(lambda: self.base + "/new.mp4" in dialog._captured_media)
        dialog._on_extract_result(
            {"documentUrl": self.base + "/old", "videos": [self.base + "/late.mp4"]},
            old_generation, self.base + "/old")
        dialog._capture_url(self.base + "/late.mp4", self.base + "/old")
        self.assertEqual(dialog._captured_media, {self.base + "/new.mp4"})
        captured = []
        dialog.finished_with_results.connect(lambda *args: captured.append(args))
        dialog._finish()
        self.assertEqual(captured[0][0], [self.base + "/new.mp4"])
        self.assertEqual(dialog.page_url, self.base + "/new")
        self.assertEqual(captured[0][2], "new")

    def test_redirect_and_cross_origin_iframe_network_media_are_retained(self):
        dialog = self.dialog("/redirect")
        wait_for(lambda: self.base + "/new.mp4" in dialog._captured_media)
        self.assertEqual(dialog.page_url, self.base + "/new")
        dialog._page.load(QUrl(self.base + "/top"))
        cross = f"http://127.0.0.1:{self.other.server_port}/cross.mp4"
        wait_for(lambda: cross in dialog._captured_media)
        self.assertNotIn(self.base + "/new.mp4", dialog._captured_media)

    def test_same_url_reload_keeps_network_only_media_and_removes_previous_capture(self):
        dialog = self.dialog("/reload")
        wait_for(lambda: self.base + "/capture1.mp4" in dialog._captured_media)
        wait_for(lambda: not dialog._page.isLoading())
        dialog._page.triggerAction(dialog._page.WebAction.Reload)
        wait_for(lambda: self.base + "/capture2.mp4" in dialog._captured_media)
        self.assertNotIn(self.base + "/capture1.mp4", dialog._captured_media)

    def test_fragment_keeps_scope_but_query_changes_clear_scope(self):
        dialog = self.dialog("/old")
        wait_for(lambda: self.base + "/old.mp4" in dialog._captured_media)
        generation = dialog._page_generation
        dialog._on_url_changed(QUrl(self.base + "/old#chapter"))
        self.assertEqual(dialog._page_generation, generation)
        self.assertIn(self.base + "/old.mp4", dialog._captured_media)
        dialog._on_url_changed(QUrl(self.base + "/old?id=2"))
        self.assertGreater(dialog._page_generation, generation)
        self.assertFalse(dialog._captured_media)

    def test_spa_rescan_replaces_old_dom_and_rejects_older_same_page_callback(self):
        dialog = self.dialog("/old")
        wait_for(lambda: self.base + "/old.mp4" in dialog._captured_media)
        dialog._page.runJavaScript("history.pushState(null,'','/old?id=2');"
                                  "setTimeout(()=>{document.querySelector('video').src='/new.mp4';"
                                  "document.title='ready';},300)")
        wait_for(lambda: dialog.page_url.endswith("?id=2"))
        dialog._run_extract()
        wait_for(lambda: self.base + "/old.mp4" in dialog._dom_media)
        wait_for(lambda: dialog._page.title() == "ready")
        dialog._run_extract()
        wait_for(lambda: self.base + "/new.mp4" in dialog._dom_media)
        self.assertNotIn(self.base + "/old.mp4", dialog._captured_media)
        callbacks = []
        with patch.object(dialog._page, "runJavaScript", side_effect=lambda _script, cb: callbacks.append(cb)):
            dialog._run_extract()
            dialog._run_extract()
        current = dialog.page_url
        callbacks[1]({"documentUrl": current, "videos": [self.base + "/new.mp4"]})
        callbacks[0]({"documentUrl": current, "videos": [self.base + "/late.mp4"]})
        self.assertNotIn(self.base + "/late.mp4", dialog._captured_media)


class MediaDeduplicationTests(unittest.TestCase):
    def scrape(self, formats, html):
        page = "https://page.example/watch"
        item = VideoItem("Original metadata", page, page, formats=formats)
        with patch.object(VideoScraper, "_scrape_with_ytdlp", return_value=[item]), \
                patch("src.scraper.ScopedCookieSession.get",
                      return_value=html_response(text=html, url=page)):
            result = VideoScraper().scrape(page)
        return item, result

    def test_known_format_is_not_a_second_video_and_metadata_is_retained(self):
        item, result = self.scrape(
            [{"url": "https://cdn.example/480.mp4", "height": 480},
             {"url": "https://cdn.example/1080.mp4", "height": 1080}],
            '<video src="https://cdn.example/480.mp4"></video>')
        self.assertEqual(result, [item])
        self.assertEqual(len(result[0].formats), 2)
        self.assertEqual(result[0].title, "Original metadata")

    def test_independent_video_and_distinct_query_are_not_merged(self):
        item, result = self.scrape(
            [{"url": "https://cdn.example/video.mp4?id=1"}],
            '<video src="https://cdn.example/video.mp4?id=1"></video>'
            '<video src="https://cdn.example/video.mp4?id=2"></video>'
            '<video src="https://cdn.example/other.mp4"></video>')
        self.assertEqual(result[0], item)
        self.assertEqual({row.url for row in result[1:]},
                         {"https://cdn.example/video.mp4?id=2", "https://cdn.example/other.mp4"})

    def test_embed_metadata_replaces_earlier_html_duplicate_but_keeps_another_video(self):
        page, embed = "https://page.example/watch", "https://player.example/embed/1"
        media, other = "https://cdn.example/video.mp4", "https://cdn.example/other.mp4"
        item = VideoItem("Embedded metadata", embed, embed, formats=[{"url": media, "height": 1080}])
        html = f'<video src="{media}"></video><video src="{other}"></video><iframe src="{embed}"></iframe>'
        with patch.object(VideoScraper, "_scrape_with_ytdlp", side_effect=lambda url, *_: [item] if url == embed else []), \
                patch("src.scraper.ScopedCookieSession.get", return_value=html_response(text=html, url=page)):
            result = VideoScraper().scrape(page)
        self.assertEqual({entry.url for entry in result}, {embed, other})
        self.assertIn(item, result)
        self.assertEqual(item.formats[0]["height"], 1080)


if __name__ == "__main__":
    unittest.main(verbosity=2)
