"""Video scraping logic.

Three-stage strategy:
1. yt-dlp extraction (handles YouTube, Bilibili, Twitter, TikTok, Vimeo,
   Instagram, etc. and many JS-rendered pages) -- preferred.
2. Plain HTML scan that pulls <video>, <source>, <iframe>, og:video meta
   and raw .mp4 / .webm / .m3u8 links from the response body.
3. JS-array obfuscation decoder for sites that hide their direct media
   URLs behind a permutation array + decodeURIComponent / replace pipeline
   (very common on self-hosted video portals).

A heavier WebEngine-based fallback lives in ``gui/js_renderer.py`` and is
invoked from the GUI when these three stages return nothing useful.
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
import yt_dlp
from bs4 import BeautifulSoup

from .js_decoder import decode_obfuscated_urls


HLS_EXTS = {"m3u8", "mpd"}


def _is_hls(url: str, ext: Optional[str] = None) -> bool:
    if ext and ext.lower() in HLS_EXTS:
        return True
    lower = url.lower()
    return ".m3u8" in lower or ".mpd" in lower


def _ext_of(url: str) -> Optional[str]:
    tail = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "." in tail:
        ext = tail.rsplit(".", 1)[-1].lower()
        if 1 <= len(ext) <= 5 and ext.isalnum():
            return ext
    return None


HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

DIRECT_VIDEO_PATTERNS = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:mp4|webm|mov|m4v|m3u8|mpd|mkv)"
    r"(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)


# Hosts whose iframes should always be treated as video embeds. Aggregator
# sites (blogs, "tube" portals, news sites) usually wrap videos from one of
# these players inside an <iframe>, so we need to follow them.
EMBED_HOSTS = (
    # Mainstream
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "vimeo.com",
    "bilibili.com",
    "twitter.com",
    "x.com",
    "dailymotion.com",
    "twitch.tv",
    "tiktok.com",
    # Adult video portals commonly embedded on aggregator pages.
    "xvideos.com",
    "xvideos4.com",
    "xnxx.com",
    "pornhub.com",
    "xhamster.com",
    "youporn.com",
    "redtube.com",
    "spankbang.com",
    "eporner.com",
    "tktube.com",
    "tnaflix.com",
)

# Path fragments that identify an embed/player iframe even when the host is
# unknown (self-hosted JW/Plyr/video.js players, custom CDNs, etc.).
EMBED_PATH_KEYWORDS = (
    "/embed",
    "/embedframe",
    "/embed-",
    "/player",
    "/iframe",
    "/e/",
)


def _is_embed_iframe(url: str) -> bool:
    """True if this iframe URL looks like a video player embed."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if any(h in host for h in EMBED_HOSTS):
        return True
    path = parsed.path.lower()
    return any(k in path for k in EMBED_PATH_KEYWORDS)


@dataclass
class VideoItem:
    """One scraped video entry shown to the user."""

    title: str
    url: str
    source_url: str
    thumbnail: Optional[str] = None
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    ext: Optional[str] = None
    uploader: Optional[str] = None
    is_direct: bool = False
    is_hls: bool = False
    formats: List[Dict] = field(default_factory=list)
    raw: Optional[Dict] = None
    referer: Optional[str] = None

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "--"


ProgressCallback = Callable[[str], None]


class VideoScraper:
    """Discover videos on a given URL."""

    def __init__(self, timeout: int = 20) -> None:
        self.timeout = timeout

    def scrape(
        self,
        url: str,
        progress: Optional[ProgressCallback] = None,
    ) -> List[VideoItem]:
        url = url.strip()
        log = progress or (lambda _msg: None)

        results: List[VideoItem] = []
        seen_urls: set[str] = set()

        log("Trying yt-dlp extraction...")
        try:
            results.extend(self._scrape_with_ytdlp(url, log))
        except Exception as exc:  # noqa: BLE001
            log(f"yt-dlp failed: {exc}")

        for item in list(results):
            seen_urls.add(item.url)

        log("Scanning page HTML for direct video links...")
        try:
            html_items, embed_iframes, html_text, page_title, poster = (
                self._scrape_html(url, log)
            )
        except Exception as exc:  # noqa: BLE001
            log(f"HTML scan failed: {exc}")
            html_items, embed_iframes, html_text, page_title, poster = (
                [],
                [],
                "",
                url,
                None,
            )

        for item in html_items:
            if item.url in seen_urls:
                continue
            seen_urls.add(item.url)
            results.append(item)

        # Resolve any embed iframes (xvideos / youtube / vimeo / self-hosted
        # /embed/ routes etc.) by handing the URL back to yt-dlp, so we get
        # real titles + formats instead of treating the embed HTML as a file.
        for iframe_url in embed_iframes:
            if iframe_url in seen_urls:
                continue
            log(f"Resolving embed iframe: {iframe_url}")
            try:
                yt_items = self._scrape_with_ytdlp(iframe_url, log)
            except Exception as exc:  # noqa: BLE001
                log(f"yt-dlp failed on iframe: {exc}")
                yt_items = []
            if yt_items:
                for it in yt_items:
                    if it.url in seen_urls:
                        continue
                    seen_urls.add(it.url)
                    it.source_url = url
                    if not it.thumbnail and poster:
                        it.thumbnail = poster
                    if not it.referer:
                        it.referer = iframe_url
                    results.append(it)
            else:
                # Non-direct fallback so the downloader routes through yt-dlp's
                # generic extractor on the embed page rather than blindly
                # parallel-downloading the HTML as a media file.
                seen_urls.add(iframe_url)
                results.append(
                    VideoItem(
                        title=f"{page_title} - embed",
                        url=iframe_url,
                        source_url=url,
                        thumbnail=poster,
                        is_direct=False,
                        referer=url,
                    )
                )

        # Backfill thumbnails for yt-dlp items missing them.
        if poster:
            for item in results:
                if not item.thumbnail:
                    item.thumbnail = poster

        if html_text:
            log("Trying JS-array obfuscation decoder...")
            try:
                hidden_urls = decode_obfuscated_urls(html_text)
            except Exception as exc:  # noqa: BLE001
                log(f"JS decoder failed: {exc}")
                hidden_urls = []
            for idx, link in enumerate(hidden_urls, 1):
                if link in seen_urls:
                    continue
                seen_urls.add(link)
                ext = _ext_of(link)
                results.append(
                    VideoItem(
                        title=f"{page_title} - hidden source {idx}",
                        url=link,
                        source_url=url,
                        thumbnail=poster,
                        ext=ext,
                        is_direct=True,
                        is_hls=_is_hls(link, ext),
                        referer=url,
                    )
                )
            if hidden_urls:
                log(f"Decoder recovered {len(hidden_urls)} hidden URL(s)")

        log(f"Found {len(results)} video(s).")
        return results

    def _scrape_with_ytdlp(
        self,
        url: str,
        log: ProgressCallback,
    ) -> List[VideoItem]:
        class _SilentLogger:
            def debug(self, _msg: str) -> None: ...
            def info(self, _msg: str) -> None: ...
            def warning(self, _msg: str) -> None: ...
            def error(self, _msg: str) -> None: ...

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": False,
            "noplaylist": False,
            "ignoreerrors": True,
            "logger": _SilentLogger(),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return []

        entries: List[Dict]
        if info.get("_type") == "playlist" and info.get("entries"):
            entries = [e for e in info["entries"] if e]
            log(f"yt-dlp found playlist with {len(entries)} entries")
        else:
            entries = [info]

        items: List[VideoItem] = []
        for entry in entries:
            video_url = (
                entry.get("webpage_url")
                or entry.get("original_url")
                or entry.get("url")
                or url
            )
            items.append(
                VideoItem(
                    title=entry.get("title") or "Untitled",
                    url=video_url,
                    source_url=url,
                    thumbnail=entry.get("thumbnail"),
                    duration=entry.get("duration"),
                    width=entry.get("width"),
                    height=entry.get("height"),
                    ext=entry.get("ext"),
                    uploader=entry.get("uploader") or entry.get("channel"),
                    is_direct=False,
                    formats=entry.get("formats") or [],
                    raw=entry,
                    referer=entry.get("webpage_url") or url,
                )
            )
        return items

    def _scrape_html(
        self,
        url: str,
        log: ProgressCallback,
    ) -> tuple[List[VideoItem], List[str], str, str, Optional[str]]:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=self.timeout)
        resp.raise_for_status()
        html = resp.text

        soup = BeautifulSoup(html, "lxml")
        candidates: List[str] = []
        embed_iframes: List[str] = []
        poster: Optional[str] = None

        # Attribute names that commonly hide the real video URL.
        media_attrs = ("src", "data-src", "data-url", "data-mp4", "data-video")

        for video in soup.find_all("video"):
            if not poster:
                p = video.get("poster") or video.get("data-poster")
                if p:
                    poster = urljoin(url, p)
            for attr in media_attrs:
                value = video.get(attr)
                if value:
                    candidates.append(urljoin(url, value))
            for source in video.find_all("source"):
                for attr in media_attrs:
                    value = source.get(attr)
                    if value:
                        candidates.append(urljoin(url, value))

        page_host = urlparse(url).netloc.lower()
        skipped_hosts: set[str] = set()
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src") or iframe.get("data-src")
            if not src:
                continue
            full = urljoin(url, src)
            parsed = urlparse(full)
            host = parsed.netloc.lower()
            if not host:
                continue
            # Same-domain iframes are usually just self-referential players
            # already covered by the <video> tag -- unless the path clearly
            # indicates an embed/player route.
            if host == page_host and not any(
                k in parsed.path.lower() for k in EMBED_PATH_KEYWORDS
            ):
                continue
            if _is_embed_iframe(full):
                embed_iframes.append(full)
            elif host not in skipped_hosts:
                skipped_hosts.add(host)
                log(f"Skipping iframe on unknown host: {host}")

        video_meta_props = {
            "og:video",
            "og:video:url",
            "og:video:secure_url",
            "twitter:player",
            "twitter:player:stream",
        }
        image_meta_props = {
            "og:image",
            "og:image:url",
            "og:image:secure_url",
            "twitter:image",
            "twitter:image:src",
        }
        media_ext_re = re.compile(
            r"\.(mp4|webm|mov|m4v|m3u8|mpd|mkv)(?:\?|$)", re.IGNORECASE
        )
        for meta in soup.find_all("meta"):
            prop = (meta.get("property") or meta.get("name") or "").lower()
            content = (meta.get("content") or "").strip()
            if not content:
                continue
            if prop in video_meta_props and content.startswith(
                ("http://", "https://", "//")
            ):
                # Only count og:video if it actually points to a media file;
                # many sites use og:video as an embed page URL, which would
                # produce duplicate / un-downloadable list entries.
                if media_ext_re.search(content):
                    candidates.append(urljoin(url, content))
            elif (
                not poster
                and prop in image_meta_props
                and content.startswith(("http://", "https://", "//"))
            ):
                poster = urljoin(url, content)

        for match in DIRECT_VIDEO_PATTERNS.finditer(html):
            candidates.append(match.group(0))

        unique: List[str] = []
        seen: set[str] = set()
        for cand in candidates:
            if not cand:
                continue
            cand = html_module.unescape(cand).strip()
            if cand and cand not in seen:
                seen.add(cand)
                unique.append(cand)

        unique_iframes: List[str] = []
        seen_iframes: set[str] = set()
        for cand in embed_iframes:
            cand = html_module.unescape(cand).strip()
            if cand and cand not in seen_iframes:
                seen_iframes.add(cand)
                unique_iframes.append(cand)

        log(
            f"HTML scan: {len(unique)} direct link(s), "
            f"{len(unique_iframes)} embed iframe(s)"
        )

        items: List[VideoItem] = []
        page_title = (soup.title.string.strip() if soup.title and soup.title.string else url)
        for idx, link in enumerate(unique, 1):
            ext = _ext_of(link)
            items.append(
                VideoItem(
                    title=f"{page_title} - clip {idx}",
                    url=link,
                    source_url=url,
                    thumbnail=poster,
                    duration=None,
                    ext=ext,
                    is_direct=True,
                    is_hls=_is_hls(link, ext),
                    referer=url,
                )
            )
        return items, unique_iframes, html, page_title, poster


def build_items_from_urls(
    media_urls: List[str],
    iframe_urls: List[str],
    source_url: str,
    page_title: str,
) -> List[VideoItem]:
    """Convert a flat list of media URLs (from WebEngine) into VideoItems."""
    items: List[VideoItem] = []
    seen: set[str] = set()

    def push(link: str, label_idx: int, kind: str, is_direct: bool) -> None:
        if not link or link in seen:
            return
        seen.add(link)
        ext = _ext_of(link) if is_direct else None
        items.append(
            VideoItem(
                title=f"{page_title or source_url} - {kind} {label_idx}",
                url=link,
                source_url=source_url,
                ext=ext,
                is_direct=is_direct,
                is_hls=_is_hls(link, ext) if is_direct else False,
                referer=source_url,
            )
        )

    for idx, link in enumerate(media_urls, 1):
        push(link, idx, "rendered", is_direct=True)
    for idx, link in enumerate(iframe_urls, 1):
        if _is_embed_iframe(link):
            # Mark as non-direct so downloader routes the iframe URL through
            # yt-dlp (parallel downloader would only fetch the embed HTML).
            push(link, idx, "iframe", is_direct=False)
    return items
