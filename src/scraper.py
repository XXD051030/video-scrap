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

import base64
import html as html_module
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
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


def _is_known_player_placeholder(url: str) -> bool:
    """Ignore demo assets bundled by web players, not the page's real media."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return host.endswith("artplayer.org") and path.startswith("/assets/sample/")


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


# --- Inline player-config HLS extraction -------------------------------------
# A family of self-hosted "tube" CMSs (91AV and its mirror domains, etc.) hide
# the real video behind a JSON player config rather than a plain <source>: an
# ``online_video`` object carrying a ``hash_id`` and a *relative* ``m3u8_path``,
# plus a ``lines`` array of CDN hosts. The page's <source> tags only hold short
# ``preview.mp4`` hover-teasers. The playable manifest is assembled at runtime
# as ``https://<cdn-host>/videos/<hash_id>/<manifest-file>``.
_CONFIG_HASH_ID_RE = re.compile(r'"hash_id"\s*:\s*"([0-9a-fA-F]{16,})"')
_CONFIG_M3U8_PATH_RE = re.compile(r'"m3u8_path"\s*:\s*"([^"]+?\.m3u8)"')
_CONFIG_LINE_HOST_RE = re.compile(
    r'\[\s*"line\d+"\s*,\s*"[^"]*"\s*,\s*"([A-Za-z0-9.\-]+\.[A-Za-z]{2,})"\s*\]'
)
_PREVIEW_HOST_RE = re.compile(
    r'https?://([A-Za-z0-9.\-]+)/videos/[0-9a-fA-F]{16,}/', re.IGNORECASE
)
_PREVIEW_TEASER_RE = re.compile(
    r'/preview\.(?:mp4|webm|m4v|mov)(?:\?|$)', re.IGNORECASE
)


def _is_preview_teaser(url: str) -> bool:
    """True for ``.../preview.mp4`` style hover-teaser clips (not the real video)."""
    return bool(_PREVIEW_TEASER_RE.search(url))


def _extract_config_hls(html: str, page_url: str) -> List[str]:
    """Recover HLS manifest URLs from an inline tube-CMS player config.

    Returns absolute ``.../videos/<hash>/<file>.m3u8`` URLs (one per CDN host
    advertised on the page), or an empty list when the pattern isn't present.
    """
    m_hash = _CONFIG_HASH_ID_RE.search(html)
    m_path = _CONFIG_M3U8_PATH_RE.search(html)
    if not m_hash or not m_path:
        return []
    hash_id = m_hash.group(1)
    manifest_file = m_path.group(1).rsplit("/", 1)[-1] or "play.m3u8"

    hosts: List[str] = []
    for host in (
        _CONFIG_LINE_HOST_RE.findall(html) + _PREVIEW_HOST_RE.findall(html)
    ):
        host = host.strip().strip("/")
        if host and host not in hosts:
            hosts.append(host)
    if not hosts:
        return []

    urls: List[str] = []
    for host in hosts:
        url = f"https://{host}/videos/{hash_id}/{manifest_file}"
        if url not in urls:
            urls.append(url)
    return urls


# --- Encrypted Next.js player config (rou.video & clones) --------------------
# These sites are Next.js apps that ship the real (signed, time-limited) HLS
# URL *encrypted* inside ``__NEXT_DATA__.props.pageProps.ev`` and only decode
# it client-side on a play click, so nothing playable is in the static HTML.
# The player's cipher is trivial: base64-decode ``ev.d``, subtract ``ev.k``
# from each byte, then JSON.parse -> {"videoUrl": ...}. The recovered URL is a
# normal HLS manifest (often disguised with a .jpg extension).
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


def _decrypt_next_ev(ev: Dict) -> Optional[Dict]:
    """Mirror the player's decode: ``JSON.parse(atob(d) shifted down by k)``."""
    d = ev.get("d")
    k = ev.get("k")
    if not isinstance(d, str) or not isinstance(k, int):
        return None
    try:
        raw = base64.b64decode(d + "=" * (-len(d) % 4))
        text = "".join(chr((b - k) % 256) for b in raw)
        obj = json.loads(text)
    except Exception:  # noqa: BLE001
        return None
    return obj if isinstance(obj, dict) else None


def _extract_next_data_ev_hls(html: str) -> Optional[Tuple[str, Optional[str]]]:
    """Recover ``(video_url, title)`` from an encrypted Next.js ``ev`` blob."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except Exception:  # noqa: BLE001
        return None
    page_props = (data.get("props") or {}).get("pageProps") or {}
    ev = page_props.get("ev")
    if not isinstance(ev, dict):
        return None
    decoded = _decrypt_next_ev(ev)
    if not decoded:
        return None
    video_url = decoded.get("videoUrl") or decoded.get("url")
    if not isinstance(video_url, str) or not video_url.lower().startswith(
        ("http://", "https://")
    ):
        return None
    video = page_props.get("video") or {}
    title = video.get("nameZh") or video.get("name")
    return video_url, (title if isinstance(title, str) else None)


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
                if _is_known_player_placeholder(link) or link in seen_urls:
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

        if html_text:
            log("Trying inline player-config HLS extractor...")
            try:
                config_hls = _extract_config_hls(html_text, url)
            except Exception as exc:  # noqa: BLE001
                log(f"Config HLS extractor failed: {exc}")
                config_hls = []
            new_hls: List[VideoItem] = []
            for link in config_hls:
                if link in seen_urls:
                    continue
                seen_urls.add(link)
                new_hls.append(
                    self._item_from_hls(link, url, page_title, poster)
                )
            if new_hls:
                # Real full-length streams go first so the UI selects one by
                # default instead of a preview teaser.
                results[:0] = new_hls
                log(
                    f"Player-config extractor recovered {len(new_hls)} "
                    f"HLS stream(s)"
                )

        if html_text:
            try:
                ev_hls = _extract_next_data_ev_hls(html_text)
            except Exception as exc:  # noqa: BLE001
                log(f"Encrypted-config extractor failed: {exc}")
                ev_hls = None
            if ev_hls and ev_hls[0] not in seen_urls:
                ev_url, ev_title = ev_hls
                seen_urls.add(ev_url)
                results.insert(
                    0,
                    self._item_from_hls(
                        ev_url, url, ev_title or page_title, poster
                    ),
                )
                log("Encrypted player-config extractor recovered the HLS stream")

        log(f"Found {len(results)} video(s).")
        return results

    def _item_from_hls(
        self,
        url: str,
        source_url: str,
        title: str,
        poster: Optional[str],
    ) -> VideoItem:
        return VideoItem(
            title=title,
            url=url,
            source_url=source_url,
            thumbnail=poster,
            ext="m3u8",
            is_direct=True,
            is_hls=True,
            referer=source_url,
        )

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
            format_urls = [
                fmt.get("url")
                for fmt in (entry.get("formats") or [])
                if fmt.get("url")
            ]
            if format_urls and all(
                _is_known_player_placeholder(link) for link in format_urls
            ):
                log("yt-dlp returned only player demo media; ignoring it.")
                continue
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
            if _is_known_player_placeholder(cand) or _is_preview_teaser(cand):
                continue
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
