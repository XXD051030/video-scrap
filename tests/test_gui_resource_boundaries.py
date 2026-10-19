"""Offline GUI audit regressions: resource, ownership and untrusted-data edges."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6 import sip
from PyQt6.QtCore import QByteArray, QBuffer, QCoreApplication, QEvent, QIODevice, QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QImage, QTextDocument
from PyQt6.QtWidgets import QApplication

from src.gui import js_renderer, main_window, preview_panel, video_list, workers
from src.gui.preview_panel import PreviewPanel
from src.scraper import VideoItem, build_items_from_urls
from src.utils import redact_log_secrets
import test_download_controls as controls
from test_gui_lifecycle_regressions import FakePage

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


def pump_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        APP.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Offline worker fixture did not finish")
        time.sleep(0.002)
    APP.processEvents()


def image_bytes(width=12, height=8):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0x336699)
    output = QBuffer()
    output.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(output, "PNG")
    return bytes(output.data())


class Response(QObject):
    metaDataChanged = pyqtSignal()
    readyRead = pyqtSignal()
    finished = pyqtSignal()

    def __init__(self, chunks=None, headers=None, before_read=None):
        super().__init__()
        self.chunks = iter(chunks or [image_bytes()])
        self.headers = headers or {"Content-Type": "image/png"}
        self.closed = False
        self.before_read = before_read
        self.buffer = bytearray()
        self.done = False
        self.read_buffer_size = None

    def start(self):
        self.metaDataChanged.emit()
        self.advance()

    def advance(self):
        if self.done:
            return
        chunk = next(self.chunks, None)
        if chunk is None:
            self.done = True
            self.finished.emit()
            return
        self.buffer.extend(chunk)
        self.readyRead.emit()
        QTimer.singleShot(0, self.advance)

    def rawHeader(self, key):
        return QByteArray(str(self.headers.get(bytes(key).decode(), "")).encode())

    def attribute(self, _key):
        return 200

    def bytesAvailable(self):
        return len(self.buffer)

    def read(self, size):
        if self.before_read:
            self.before_read()
        data = self.buffer[:size]
        del self.buffer[:size]
        return QByteArray(data)

    def setReadBufferSize(self, value):
        self.read_buffer_size = value

    def isFinished(self):
        return self.done

    def error(self):
        return workers.QNetworkReply.NetworkError.NoError

    def abort(self):
        self.closed = True
        if not self.done:
            self.done = True
            self.finished.emit()


class Session(QObject):
    def __init__(self, response):
        super().__init__()
        self.response = response
        self.closed = False
        self.request = None

    def setCookieJar(self, jar):
        self.jar = jar

    def get(self, request):
        self.request = request
        self.response.setParent(self)
        QTimer.singleShot(0, self.response.start)
        return self.response

    def deleteLater(self):
        self.closed = True
        super().deleteLater()


class ThumbnailInputTests(unittest.TestCase):
    def test_exact_qt_cookie_transfer_preserves_host_domain_secure_path_and_expiry(self):
        from http.cookiejar import CookieJar
        from requests.cookies import create_cookie
        now = int(time.time())
        records = [
            {"name": "host", "value": "one", "domain": "origin.invalid", "host_only": True,
             "path": "/private", "secure": True, "expires": now + 3600},
            {"name": "domain", "value": "two", "domain": ".origin.invalid", "host_only": False,
             "path": "/", "secure": False, "expires": None},
            {"name": "expired", "value": "three", "domain": "origin.invalid", "host_only": True,
             "path": "/", "secure": False, "expires": now - 3600},
        ]
        base = CookieJar()
        base_cookie = create_cookie(name="base_host", value="four", domain="origin.invalid", path="/")
        base_cookie.domain_specified = False
        base.set_cookie(base_cookie)
        parent = QObject()
        jar = workers.thumbnail_cookiejar(parent, records, base)
        def names(url):
            return {bytes(cookie.name()).decode() for cookie in jar.cookiesForUrl(QUrl(url))}
        self.assertEqual(names("https://origin.invalid/private/a"), {"host", "domain", "base_host"})
        self.assertEqual(names("https://sub.origin.invalid/private/a"), {"domain"})
        self.assertEqual(names("http://origin.invalid/private/a"), {"domain", "base_host"})
        self.assertEqual(names("https://origin.invalid/privateish"), {"domain", "base_host"})
        self.assertEqual(names("https://unrelated.invalid/private/a"), set())
        self.assertEqual(names("https://origin.invalid.evil.invalid/private/a"), set())
        self.assertEqual(names("https://origin.invalid/public"), {"domain", "base_host"})
        parent.deleteLater()

    def run_thumbnail(self, response, extra_patches=()):
        session = Session(response)
        worker = workers.ThumbnailWorker(7, "https://images.invalid/thumb.png")
        ready, failed = [], []
        worker.ready.connect(lambda *args: ready.append(args))
        worker.failed.connect(lambda *args: failed.append(args))
        with mock.patch.object(workers, "QNetworkAccessManager", return_value=session):
            for patch in extra_patches:
                patch.start()
            try:
                worker.run()
            finally:
                for patch in reversed(extra_patches):
                    patch.stop()
        self.assertTrue(response.closed)
        self.assertTrue(session.closed)
        worker.deleteLater()
        return ready, failed, session

    def test_valid_thumbnail_is_streamed_and_scaled_before_gui_delivery(self):
        ready, failed, session = self.run_thumbnail(Response([image_bytes(2000, 1000)]))
        self.assertFalse(failed)
        self.assertEqual(ready[0][0], 7)
        image = QImage.fromData(ready[0][1])
        self.assertEqual((image.width(), image.height()), (1280, 640))
        self.assertEqual(session.request.transferTimeout(), 3000)
        self.assertEqual(session.request.maximumRedirectsAllowed(), 5)
        self.assertEqual(bytes(session.request.rawHeader(b"Accept-Encoding")), b"identity")

    def test_declared_oversize_is_rejected_before_read(self):
        response = Response(headers={"Content-Type": "image/png", "Content-Length": str(workers.THUMB_MAX_BYTES + 1)},
                            before_read=lambda: self.fail("Oversized source must not be read"))
        ready, failed, _ = self.run_thumbnail(response)
        self.assertFalse(ready)
        self.assertIn("byte limit", failed[0][1])

    def test_streaming_byte_limit_applies_without_content_length(self):
        ready, failed, _ = self.run_thumbnail(Response([b"a" * 20, b"b" * 20]),
            [mock.patch.object(workers, "THUMB_MAX_BYTES", 32)])
        self.assertFalse(ready)
        self.assertIn("byte limit", failed[0][1])

    def test_wrong_type_and_compressed_transport_rejected(self):
        for headers in ({"Content-Type": "text/html"},
                        {"Content-Type": "image/png", "Content-Encoding": "gzip"}):
            ready, failed, _ = self.run_thumbnail(Response(headers=headers))
            self.assertFalse(ready)
            self.assertTrue(failed)

    def test_absolute_budget_checked_between_socket_reads(self):
        clock = [0.0]
        def drip():
            clock[0] += 20
        ready, failed, _ = self.run_thumbnail(Response([b"a", b"b"], before_read=drip),
            [mock.patch.object(workers.time, "monotonic", side_effect=lambda: clock[0])])
        self.assertFalse(ready)
        self.assertIn("total time limit", failed[0][1])

    def test_decode_dimension_budget_and_raster_type(self):
        with mock.patch.object(workers, "THUMB_MAX_PIXELS", 1000):
            with self.assertRaisesRegex(ValueError, "dimension limit"):
                workers.decode_thumbnail(image_bytes(40, 40))
        with self.assertRaisesRegex(ValueError, "raster image"):
            workers.decode_thumbnail(b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>')

    def test_cancel_during_stream_suppresses_ready_and_failure(self):
        worker = workers.ThumbnailWorker(0, "https://images.invalid/thumb.png")
        response = Response([image_bytes()], before_read=worker.cancel)
        session = Session(response)
        signals = []
        worker.ready.connect(lambda *_: signals.append("ready"))
        worker.failed.connect(lambda *_: signals.append("failed"))
        with mock.patch.object(workers, "QNetworkAccessManager", return_value=session):
            worker.run()
        self.assertEqual(signals, [])
        self.assertTrue(response.closed and session.closed)
        worker.deleteLater()


class BlockedThumbnail(workers.ThumbnailWorker):
    release = threading.Event()
    entered = []

    def run(self):
        self.entered.append(self)
        self.release.wait(3)
        # Deliberately emit after cancellation, as queued old callbacks may do.
        self.ready.emit(self.index, image_bytes())
        self.failed.emit(self.index, "old synthetic failure")


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        BlockedThumbnail.release = threading.Event()
        BlockedThumbnail.entered = []

    def tearDown(self):
        BlockedThumbnail.release.set()
        workers.finish_background_workers(APP)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_list_replacement_is_immediate_and_keeps_real_worker_limit(self):
        widget = video_list.VideoListWidget()
        item = VideoItem("Fixture", "https://media.invalid/a.mp4", "https://media.invalid/",
                         thumbnail="https://images.invalid/a.png", is_direct=True)
        with mock.patch.object(video_list, "ThumbnailWorker", BlockedThumbnail):
            widget.set_videos([item] * 8)
            pump_until(lambda: len(BlockedThumbnail.entered) == 6)
            old = list(widget._thumb_workers)
            start = time.monotonic()
            for _ in range(20):
                widget.set_videos([item] * 8)
            self.assertLess(time.monotonic() - start, 0.3)
            self.assertEqual(len(widget._thumb_workers), 6)
            self.assertEqual(len(BlockedThumbnail.entered), 6)
            self.assertTrue(all(thread.parent() is None for thread in old))
            widget.shutdown()
            BlockedThumbnail.release.set()
            pump_until(lambda: not widget._thumb_workers)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertTrue(all(sip.isdeleted(thread) for thread in old))
            self.assertEqual(widget.findChildren(workers.ThumbnailWorker), [])
        widget.deleteLater()

    def test_preview_replacement_ignores_old_ready_and_failure_and_bounds_threads(self):
        panel = PreviewPanel()
        a = VideoItem("A", "https://media.invalid/a.mp4", "https://media.invalid/",
                      thumbnail="https://images.invalid/a.png", is_direct=True)
        b = VideoItem("B", "https://media.invalid/b.mp4", "https://media.invalid/", is_direct=True)
        with mock.patch.object(preview_panel, "ThumbnailWorker", BlockedThumbnail):
            panel.show_video(a)
            pump_until(lambda: len(BlockedThumbnail.entered) == 1)
            old = panel._thumb_worker
            start = time.monotonic()
            for _ in range(20):
                panel.show_video(a)
            panel.show_video(b)
            self.assertLess(time.monotonic() - start, 0.3)
            self.assertEqual(len(BlockedThumbnail.entered), 1)
            BlockedThumbnail.release.set()
            pump_until(lambda: panel._thumb_worker is None)
            self.assertEqual(panel.title_label.text(), "B")
            self.assertIsNone(panel._thumbnail_pixmap)
            self.assertEqual(panel.thumbnail_label.text(), "(No thumbnail available)")
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertTrue(sip.isdeleted(old))
        panel.shutdown()
        panel.deleteLater()

    def test_destroyed_widget_does_not_destroy_its_running_thumbnail(self):
        widget = video_list.VideoListWidget()
        item = VideoItem("Fixture", "https://media.invalid/a.mp4", "https://media.invalid/",
                         thumbnail="https://images.invalid/a.png", is_direct=True)
        with mock.patch.object(video_list, "ThumbnailWorker", BlockedThumbnail):
            widget.set_videos([item])
            pump_until(lambda: len(BlockedThumbnail.entered) == 1)
            thread = widget._thumb_workers[0]
            widget.shutdown()
            widget.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertTrue(sip.isdeleted(widget))
            self.assertFalse(sip.isdeleted(thread))
            self.assertTrue(thread.isRunning())
            BlockedThumbnail.release.set()
            workers.finish_background_workers(APP)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertTrue(sip.isdeleted(thread))

    def test_closing_scrape_uses_cooperative_cancel_without_gui_wait(self):
        entered, release = threading.Event(), threading.Event()
        observed = []
        def scrape(_self, _url, **kwargs):
            observed.append(kwargs["cancel_event"])
            entered.set()
            release.wait(3)
            return []
        with controls._window() as window, mock.patch.object(workers.VideoScraper, "scrape", scrape):
            window.url_input.setText("https://page.invalid/watch")
            window.start_scrape()
            thread = window.scrape_worker
            self.assertTrue(entered.wait(1))
            start = time.monotonic()
            window.close()
            self.assertLess(time.monotonic() - start, 0.3)
            self.assertTrue(observed[0].is_set())
            self.assertTrue(thread.isRunning())
            release.set()
            workers.finish_background_workers(APP)


class PreviewBoundaryTests(unittest.TestCase):
    def test_qt_anchor_preserves_url_and_blocks_other_protocols(self):
        panel = PreviewPanel()
        url = 'https://safe.invalid/a.mp4?x="href="file:///tmp/probe&literal=&amp;'
        item = build_items_from_urls([url], [], "https://safe.invalid/watch", "Fixture")[0]
        panel.show_video(item)
        document = QTextDocument()
        document.setHtml(panel.url_label.text())
        cursor = document.begin().begin()
        anchors = []
        while not cursor.atEnd():
            fragment = cursor.fragment()
            if fragment.isValid() and fragment.charFormat().isAnchor():
                anchors.append(fragment.charFormat().anchorHref())
            cursor += 1
        self.assertTrue(anchors)
        self.assertTrue(all(anchor == url for anchor in anchors))
        self.assertFalse(panel.url_label.openExternalLinks())
        with mock.patch.object(preview_panel.QDesktopServices, "openUrl", return_value=True) as open_url:
            for blocked in ("file:///tmp/probe", "javascript:alert(1)", "https://host.invalid:99999/x", "https://user:pass@host.invalid/x"):
                self.assertFalse(panel._open_source_link(blocked))
            open_url.assert_not_called()
            self.assertTrue(panel._open_source_link(url))
            self.assertEqual(open_url.call_args.args[0], QUrl(url))
        panel.shutdown()
        panel.deleteLater()

    def test_x_browser_direct_preview_keeps_browser_session(self):
        proxy = SimpleNamespace(register=mock.Mock(return_value="http://127.0.0.1/play/fixture"),
                                unregister=mock.Mock())
        panel = PreviewPanel(proxy=proxy)
        item = build_items_from_urls(["https://video.twimg.com/a.mp4"], [],
                                     "https://x.com/example/status/123", "Fixture")[0]
        item.browser_cookies = [{"name": "sid", "value": "fixture", "domain": "video.twimg.com", "path": "/"}]
        panel.show_video(item)
        self.assertTrue(panel.play_button.isEnabled())
        self.assertEqual(panel._pending_playable_url, item.url)
        panel._wrap_with_proxy(item.url, item)
        self.assertEqual(proxy.register.call_args.kwargs["browser_cookies"], item.browser_cookies)
        panel.shutdown()
        panel.deleteLater()

    def test_nonfinite_height_and_wrong_format_members_fall_back(self):
        panel = PreviewPanel()
        for source in ("https://site.invalid/watch", "https://x.com/example/status/123"):
            item = VideoItem("Fixture", source, source)
            item.formats = [None, {"url": "https://media.invalid/a.mp4", "ext": "mp4", "height": float("inf")},
                            {"url": "https://media.invalid/b.mp4", "ext": "mp4", "height": "NaN"}]
            panel.show_video(item)
            self.assertTrue(panel.play_button.isEnabled())
        panel.shutdown()
        panel.deleteLater()

    def test_js_old_callbacks_and_timer_do_not_finish_a_reused_scraper(self):
        profile = SimpleNamespace(_media_cookies={})
        with mock.patch.multiple(js_renderer, WEBENGINE_AVAILABLE=True, QWebEnginePage=FakePage,
                                 get_shared_profile=lambda _parent: profile):
            scraper = js_renderer.JSPageScraper()
            results, failures = [], []
            scraper.finished.connect(lambda *args: results.append(args))
            scraper.failed.connect(failures.append)
            scraper.scrape("https://a.invalid/watch")
            page_a = scraper._page
            timeout_a = scraper._timeout_timer
            wait_a = scraper._wait_timer
            scraper._extract()
            callback_a = page_a.callback
            scraper.scrape("https://b.invalid/watch")
            page_b = scraper._page
            callback_a({"videos": ["https://a.invalid/old.mp4"]})
            page_a.loadFinished.emit(False)
            timeout_a.timeout.emit()
            wait_a.timeout.emit()
            self.assertEqual((results, failures), ([], []))
            self.assertIs(scraper._page, page_b)
            self.assertFalse(page_b.deleted)
            scraper._extract()
            page_b.callback({"videos": ["https://b.invalid/new.mp4"], "pageTitle": "B"})
            self.assertEqual(results, [(["https://b.invalid/new.mp4"], [], "B")])
            scraper.deleteLater()

    def test_missing_optional_webengine_hides_browser_entry_and_offer(self):
        with mock.patch.object(main_window, "INTERACTIVE_AVAILABLE", False), controls._window() as window:
            self.assertIsNone(window.browser_scrape_action)
            window._current_url = "https://site.invalid/watch"
            with mock.patch.object(main_window.QMessageBox, "question") as question:
                self.assertFalse(window._offer_interactive("Fixture"))
                question.assert_not_called()

    def test_logs_remove_signed_query_userinfo_and_fragment(self):
        secret_url = "https://name:password@media.invalid/a.mp4?token=secret#fragment"
        self.assertEqual(redact_log_secrets(secret_url), "https://media.invalid/a.mp4?REDACTED")
        with controls._window() as window:
            window.log(f"Failed loading {secret_url}")
            log = window.log_view.toPlainText()
            self.assertIn("media.invalid/a.mp4", log)
            for secret in ("name", "password", "token=secret", "fragment"):
                self.assertNotIn(secret, log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
