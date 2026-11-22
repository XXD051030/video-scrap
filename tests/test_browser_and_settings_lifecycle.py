"""Offline checks for browser disposal, cookie snapshots and preference retry."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QCoreApplication, QEvent, QObject, QUrl, Qt, pyqtSignal
from PyQt6.QtNetwork import QNetworkCookie
from PyQt6.QtWidgets import QApplication, QDialog, QWidget

from src import settings
from src.gui import interactive_scraper, js_renderer
from src.gui.settings_dialog import SettingsDialog


APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


class FakePage(QObject):
    loadFinished = pyqtSignal(bool)
    urlChanged = pyqtSignal(QUrl)
    titleChanged = pyqtSignal(str)
    WebAction = SimpleNamespace(Stop="stop")
    instances = []

    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self.profile = profile
        self.actions = []
        self.deleted = False
        self.callback = None
        self.current_url = QUrl()
        self.instances.append(self)

    def load(self, url):
        self.current_url = url

    def url(self):
        return self.current_url

    def runJavaScript(self, _script, callback):
        self.callback = callback

    def triggerAction(self, action):
        self.actions.append(action)

    def deleteLater(self):
        self.deleted = True
        super().deleteLater()


class FakeView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.stopped = False
        self.deleted = False

    def setPage(self, page):
        self.page = page

    def load(self, url):
        self.page.load(url)

    def stop(self):
        self.stopped = True

    def deleteLater(self):
        self.deleted = True
        super().deleteLater()


class FakeCookieStore(QObject):
    cookieAdded = pyqtSignal(object)
    cookieRemoved = pyqtSignal(object)

    def loadAllCookies(self):
        self.loaded = True


class FakeProfile(QObject):
    HttpCacheType = SimpleNamespace(DiskHttpCache="disk")
    PersistentCookiesPolicy = SimpleNamespace(ForcePersistentCookies="force")

    def __init__(self, _name, parent):
        super().__init__(parent)
        self.store = FakeCookieStore(self)

    def setPersistentStoragePath(self, value):
        self.storage_path = value

    def setCachePath(self, value):
        self.cache_path = value

    def setHttpCacheType(self, value):
        self.cache_type = value

    def setPersistentCookiesPolicy(self, value):
        self.cookie_policy = value

    def setHttpUserAgent(self, value):
        self.user_agent = value

    def cookieStore(self):
        return self.store


def cookie(name="sid", domain="origin.invalid", **changes):
    value = dict(name=name, value="fixture", domain=domain, path="/", secure=True,
                 expires=None, host_only=not domain.startswith("."))
    value.update(changes)
    return value


class BrowserLifecycleTests(unittest.TestCase):
    def setUp(self):
        FakePage.instances = []

    def tearDown(self):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def _renderer(self):
        profile = SimpleNamespace(_media_cookies={})
        patches = mock.patch.multiple(js_renderer, WEBENGINE_AVAILABLE=True,
                                      QWebEnginePage=FakePage,
                                      get_shared_profile=lambda _parent: profile)
        patches.start()
        self.addCleanup(patches.stop)
        scraper = js_renderer.JSPageScraper()
        self.addCleanup(scraper.deleteLater)
        scraper.scrape("https://origin.invalid/watch")
        return scraper, profile, FakePage.instances[-1]

    def test_dispose_stops_timers_and_suppresses_queued_browser_callbacks(self):
        scraper, _profile, page = self._renderer()
        finished, failures = [], []
        scraper.finished.connect(lambda *args: finished.append(args))
        scraper.failed.connect(failures.append)
        page.loadFinished.emit(True)
        scraper._extract()
        callback = page.callback
        self.assertTrue(scraper._timeout_timer.isActive())
        self.assertTrue(scraper._wait_timer.isActive())
        scraper.dispose()
        scraper.dispose()
        callback({"videos": ["https://origin.invalid/late.mp4"]})
        scraper._on_timeout()
        page.loadFinished.emit(False)
        self.assertEqual((finished, failures), ([], []))
        self.assertEqual(page.actions, ["stop"])
        self.assertTrue(page.deleted)
        self.assertIsNone(scraper._page)
        self.assertFalse(scraper._timeout_timer.isActive())
        self.assertFalse(scraper._wait_timer.isActive())

    def test_success_retains_final_referer_and_cookie_copy_after_page_disposal(self):
        scraper, profile, page = self._renderer()
        original = cookie(domain="final.invalid")
        profile._media_cookies["fixture"] = original
        page.current_url = QUrl("https://final.invalid/watch")
        observed = []
        scraper.finished.connect(lambda *args: observed.append((args, page.deleted)))
        scraper._extract()
        page.callback({"videos": ["https://final.invalid/clip.mp4"],
                       "iframes": [], "pageTitle": "Fixture"})
        scraper._on_timeout()
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0][1])
        self.assertEqual(scraper.page_url, "https://final.invalid/watch")
        self.assertEqual(scraper.browser_cookies, [original])
        self.assertIsNot(scraper.browser_cookies[0], original)
        self.assertIsNone(scraper._page)

    def test_load_failure_releases_page_and_ignores_later_result(self):
        scraper, _profile, page = self._renderer()
        failures, results = [], []
        scraper.failed.connect(failures.append)
        scraper.finished.connect(lambda *args: results.append(args))
        scraper._extract()
        page.loadFinished.emit(False)
        page.callback({"videos": ["https://origin.invalid/late.mp4"]})
        self.assertEqual(len(failures), 1)
        self.assertEqual(results, [])
        self.assertTrue(page.deleted)
        self.assertFalse(scraper._timeout_timer.isActive())

    def test_interactive_reject_disposes_view_page_and_detaches_interceptor(self):
        detached = []
        profile = SimpleNamespace(setUrlRequestInterceptor=detached.append,
                                  _media_cookies={})
        with mock.patch.multiple(interactive_scraper, WEBENGINE_AVAILABLE=True,
                                 QWebEnginePage=FakePage, QWebEngineView=FakeView,
                                 get_shared_profile=lambda _parent: profile):
            dialog = interactive_scraper.InteractiveScrapeDialog("https://origin.invalid/watch")
            page, view = dialog._page, dialog._view
            dialog._run_extract()
            callback = page.callback
            dialog.reject()
            dialog.dispose()
            callback({"videos": ["https://origin.invalid/late.mp4"]})
            dialog._capture_url("https://origin.invalid/late.mp4")
            self.assertEqual(dialog._captured_media, set())
            self.assertTrue(dialog._disposed and page.deleted and view.deleted)
            self.assertTrue(view.stopped)
            self.assertEqual(page.actions, ["stop"])
            self.assertIsNone(detached[-1])
            dialog.deleteLater()

    def test_shared_profile_is_owned_by_application_and_reused_after_requester_deletion(self):
        with tempfile.TemporaryDirectory(prefix="browser-profile-fixture-") as directory, \
                mock.patch.multiple(interactive_scraper, _SHARED_PROFILE=None,
                                    QWebEngineProfile=FakeProfile, WEBENGINE_AVAILABLE=True,
                                    _default_profile_dir=lambda: Path(directory)):
            requester = QObject()
            profile = interactive_scraper.get_shared_profile(requester)
            self.assertIs(profile.parent(), APP)
            self.assertTrue(profile.store.loaded)
            fixture = QNetworkCookie(b"sid", b"fixture")
            fixture.setDomain("origin.invalid")
            fixture.setPath("/")
            profile.store.cookieAdded.emit(fixture)
            self.assertEqual(len(profile._media_cookies), 1)
            requester.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertIs(interactive_scraper.get_shared_profile(QObject()), profile)
            profile.store.cookieRemoved.emit(fixture)
            self.assertEqual(profile._media_cookies, {})
            profile.deleteLater()

    def test_cookie_snapshot_preserves_host_scope_expiry_and_independent_values(self):
        cookies = [cookie(), cookie("domain", ".origin.invalid"),
                   cookie("other", "other.invalid"),
                   cookie("expired", "origin.invalid", expires=1)]
        profile = SimpleNamespace(_media_cookies={i: value for i, value in enumerate(cookies)})
        result = interactive_scraper.snapshot_browser_cookies(
            profile, ["https://sub.origin.invalid/clip.mp4", "https://[bad", None,
                      "file:///origin.invalid", "https://evil-origin.invalid"])
        self.assertEqual([value["name"] for value in result], ["domain"])
        result[0]["value"] = "changed"
        self.assertEqual(cookies[1]["value"], "fixture")


class SettingsRetryTests(unittest.TestCase):
    def test_failed_save_stays_open_then_retries_without_losing_session_value(self):
        with tempfile.TemporaryDirectory(prefix="settings-retry-fixture-") as directory:
            path = Path(directory) / "settings.json"
            with mock.patch.object(settings, "SETTINGS_PATH", path):
                original = settings.AppSettings(playback_buffer_seconds=10)
                self.assertTrue(original.save())
                store = settings.SettingsStore()
                dialog = SettingsDialog(store)
                accepted = []
                dialog.accepted.connect(lambda: accepted.append(True))
                dialog.show()
                dialog.buffer_spin.setValue(35)
                with mock.patch.object(settings.os, "replace", side_effect=OSError("fixture disk failure")):
                    dialog.save_button.click()
                self.assertTrue(dialog.isVisible())
                self.assertEqual(accepted, [])
                self.assertTrue(dialog.save_error_label.isVisible())
                self.assertEqual(dialog.save_error_label.textFormat(), Qt.TextFormat.PlainText)
                self.assertIn("fixture disk failure", dialog.save_error_label.text())
                self.assertEqual(store.get().playback_buffer_seconds, 35)
                self.assertEqual(settings.AppSettings.load().playback_buffer_seconds, 10)
                self.assertEqual(list(path.parent.glob(".settings.json.*.tmp")), [])
                dialog.save_button.click()
                self.assertEqual(accepted, [True])
                self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
                self.assertEqual(settings.AppSettings.load().playback_buffer_seconds, 35)
                self.assertEqual(store.last_save_error, "")
                dialog.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main(verbosity=2)
