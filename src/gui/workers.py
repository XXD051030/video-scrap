"""Background worker threads for scraping and downloading.

Keeping these in QThread subclasses prevents the GUI from freezing during
network I/O.
"""

from __future__ import annotations

from http.cookiejar import CookieJar
from pathlib import Path
import threading
import time
from typing import List, Optional

from PyQt6.QtCore import (QByteArray, QBuffer, QDateTime, QIODevice, QObject, QSize, QThread,
                          QTimer, Qt, QUrl, pyqtSignal)
from PyQt6.QtGui import QImageReader
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkCookie, QNetworkCookieJar, QNetworkReply, QNetworkRequest

from ..downloader import (
    DownloadLaneLimiter,
    DownloadProgress,
    build_request_headers,
)
from ..download_job import DownloadCancelled, DownloadCleanupError, DownloadJob
from ..browser_cookies import browser_cookiejar
from ..scraper import VideoItem, VideoScraper

THUMB_MAX_BYTES = 8 * 1024 * 1024
THUMB_MAX_PIXELS = 16 * 1024 * 1024
THUMB_MAX_DIMENSION = 8192
THUMB_TOTAL_SECONDS = 15.0
THUMB_MAX_REQUESTS = 6  # the original request plus five redirects
THUMB_DECODE_SIZE = QSize(1280, 720)

# Workers have no widget parent. Keep them alive until QThread.finished,
# even when their old widget has been replaced or destroyed.
_BACKGROUND_WORKERS: set[QThread] = set()


def track_background_worker(worker: QThread) -> None:
    if worker in _BACKGROUND_WORKERS:
        return
    worker.setParent(None)
    _BACKGROUND_WORKERS.add(worker)

    def finished() -> None:
        _BACKGROUND_WORKERS.discard(worker)
        worker.deleteLater()

    worker.finished.connect(finished)
    worker.destroyed.connect(lambda *_: _BACKGROUND_WORKERS.discard(worker))


def cancel_background_worker(worker: QThread) -> None:
    """Request cancellation without blocking the GUI or killing a thread."""
    cancel = getattr(worker, "cancel", None)
    if cancel is not None:
        cancel()
    else:
        worker.requestInterruption()


def finish_background_workers(app) -> None:
    """Drain retained workers after the GUI event loop has exited."""
    for worker in list(_BACKGROUND_WORKERS):
        cancel_background_worker(worker)
    while _BACKGROUND_WORKERS:
        for worker in list(_BACKGROUND_WORKERS):
            if worker.isRunning():
                worker.wait(50)
            elif not getattr(worker, "fetch_pending", False):
                _BACKGROUND_WORKERS.discard(worker)
                worker.deleteLater()
        # Cancelled thumbnail fetches finish on this (GUI) thread's events.
        app.processEvents()


def decode_thumbnail(data: bytes) -> bytes:
    """Decode a bounded raster image off the GUI thread and scale its output."""
    if not data or len(data) > THUMB_MAX_BYTES:
        raise ValueError("Thumbnail exceeds the image byte limit")
    source = QBuffer()
    source.setData(QByteArray(data))
    source.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(source)
    reader.setDecideFormatFromContent(True)
    # Vector input can have complex rendering work unrelated to its size.
    if bytes(reader.format()).lower() not in {b"jpeg", b"jpg", b"png", b"webp", b"gif", b"bmp", b"ico", b"avif", b"heif", b"heic"}:
        raise ValueError("Thumbnail is not a supported raster image")
    size = reader.size()
    if (not size.isValid() or size.width() > THUMB_MAX_DIMENSION
            or size.height() > THUMB_MAX_DIMENSION
            or size.width() * size.height() > THUMB_MAX_PIXELS):
        raise ValueError("Thumbnail exceeds the image dimension limit")
    if size.width() > THUMB_DECODE_SIZE.width() or size.height() > THUMB_DECODE_SIZE.height():
        reader.setScaledSize(size.scaled(THUMB_DECODE_SIZE, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        raise ValueError("Thumbnail could not be decoded")
    # Always hand the GUI a small, decoded raster, never the untrusted source.
    if image.width() > THUMB_DECODE_SIZE.width() or image.height() > THUMB_DECODE_SIZE.height():
        image = image.scaled(THUMB_DECODE_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    output = QBuffer()
    output.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(output, "PNG"):
        raise ValueError("Thumbnail could not be resized")
    return bytes(output.data())


def thumbnail_cookiejar(parent, records=None, base=None) -> QNetworkCookieJar:
    """Transfer scope explicitly; omitting Domain preserves host-only cookies."""
    jar = QNetworkCookieJar(parent)
    for cookie in browser_cookiejar(records, base=base):
        qcookie = QNetworkCookie(cookie.name.encode(), cookie.value.encode())
        qcookie.setPath(cookie.path or "/")
        qcookie.setSecure(cookie.secure)
        if cookie.domain_specified:
            qcookie.setDomain(cookie.domain)
        if cookie.expires is not None:
            qcookie.setExpirationDate(QDateTime.fromSecsSinceEpoch(cookie.expires))
        cookie_url = QUrl()
        cookie_url.setScheme("https" if cookie.secure else "http")
        cookie_url.setHost(cookie.domain.lstrip("."))
        cookie_url.setPath(cookie.path or "/")
        jar.setCookiesFromUrl([qcookie], cookie_url)
    return jar


class ScrapeWorker(QThread):
    """Run VideoScraper.scrape on a background thread."""

    log = pyqtSignal(str)
    finished_with_results = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(
        self,
        url: str,
        parent: Optional[QObject] = None,
        x_auth_browser: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        self.url = url
        self.x_auth_browser = x_auth_browser
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()
        self.requestInterruption()

    def run(self) -> None:
        scraper = VideoScraper()
        try:
            results = scraper.scrape(
                self.url,
                progress=lambda message: None if self._cancel_event.is_set() else self.log.emit(message),
                x_auth_browser=self.x_auth_browser,
                cancel_event=self._cancel_event,
            )
            if not self._cancel_event.is_set():
                self.finished_with_results.emit(results)
        except Exception as exc:  # noqa: BLE001
            if not self._cancel_event.is_set():
                self.failed.emit(str(exc))


def _thumbnail_url_allowed(url: QUrl) -> bool:
    return (url.isValid() and url.scheme().lower() in {"http", "https"}
            and bool(url.host()) and not url.userInfo())


class _ThumbnailFetch(QObject):
    """Download thumbnail bytes asynchronously on the GUI thread.

    PyQt invalidates the wrapper of a C++-created QObject, such as a
    QNetworkReply, from a monitor that lives on the GUI thread. When a reply
    is destroyed on a worker thread, that notice arrives through a queued
    call; a newer reply can occupy the same address by then, so its live
    wrapper is invalidated, the next slot raises RuntimeError and PyQt aborts
    the process. Keeping every reply on the GUI thread removes that delay,
    and QNetworkAccessManager is asynchronous, so it needs no thread of its own.
    """

    done = pyqtSignal(object, object)  # (bytes or None, Exception or None)

    def __init__(self, worker: "ThumbnailWorker") -> None:
        super().__init__(worker)
        self._worker = worker
        self._manager = None
        self._reply = None
        self._url = QUrl()
        self._requests = 0
        self._payload = bytearray()
        self._finished = False
        self._budget_timer = QTimer(self)
        self._budget_timer.setInterval(25)
        self._budget_timer.timeout.connect(self._check_budget)
        # cancel() may run inside a reply slot; observe it on the next turn.
        self._poke_timer = QTimer(self)
        self._poke_timer.setSingleShot(True)
        self._poke_timer.setInterval(0)
        self._poke_timer.timeout.connect(self._check_budget)

    def begin(self) -> None:
        try:
            self._worker._check_cancelled(self._worker._deadline)
            url = QUrl(self._worker.url)
            if not _thumbnail_url_allowed(url):
                raise ValueError("Thumbnail URL must use HTTP(S) without credentials")
            self._manager = QNetworkAccessManager(self)
            self._manager.setCookieJar(thumbnail_cookiejar(
                self._manager, self._worker.browser_cookies, self._worker.cookies))
            self._budget_timer.start()
            self._request(url)
        except Exception as exc:  # noqa: BLE001
            self._finish(None, exc)

    def poke(self) -> None:
        self._poke_timer.start()

    def _check_budget(self) -> bool:
        if self._finished:
            return False
        try:
            self._worker._check_cancelled(self._worker._deadline)
        except Exception as exc:  # noqa: BLE001
            self._finish(None, exc)
            return False
        return True

    def _request(self, url: QUrl) -> None:
        # Qt automatic redirects can carry an already-prepared Cookie header
        # to another host. A fresh request per hop asks the jar to scope it
        # again, and never reuses the previous request's derived headers.
        self._requests += 1
        self._url = url
        self._payload = bytearray()
        request = QNetworkRequest(url)
        request.setTransferTimeout(3000)
        request.setMaximumRedirectsAllowed(5)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        headers = build_request_headers(self._worker.referer)
        headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
        headers["Accept-Encoding"] = "identity"
        for name, value in headers.items():
            request.setRawHeader(name.encode(), value.encode())
        reply = self._manager.get(request)
        reply.setReadBufferSize(64 * 1024)
        self._reply = reply
        # Each slot names its reply, so signals from a released hop are ignored.
        reply.metaDataChanged.connect(lambda: self._on_headers(reply))
        reply.readyRead.connect(lambda: self._on_ready_read(reply))
        reply.finished.connect(lambda: self._on_finished(reply))

    def _on_headers(self, reply) -> None:
        if reply is not self._reply or not self._check_budget():
            return
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if status is not None and 300 <= int(status) < 400:
            location = bytes(reply.rawHeader(b"Location"))
            if location:
                self._follow(self._url.resolved(QUrl.fromEncoded(location)))
            return
        encoding = bytes(reply.rawHeader(b"Content-Encoding")).decode("ascii", "replace").lower()
        if encoding not in {"", "identity"}:
            self._finish(None, ValueError("Compressed thumbnail transport is not supported"))
            return
        content_type = bytes(reply.rawHeader(b"Content-Type")).decode("ascii", "replace").split(";", 1)[0].strip().lower()
        if content_type and not (content_type.startswith("image/") or content_type == "application/octet-stream"):
            self._finish(None, ValueError("Thumbnail response is not an image"))
            return
        try:
            length = int(bytes(reply.rawHeader(b"Content-Length")) or b"0")
        except ValueError:
            length = 0
        if length > THUMB_MAX_BYTES:
            self._finish(None, ValueError("Thumbnail exceeds the image byte limit"))

    def _on_ready_read(self, reply) -> None:
        self._on_headers(reply)
        if reply is not self._reply:
            return
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if status is not None and 300 <= int(status) < 400:
            return
        while reply is self._reply and reply.bytesAvailable():
            chunk = bytes(reply.read(min(32 * 1024, THUMB_MAX_BYTES - len(self._payload) + 1)))
            if not chunk or reply is not self._reply:
                break
            if len(self._payload) + len(chunk) > THUMB_MAX_BYTES:
                self._finish(None, ValueError("Thumbnail exceeds the image byte limit"))
                return
            self._payload.extend(chunk)

    def _on_finished(self, reply) -> None:
        if reply is not self._reply:
            return
        self._on_ready_read(reply)
        if reply is not self._reply:
            return
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self._finish(None, RuntimeError(reply.errorString()))
            return
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if status is None or not 200 <= int(status) < 300:
            self._finish(None, ValueError("Thumbnail server returned an unsuccessful response"))
            return
        self._finish(bytes(self._payload), None)

    def _follow(self, target: QUrl) -> None:
        self._release_reply()
        if (not _thumbnail_url_allowed(target)
                or (self._url.scheme().lower() == "https" and target.scheme().lower() != "https")):
            self._finish(None, ValueError("Thumbnail redirect has an unsafe destination"))
        elif self._requests >= THUMB_MAX_REQUESTS:
            self._finish(None, ValueError("Thumbnail exceeded the redirect limit"))
        elif self._check_budget():
            self._request(target)

    def _release_reply(self) -> None:
        # Clear first: abort() emits finished synchronously into our slots.
        reply, self._reply = self._reply, None
        if reply is not None:
            reply.abort()
            reply.deleteLater()

    def _finish(self, data: Optional[bytes], error: Optional[Exception]) -> None:
        if self._finished:
            return
        self._finished = True
        self._budget_timer.stop()
        self._poke_timer.stop()
        self._release_reply()
        manager, self._manager = self._manager, None
        if manager is not None:
            manager.deleteLater()
        self.done.emit(data, error)


class ThumbnailWorker(QThread):
    """Download a thumbnail on the GUI thread, then decode it on this thread.

    Sends a browser-like header set (with optional Referer) so CDNs that
    hot-link-protect their image hosts will still serve the file. Call
    start() from the thread that owns the worker (the GUI thread): the
    asynchronous fetch runs there (see _ThumbnailFetch), and only the
    untrusted image decode runs on the worker thread. finished is emitted
    in every outcome, including failure or cancellation during the fetch.
    """

    ready = pyqtSignal(int, bytes)
    failed = pyqtSignal(int, str)

    def __init__(
        self,
        index: int,
        url: str,
        referer: Optional[str] = None,
        parent: Optional[QObject] = None,
        cookies: Optional[CookieJar] = None,
        browser_cookies: Optional[list[dict]] = None,
    ) -> None:
        super().__init__(parent)
        self.index = index
        self.url = url
        self.referer = referer
        self.cookies = cookies
        self.browser_cookies = browser_cookies or []
        self._cancel_event = threading.Event()
        self._deadline = 0.0
        self._priority = QThread.Priority.InheritPriority
        self._owner_ident = threading.get_ident()
        self._fetch: Optional[_ThumbnailFetch] = None
        self._fetched: Optional[bytes] = None
        self._fetch_error: Optional[Exception] = None

    @property
    def fetch_pending(self) -> bool:
        return self._fetch is not None

    def start(self, priority: QThread.Priority = QThread.Priority.InheritPriority) -> None:
        if self._fetch is not None or self.isRunning():
            return
        self._priority = priority
        self._owner_ident = threading.get_ident()
        self._deadline = time.monotonic() + THUMB_TOTAL_SECONDS
        self._fetch = _ThumbnailFetch(self)
        self._fetch.done.connect(self._on_fetch_done)
        self._fetch.begin()

    def cancel(self) -> None:
        self._cancel_event.set()
        self.requestInterruption()
        fetch = self._fetch
        if fetch is not None and threading.get_ident() == self._owner_ident:
            fetch.poke()

    def _check_cancelled(self, deadline: float) -> None:
        if self._cancel_event.is_set() or self.isInterruptionRequested():
            raise InterruptedError("Thumbnail cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Thumbnail exceeded its total time limit")

    def _on_fetch_done(self, data: Optional[bytes], error: Optional[Exception]) -> None:
        fetch, self._fetch = self._fetch, None
        if fetch is not None:
            fetch.deleteLater()
        self._fetched, self._fetch_error = data, error
        # Run even after a failed or cancelled fetch so finished always fires.
        super().start(self._priority)

    def run(self) -> None:
        data, error = self._fetched, self._fetch_error
        self._fetched = self._fetch_error = None
        try:
            if error is not None:
                raise error
            if data is None:
                raise RuntimeError("Thumbnail fetch did not run")
            thumbnail = decode_thumbnail(data)
            self._check_cancelled(self._deadline)
            self.ready.emit(self.index, thumbnail)
        except Exception as exc:  # noqa: BLE001
            if not self._cancel_event.is_set() and not self.isInterruptionRequested():
                self.failed.emit(self.index, str(exc))


class DownloadWorker(QThread):
    """Supervise isolated download jobs without blocking the GUI controls."""

    item_started = pyqtSignal(int, object)
    item_progress = pyqtSignal(int, object)
    item_finished = pyqtSignal(int, str)
    item_failed = pyqtSignal(int, str)
    item_state = pyqtSignal(int, str)
    item_control_error = pyqtSignal(int, str)
    all_done = pyqtSignal()

    def __init__(
        self,
        items: List[VideoItem],
        output_dir: Path,
        quality: str = "best",
        parallel_connections: int = 8,
        lane_limiter: Optional[DownloadLaneLimiter] = None,
        parent: Optional[QObject] = None,
        audio_format: str = "original",
    ) -> None:
        super().__init__(parent)
        self.items = items
        self.output_dir = output_dir
        self.quality = quality
        self.audio_format = audio_format
        self.parallel_connections = parallel_connections
        self.lane_limiter = lane_limiter
        # Compatibility input for older callers only. Every isolated child
        # owns its own parallel_connections limit; there is no shared cap.
        self._cancelled = False
        self._pause_requested = False
        self._done = False
        self._phase = "starting"
        self._current_index = 0
        self._job: Optional[DownloadJob] = None
        self._job_lock = threading.RLock()

    def pause(self) -> bool:
        with self._job_lock:
            if self._cancelled or self._done or self._phase in {
                "finalizing", "completed", "failed", "cancelled",
            }:
                return False
            self._pause_requested = True
            job, index = self._job, self._current_index
        if job is not None:
            accepted = job.pause()
            if not accepted:
                with self._job_lock:
                    self._pause_requested = False
            return accepted
        self.item_state.emit(index, "pausing")
        return True

    def resume(self) -> bool:
        with self._job_lock:
            if self._cancelled or self._done or not self._pause_requested:
                return False
            self._pause_requested = False
            job, index = self._job, self._current_index
        if job is not None:
            return job.resume()
        self.item_state.emit(index, "resuming")
        return True

    def cancel(self, keep_partial: bool = False) -> bool:
        """Cancel unfinished output; pause is the operation that retains it.

        The legacy argument remains accepted for callers. GUI cancellation no
        longer exports a partial result: the complete private job is removed.
        Process shutdown happens in run(), never in this GUI-thread method.
        """
        with self._job_lock:
            if self._done or (
                self._phase == "completed" and self._current_index == len(self.items) - 1
            ):
                return False
            job, index = self._job, self._current_index
            if job is None:
                self._cancelled = True
                self._pause_requested = False
        if job is not None:
            if job.cancel() is False:
                return False
            with self._job_lock:
                self._cancelled = True
                self._pause_requested = False
        else:
            self.item_state.emit(index, "cancelling")
        return True

    def run(self) -> None:
        try:
            for index, item in enumerate(self.items):
                with self._job_lock:
                    self._current_index = index
                    self._phase = "starting"
                    cancelled = self._cancelled
                if cancelled:
                    self.item_state.emit(index, "cancelled")
                    self.item_failed.emit(index, "Cancelled")
                    break
                self.item_started.emit(index, item)

                def on_progress(progress: DownloadProgress, idx: int = index) -> None:
                    self.item_progress.emit(idx, progress)

                def on_state(state: str, idx: int = index) -> None:
                    with self._job_lock:
                        self._phase = state
                    self.item_state.emit(idx, state)

                job = DownloadJob(
                    item,
                    self.output_dir,
                    quality=self.quality,
                    audio_format=self.audio_format,
                    parallel_connections=min(16, self.parallel_connections),
                    on_progress=on_progress,
                    on_state=on_state,
                )
                with self._job_lock:
                    self._job = job
                    cancelled, paused = self._cancelled, self._pause_requested
                if cancelled:
                    job.cancel()
                elif paused:
                    job.pause()
                try:
                    final_path = job.run()
                    # A successful return means final publication was committed
                    # before any late cancellation request could be accepted.
                    self.item_finished.emit(index, str(final_path))
                except DownloadCancelled:
                    on_state("cancelled")
                    self.item_failed.emit(index, "Cancelled")
                except DownloadCleanupError as exc:
                    on_state("failed")
                    self.item_control_error.emit(index, str(exc))
                except Exception as exc:  # noqa: BLE001
                    with self._job_lock:
                        cancelled = self._cancelled
                    on_state("cancelled" if cancelled else "failed")
                    self.item_failed.emit(index, "Cancelled" if cancelled else str(exc))
                finally:
                    with self._job_lock:
                        self._job = None
                if self._cancelled:
                    break
        finally:
            with self._job_lock:
                self._done = True
            self.all_done.emit()
