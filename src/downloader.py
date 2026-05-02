"""Video downloader built on top of yt-dlp.

Supports both yt-dlp recognized URLs and plain direct media links via
yt-dlp's generic extractor. Emits progress events through callbacks so the
GUI thread can stay responsive.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional
from urllib.parse import urlparse

import yt_dlp

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


class VideoDownloader:
    """Routes direct .mp4 URLs to a parallel downloader, the rest to yt-dlp."""

    def __init__(self, output_dir: Path, parallel_connections: int = 8) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.parallel_connections = parallel_connections
        self._parallel: Optional[ParallelDownloader] = None

    def cancel(self) -> None:
        if self._parallel is not None:
            self._parallel.cancel()

    def download(
        self,
        item: VideoItem,
        quality: str = "best",
        on_progress: Optional[ProgressHandler] = None,
    ) -> Path:
        # HLS / DASH manifests are not single files - they reference dozens
        # of segment URLs. yt-dlp knows how to fetch + concat them, so route
        # through it. Only single-file direct URLs use the parallel path.
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
            "concurrent_fragment_downloads": 4,
            "retries": 5,
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
            connections=self.parallel_connections
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


def _guess_ext(url: str) -> Optional[str]:
    tail = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "." in tail:
        ext = tail.rsplit(".", 1)[-1].lower()
        if 1 <= len(ext) <= 5 and ext.isalnum():
            return ext
    return None
