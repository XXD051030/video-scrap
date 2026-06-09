"""Video downloader built on top of yt-dlp.

Supports both yt-dlp recognized URLs and plain direct media links via
yt-dlp's generic extractor. Emits progress events through callbacks so the
GUI thread can stay responsive.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from dataclasses import dataclass
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

from .parallel_downloader import ParallelDownloader
from .scraper import VideoItem
from .utils import safe_filename


QUALITY_FORMATS: Dict[str, str] = {
    "best": "bestvideo*+bestaudio/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "audio only": "bestaudio/best",
}


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
    ) -> Path:
        self._cancelled = False
        self._partial_on_cancel = False
        # HLS / DASH manifests are not single files - they reference dozens
        # of segment URLs. yt-dlp knows how to fetch + concat them, so route
        # through it. Only single-file direct URLs use the parallel path.
        if item.is_direct and item.is_hls:
            try:
                return self._download_hls_parallel(item, on_progress)
            except Exception:
                if self._cancelled:
                    raise
                if on_progress is not None:
                    on_progress(
                        DownloadProgress(
                            status="downloading",
                            message="Parallel HLS failed; retrying with yt-dlp...",
                        )
                    )
                try:
                    return self._download_hls_ytdlp(item, on_progress)
                except Exception:
                    if self._cancelled:
                        raise
                    ffmpeg = shutil.which("ffmpeg")
                    if not ffmpeg:
                        raise
                    if on_progress is not None:
                        on_progress(
                            DownloadProgress(
                                status="downloading",
                                message="Native HLS failed; retrying with ffmpeg...",
                            )
                        )
                    return self._download_hls_ffmpeg(item, ffmpeg, on_progress)
        if item.is_direct and not item.is_hls:
            return self._download_direct(item, on_progress)

        fmt = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["best"])
        title = safe_filename(item.title)
        outtmpl = str(self.output_dir / f"{title}.%(ext)s")

        last_path: Dict[str, Optional[str]] = {"path": None}

        def hook(data: Dict) -> None:
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
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": 2,
            "retries": 5,
            "fragment_retries": 10,
            "extractor_retries": 5,
            "socket_timeout": 60,
            "http_headers": http_headers,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([item.url])

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
    ) -> Path:
        """Download HLS segments concurrently, then remux to mp4."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required for HLS remuxing")

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

        manifest_url, manifest_text = self._fetch_hls_manifest(item.url, headers)
        segments = self._parse_hls_segments(manifest_text, manifest_url, headers)
        if not segments:
            raise RuntimeError("HLS manifest did not contain media segments")

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
    ) -> tuple[str, str]:
        resp = requests.get(url, headers=headers, timeout=(10, 60), allow_redirects=True)
        resp.raise_for_status()
        text = resp.text
        final_url = resp.url
        variant = self._pick_hls_variant(text, final_url)
        resp.close()
        if variant is None:
            return final_url, text
        child = requests.get(
            variant, headers=headers, timeout=(10, 60), allow_redirects=True
        )
        child.raise_for_status()
        child_url, child_text = child.url, child.text
        child.close()
        return child_url, child_text

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
        segments: list[_HlsSegment] = []
        key_cache: Dict[str, bytes] = {}
        media_sequence = 0
        segment_number = media_sequence
        pending_duration = 4.0
        key_method: Optional[str] = None
        key_uri: Optional[str] = None
        key_iv: Optional[str] = None

        for raw_line in text.splitlines():
            line = raw_line.strip()
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
            elif line.startswith("#"):
                continue
            else:
                key: Optional[bytes] = None
                iv: Optional[bytes] = None
                if key_method == "AES-128" and key_uri:
                    absolute_key = urljoin(manifest_url, key_uri)
                    key = key_cache.get(absolute_key)
                    if key is None:
                        key_resp = requests.get(
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
                        url=urljoin(manifest_url, line),
                        duration=pending_duration,
                        key=key,
                        iv=iv,
                    )
                )
                segment_number += 1
        return segments

    def _download_hls_segment(
        self,
        segment: _HlsSegment,
        headers: Dict[str, str],
    ) -> bytes:
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
                with requests.get(
                    segment.url,
                    headers=headers,
                    stream=True,
                    timeout=(10, 30),
                    allow_redirects=True,
                ) as resp:
                    resp.raise_for_status()
                    for chunk in resp.iter_content(chunk_size=256 * 1024):
                        if self._cancelled:
                            raise RuntimeError("Download cancelled")
                        if chunk:
                            chunks.append(chunk)
                data = b"".join(chunks)
                if segment.key and segment.iv:
                    data = _aes_cbc_decrypt_bytes(data, segment.key, segment.iv)
                    data = _strip_pkcs7_padding(data)
                return data
            except Exception as exc:  # noqa: BLE001
                if self._cancelled:
                    raise
                last_error = exc
            finally:
                if acquired and self._lane_limiter is not None:
                    self._lane_limiter.release()
        raise RuntimeError(f"Failed to download HLS segment {segment.index}: {last_error}")

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
            "merge_output_format": "mp4",
            "hls_prefer_native": True,
            "concurrent_fragment_downloads": 4,
            "retries": 5,
            "fragment_retries": 10,
            "extractor_retries": 5,
            "socket_timeout": 60,
            "http_headers": build_request_headers(item.referer),
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([item.url])

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
            resp = requests.get(
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
