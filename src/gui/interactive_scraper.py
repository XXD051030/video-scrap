"""Interactive browser dialog for sites that require human verification.

Some sites gate every page behind a CAPTCHA / PoW / login wall. We can't
solve those programmatically, so this module surfaces a visible
``QWebEngineView`` that lets the user clear the challenge manually. While
the user navigates, we:

1. Observe every outgoing network request through a per-profile
   ``QWebEngineUrlRequestInterceptor`` and record the URLs that look like
   media assets (.mp4 / .m3u8 / .webm / fragmented mp4 etc.).
2. After each page load (or when the user clicks "Re-scan"), run the same
   DOM extraction script used by :mod:`js_renderer` to pick up any
   <video>/<source>/<iframe> URLs that haven't been requested yet.

Cookies and cache are persisted via a named ``QWebEngineProfile`` shared
with the headless ``JSPageScraper``, so subsequent scrapes of the same
site skip the CAPTCHA as long as the session is still valid server-side.
"""

from __future__ import annotations

from pathlib import Path
import time
from typing import Callable, List, Optional, Set
from urllib.parse import urlparse
from ..utils import redact_log_secrets

from PyQt6.QtCore import QCoreApplication, QObject, QUrl, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

try:
    from PyQt6.QtWebEngineCore import (
        QWebEnginePage,
        QWebEngineProfile,
        QWebEngineUrlRequestInterceptor,
    )
    from PyQt6.QtWebEngineWidgets import QWebEngineView

    WEBENGINE_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dep
    QWebEnginePage = None  # type: ignore[assignment]
    QWebEngineProfile = None  # type: ignore[assignment]
    QWebEngineUrlRequestInterceptor = object  # type: ignore[assignment]
    QWebEngineView = None  # type: ignore[assignment]
    WEBENGINE_AVAILABLE = False


_MEDIA_EXTENSIONS = (
    "mp4",
    "webm",
    "mov",
    "m4v",
    "m3u8",
    "mpd",
    "mkv",
)


EXTRACT_SCRIPT = r"""
(function () {
    const out = { videos: [], iframes: [], pageTitle: document.title || '',
                  documentUrl: location.href };
    function push(arr, url) {
        if (!url || typeof url !== 'string') return;
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


def _default_profile_dir() -> Path:
    return Path.home() / ".cache" / "video-scraper" / "webengine-profile"


_SHARED_PROFILE: "Optional[QWebEngineProfile]" = None


def get_shared_profile(
    parent: Optional[QObject] = None,
) -> "QWebEngineProfile":
    """Return (creating if needed) the persistent profile shared by
    :class:`InteractiveScrapeDialog` and :class:`~.js_renderer.JSPageScraper`.

    Cookies and disk cache live under ``~/.cache/video-scraper/`` so the
    session survives app restarts.
    """
    global _SHARED_PROFILE
    if not WEBENGINE_AVAILABLE:
        raise RuntimeError("PyQt6-WebEngine is not installed.")
    if _SHARED_PROFILE is not None:
        return _SHARED_PROFILE

    profile_dir = _default_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)

    # A named profile (non-empty storage name) is persistent by default.
    # Pages/dialogs are short-lived. The application owns their shared profile.
    profile = QWebEngineProfile("video-scraper-persistent", QCoreApplication.instance())
    profile.setPersistentStoragePath(str(profile_dir))
    profile.setCachePath(str(profile_dir / "cache"))
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
    )
    # Bot-detection scripts flag the default "QtWebEngine/..." UA, so we
    # pretend to be mainline desktop Chrome -- matching the UA we already
    # send from our requests-based scraper.
    profile.setHttpUserAgent(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    profile._media_cookies = {}
    store = profile.cookieStore()
    store.cookieAdded.connect(lambda cookie: _remember_cookie(profile, cookie))
    store.cookieRemoved.connect(lambda cookie: profile._media_cookies.pop(_cookie_key(cookie), None))
    store.loadAllCookies()
    _SHARED_PROFILE = profile
    return profile


def _cookie_key(cookie) -> tuple:
    return (cookie.domain(), cookie.path(), bytes(cookie.name()))


def _remember_cookie(profile, cookie) -> None:
    domain = cookie.domain().lower()
    if not domain:
        return  # No origin information is available; never create a global cookie.
    profile._media_cookies[_cookie_key(cookie)] = {
        "name": bytes(cookie.name()).decode("latin-1"),
        "value": bytes(cookie.value()).decode("latin-1"),
        "domain": domain,
        "path": cookie.path() or "/",
        "secure": cookie.isSecure(),
        "expires": None if cookie.isSessionCookie() else cookie.expirationDate().toSecsSinceEpoch(),
        "host_only": not domain.startswith("."),
    }


def snapshot_browser_cookies(profile, urls: list[str]) -> list[dict]:
    """Copy only cookies relevant to the document or discovered media hosts."""
    hosts = set()
    for url in urls:
        try:
            parsed = urlparse(url)
            if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
                hosts.add(parsed.hostname.lower())
        except (TypeError, ValueError, AttributeError):
            continue
    result = []
    for cookie in getattr(profile, "_media_cookies", {}).values():
        domain = cookie["domain"].lstrip(".")
        if cookie["expires"] is not None and cookie["expires"] <= time.time():
            continue
        if any(host == domain or (not cookie["host_only"] and host.endswith("." + domain))
               for host in hosts if host):
            result.append(dict(cookie))
    return result


class _MediaInterceptor(QWebEngineUrlRequestInterceptor):  # type: ignore[misc]
    """Records every request whose URL ends with a known media extension."""

    def __init__(
        self,
        on_url: Callable[[str, str], None],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._on_url = on_url

    def interceptRequest(self, info) -> None:  # noqa: N802 (Qt API)
        try:
            url = info.requestUrl().toString()
            document_url = info.firstPartyUrl().toString()
        except Exception:  # noqa: BLE001
            return
        if not url:
            return
        path = url.lower().split("?", 1)[0]
        if any(path.endswith("." + ext) for ext in _MEDIA_EXTENSIONS):
            self._on_url(url, document_url)


class InteractiveScrapeDialog(QDialog):
    """Visible browser window that captures videos after the user
    completes a CAPTCHA / login / age-gate flow."""

    # Match JSPageScraper's signal shape so the main window can reuse the
    # same slot for both paths.
    finished_with_results = pyqtSignal(list, list, str)
    failed = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, url: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        if not WEBENGINE_AVAILABLE:
            raise RuntimeError("PyQt6-WebEngine is not installed.")
        self.setWindowTitle("Solve verification / login to continue")
        self.resize(1040, 760)

        self._initial_url = url
        self.page_url = url
        self.browser_cookies: list[dict] = []
        self._disposed = False
        self._page_generation = 0
        self._extract_serial = 0
        self._navigation_pending = False
        self._captured_media: Set[str] = set()
        self._network_media: Set[str] = set()
        self._dom_media: Set[str] = set()
        self._captured_iframes: Set[str] = set()
        self._page_title: str = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        hint = QLabel(
            "Complete the verification / login in the page below.\n"
            "Cookies are saved so you won't have to do this again. "
            "Click \"Use videos\" once you've reached the video page."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        profile = get_shared_profile(self)
        self._profile = profile
        self._page = QWebEnginePage(profile, self)
        self._view = QWebEngineView(self)
        self._view.setPage(self._page)
        layout.addWidget(self._view, stretch=1)

        self._status = QLabel("Loading…")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        buttons = QHBoxLayout()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(self._cancel_btn)

        buttons.addStretch()

        self._extract_btn = QPushButton("Re-scan page")
        self._extract_btn.clicked.connect(self._run_extract)
        buttons.addWidget(self._extract_btn)

        self._use_btn = QPushButton("Use videos")
        self._use_btn.setEnabled(False)
        self._use_btn.setDefault(True)
        self._use_btn.clicked.connect(self._finish)
        buttons.addWidget(self._use_btn)
        layout.addLayout(buttons)

        self._interceptor = _MediaInterceptor(self._capture_url, self)
        # The profile's interceptor is global state; we restore it on close
        # so we don't leak observers across dialogs.
        self._prev_interceptor = None
        try:
            profile.setUrlRequestInterceptor(self._interceptor)
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"[interactive] interceptor install failed: {exc}")

        # The "Use videos" / "Cancel" buttons call QDialog.done()/hide(),
        # which does NOT fire closeEvent, so also detach on `finished` -
        # otherwise the interceptor leaks onto the shared profile and keeps
        # observing requests during later headless scrapes.
        self.finished.connect(self._detach_interceptor)

        self._page.loadFinished.connect(self._on_load_finished)
        load_started = getattr(self._page, "loadStarted", None)
        if load_started is not None:
            load_started.connect(self._on_load_started)
        self._page.urlChanged.connect(self._on_url_changed)
        self._page.titleChanged.connect(self._on_title_changed)

        self._page.load(QUrl(url))

    # ---------------------------------------------------------------- hooks

    @staticmethod
    def _document_identity(url: str) -> str:
        # Fragment changes stay in the same document. Query/path changes
        # represent a new capture scope, including SPA history navigation.
        return QUrl(url).adjusted(QUrl.UrlFormattingOption.RemoveFragment).toString()

    def _reset_capture(self) -> None:
        self._page_generation += 1
        self._extract_serial += 1
        self._captured_media.clear()
        self._network_media.clear()
        self._dom_media.clear()
        self._captured_iframes.clear()
        self._page_title = ""
        self.browser_cookies = []
        self._use_btn.setEnabled(False)

    def _on_load_started(self) -> None:
        if self._disposed:
            return
        self._navigation_pending = True
        self._reset_capture()

    def _capture_url(self, url: str, document_url: str = "") -> None:
        if self._disposed:
            return
        current = self._document_identity(self.page_url)
        if self._navigation_pending:
            # Reloading the same URL need not emit urlChanged. Keep network
            # media from that document, while blocking the old page during
            # navigation to a different requested URL.
            requested = self._document_identity(self._page.requestedUrl().toString())
            if requested != current:
                return
        if (not document_url or self._document_identity(document_url) != current
                or self._document_identity(self._page.url().toString()) != current):
            # firstPartyUrl is the top-level document even for a cross-origin
            # iframe/CDN request. Unknown sources are not assigned to the page;
            # current DOM extraction can still identify their video elements.
            return
        if url in self._network_media:
            return
        self._network_media.add(url)
        self.log.emit(redact_log_secrets(f"[interactive] media request: {url}"))
        self._refresh_status()

    def _on_url_changed(self, qurl: QUrl) -> None:
        if self._disposed:
            return
        text = qurl.toString()
        if self._document_identity(text) != self._document_identity(self.page_url):
            self._reset_capture()
        self.page_url = text
        self._navigation_pending = False
        self._status.setText(f"At {text}")

    def _on_title_changed(self, title: str) -> None:
        if title:
            self._page_title = title

    def _on_load_finished(self, ok: bool) -> None:
        if self._disposed:
            return
        if self._page.isLoading():
            return  # An aborted older navigation must not unlock a new one.
        self._navigation_pending = False
        if not ok:
            return
        self._run_extract()

    def _run_extract(self) -> None:
        if self._disposed or self._navigation_pending:
            return
        generation = self._page_generation
        document_url = self._document_identity(self.page_url)
        if self._document_identity(self._page.url().toString()) != document_url:
            return
        self._extract_serial += 1
        serial = self._extract_serial
        try:
            self._page.runJavaScript(
                EXTRACT_SCRIPT,
                lambda result: self._on_extract_result(result, generation, document_url, serial),
            )
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"[interactive] extract failed: {exc}")

    def _on_extract_result(self, result, generation: int, document_url: str, serial=None) -> None:
        if (self._disposed or self._navigation_pending or not isinstance(result, dict)
                or generation != self._page_generation
                or (serial is not None and serial != self._extract_serial)
                or document_url != self._document_identity(self.page_url)
                or document_url != self._document_identity(self._page.url().toString())):
            return
        reported_url = result.get("documentUrl")
        if (isinstance(reported_url, str)
                and self._document_identity(reported_url) != document_url):
            return
        videos = result.get("videos")
        iframes = result.get("iframes")
        # Re-scan replaces the prior DOM view; network-only media for this
        # navigation is kept separately. A SPA may replace its video after
        # updating history without creating a new document or loadFinished.
        self._dom_media.clear()
        self._captured_iframes.clear()
        for link in videos if isinstance(videos, list) else []:
            if isinstance(link, str) and link.startswith("http"):
                self._dom_media.add(link)
        for link in iframes if isinstance(iframes, list) else []:
            if isinstance(link, str) and link.startswith("http"):
                self._captured_iframes.add(link)
        title = result.get("pageTitle")
        if isinstance(title, str) and title:
            self._page_title = title
        self._refresh_status()

    def _refresh_status(self) -> None:
        self._captured_media = self._network_media | self._dom_media
        total = len(self._captured_media) + len(self._captured_iframes)
        self._status.setText(
            f"Captured {len(self._captured_media)} media URL(s), "
            f"{len(self._captured_iframes)} iframe(s)"
        )
        self._use_btn.setEnabled(total > 0 and not self._navigation_pending)

    # ---------------------------------------------------------------- exit

    def _finish(self) -> None:
        if self._disposed or self._navigation_pending:
            return
        current_url = self._page.url().toString()
        if self._document_identity(current_url) != self._document_identity(self.page_url):
            self._on_url_changed(QUrl(current_url))
            return
        if not self._captured_media and not self._captured_iframes:
            return
        self.page_url = current_url
        videos = sorted(self._captured_media)
        iframes = sorted(self._captured_iframes)
        self.browser_cookies = snapshot_browser_cookies(
            self._profile, [self.page_url, *videos, *iframes]
        )
        self.finished_with_results.emit(videos, iframes, self._page_title)
        self.accept()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._detach_interceptor()
        self._view.stop()
        self._page.triggerAction(QWebEnginePage.WebAction.Stop)
        self._view.deleteLater()
        self._page.deleteLater()

    def done(self, result: int) -> None:
        self.dispose()
        super().done(result)

    def _detach_interceptor(self, *_args) -> None:
        # Detach our interceptor so the next dialog / headless fallback
        # starts from a clean slate. The profile itself (+ cookies) lives on.
        # Safe to call more than once.
        try:
            self._profile.setUrlRequestInterceptor(None)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            pass

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self.dispose()
        super().closeEvent(event)
