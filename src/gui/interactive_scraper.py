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
from typing import Callable, List, Optional, Set

from PyQt6.QtCore import QObject, QUrl, pyqtSignal
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
    "ts",
    "m4s",
)


EXTRACT_SCRIPT = r"""
(function () {
    const out = { videos: [], iframes: [], pageTitle: document.title || '' };
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
    profile = QWebEngineProfile("video-scraper-persistent", parent)
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
    _SHARED_PROFILE = profile
    return profile


class _MediaInterceptor(QWebEngineUrlRequestInterceptor):  # type: ignore[misc]
    """Records every request whose URL ends with a known media extension."""

    def __init__(
        self,
        on_url: Callable[[str], None],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._on_url = on_url

    def interceptRequest(self, info) -> None:  # noqa: N802 (Qt API)
        try:
            url = info.requestUrl().toString()
        except Exception:  # noqa: BLE001
            return
        if not url:
            return
        path = url.lower().split("?", 1)[0]
        if any(path.endswith("." + ext) for ext in _MEDIA_EXTENSIONS):
            self._on_url(url)


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
        self._captured_media: Set[str] = set()
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

        self._page.loadFinished.connect(self._on_load_finished)
        self._page.urlChanged.connect(self._on_url_changed)
        self._page.titleChanged.connect(self._on_title_changed)

        self._page.load(QUrl(url))

    # ---------------------------------------------------------------- hooks

    def _capture_url(self, url: str) -> None:
        if url in self._captured_media:
            return
        self._captured_media.add(url)
        self.log.emit(f"[interactive] media request: {url}")
        self._refresh_status()

    def _on_url_changed(self, qurl: QUrl) -> None:
        text = qurl.toString()
        self._status.setText(f"At {text}")

    def _on_title_changed(self, title: str) -> None:
        if title:
            self._page_title = title

    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            return
        self._run_extract()

    def _run_extract(self) -> None:
        try:
            self._page.runJavaScript(EXTRACT_SCRIPT, self._on_extract_result)
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"[interactive] extract failed: {exc}")

    def _on_extract_result(self, result) -> None:
        if not isinstance(result, dict):
            return
        for link in result.get("videos") or []:
            if isinstance(link, str) and link.startswith("http"):
                self._captured_media.add(link)
        for link in result.get("iframes") or []:
            if isinstance(link, str) and link.startswith("http"):
                self._captured_iframes.add(link)
        title = result.get("pageTitle") or ""
        if title:
            self._page_title = title
        self._refresh_status()

    def _refresh_status(self) -> None:
        total = len(self._captured_media) + len(self._captured_iframes)
        self._status.setText(
            f"Captured {len(self._captured_media)} media URL(s), "
            f"{len(self._captured_iframes)} iframe(s)"
        )
        self._use_btn.setEnabled(total > 0)

    # ---------------------------------------------------------------- exit

    def _finish(self) -> None:
        videos = sorted(self._captured_media)
        iframes = sorted(self._captured_iframes)
        self.finished_with_results.emit(videos, iframes, self._page_title)
        self.accept()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Detach our interceptor so the next dialog / headless fallback
        # starts from a clean slate. The profile itself (+ cookies) lives on.
        try:
            self._profile.setUrlRequestInterceptor(None)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)
