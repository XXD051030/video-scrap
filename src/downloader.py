"""Video downloader built on top of yt-dlp.

Supports both yt-dlp recognized URLs and plain direct media links via
yt-dlp's generic extractor. Emits progress events through callbacks so the
GUI thread can stay responsive.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from dataclasses import dataclass, replace
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Callable, Dict, Optional
from urllib.parse import urljoin, urlparse

import requests
import yt_dlp
from yt_dlp.aes import aes_cbc_decrypt_bytes as _yt_dlp_aes_cbc_decrypt_bytes

try:
    from Crypto.Cipher import AES as _CryptoAES
except Exception:  # noqa: BLE001
    _CryptoAES = None

from .net import build_session
from .ffmpeg import resolve_ffmpeg
from .parallel_downloader import ParallelDownloader, _publish_unique
from .scraper import VideoItem, is_x_post_url
from .utils import safe_filename


QUALITY_FORMATS: Dict[str, str] = {
    "best": "bestvideo*+bestaudio/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "audio only": "bestaudio/best",
}

AUDIO_FORMATS = ("original", "mp3", "m4a")


def _audio_output_format(codec: str, audio_format: str) -> tuple[str, list[str]]:
    """Choose an audio container without silently recompressing original audio."""
    if audio_format == "mp3":
        return "mp3", (["-c:a", "copy"] if codec == "mp3"
                       else ["-c:a", "libmp3lame", "-q:a", "2"])
    if audio_format == "m4a":
        return "m4a", (["-c:a", "copy"] if codec == "aac"
                       else ["-c:a", "aac", "-b:a", "192k"])
    extensions = {
        "aac": "m4a", "alac": "m4a", "mp3": "mp3", "opus": "opus",
        "vorbis": "ogg", "flac": "flac", "ac3": "ac3", "eac3": "eac3",
        "dts": "dts", "wmav1": "wma", "wmav2": "wma",
    }
    # WAV supports these PCM representations. Uncommon codecs use Matroska;
    # if it cannot hold the source codec, fail rather than change its quality.
    wav_codecs = {"pcm_u8", "pcm_s16le", "pcm_s24le", "pcm_s32le",
                  "pcm_f32le", "pcm_f64le", "pcm_alaw", "pcm_mulaw"}
    extension = "wav" if codec in wav_codecs else extensions.get(codec, "mka")
    return extension, ["-c:a", "copy"]


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def build_request_headers(referer: Optional[str]) -> Dict[str, str]:
    """Browser-like header set used for both downloads and proxying."""
    headers: Dict[str, str] = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    }
    if referer:
        headers["Referer"] = referer
        parsed = urlparse(referer)
        if parsed.scheme and parsed.netloc:
            headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    return headers


def _x_download_url(url: str) -> str:
    """Use the post URL so playlist selection is not bypassed by /video/N."""
    parsed = urlparse(url)
    path = re.sub(r"/(?:video|photo)/\d+/?$", "", parsed.path).rstrip("/")
    return parsed._replace(path=path, query="", fragment="").geturl()


def _x_output_stem(item: VideoItem, playlist_index: int) -> str:
    """Keep different videos in one post (and posts with equal titles) separate."""
    post_id = re.search(r"/(?:status|statuses)/(\d+)", urlparse(item.url).path)
    if post_id is None:
        raise ValueError("Invalid X post URL")
    return f"{safe_filename(item.title, max_len=80)}_x_{post_id.group(1)}_video_{playlist_index}"


@dataclass
class DownloadProgress:
    """Event payload reported during a download."""

    status: str
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed: float = 0.0
    eta: Optional[int] = None
    filename: Optional[str] = None
    message: str = ""


ProgressHandler = Callable[[DownloadProgress], None]


def _aes_backend_name() -> str:
    return "pycryptodome" if _CryptoAES is not None else "yt-dlp-python"


def _aes_cbc_decrypt_bytes(data: bytes, key: bytes, iv: bytes) -> bytes:
    if _CryptoAES is not None:
        return _CryptoAES.new(key, _CryptoAES.MODE_CBC, iv).decrypt(data)
    return _yt_dlp_aes_cbc_decrypt_bytes(data, key, iv)


class DownloadLaneLimiter:
    """Shared dynamic cap for concurrent segment/network requests."""

    def __init__(self, limit: int = 8) -> None:
        self._limit = max(1, int(limit))
        self._active = 0
        self._cond = threading.Condition()

    def set_limit(self, limit: int) -> None:
        with self._cond:
            self._limit = max(1, int(limit))
            self._cond.notify_all()

    @property
    def limit(self) -> int:
        with self._cond:
            return self._limit

    def wake(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def acquire(self, should_cancel: Callable[[], bool]) -> None:
        with self._cond:
            while self._active >= self._limit:
                if should_cancel():
                    raise RuntimeError("Download cancelled")
                self._cond.wait(timeout=0.25)
            if should_cancel():
                raise RuntimeError("Download cancelled")
            self._active += 1

    def release(self) -> None:
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()


@dataclass
class _HlsSegment:
    index: int
    url: str
    duration: float
    key: Optional[bytes]
    iv: Optional[bytes]
    range_start: Optional[int] = None
    range_length: Optional[int] = None


class _HlsByteRangeError(RuntimeError):
    """A ranged playlist cannot safely use an unvalidated fallback backend."""


@dataclass
class _HlsMasterContext:
    url: str
    text: str


class _HlsFallbackError(RuntimeError):
    """An inspected ordinary media playlist may use a fallback backend."""

    def __init__(
        self,
        message: str,
        manifest_url: str,
        master_context: Optional[_HlsMasterContext] = None,
    ) -> None:
        super().__init__(message)
        self.manifest_url = manifest_url
        self.master_context = master_context


def _is_hls_tag(line: str, tag: str) -> bool:
    return bool(re.match(re.escape(tag) + r"(?=[:\s]|$)", line))


def _hls_has_byte_ranges(text: str) -> bool:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if _is_hls_tag(line, "#EXT-X-BYTERANGE"):
            return True
        if _is_hls_tag(line, "#EXT-X-MAP") and re.search(
            r"(?:^|[:,])\s*BYTERANGE(?=[=,\s]|$)", line
        ):
            return True
    return False


class VideoDownloader:
    """Routes direct .mp4 URLs to a parallel downloader, the rest to yt-dlp."""

    def __init__(
        self,
        output_dir: Path,
        parallel_connections: int = 8,
        lane_limiter: Optional[DownloadLaneLimiter] = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.parallel_connections = parallel_connections
        self._lane_limiter = lane_limiter
        self._parallel: Optional[ParallelDownloader] = None
        self._ffmpeg_proc: Optional[subprocess.Popen[str]] = None
        self._cancelled = False
        self._partial_on_cancel = False
        self._session = build_session(pool=max(self.parallel_connections, 8))

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass

    def cancel(self, keep_partial: bool = False) -> None:
        self._cancelled = True
        self._partial_on_cancel = self._partial_on_cancel or keep_partial
        if self._parallel is not None:
            self._parallel.cancel()
        proc = self._ffmpeg_proc
        if proc is not None and proc.poll() is None:
            # Snapshot into a local first: the download thread may null out
            # self._ffmpeg_proc between the check and the call, which would
            # otherwise raise AttributeError on None.
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._lane_limiter is not None:
            self._lane_limiter.wake()

    def download(
        self,
        item: VideoItem,
        quality: str = "best",
        on_progress: Optional[ProgressHandler] = None,
        audio_format: str = "original",
    ) -> Path:
        """Keep each task's backend files private until its result is ready."""
        audio_only = quality == "audio only"
        if audio_only:
            if audio_format not in AUDIO_FORMATS:
                raise ValueError(f"Unsupported audio format: {audio_format}")
            if not resolve_ffmpeg():
                raise RuntimeError("FFmpeg is required for audio-only downloads")
        destination = self.output_dir
        final_progress: Optional[DownloadProgress] = None
        with tempfile.TemporaryDirectory(
            prefix=".video-scrap-job-", dir=destination, ignore_cleanup_errors=True
        ) as work_dir:
            self.output_dir = Path(work_dir)

            def relay(progress: DownloadProgress) -> None:
                nonlocal final_progress
                if progress.status in {"completed", "partial"}:
                    final_progress = progress
                elif on_progress is not None:
                    on_progress(progress)

            try:
                staged = self._download_to_stage(
                    item, quality, relay if on_progress is not None or audio_only else None
                )
                if (
                    not staged.is_file()
                    or staged.resolve().parent != self.output_dir.resolve()
                ):
                    raise RuntimeError("Download did not produce a finished file")
                if audio_only:
                    keep_partial = bool(
                        final_progress is not None
                        and final_progress.status == "partial"
                        and self._partial_on_cancel
                    )
                    staged = self._extract_audio(
                        staged, audio_format, on_progress, allow_cancelled=keep_partial
                    )
                    if self._cancelled and not keep_partial:
                        raise RuntimeError("Download cancelled")
                    size = staged.stat().st_size
                    final_progress = DownloadProgress(
                        status="partial" if keep_partial else "completed",
                        downloaded_bytes=size,
                        total_bytes=size,
                        filename=str(staged),
                        message=(
                            f"{final_progress.message}; audio saved ({audio_format})"
                            if keep_partial and final_progress is not None
                            else f"Audio saved ({audio_format})"
                        ),
                    )
                published = _publish_unique(staged, destination / staged.name)
            finally:
                self.output_dir = destination

        if on_progress is not None and final_progress is not None:
            on_progress(replace(final_progress, filename=str(published)))
        return published

    def _run_audio_ffmpeg(
        self, command: list[str], *, allow_cancelled: bool = False
    ) -> tuple[int, str]:
        """Keep probing and conversion cancellable, including on Windows."""
        if self._cancelled and not allow_cancelled:
            raise RuntimeError("Download cancelled")
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._ffmpeg_proc = proc
        try:
            while True:
                if self._cancelled and not allow_cancelled:
                    raise RuntimeError("Download cancelled")
                try:
                    _stdout, stderr = proc.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if self._cancelled and not allow_cancelled:
                raise RuntimeError("Download cancelled")
            return proc.returncode, stderr or ""
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
            if self._ffmpeg_proc is proc:
                self._ffmpeg_proc = None

    def _extract_audio(
        self,
        source: Path,
        audio_format: str,
        on_progress: Optional[ProgressHandler],
        *,
        allow_cancelled: bool = False,
    ) -> Path:
        """Publish only audio, even when the source has no independent audio URL."""
        ffmpeg = resolve_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("FFmpeg is required for audio-only downloads")
        if on_progress is not None:
            on_progress(DownloadProgress(
                status="finished", filename=str(source),
                message=("Extracting original audio..." if audio_format == "original"
                         else f"Preparing {audio_format.upper()} audio..."),
            ))
        # The bundled tool is FFmpeg, not FFprobe. Input inspection reads the
        # stream headers and exits without decoding the complete media file.
        code, probe = self._run_audio_ffmpeg(
            [ffmpeg, "-hide_banner", "-nostdin", "-i", str(source)],
            allow_cancelled=allow_cancelled,
        )
        match = re.search(
            r"^\s*Stream #\d+:\d+(?:\[[^\]\r\n]+\])?"
            r"(?:\([^\)\r\n]+\))?: Audio:\s+([a-zA-Z0-9_]+)",
            probe, re.MULTILINE,
        )
        if code != 1 or match is None:
            raise RuntimeError("No readable audio track was found in the downloaded media")
        extension, codec_args = _audio_output_format(match.group(1).lower(), audio_format)
        target = source.with_suffix(f".{extension}")
        # Never give FFmpeg the same input/output path. Replacement happens
        # only after success and is confined to this task's private directory.
        with tempfile.TemporaryDirectory(prefix=".audio-", dir=source.parent) as directory:
            converted = Path(directory) / target.name
            code, stderr = self._run_audio_ffmpeg(
                [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                 "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
                 *codec_args, str(converted)],
                allow_cancelled=allow_cancelled,
            )
            if code or not converted.is_file() or converted.stat().st_size == 0:
                detail = "\n".join(stderr.splitlines()[-8:])
                raise RuntimeError(f"Audio extraction failed: {detail or 'FFmpeg produced no audio file'}")
            if self._cancelled and not allow_cancelled:
                raise RuntimeError("Download cancelled")
            converted.replace(target)
        return target

    def _discard_staged_outputs(self) -> None:
        """Remove an unsuccessful HLS backend's files before trying another."""
        for path in self.output_dir.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    def _download_to_stage(
        self,
        item: VideoItem,
        quality: str,
        on_progress: Optional[ProgressHandler],
    ) -> Path:
        self._cancelled = False
        self._partial_on_cancel = False
        # HLS / DASH manifests are not single files - they reference dozens
        # of segment URLs. yt-dlp knows how to fetch + concat them, so route
        # through it. Only single-file direct URLs use the parallel path.
        if item.is_direct and item.is_hls:
            try:
                if quality == "audio only":
                    return self._download_hls_parallel(item, on_progress, audio_only=True)
                return self._download_hls_parallel(item, on_progress)
            except _HlsByteRangeError:
                # yt-dlp's HTTP backend can accept a whole-file 200 or an
                # incorrect 206 for a Range request. Do not publish those
                # bytes by retrying a ranged playlist with that backend.
                raise
            except _HlsFallbackError as exc:
                if self._cancelled:
                    raise
                # Keep every backend on the same inspected media playlist.
                # Reusing a master URL can select an unchecked ranged variant.
                fallback_url = exc.manifest_url
                if exc.master_context is not None:
                    # Preserve an external audio rendition only after checking
                    # every media choice a backend could make from the master.
                    self._validate_hls_master_fallback(exc.master_context, item)
                    fallback_url = exc.master_context.url
                fallback_item = replace(item, url=fallback_url)
                self._discard_staged_outputs()
                if on_progress is not None:
                    on_progress(
                        DownloadProgress(
                            status="downloading",
                            message="Parallel HLS failed; retrying with yt-dlp...",
                        )
                    )
                try:
                    return self._download_hls_ytdlp(fallback_item, on_progress)
                except Exception:
                    if self._cancelled:
                        raise
                    self._discard_staged_outputs()
                    ffmpeg = resolve_ffmpeg()
                    if not ffmpeg:
                        raise
                    if on_progress is not None:
                        on_progress(
                            DownloadProgress(
                                status="downloading",
                                message="Native HLS failed; retrying with ffmpeg...",
                            )
                        )
                    return self._download_hls_ffmpeg(fallback_item, ffmpeg, on_progress)
        if item.is_direct and not item.is_hls:
            return self._download_direct(item, on_progress)

        fmt = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["best"])
        is_x_item = is_x_post_url(item.url)
        x_index = item.x_playlist_index if is_x_item else None
        if x_index is not None and x_index < 1:
            raise ValueError("X video index must be positive")
        title = (
            _x_output_stem(item, x_index)
            if x_index is not None
            else safe_filename(item.title)
        )
        outtmpl = str(self.output_dir / f"{title}.%(ext)s")

        last_path: Dict[str, Optional[str]] = {"path": None}
        final_path: Dict[str, Optional[str]] = {"path": None}

        def post_hook(filename: str) -> None:
            # yt-dlp calls this after merging and moving the finished file.
            # The progress hook can still point to a temporary format file.
            final_path["path"] = filename

        def hook(data: Dict) -> None:
            if quality == "audio only" and self._cancelled:
                raise RuntimeError("Download cancelled")
            status = data.get("status", "")
            if status == "downloading":
                progress = DownloadProgress(
                    status="downloading",
                    downloaded_bytes=int(data.get("downloaded_bytes") or 0),
                    total_bytes=int(
                        data.get("total_bytes")
                        or data.get("total_bytes_estimate")
                        or 0
                    ),
                    speed=float(data.get("speed") or 0.0),
                    eta=data.get("eta"),
                    filename=data.get("filename"),
                )
                last_path["path"] = data.get("filename") or last_path["path"]
            elif status == "finished":
                progress = DownloadProgress(
                    status="finished",
                    filename=data.get("filename"),
                    message="Post-processing...",
                )
                last_path["path"] = data.get("filename") or last_path["path"]
            elif status == "error":
                progress = DownloadProgress(
                    status="error",
                    message=str(data.get("error") or "Download error"),
                )
            else:
                return
            if on_progress:
                on_progress(progress)

        http_headers = build_request_headers(item.referer)

        ydl_opts = {
            "outtmpl": outtmpl,
            "format": fmt,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [hook],
            "post_hooks": [post_hook],
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": 2,
            "retries": 5,
            "fragment_retries": 10,
            "extractor_retries": 5,
            "socket_timeout": 60,
            "http_headers": http_headers,
        }
        ffmpeg = resolve_ffmpeg()
        # Keep yt-dlp's existing PATH/ffprobe discovery when the system tool
        # is available; non-PATH fallback binaries need their exact location.
        if ffmpeg and not shutil.which("ffmpeg"):
            ydl_opts["ffmpeg_location"] = ffmpeg

        if is_x_item:
            ydl_opts["overwrites"] = False
            if item.x_auth_browser:
                ydl_opts["cookiesfrombrowser"] = (item.x_auth_browser,)
            if x_index is not None:
                ydl_opts["playlist_items"] = str(x_index)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.download([_x_download_url(item.url) if is_x_item else item.url])

        if result and not is_x_item:
            raise RuntimeError("yt-dlp download failed")
        completed = final_path["path"] or last_path["path"]
        if is_x_item:
            if result or not completed or not Path(completed).is_file():
                raise RuntimeError("Selected X video did not produce a finished file")
        if completed and Path(completed).is_file():
            last_path["path"] = completed

        if on_progress:
            on_progress(
                DownloadProgress(
                    status="completed",
                    filename=last_path["path"],
                    message="Done",
                )
            )
        return Path(last_path["path"]) if last_path["path"] else self.output_dir

    def _download_hls_parallel(
        self,
        item: VideoItem,
        on_progress: Optional[ProgressHandler],
        *,
        audio_only: bool = False,
    ) -> Path:
        """Download HLS segments concurrently, then remux to mp4."""
        title = safe_filename(item.title)
        output_path = self.output_dir / f"{title}.mp4"
        headers = build_request_headers(item.referer)
        headers["Accept"] = "*/*"

        if on_progress is not None:
            on_progress(
                DownloadProgress(
                    status="downloading",
                    filename=str(output_path),
                    message="Reading HLS manifest...",
                )
            )

        try:
            if audio_only:
                manifest_url, manifest_text, master_context = self._fetch_hls_manifest(
                    item.url, headers, audio_only=True
                )
            else:
                manifest_url, manifest_text, master_context = self._fetch_hls_manifest(
                    item.url, headers
                )
        except _HlsByteRangeError:
            raise
        except Exception as exc:
            # Until a media playlist has been inspected, another backend
            # could fetch ranges and accept whole-file responses as segments.
            raise _HlsByteRangeError(
                f"Could not safely inspect the HLS media manifest: {exc}"
            ) from exc
        has_byte_ranges = _hls_has_byte_ranges(manifest_text)
        try:
            segments = self._parse_hls_segments(manifest_text, manifest_url, headers)
            if not segments:
                raise RuntimeError("HLS manifest did not contain media segments")
            ffmpeg = resolve_ffmpeg()
            if not ffmpeg:
                raise RuntimeError("ffmpeg is required for HLS remuxing")
            return self._download_hls_segments_parallel(
                item, on_progress, segments, headers, ffmpeg
            )
        except _HlsByteRangeError:
            raise
        except Exception as exc:
            if has_byte_ranges:
                raise _HlsByteRangeError(
                    f"HLS byte-range download failed safely: {exc}"
                ) from exc
            raise _HlsFallbackError(str(exc), manifest_url, master_context) from exc

    def _download_hls_segments_parallel(
        self,
        item: VideoItem,
        on_progress: Optional[ProgressHandler],
        segments: list[_HlsSegment],
        headers: Dict[str, str],
        ffmpeg: str,
    ) -> Path:
        """Keep the ordinary HLS transfer, cancellation and remux flow."""
        title = safe_filename(item.title)
        output_path = self.output_dir / f"{title}.mp4"

        total = len(segments)
        started = time.monotonic()
        transfer_started = time.monotonic()
        completed = 0
        downloaded_bytes = 0

        with tempfile.TemporaryDirectory(prefix="video_scraper_hls_") as tmp:
            tmp_dir = Path(tmp)
            paths = [tmp_dir / f"{idx:06d}.ts" for idx in range(total)]

            def fetch(seg: _HlsSegment) -> tuple[int, int]:
                if self._cancelled:
                    raise RuntimeError("Download cancelled")
                data = self._download_hls_segment(seg, headers)
                paths[seg.index].write_bytes(data)
                return seg.index, len(data)

            def contiguous_count() -> int:
                count = 0
                for path in paths:
                    try:
                        if path.exists() and path.stat().st_size > 0:
                            count += 1
                            continue
                    except OSError:
                        pass
                    break
                return count

            def merge_downloaded(count: int, destination: Path) -> float:
                merge_started = time.monotonic()
                concat_list = tmp_dir / f"{destination.stem}.concat.txt"
                self._write_ffmpeg_concat_list(paths[:count], concat_list)
                self._remux_hls_segments(ffmpeg, concat_list, destination)
                return time.monotonic() - merge_started

            def build_partial() -> Path:
                partial_count = contiguous_count()
                if partial_count <= 0:
                    raise RuntimeError(
                        "Download cancelled before any initial HLS segments finished"
                    )
                partial_path = self.output_dir / f"{title}.partial.mp4"
                if on_progress is not None:
                    on_progress(
                        DownloadProgress(
                            status="downloading",
                            downloaded_bytes=partial_count,
                            total_bytes=total,
                            filename=str(partial_path),
                            message=(
                                f"Merging partial file "
                                f"({partial_count}/{total} segments)..."
                            ),
                        )
                    )
                merge_seconds = merge_downloaded(partial_count, partial_path)
                if on_progress is not None:
                    on_progress(
                        DownloadProgress(
                            status="partial",
                            downloaded_bytes=partial_count,
                            total_bytes=total,
                            filename=str(partial_path),
                            message=(
                                f"Partial saved ({partial_count}/{total} segments); "
                                f"merge {merge_seconds:.1f}s"
                            ),
                        )
                    )
                return partial_path

            workers = max(1, min(self.parallel_connections, total))
            pool = ThreadPoolExecutor(max_workers=workers)
            futures = [pool.submit(fetch, seg) for seg in segments]
            make_partial = False
            pending_error: Optional[Exception] = None
            try:
                for fut in as_completed(futures):
                    if self._cancelled:
                        for pending in futures:
                            pending.cancel()
                        if self._partial_on_cancel:
                            make_partial = True
                            break
                        pending_error = RuntimeError("Download cancelled")
                        break
                    try:
                        _idx, size = fut.result()
                    except Exception as exc:
                        for pending in futures:
                            pending.cancel()
                        if self._cancelled and self._partial_on_cancel:
                            make_partial = True
                            break
                        pending_error = exc
                        break
                    completed += 1
                    downloaded_bytes += size
                    elapsed = max(time.monotonic() - started, 1e-3)
                    speed = downloaded_bytes / elapsed
                    eta = int((total - completed) / max(completed / elapsed, 1e-3))
                    if on_progress is not None:
                        on_progress(
                            DownloadProgress(
                                status="downloading",
                                downloaded_bytes=completed,
                                total_bytes=total,
                                speed=speed,
                                eta=eta,
                                filename=str(output_path),
                                message=f"Segment {completed}/{total}",
                            )
                        )
            finally:
                if self._cancelled or pending_error is not None:
                    for pending in futures:
                        pending.cancel()
                pool.shutdown(wait=True, cancel_futures=True)

            if make_partial:
                return build_partial()
            if pending_error is not None:
                raise pending_error

            if on_progress is not None:
                on_progress(
                    DownloadProgress(
                        status="downloading",
                        downloaded_bytes=total,
                        total_bytes=total,
                        filename=str(output_path),
                        message="Merging segments...",
                    )
                )
            transfer_seconds = time.monotonic() - transfer_started
            merge_seconds = merge_downloaded(total, output_path)

        if on_progress is not None:
            final_size = output_path.stat().st_size if output_path.exists() else 0
            on_progress(
                DownloadProgress(
                    status="completed",
                    downloaded_bytes=total,
                    total_bytes=total,
                    filename=str(output_path),
                    message=(
                        f"HLS timings: download {transfer_seconds:.1f}s, "
                        f"merge {merge_seconds:.1f}s, total "
                        f"{time.monotonic() - started:.1f}s, "
                        f"output {final_size / 1024 / 1024:.1f} MB, "
                        f"AES {_aes_backend_name()}"
                    ),
                )
            )
        return output_path

    def _fetch_hls_manifest(
        self,
        url: str,
        headers: Dict[str, str],
        *,
        media_only: bool = False,
        audio_only: bool = False,
    ) -> tuple[str, str, Optional[_HlsMasterContext]]:
        with self._session.get(
            url, headers=headers, timeout=(10, 60), allow_redirects=True
        ) as resp:
            resp.raise_for_status()
            text = resp.text
            final_url = resp.url
        self._validate_hls_manifest_header(text)
        if media_only:
            self._validate_hls_media_manifest(text)
            return final_url, text, None
        variant = self._pick_hls_variant(text, final_url)
        if variant is None:
            self._validate_hls_media_manifest(text)
            return final_url, text, None
        audio_variant = None
        if audio_only:
            audio_variant = self._pick_hls_audio_rendition(text, final_url, variant)
            if audio_variant is not None:
                # Inspect and download this audio playlist with the same range
                # validation as ordinary HLS. Fallbacks must stay on it, rather
                # than revisit the master and select unchecked media choices.
                variant = audio_variant
        master_context = (
            _HlsMasterContext(final_url, text)
            if audio_variant is None
            and self._hls_variant_has_external_audio(text, final_url, variant)
            else None
        )
        with self._session.get(
            variant, headers=headers, timeout=(10, 60), allow_redirects=True
        ) as child:
            child.raise_for_status()
            child_url, child_text = child.url, child.text
        self._validate_hls_media_manifest(child_text)
        return child_url, child_text, master_context

    @staticmethod
    def _pick_hls_audio_rendition(
        text: str, manifest_url: str, variant_url: str
    ) -> Optional[str]:
        group = None
        group_score = -1
        stream_attrs: Dict[str, str] = {}
        renditions = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("#EXT-X-MEDIA:"):
                attrs = _parse_m3u8_attrs(line.split(":", 1)[1])
                if attrs.get("TYPE") == "AUDIO":
                    renditions.append(attrs)
            elif line.startswith("#EXT-X-STREAM-INF:"):
                stream_attrs = _parse_m3u8_attrs(line.split(":", 1)[1])
            elif line and not line.startswith("#"):
                if urljoin(manifest_url, line) == variant_url:
                    bandwidth = int(stream_attrs.get("BANDWIDTH") or 0)
                    if bandwidth > group_score:
                        group, group_score = stream_attrs.get("AUDIO"), bandwidth
                stream_attrs = {}
        candidates = [attrs for attrs in renditions
                      if group is not None and attrs.get("GROUP-ID") == group]
        if not candidates:
            return None
        selected = max(candidates, key=lambda attrs: (
            attrs.get("DEFAULT") == "YES", attrs.get("AUTOSELECT") == "YES"
        ))
        # A default rendition without URI is already in the video stream.
        # Do not switch to a different language just because it has a URL.
        uri = selected.get("URI")
        return urljoin(manifest_url, uri) if uri else None

    @staticmethod
    def _hls_variant_has_external_audio(
        text: str, manifest_url: str, variant_url: str
    ) -> bool:
        external_audio_groups = set()
        lines = [line.strip() for line in text.splitlines()]
        for line in lines:
            if line.startswith("#EXT-X-MEDIA:"):
                attrs = _parse_m3u8_attrs(line.split(":", 1)[1])
                if attrs.get("TYPE") == "AUDIO" and attrs.get("URI"):
                    external_audio_groups.add(attrs.get("GROUP-ID"))
        stream_attrs: Dict[str, str] = {}
        for line in lines:
            if line.startswith("#EXT-X-STREAM-INF:"):
                stream_attrs = _parse_m3u8_attrs(line.split(":", 1)[1])
            elif line and not line.startswith("#"):
                if (
                    urljoin(manifest_url, line) == variant_url
                    and stream_attrs.get("AUDIO") in external_audio_groups
                ):
                    return True
                stream_attrs = {}
        return False

    def _validate_hls_master_fallback(
        self, context: _HlsMasterContext, item: VideoItem
    ) -> None:
        headers = build_request_headers(item.referer)
        headers["Accept"] = "*/*"
        candidates: set[str] = set()
        pending_stream = False
        try:
            for raw_line in context.text.splitlines():
                line = raw_line.strip()
                if _is_hls_tag(line, "#EXT-X-STREAM-INF"):
                    if pending_stream or not line.startswith("#EXT-X-STREAM-INF:"):
                        raise _HlsByteRangeError("Malformed HLS master variant reference")
                    pending_stream = True
                elif _is_hls_tag(line, "#EXT-X-MEDIA") or _is_hls_tag(
                    line, "#EXT-X-I-FRAME-STREAM-INF"
                ):
                    attrs = _parse_m3u8_attrs(line.split(":", 1)[1] if ":" in line else "")
                    uri = attrs.get("URI")
                    if uri:
                        candidates.add(urljoin(context.url, uri))
                    elif _is_hls_tag(line, "#EXT-X-I-FRAME-STREAM-INF"):
                        raise _HlsByteRangeError("Missing HLS I-frame playlist URI")
                elif line and not line.startswith("#"):
                    if not pending_stream:
                        raise _HlsByteRangeError("Unclassified URI in the HLS master playlist")
                    candidates.add(urljoin(context.url, line))
                    pending_stream = False
            if pending_stream or not candidates:
                raise _HlsByteRangeError("HLS master does not identify all media candidates")
            for candidate in sorted(candidates):
                if self._cancelled:
                    raise _HlsByteRangeError("Download cancelled")
                _url, text, _context = self._fetch_hls_manifest(
                    candidate, headers, media_only=True
                )
                if _hls_has_byte_ranges(text):
                    raise _HlsByteRangeError(
                        "HLS master fallback contains byte ranges in a media rendition"
                    )
        except _HlsByteRangeError:
            raise
        except Exception as exc:
            raise _HlsByteRangeError(
                f"Could not safely inspect every HLS master rendition: {exc}"
            ) from exc

    @staticmethod
    def _validate_hls_manifest_header(text: str) -> None:
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if first_line != "#EXTM3U":
            raise _HlsByteRangeError("The HLS response is not an EXTM3U playlist")

    @classmethod
    def _validate_hls_media_manifest(cls, text: str) -> None:
        cls._validate_hls_manifest_header(text)
        lines = [line.strip() for line in text.splitlines()]
        master_tags = (
            "#EXT-X-STREAM-INF", "#EXT-X-I-FRAME-STREAM-INF", "#EXT-X-MEDIA",
            "#EXT-X-SESSION-DATA", "#EXT-X-SESSION-KEY",
        )
        if any(_is_hls_tag(line, tag) for line in lines for tag in master_tags):
            raise _HlsByteRangeError(
                "The selected HLS playlist is still a master playlist; "
                "a media playlist could not be safely inspected"
            )
        if not any(
            _is_hls_tag(line, "#EXTINF") or _is_hls_tag(line, "#EXT-X-TARGETDURATION")
            for line in lines
        ):
            raise _HlsByteRangeError("The HLS response does not identify a media playlist")

    def _pick_hls_variant(self, text: str, manifest_url: str) -> Optional[str]:
        best_score = -1
        best_url: Optional[str] = None
        lines = text.splitlines()
        for idx, raw in enumerate(lines):
            line = raw.strip()
            if not line.startswith("#EXT-X-STREAM-INF"):
                continue
            attrs = _parse_m3u8_attrs(line.split(":", 1)[1] if ":" in line else "")
            bandwidth = int(attrs.get("BANDWIDTH") or 0)
            for next_line in lines[idx + 1:]:
                next_line = next_line.strip()
                if not next_line or next_line.startswith("#"):
                    continue
                if bandwidth > best_score:
                    best_score = bandwidth
                    best_url = urljoin(manifest_url, next_line)
                break
        return best_url

    def _parse_hls_segments(
        self,
        text: str,
        manifest_url: str,
        headers: Dict[str, str],
    ) -> list[_HlsSegment]:
        lines = [line.strip() for line in text.splitlines()]
        has_media_ranges = any(
            _is_hls_tag(line, "#EXT-X-BYTERANGE") for line in lines
        )
        if _hls_has_byte_ranges(text) and any(
            _is_hls_tag(line, "#EXT-X-MAP") for line in lines
        ):
            # This transfer path remuxes independent TS segments and does not
            # prepend initialization sections. Do not silently omit a MAP or
            # pass its ranges to a backend that accepts incorrect responses.
            raise _HlsByteRangeError(
                "HLS byte ranges with EXT-X-MAP initialization sections are not supported"
            )
        if has_media_ranges and any(
            _is_hls_tag(line, "#EXT-X-I-FRAMES-ONLY") for line in lines
        ) and any(
            line.startswith("#EXT-X-KEY:")
            and _parse_m3u8_attrs(line.split(":", 1)[1]).get("METHOD") == "AES-128"
            for line in lines
        ):
            # Encrypted I-frame ranges require block-boundary expansion and
            # recovering the IV from the preceding block (RFC 8216 6.3.6).
            raise _HlsByteRangeError(
                "AES-128 encrypted I-frame byte ranges are not supported"
            )
        segments: list[_HlsSegment] = []
        key_cache: Dict[str, bytes] = {}
        media_sequence = 0
        segment_number = media_sequence
        pending_duration = 4.0
        key_method: Optional[str] = None
        key_uri: Optional[str] = None
        key_iv: Optional[str] = None
        pending_range: Optional[tuple[int, Optional[int]]] = None
        previous_range_url: Optional[str] = None
        previous_range_end: Optional[int] = None

        for line in lines:
            if not line:
                continue
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    media_sequence = int(line.split(":", 1)[1])
                    segment_number = media_sequence
                except ValueError:
                    pass
            elif line.startswith("#EXTINF:"):
                try:
                    pending_duration = float(line.split(":", 1)[1].split(",", 1)[0])
                except ValueError:
                    pending_duration = 4.0
            elif line.startswith("#EXT-X-KEY:"):
                attrs = _parse_m3u8_attrs(line.split(":", 1)[1])
                key_method = attrs.get("METHOD")
                key_uri = attrs.get("URI")
                key_iv = attrs.get("IV")
                if key_method == "NONE":
                    key_uri = None
                    key_iv = None
            elif _is_hls_tag(line, "#EXT-X-BYTERANGE"):
                if pending_range is not None:
                    raise _HlsByteRangeError(
                        "HLS byte-range tag was not followed by a media URI"
                    )
                match = re.fullmatch(r"#EXT-X-BYTERANGE:([0-9]+)(?:@([0-9]+))?", line)
                if match is None or int(match.group(1)) <= 0:
                    raise _HlsByteRangeError(f"Invalid HLS byte-range tag: {line}")
                pending_range = (
                    int(match.group(1)),
                    int(match.group(2)) if match.group(2) is not None else None,
                )
            elif line.startswith("#"):
                continue
            else:
                segment_url = urljoin(manifest_url, line)
                range_start: Optional[int] = None
                range_length: Optional[int] = None
                if pending_range is not None:
                    range_length, range_start = pending_range
                    if range_start is None:
                        if (
                            previous_range_url != segment_url
                            or previous_range_end is None
                        ):
                            raise _HlsByteRangeError(
                                "An implicit HLS byte-range offset requires a preceding "
                                "ranged segment of the same media resource"
                            )
                        range_start = previous_range_end
                    previous_range_url = segment_url
                    previous_range_end = range_start + range_length
                    pending_range = None
                    if key_method not in {None, "NONE", "AES-128"}:
                        raise _HlsByteRangeError(
                            f"Unsupported encryption for HLS byte ranges: {key_method}"
                        )
                    if key_method == "AES-128" and not key_uri:
                        raise _HlsByteRangeError(
                            "AES-128 encrypted HLS byte ranges require a key URI"
                        )
                else:
                    # An intervening whole-file segment cannot supply the
                    # offset of a later implicit byte range (RFC 8216 4.3.2.2).
                    previous_range_url = None
                    previous_range_end = None
                key: Optional[bytes] = None
                iv: Optional[bytes] = None
                if key_method == "AES-128" and key_uri:
                    absolute_key = urljoin(manifest_url, key_uri)
                    key = key_cache.get(absolute_key)
                    if key is None:
                        key_resp = self._session.get(
                            absolute_key,
                            headers=headers,
                            timeout=(10, 60),
                            allow_redirects=True,
                        )
                        key_resp.raise_for_status()
                        key = key_resp.content
                        key_cache[absolute_key] = key
                    iv = _parse_hls_iv(key_iv, segment_number)
                segments.append(
                    _HlsSegment(
                        index=len(segments),
                        url=segment_url,
                        duration=pending_duration,
                        key=key,
                        iv=iv,
                        range_start=range_start,
                        range_length=range_length,
                    )
                )
                segment_number += 1
        if pending_range is not None:
            raise _HlsByteRangeError(
                "HLS byte-range tag was not followed by a media URI"
            )
        return segments

    def _download_hls_segment(
        self,
        segment: _HlsSegment,
        headers: Dict[str, str],
    ) -> bytes:
        is_ranged = segment.range_start is not None or segment.range_length is not None
        request_headers = headers
        if is_ranged:
            if (
                segment.range_start is None
                or segment.range_length is None
                or segment.range_start < 0
                or segment.range_length <= 0
            ):
                raise _HlsByteRangeError("Invalid HLS segment byte-range metadata")
            request_headers = dict(headers)
            request_headers["Range"] = (
                f"bytes={segment.range_start}-"
                f"{segment.range_start + segment.range_length - 1}"
            )
            request_headers["Accept-Encoding"] = "identity"
        last_error: Optional[Exception] = None
        for _attempt in range(1, 11):
            if self._cancelled:
                raise RuntimeError("Download cancelled")
            acquired = False
            try:
                if self._lane_limiter is not None:
                    self._lane_limiter.acquire(lambda: self._cancelled)
                    acquired = True
                chunks: list[bytes] = []
                received = 0
                with self._session.get(
                    segment.url,
                    headers=request_headers,
                    stream=True,
                    timeout=(10, 30),
                    allow_redirects=True,
                ) as resp:
                    resp.raise_for_status()
                    if is_ranged:
                        self._validate_hls_range_response(resp, segment)
                    for chunk in resp.iter_content(chunk_size=256 * 1024):
                        if self._cancelled:
                            raise RuntimeError("Download cancelled")
                        if chunk:
                            received += len(chunk)
                            if is_ranged and received > segment.range_length:
                                raise _HlsByteRangeError(
                                    "HLS byte-range response contains too many bytes"
                                )
                            chunks.append(chunk)
                    if is_ranged and received != segment.range_length:
                        raise _HlsByteRangeError(
                            "HLS byte-range response is shorter than the requested range"
                        )
                data = b"".join(chunks)
                if segment.key and segment.iv:
                    data = _aes_cbc_decrypt_bytes(data, segment.key, segment.iv)
                    data = _strip_pkcs7_padding(data)
                return data
            except _HlsByteRangeError:
                raise
            except Exception as exc:  # noqa: BLE001
                if self._cancelled:
                    raise
                last_error = exc
            finally:
                if acquired and self._lane_limiter is not None:
                    self._lane_limiter.release()
        error_class = _HlsByteRangeError if is_ranged else RuntimeError
        raise error_class(f"Failed to download HLS segment {segment.index}: {last_error}")

    @staticmethod
    def _validate_hls_range_response(resp: requests.Response, segment: _HlsSegment) -> None:
        """Require exact, uncompressed bytes before accepting a ranged segment."""
        if resp.status_code != 206:
            raise _HlsByteRangeError(
                f"HLS byte-range request expected HTTP 206, got {resp.status_code}"
            )
        encoding = resp.headers.get("Content-Encoding", "identity").strip().lower()
        if encoding != "identity":
            raise _HlsByteRangeError("Compressed HLS byte-range responses are not supported")
        value = resp.headers.get("Content-Range", "")
        match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+|\*)", value.strip())
        if match is None:
            raise _HlsByteRangeError(f"Invalid HLS Content-Range: {value!r}")
        start, end = int(match.group(1)), int(match.group(2))
        expected_end = segment.range_start + segment.range_length - 1
        if (
            start != segment.range_start
            or end != expected_end
            or (match.group(3) != "*" and int(match.group(3)) <= end)
        ):
            raise _HlsByteRangeError(f"Incorrect HLS Content-Range: {value!r}")
        content_length = resp.headers.get("Content-Length")
        if content_length is not None and (
            re.fullmatch(r"[0-9]+", content_length.strip()) is None
            or int(content_length) != segment.range_length
        ):
            raise _HlsByteRangeError("Incorrect HLS byte-range Content-Length")

    def _write_ffmpeg_concat_list(self, sources: list[Path], target: Path) -> None:
        with open(target, "w", encoding="utf-8") as file:
            for source in sources:
                escaped = str(source).replace("\\", "\\\\").replace("'", "\\'")
                file.write(f"file '{escaped}'\n")

    def _remux_hls_segments(self, ffmpeg: str, concat_list: Path, output: Path) -> None:
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            str(output),
        ]
        proc = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
        )
        self._ffmpeg_proc = proc
        try:
            _stdout, stderr = proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(stderr.strip() or f"ffmpeg exited with {proc.returncode}")
        finally:
            self._ffmpeg_proc = None

    def _download_hls_ytdlp(
        self,
        item: VideoItem,
        on_progress: Optional[ProgressHandler],
    ) -> Path:
        title = safe_filename(item.title)
        outtmpl = str(self.output_dir / f"{title}.%(ext)s")
        last_path: Dict[str, Optional[str]] = {"path": None}
        final_path: Dict[str, Optional[str]] = {"path": None}

        def post_hook(filename: str) -> None:
            final_path["path"] = filename

        def hook(data: Dict) -> None:
            if self._cancelled:
                raise RuntimeError("Download cancelled")
            status = data.get("status", "")
            filename = data.get("filename") or data.get("tmpfilename")
            if filename:
                last_path["path"] = filename
            if status == "downloading":
                downloaded = int(data.get("downloaded_bytes") or 0)
                total = int(
                    data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                )
                message = ""
                fragment_index = data.get("fragment_index")
                fragment_count = data.get("fragment_count")
                if (
                    fragment_index is not None
                    and fragment_count is not None
                    and int(fragment_count) > 1
                ):
                    downloaded = int(fragment_index)
                    total = int(fragment_count)
                    message = f"Fragment {downloaded}/{total}"
                elif data.get("_percent_str"):
                    message = _clean_yt_dlp_text(str(data.get("_percent_str")))
                progress = DownloadProgress(
                    status="downloading",
                    downloaded_bytes=downloaded,
                    total_bytes=total,
                    speed=float(data.get("speed") or 0.0),
                    eta=data.get("eta"),
                    filename=filename,
                    message=message,
                )
            elif status == "finished":
                progress = DownloadProgress(
                    status="finished",
                    filename=filename,
                    message="Post-processing...",
                )
            elif status == "error":
                progress = DownloadProgress(
                    status="error",
                    message=str(data.get("error") or "Download error"),
                )
            else:
                return
            if on_progress is not None:
                on_progress(progress)

        if on_progress is not None:
            on_progress(
                DownloadProgress(
                    status="downloading",
                    filename=str(self.output_dir / f"{title}.mp4"),
                    message="Starting native HLS download...",
                )
            )

        ydl_opts = {
            "outtmpl": outtmpl,
            "format": "best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "progress_hooks": [hook],
            "post_hooks": [post_hook],
            "merge_output_format": "mp4",
            "hls_prefer_native": True,
            "concurrent_fragment_downloads": 4,
            "retries": 5,
            "fragment_retries": 10,
            "extractor_retries": 5,
            "socket_timeout": 60,
            "http_headers": build_request_headers(item.referer),
        }
        ffmpeg = resolve_ffmpeg()
        if ffmpeg and not shutil.which("ffmpeg"):
            ydl_opts["ffmpeg_location"] = ffmpeg

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.download([item.url])

        if result:
            raise RuntimeError("Native HLS download failed")

        if final_path["path"] and Path(final_path["path"]).is_file():
            last_path["path"] = final_path["path"]

        if on_progress is not None:
            on_progress(
                DownloadProgress(
                    status="completed",
                    filename=last_path["path"],
                    message="Done",
                )
            )
        return Path(last_path["path"]) if last_path["path"] else self.output_dir

    def _download_direct(
        self,
        item: VideoItem,
        on_progress: Optional[ProgressHandler],
    ) -> Path:
        """Pull a direct media URL using N parallel Range connections."""
        ext = item.ext or _guess_ext(item.url) or "mp4"
        title = safe_filename(item.title)
        output_path = self.output_dir / f"{title}.{ext}"

        headers = build_request_headers(item.referer)
        # CDNs streaming media usually want a "media" Accept rather than HTML.
        headers["Accept"] = "*/*"

        self._parallel = ParallelDownloader(
            connections=self.parallel_connections,
            lane_limiter=self._lane_limiter,
        )

        def relay(downloaded: int, total: int, speed: float) -> None:
            if on_progress is None:
                return
            eta = None
            if speed > 0 and total > 0 and downloaded < total:
                eta = int(max(0, (total - downloaded) / speed))
            on_progress(
                DownloadProgress(
                    status="downloading",
                    downloaded_bytes=downloaded,
                    total_bytes=total,
                    speed=speed,
                    eta=eta,
                    filename=str(output_path),
                )
            )

        try:
            result = self._parallel.download(
                url=item.url,
                output_path=output_path,
                headers=headers,
                progress=relay,
            )
        finally:
            self._parallel.close()
            self._parallel = None

        if on_progress is not None:
            on_progress(
                DownloadProgress(
                    status="completed",
                    filename=str(result.path),
                    downloaded_bytes=result.size,
                    total_bytes=result.size,
                    message=(
                        f"Done in {result.elapsed:.1f}s "
                        f"({result.size / max(result.elapsed, 1e-3) / 1024:.0f} KB/s)"
                    ),
                )
            )
        return result.path

    def _download_hls_ffmpeg(
        self,
        item: VideoItem,
        ffmpeg: str,
        on_progress: Optional[ProgressHandler],
    ) -> Path:
        """Download a direct HLS manifest with ffmpeg and parse live progress."""
        title = safe_filename(item.title)
        output_path = self.output_dir / f"{title}.mp4"
        if on_progress is not None:
            on_progress(
                DownloadProgress(
                    status="downloading",
                    filename=str(output_path),
                    message="Reading HLS manifest...",
                )
            )
        duration = item.duration or self._probe_hls_duration(item)
        headers = build_request_headers(item.referer)
        header_blob = "".join(f"{key}: {value}\r\n" for key, value in headers.items())

        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
        ]
        if item.url.lower().startswith(("http://", "https://")):
            cmd.extend(["-headers", header_blob])
        cmd.extend(
            [
                "-i",
                item.url,
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                "-progress",
                "pipe:2",
                str(output_path),
            ]
        )

        if on_progress is not None:
            on_progress(
                DownloadProgress(
                    status="downloading",
                    total_bytes=int(duration * 1000) if duration else 0,
                    filename=str(output_path),
                    message="Starting HLS download...",
                )
            )

        proc = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._ffmpeg_proc = proc
        stderr_tail: list[str] = []
        last_ms = 0
        try:
            assert proc.stderr is not None
            for raw_line in proc.stderr:
                line = raw_line.strip()
                if not line:
                    continue
                if len(stderr_tail) >= 20:
                    stderr_tail.pop(0)
                stderr_tail.append(line)

                if line.startswith("out_time_ms="):
                    try:
                        last_ms = max(0, int(line.split("=", 1)[1]) // 1000)
                    except ValueError:
                        continue
                elif line.startswith("out_time="):
                    parsed = _parse_ffmpeg_time(line.split("=", 1)[1])
                    if parsed is not None:
                        last_ms = max(0, int(parsed * 1000))
                else:
                    continue

                if on_progress is not None:
                    on_progress(
                        DownloadProgress(
                            status="downloading",
                            downloaded_bytes=last_ms,
                            total_bytes=int(duration * 1000) if duration else 0,
                            filename=str(output_path),
                            message=f"HLS time {last_ms / 1000:.1f}s",
                        )
                    )

            code = proc.wait()
            if code != 0:
                tail = "\n".join(stderr_tail[-8:]) or f"ffmpeg exited with {code}"
                raise RuntimeError(tail)
        finally:
            self._ffmpeg_proc = None

        if on_progress is not None:
            on_progress(
                DownloadProgress(
                    status="completed",
                    downloaded_bytes=int(duration * 1000) if duration else last_ms,
                    total_bytes=int(duration * 1000) if duration else last_ms,
                    filename=str(output_path),
                    message="Done",
                )
            )
        return output_path

    def _probe_hls_duration(self, item: VideoItem) -> Optional[float]:
        try:
            resp = self._session.get(
                item.url,
                headers=build_request_headers(item.referer),
                timeout=(10, 60),
            )
            resp.raise_for_status()
        except Exception:  # noqa: BLE001
            return None
        total = 0.0
        for match in re.finditer(r"#EXTINF:([0-9.]+)", resp.text, re.IGNORECASE):
            try:
                total += float(match.group(1))
            except ValueError:
                pass
        return total or None


def _guess_ext(url: str) -> Optional[str]:
    tail = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "." in tail:
        ext = tail.rsplit(".", 1)[-1].lower()
        if 1 <= len(ext) <= 5 and ext.isalnum():
            return ext
    return None


def _parse_ffmpeg_time(value: str) -> Optional[float]:
    match = re.match(r"(?P<h>\d+):(?P<m>\d+):(?P<s>\d+(?:\.\d+)?)", value)
    if not match:
        return None
    return (
        int(match.group("h")) * 3600
        + int(match.group("m")) * 60
        + float(match.group("s"))
    )


def _clean_yt_dlp_text(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", value).strip()


def _parse_m3u8_attrs(value: str) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', value):
        raw = match.group(2).strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        attrs[match.group(1)] = raw
    return attrs


def _parse_hls_iv(value: Optional[str], sequence: int) -> bytes:
    if value:
        clean = value[2:] if value.lower().startswith("0x") else value
        clean = clean.zfill(32)
        return bytes.fromhex(clean[-32:])
    return sequence.to_bytes(16, "big")


def _strip_pkcs7_padding(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data.endswith(bytes([pad]) * pad):
        return data[:-pad]
    return data
