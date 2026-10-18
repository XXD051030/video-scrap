"""Optional Chromium-based fallback that renders JS-driven pages.

If ``PyQt6-WebEngine`` is installed, ``JSPageScraper`` loads a URL in a
headless ``QWebEnginePage`` so client-side scripts can fully execute,
then extracts every video URL it can find from the resulting DOM.

Must run in the main GUI thread because Qt WebEngine is not thread-safe.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal

try:
    from PyQt6.QtWebEngineCore import QWebEnginePage
    WEBENGINE_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dep
    QWebEnginePage = None  # type: ignore[assignment]
    WEBENGINE_AVAILABLE = False

try:
    from .interactive_scraper import get_shared_profile, snapshot_browser_cookies
except Exception:  # pragma: no cover - optional dep
    get_shared_profile = None  # type: ignore[assignment]


# JS executed inside the rendered page to harvest video sources.
EXTRACT_SCRIPT = r"""
(function () {
    const out = { videos: [], iframes: [], pageTitle: document.title || '' };
    function push(arr, url) {
        if (!url) return;
        if (typeof url !== 'string') return;
        if (url.startsWith('blob:') || url.startsWith('data:')) return;
        if (!url.startsWith('http')) return;
        if (!arr.includes(url)) arr.push(url);
    }
    document.querySelectorAll('video').forEach(v => {
        push(out.videos, v.currentSrc);
        push(out.videos, v.src);
        v.querySelectorAll('source').forEach(s => push(out.videos, s.src));
    });
    document.querySelectorAll('source').forEach(s => push(out.videos, s.src));
    document.querySelectorAll('iframe').forEach(f => push(out.iframes, f.src));
    const re = /https?:\/\/[^\s"'<>]+?\.(?:mp4|webm|mov|m4v|m3u8|mpd|mkv)(?:\?[^\s"'<>]*)?/gi;
    const text = document.documentElement.outerHTML;
    let m;
    while ((m = re.exec(text)) !== null) {
        push(out.videos, m[0]);
    }
    return out;
})();
"""


ResultCallback = Callable[[List[str], List[str], str], None]
ErrorCallback = Callable[[str], None]
LogCallback = Callable[[str], None]


class JSPageScraper(QObject):
    """Headless QWebEnginePage that surfaces video URLs after JS executes."""

    finished = pyqtSignal(list, list, str)  # videos, iframes, page_title
    failed = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(
        self,
        wait_after_load_ms: int = 2500,
        load_timeout_ms: int = 25000,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        if not WEBENGINE_AVAILABLE:
            raise RuntimeError(
                "PyQt6-WebEngine is not installed. "
                "Run: pip install PyQt6-WebEngine"
            )
        self.wait_after_load_ms = wait_after_load_ms
        self.load_timeout_ms = load_timeout_ms
        self._page: Optional[QWebEnginePage] = None
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(self._on_timeout)
        self._wait_timer = QTimer(self)
        self._wait_timer.setSingleShot(True)
        self._wait_timer.timeout.connect(self._extract)
        self._done = False
        self._profile = None
        self.page_url = ""
        self.browser_cookies: list[dict] = []

    def scrape(self, url: str) -> None:
        self.dispose()
        self._done = False
        self.page_url = url
        self.browser_cookies = []
        self.log.emit(f"WebEngine: loading {url}")
        # Reuse the persistent profile so any cookies the user established
        # via the interactive dialog (after clearing a CAPTCHA / logging in)
        # are available to the headless renderer as well.
        profile = None
        if get_shared_profile is not None:
            try:
                profile = get_shared_profile(self)
            except Exception as exc:  # noqa: BLE001
                self.log.emit(f"WebEngine: shared profile unavailable: {exc}")
        self._page = (
            QWebEnginePage(profile, self) if profile is not None
            else QWebEnginePage(self)
        )
        self._profile = profile
        self._page.loadFinished.connect(self._on_loaded)
        self._timeout_timer.start(self.load_timeout_ms)
        self._page.load(QUrl(url))

    def _on_loaded(self, ok: bool) -> None:
        if self._done:
            return
        if not ok:
            self._fail("WebEngine load reported failure")
            return
        self.log.emit(
            f"WebEngine: load complete, waiting {self.wait_after_load_ms}ms for scripts"
        )
        self._wait_timer.start(self.wait_after_load_ms)

    def _extract(self) -> None:
        if self._done or self._page is None:
            return
        self._page.runJavaScript(EXTRACT_SCRIPT, self._on_extract_result)

    def _on_extract_result(self, result) -> None:
        if self._done:
            return
        self._timeout_timer.stop()
        self._done = True
        if not isinstance(result, dict):
            self._fail("WebEngine returned no result")
            return
        videos = list(result.get("videos") or [])
        iframes = list(result.get("iframes") or [])
        title = result.get("pageTitle") or ""
        if self._page is not None:
            self.page_url = self._page.url().toString() or self.page_url
        if self._profile is not None:
            self.browser_cookies = snapshot_browser_cookies(
                self._profile, [self.page_url, *videos, *iframes]
            )
        self.log.emit(
            f"WebEngine: found {len(videos)} video URL(s), {len(iframes)} iframe(s)"
        )
        self.dispose()
        self.finished.emit(videos, iframes, title)

    def _on_timeout(self) -> None:
        if self._done:
            return
        self._fail("WebEngine load timed out")

    def _fail(self, message: str) -> None:
        self.dispose()
        self.failed.emit(message)

    def dispose(self) -> None:
        self._done = True
        self._timeout_timer.stop()
        self._wait_timer.stop()
        page, self._page = self._page, None
        if page is not None:
            page.triggerAction(QWebEnginePage.WebAction.Stop)
            page.deleteLater()
