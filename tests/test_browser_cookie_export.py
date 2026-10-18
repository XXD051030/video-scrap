"""Qt browser-cookie export checks using only synthetic, in-memory cookies."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer, QUrl
from PyQt6.QtNetwork import QNetworkCookie
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile

from src.gui.interactive_scraper import _remember_cookie, snapshot_browser_cookies


class BrowserCookieExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["cookie-export-check"])

    def test_real_cookie_store_retains_host_only_distinction(self):
        profile = QWebEngineProfile(self.app)
        profile._media_cookies = {}
        page = QWebEnginePage(profile, self.app)
        loop = QEventLoop()
        seen = {}

        def capture(cookie):
            name = bytes(cookie.name()).decode("ascii")
            if name not in ("host-only-fixture", "domain-fixture"):
                return
            seen[name] = cookie.domain()
            _remember_cookie(profile, cookie)
            if len(seen) == 2:
                loop.quit()

        def add_cookies(_ok):
            host = QNetworkCookie(b"host-only-fixture", b"H")
            host.setPath("/")
            domain = QNetworkCookie(b"domain-fixture", b"D")
            domain.setDomain("example.test")
            domain.setPath("/")
            profile.cookieStore().setCookie(host, QUrl("https://example.test/"))
            profile.cookieStore().setCookie(domain, QUrl("https://example.test/"))

        profile.cookieStore().cookieAdded.connect(capture)
        page.loadFinished.connect(add_cookies)
        page.setHtml("<!doctype html><title>Cookie scope fixture</title>",
                     QUrl("https://example.test/"))
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(10000)
        try:
            loop.exec()
            self.assertEqual(seen, {"host-only-fixture": "example.test",
                                    "domain-fixture": ".example.test"})
            exported = snapshot_browser_cookies(profile, ["https://sub.example.test/video"])
            self.assertEqual([cookie["name"] for cookie in exported], ["domain-fixture"])
            self.assertFalse(exported[0]["host_only"])
        finally:
            timer.stop()
            page.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            profile.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main(verbosity=2)
