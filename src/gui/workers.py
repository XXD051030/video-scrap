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

from PyQt6.QtCore import (QByteArray, QBuffer, QCoreApplication, QDateTime, QEvent, QEventLoop,
                          QIODevice, QObject, QSize, QThread, QTimer, Qt, QUrl, pyqtSignal)
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
            else:
                _BACKGROUND_WORKERS.discard(worker)
                worker.deleteLater()
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


class ThumbnailWorker(QThread):
    """Download a thumbnail image and emit raw bytes.

    Sends a browser-like header set (with optional Referer) so CDNs that
    hot-link-protect their image hosts will still serve the file.
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

    def cancel(self) -> None:
        self._cancel_event.set()
        self.requestInterruption()

    def _check_cancelled(self, deadline: float) -> None:
        if self._cancel_event.is_set() or self.isInterruptionRequested():
            raise InterruptedError("Thumbnail cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Thumbnail exceeded its total time limit")

    def _fetch_image(self, deadline: float) -> bytes:
        """Use abortable Qt I/O so the deadline also covers DNS and headers."""
        self._check_cancelled(deadline)
        url = QUrl(self.url)
        if (not url.isValid() or url.scheme().lower() not in {"http", "https"}
                or not url.host() or url.userInfo()):
            raise ValueError("Thumbnail URL must use HTTP(S) without credentials")
        manager = QNetworkAccessManager()
        manager.setCookieJar(thumbnail_cookiejar(manager, self.browser_cookies, self.cookies))
        try:
            for _hop in range(6):
                self._check_cancelled(deadline)
                data, target = self._fetch_reply(manager, url, deadline)
                if target is None:
                    return data
                if (not target.isValid() or target.scheme().lower() not in {"http", "https"}
                        or not target.host() or target.userInfo()
                        or (url.scheme().lower() == "https" and target.scheme().lower() != "https")):
                    raise ValueError("Thumbnail redirect has an unsafe destination")
                url = target
            raise ValueError("Thumbnail exceeded the redirect limit")
        finally:
            manager.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def _fetch_reply(self, manager, url: QUrl, deadline: float) -> tuple[bytes, Optional[QUrl]]:
        # Qt automatic redirects can carry an already-prepared Cookie header
        # to another host. A fresh request per hop asks the jar to scope it
        # again, and never reuses the previous request's derived headers.
        request = QNetworkRequest(url)
        request.setTransferTimeout(3000)
        request.setMaximumRedirectsAllowed(5)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        headers = build_request_headers(self.referer)
        headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
        headers["Accept-Encoding"] = "identity"
        for name, value in headers.items():
            request.setRawHeader(name.encode(), value.encode())
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(25)
        payload = bytearray()
        errors = []
        redirect_target = []
        reply = manager.get(request)
        reply.setReadBufferSize(64 * 1024)

        def fail(error):
            if not errors:
                errors.append(error)
            reply.abort()
            loop.quit()

        def check_budget():
            try:
                self._check_cancelled(deadline)
            except Exception as exc:
                fail(exc)

        def check_headers():
            check_budget()
            if errors:
                return
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if status is not None and 300 <= int(status) < 400:
                payload.clear()
                location = bytes(reply.rawHeader(b"Location"))
                if location and not redirect_target:
                    redirect_target.append(url.resolved(QUrl.fromEncoded(location)))
                    reply.abort()
                    loop.quit()
                return
            encoding = bytes(reply.rawHeader(b"Content-Encoding")).decode("ascii", "replace").lower()
            if encoding not in {"", "identity"}:
                fail(ValueError("Compressed thumbnail transport is not supported"))
                return
            content_type = bytes(reply.rawHeader(b"Content-Type")).decode("ascii", "replace").split(";", 1)[0].strip().lower()
            if content_type and not (content_type.startswith("image/") or content_type == "application/octet-stream"):
                fail(ValueError("Thumbnail response is not an image"))
                return
            try:
                length = int(bytes(reply.rawHeader(b"Content-Length")) or b"0")
            except ValueError:
                length = 0
            if length > THUMB_MAX_BYTES:
                fail(ValueError("Thumbnail exceeds the image byte limit"))

        def read_available():
            check_headers()
            if errors:
                return
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if status is not None and 300 <= int(status) < 400:
                return
            while reply.bytesAvailable():
                chunk = bytes(reply.read(min(32 * 1024, THUMB_MAX_BYTES - len(payload) + 1)))
                if not chunk:
                    break
                if len(payload) + len(chunk) > THUMB_MAX_BYTES:
                    fail(ValueError("Thumbnail exceeds the image byte limit"))
                    return
                payload.extend(chunk)

        reply.metaDataChanged.connect(check_headers)
        reply.readyRead.connect(read_available)
        reply.finished.connect(loop.quit)
        timer.timeout.connect(check_budget)
        timer.start()
        try:
            if not reply.isFinished():
                loop.exec()
            timer.stop()
            self._check_cancelled(deadline)
            if errors:
                raise errors[0]
            if redirect_target:
                return b"", redirect_target[0]
            read_available()
            if errors:
                raise errors[0]
            if reply.error() != QNetworkReply.NetworkError.NoError:
                raise RuntimeError(reply.errorString())
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if status is None or not 200 <= int(status) < 300:
                raise ValueError("Thumbnail server returned an unsuccessful response")
            return bytes(payload), None
        finally:
            timer.stop()
            reply.abort()
            reply.deleteLater()

    def run(self) -> None:
        try:
            deadline = time.monotonic() + THUMB_TOTAL_SECONDS
            thumbnail = decode_thumbnail(self._fetch_image(deadline))
            self._check_cancelled(deadline)
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
