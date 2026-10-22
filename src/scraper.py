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
import copy
import html as html_module
import json
import math
import re
import threading
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

import requests
import yt_dlp
from bs4 import BeautifulSoup

from .js_decoder import decode_obfuscated_urls
from .http_limits import read_limited_response
from .browser_cookies import ScopedCookieSession, build_scoped_session

HTML_MAX_BYTES = 8 * 1024 * 1024


def _finite_number(value, *, integer=False):
    """Extractor metadata is untrusted; unknown sizes must remain unknown."""
    try:
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            return None
        return int(number) if integer else number
    except (TypeError, ValueError, OverflowError):
        return None


HLS_EXTS = {"m3u8"}

_X_POST_HOSTS = {
    "x.com",
    "www.x.com",
    "m.x.com",
    "mobile.x.com",
    "twitter.com",
    "www.twitter.com",
    "m.twitter.com",
    "mobile.twitter.com",
}
_X_POST_PATH = re.compile(
    r"^/(?:[^/]+/status|i/web/status|statuses)/\d+(?:/(?:video|photo)/\d+)?/?$"
)
_X_COOKIE_DOMAINS = ("x.com", "twitter.com", "twimg.com")


def is_x_post_url(url: str) -> bool:
    """Recognize actual X/Twitter post URLs, never lookalike domains."""
    try:
        parsed = urlparse(url.strip())
        return (
            parsed.scheme.lower() in {"http", "https"}
            and parsed.hostname is not None
            and parsed.hostname.lower() in _X_POST_HOSTS
            and not parsed.username
            and not parsed.password
            and bool(_X_POST_PATH.fullmatch(parsed.path))
        )
    except ValueError:
        return False


def _x_cookiejar_from(jar: CookieJar) -> CookieJar:
    """Retain only X media-site cookies; never attach the whole browser jar."""
    filtered = CookieJar()
    for cookie in jar:
        domain = cookie.domain.lstrip(".").lower()
        if any(
            domain == allowed or domain.endswith(f".{allowed}")
            for allowed in _X_COOKIE_DOMAINS
        ):
            filtered.set_cookie(copy.copy(cookie))
    return filtered


def _has_video_format(entry: Dict) -> bool:
    """Reject photos, audio-only entries, and unresolved embed pages."""
    for fmt in entry.get("formats") or []:
        if not isinstance(fmt, dict) or not fmt.get("url"):
            continue
        if (fmt.get("vcodec") or "").lower() == "none":
            continue
        if (fmt.get("ext") or "").lower() in {
            "m4a", "mp3", "aac", "opus", "ogg", "flac", "wav"
        }:
            continue
        return True
    return False


def _x_full_post_url(url: str) -> str:
    """A /video/N share link must still expose the full post playlist."""
    parsed = urlparse(url)
    path = re.sub(r"/(?:video|photo)/\d+/?$", "", parsed.path)
    return urlunparse(parsed._replace(path=path))


def _is_hls(url: str, ext: Optional[str] = None) -> bool:
    return (ext or _ext_of(url) or "").lower() in HLS_EXTS


def _is_dash(url: str, ext: Optional[str] = None) -> bool:
    return (ext or _ext_of(url) or "").lower() == "mpd"


def _ext_of(url: str) -> Optional[str]:
    tail = urlparse(url).path.rsplit("/", 1)[-1]
    if "." in tail:
        ext = tail.rsplit(".", 1)[-1].lower()
        if 1 <= len(ext) <= 5 and ext.isalnum():
            return ext
    return None


def _is_known_player_placeholder(url: str) -> bool:
    """Ignore demo assets bundled by web players, not the page's real media."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    return (host == "artplayer.org" or host.endswith(".artplayer.org")) and path.startswith("/assets/sample/")


HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

DIRECT_VIDEO_PATTERNS = re.compile(
    r"(?<![\w:])https?://[^\s\"'<>]+?\.(?:mp4|webm|mov|m4v|m3u8|mpd|mkv)"
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
    valid = _resolve_player_media_url(url, url)
    if not valid:
        return False
    parsed = urlparse(valid)
    host = (parsed.hostname or "").lower()
    if any(host == h or host.endswith(f".{h}") for h in EMBED_HOSTS):
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
    if not isinstance(data, dict):
        return None
    props = data.get("props")
    if not isinstance(props, dict):
        return None
    page_props = props.get("pageProps")
    if not isinstance(page_props, dict):
        return None
    ev = page_props.get("ev")
    if not isinstance(ev, dict):
        return None
    decoded = _decrypt_next_ev(ev)
    if not decoded:
        return None
    # Preserve this extractor's absolute-URL contract while applying the same
    # origin/port/scheme checks as the HTML and streamed-player paths. Invalid
    # Next data must not suppress a later valid TanStack player config.
    video_url = _resolve_player_media_url(
        decoded.get("videoUrl") or decoded.get("url"), ""
    )
    if not video_url:
        return None
    video = page_props.get("video")
    video = video if isinstance(video, dict) else {}
    title = video.get("nameZh") or video.get("name")
    return video_url, (title if isinstance(title, str) else None)


# TanStack Start streams the same player blob as JS object literals instead
# of __NEXT_DATA__ JSON. Read only that data; never evaluate page scripts.
_JSON_STRING_LITERAL = r'"(?:[^"\\]|\\.)*"'
_JS_SINGLE_STRING_LITERAL = r"'(?:[^'\\]|\\.)*'"
_TSR_EV_TOKENS = re.compile(
    rf"(?P<ev>(?<![\w$])(?:ev|\"ev\"|'ev')\s*:\s*"
    rf"\$R\[\d+\]\s*=(?![=>])\s*\{{"
    rf"(?P<fields>(?:{_JSON_STRING_LITERAL}|{_JS_SINGLE_STRING_LITERAL}|"
    rf"[^{{}}\"'`])*)\}})"
    rf"|{_JSON_STRING_LITERAL}|{_JS_SINGLE_STRING_LITERAL}"
    r"|`(?:[^`\\]|\\.)*`|//[^\n]*|/\*.*?\*/",
    re.DOTALL,
)
_TSR_EV_FIELD = re.compile(
    rf"\s*(?P<key>d|k|\"d\"|\"k\"|'d'|'k')\s*:\s*"
    rf"(?P<value>{_JSON_STRING_LITERAL}|-?(?:0|[1-9]\d*))\s*"
    r"(?P<separator>,|$)"
)


def _parse_tsr_ev_literal(fields: str) -> Optional[Dict]:
    """Accept only literal d/k fields belonging to one streamed ev object."""
    values: Dict = {}
    pos = 0
    while fields[pos:].strip():
        match = _TSR_EV_FIELD.match(fields, pos)
        if not match:
            return None
        key = match["key"].strip("\"'")
        if key in values:
            return None
        try:
            values[key] = json.loads(match["value"])
        except ValueError:
            return None
        pos = match.end()
    return values if set(values) == {"d", "k"} else None


def _resolve_player_media_url(value: object, page_url: str) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        return None
    try:
        original = urlparse(value)
        if original.scheme and (
            original.scheme.lower() not in {"http", "https"}
            or not original.netloc
        ):
            return None
        resolved = urljoin(page_url, value)
        parsed = urlparse(resolved)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        parsed.port  # Validate malformed / out-of-range ports as well.
        return resolved
    except ValueError:
        return None


def _extract_tanstack_ev_hls(html: str, page_url: str) -> Optional[str]:
    """Recover the streamed player's HLS URL, including relative API routes."""
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script", attrs={"data-tsr-stream-part": True}):
        text = script.get_text()
        if not re.match(
            r"\s*\$_TSR\s*\.\s*router\s*=(?![=>])\s*"
            r"\(\s*\$R\s*=>\s*\$R\[\d+\]\s*=(?![=>])\s*\{",
            text,
        ):
            continue
        for match in _TSR_EV_TOKENS.finditer(text):
            if not match["ev"]:
                continue
            ev = _parse_tsr_ev_literal(match["fields"])
            decoded = _decrypt_next_ev(ev) if ev else None
            if not decoded:
                continue
            video_url = _resolve_player_media_url(
                decoded.get("videoUrl") or decoded.get("url"), page_url
            )
            if video_url:
                return video_url
    return None


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
    x_playlist_index: Optional[int] = None
    x_auth_browser: Optional[str] = None
    x_cookiejar: Optional[CookieJar] = field(default=None, repr=False)
    hls_png_wrapped: bool = False
    is_dash: bool = False
    # Structured cookies keep their original origin/path/expiry restrictions.
    # Never turn this into a global Cookie header shared across media hosts.
    browser_cookies: List[Dict] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.width = _finite_number(self.width, integer=True)
        self.height = _finite_number(self.height, integer=True)
        self.duration = _finite_number(self.duration)
        formats = []
        for entry in self.formats if isinstance(self.formats, list) else []:
            if not isinstance(entry, dict):
                continue
            entry = dict(entry)
            for name in ("width", "height", "fps", "tbr", "abr", "vbr", "filesize", "filesize_approx"):
                if name in entry:
                    entry[name] = _finite_number(entry[name], integer=name in {"width", "height", "filesize", "filesize_approx"})
            formats.append(entry)
        self.formats = formats

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "--"


ProgressCallback = Callable[[str], None]


@dataclass
class _HtmlScanResult:
    items: List[VideoItem]
    iframes: List[str]
    html: str
    title: str
    poster: Optional[str]
    document_url: str
    base_url: str

    def __iter__(self):
        # Retain the five-value private scan API used by integrations/tests.
        return iter((self.items, self.iframes, self.html, self.title, self.poster))


class VideoScraper:
    """Discover videos on a given URL."""

    def __init__(self, timeout: int = 20) -> None:
        self.timeout = timeout
        self._cancel_event = None

    def _check_cancelled(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise InterruptedError("Scraping cancelled")

    def scrape(
        self,
        url: str,
        progress: Optional[ProgressCallback] = None,
        x_auth_browser: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> List[VideoItem]:
        url = url.strip()
        self._cancel_event = cancel_event
        def log(message):
            self._check_cancelled()
            if progress is not None:
                progress(message)
        self._check_cancelled()

        # A pasted media URL is already discovered. Never read its body as a
        # web page, or ask a generic extractor to scan the complete video.
        if _ext_of(url) in {"mp4", "webm", "mov", "m4v", "mkv", "m3u8", "mpd"}:
            direct = build_items_from_urls([url], [], url, urlparse(url).path.rsplit("/", 1)[-1])
            log(f"Found {len(direct)} direct media URL(s).")
            return direct

        if is_x_post_url(url):
            return self._scrape_x_with_ytdlp(url, log, x_auth_browser)

        results: List[VideoItem] = []
        seen_urls: set[str] = set()

        log("Trying yt-dlp extraction...")
        try:
            results.extend(self._scrape_with_ytdlp(url, log))
        except Exception as exc:  # noqa: BLE001
            log(f"yt-dlp failed: {exc}")

        for item in list(results):
            seen_urls.add(item.url)
            # Extractor items retain the page URL and all format metadata.
            # A later HTML candidate for one of those exact media URLs is a
            # duplicate format, not another discovered video. Do not compare
            # titles, hosts, or query-stripped URLs: different clips and signed
            # format URLs must remain distinct.
            for fmt in item.formats:
                media_url = _resolve_player_media_url(fmt.get("url"), url)
                if media_url:
                    seen_urls.add(media_url)

        log("Scanning page HTML for direct video links...")
        document_url = base_url = url
        try:
            scan = self._scrape_html(url, log)
            html_items, embed_iframes, html_text, page_title, poster = (
                scan
            )
            document_url = getattr(scan, "document_url", url)
            base_url = getattr(scan, "base_url", document_url)
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
                    media_urls = {
                        resolved for fmt in it.formats
                        if (resolved := _resolve_player_media_url(fmt.get("url"), iframe_url))
                    }
                    # HTML is scanned before embeds. Prefer the embed's full
                    # format metadata over an earlier direct copy of that same
                    # media; later hidden/config scans also see these URLs.
                    results = [entry for entry in results if not (
                        entry.is_direct and not entry.hls_png_wrapped and entry.url in media_urls)]
                    seen_urls.update(media_urls)
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
                        referer=document_url,
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
                link = _resolve_player_media_url(link, base_url)
                if not link or _is_known_player_placeholder(link) or link in seen_urls:
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
                        is_dash=_is_dash(link, ext),
                        referer=document_url,
                    )
                )
            if hidden_urls:
                log(f"Decoder recovered {len(hidden_urls)} hidden URL(s)")

        if html_text:
            log("Trying inline player-config HLS extractor...")
            try:
                config_hls = _extract_config_hls(html_text, base_url)
            except Exception as exc:  # noqa: BLE001
                log(f"Config HLS extractor failed: {exc}")
                config_hls = []
            new_hls: List[VideoItem] = []
            for link in config_hls:
                if link in seen_urls:
                    continue
                seen_urls.add(link)
                new_hls.append(
                    self._item_from_hls(link, url, page_title, poster, document_url)
                )
            if new_hls:
                # Real full-length streams go first so the UI selects one by
                # default instead of a preview teaser.
                results[:0] = new_hls
                log(
                    f"Player-config extractor recovered {len(new_hls)} "
                    f"HLS stream(s)"
                )
            if config_hls and any(item.url in config_hls for item in results):
                # Only this recognized player config establishes that a
                # preview-named asset is a teaser, rather than real media.
                results = [item for item in results if not _is_preview_teaser(item.url)]

        if html_text:
            streamed_player = False
            try:
                ev_hls = _extract_next_data_ev_hls(html_text)
            except Exception as exc:  # noqa: BLE001
                log(f"Next.js extractor failed: {exc}")
                ev_hls = None
            if not ev_hls:
                try:
                    streamed_url = _extract_tanstack_ev_hls(html_text, base_url)
                    ev_hls = (streamed_url, None) if streamed_url else None
                    streamed_player = bool(streamed_url)
                except Exception as exc:  # noqa: BLE001
                    log(f"TanStack extractor failed: {exc}")
                    ev_hls = None
            if ev_hls:
                ev_url, ev_title = ev_hls
                seen_urls.add(ev_url)
                # A disguised / extensionless manifest may already have been
                # scanned as a plain video. Upgrade it rather than letting
                # deduplication suppress the authoritative player config.
                existing = next((item for item in results if item.url == ev_url), None)
                if existing:
                    results = [item for item in results if item.url != ev_url]
                    existing.title = ev_title or page_title
                    existing.source_url = url
                    existing.referer = document_url
                    existing.ext = "m3u8"
                    existing.is_direct = True
                    existing.is_hls = True
                    existing.is_dash = False
                    if not existing.thumbnail:
                        existing.thumbnail = poster
                    main_video = existing
                else:
                    main_video = self._item_from_hls(
                        ev_url, url, ev_title or page_title, poster, document_url
                    )
                main_video.hls_png_wrapped = streamed_player
                results.insert(0, main_video)
                results = [item for item in results if item is main_video or not _is_preview_teaser(item.url)]
                log("Encrypted player-config extractor recovered the HLS stream")

        log(f"Found {len(results)} video(s).")
        return results

    def _scrape_x_with_ytdlp(
        self,
        url: str,
        log: ProgressCallback,
        x_auth_browser: Optional[str],
    ) -> List[VideoItem]:
        """Extract each X post video once, keeping its playlist identity."""
        check_cancelled = self._check_cancelled
        class _SilentLogger:
            def debug(self, _msg: str) -> None: check_cancelled()
            def info(self, _msg: str) -> None: check_cancelled()
            def warning(self, _msg: str) -> None: check_cancelled()
            def error(self, _msg: str) -> None: check_cancelled()

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": False,
            "noplaylist": False,
            "ignoreerrors": False,
            "logger": _SilentLogger(),
            "socket_timeout": self.timeout,
        }
        if x_auth_browser:
            ydl_opts["cookiesfrombrowser"] = (x_auth_browser,)

        log("Reading X post videos with yt-dlp...")
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(_x_full_post_url(url), download=False)
                cookies = (
                    _x_cookiejar_from(ydl.cookiejar) if x_auth_browser else None
                )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"X video extraction failed: {exc}") from exc

        if not isinstance(info, dict):
            raise RuntimeError("No video information was returned for this X post")

        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
        else:
            entries = [info]

        items: List[VideoItem] = []
        seen_media: set[str] = set()
        used_titles: set[str] = set()
        for index, entry in enumerate(entries, 1):
            if not isinstance(entry, dict) or not _has_video_format(entry):
                continue
            entry_index = entry.get("playlist_index")
            if not isinstance(entry_index, int) or entry_index < 1:
                entry_index = index
            # TwitterIE uses the media entity id for each playlist entry.
            # A duplicate card or variant of that entity must not add another
            # row, while a distinct entity keeps its original playlist index.
            media_id = entry.get("id")
            if not media_id or str(media_id) == str(info.get("id")):
                media_id = entry.get("thumbnail") or next(
                    (fmt.get("url") for fmt in entry.get("formats") or []
                     if isinstance(fmt, dict) and fmt.get("url")),
                    f"entry:{entry_index}",
                )
            media_key = str(media_id)
            if media_key in seen_media:
                continue
            seen_media.add(media_key)

            base_title = str(entry.get("title") or info.get("title") or "X video")
            title = base_title
            suffix = entry_index
            while title in used_titles:
                title = f"{base_title} #{suffix}"
                suffix += 1
            used_titles.add(title)

            items.append(
                VideoItem(
                    title=title,
                    url=url,
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
                    referer=url,
                    x_playlist_index=entry_index,
                    x_auth_browser=x_auth_browser,
                    x_cookiejar=cookies,
                )
            )

        if not items:
            raise RuntimeError(
                "No playable video found in this X post; it may require "
                "login, or the post may have no video"
            )
        log(f"Found {len(items)} X video(s).")
        return items

    def _item_from_hls(
        self,
        url: str,
        source_url: str,
        title: str,
        poster: Optional[str],
        referer: Optional[str] = None,
    ) -> VideoItem:
        return VideoItem(
            title=title,
            url=url,
            source_url=source_url,
            thumbnail=poster,
            ext="m3u8",
            is_direct=True,
            is_hls=True,
            referer=referer or source_url,
        )

    def _scrape_with_ytdlp(
        self,
        url: str,
        log: ProgressCallback,
    ) -> List[VideoItem]:
        check_cancelled = self._check_cancelled
        class _SilentLogger:
            def debug(self, _msg: str) -> None: check_cancelled()
            def info(self, _msg: str) -> None: check_cancelled()
            def warning(self, _msg: str) -> None: check_cancelled()
            def error(self, _msg: str) -> None: check_cancelled()

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": False,
            "noplaylist": False,
            "ignoreerrors": True,
            "logger": _SilentLogger(),
            "socket_timeout": self.timeout,
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
            self._check_cancelled()
            if not isinstance(entry, dict):
                continue
            format_urls = [
                fmt.get("url")
                for fmt in (entry.get("formats") or [])
                if isinstance(fmt, dict) and fmt.get("url")
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
    ) -> _HtmlScanResult:
        self._check_cancelled()
        session = build_scoped_session(pool=4)
        stopped = threading.Event()
        response_holder = []
        def cancel_request():
            while not stopped.wait(.05):
                if self._cancel_event is not None and self._cancel_event.is_set():
                    from .http_limits import abort_response
                    for response in response_holder:
                        abort_response(response)
                    session.close()
                    return
        watcher = threading.Thread(target=cancel_request, name="ScrapeCancellation", daemon=True)
        watcher.start()
        resp = None
        try:
            resp = session.get(url, headers=HTTP_HEADERS, timeout=(min(10, self.timeout), min(5, self.timeout)), stream=True)
            response_holder.append(resp)
            resp.raise_for_status()
            document_url = resp.url if isinstance(resp.url, str) else url
            content_type = str((getattr(resp, "headers", {}) or {}).get("Content-Type", "")).split(";", 1)[0].lower()
            if content_type.startswith(("video/", "audio/")) or content_type in {"application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml"}:
                items = build_items_from_urls([document_url], [], url, urlparse(document_url).path.rsplit("/", 1)[-1])
                if items and content_type in {"application/vnd.apple.mpegurl", "application/x-mpegurl"}:
                    items[0].is_hls, items[0].ext = True, "m3u8"
                if items and content_type == "application/dash+xml":
                    items[0].is_dash, items[0].ext = True, "mpd"
                return _HtmlScanResult(items, [], "", url, None, document_url, document_url)
            body = read_limited_response(resp, HTML_MAX_BYTES, self.timeout, lambda: self._cancel_event is not None and self._cancel_event.is_set())
            if b"\x00" in body[:1024] or (content_type and content_type not in {"text/html", "application/xhtml+xml", "text/plain", "application/octet-stream"}):
                raise ValueError("The response is not an HTML page")
            html = body.decode(getattr(resp, "encoding", None) or "utf-8", errors="replace")
        finally:
            stopped.set()
            if resp is not None:
                resp.close()
            session.close()
        # requests follows redirects. Relative resources belong to the final
        # document; <base> affects URL resolution, never the HTTP Referer.
        document_url = resp.url if isinstance(resp.url, str) else url

        soup = BeautifulSoup(html, "lxml")
        base = soup.find("base", href=True)
        base_url = (
            _resolve_player_media_url(base.get("href"), document_url) if base else None
        ) or document_url
        candidates: List[str] = []
        embed_iframes: List[str] = []
        poster: Optional[str] = None

        # Attribute names that commonly hide the real video URL.
        media_attrs = ("src", "data-src", "data-url", "data-mp4", "data-video")

        for video in soup.find_all("video"):
            if not poster:
                p = video.get("poster") or video.get("data-poster")
                if p:
                    poster = _resolve_player_media_url(p, base_url)
            for attr in media_attrs:
                value = video.get(attr)
                if value:
                    candidates.append(value)
            for source in video.find_all("source"):
                for attr in media_attrs:
                    value = source.get(attr)
                    if value:
                        candidates.append(value)

        page_host = (urlparse(document_url).hostname or "").lower()
        skipped_hosts: set[str] = set()
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src") or iframe.get("data-src")
            if not src:
                continue
            full = _resolve_player_media_url(src, base_url)
            if not full:
                continue
            parsed = urlparse(full)
            host = (parsed.hostname or "").lower()
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
            if prop in video_meta_props:
                # Only count og:video if it actually points to a media file;
                # many sites use og:video as an embed page URL, which would
                # produce duplicate / un-downloadable list entries.
                if media_ext_re.search(content):
                    candidates.append(content)
            elif (
                not poster
                and prop in image_meta_props
            ):
                poster = _resolve_player_media_url(content, base_url)

        for match in DIRECT_VIDEO_PATTERNS.finditer(html):
            candidates.append(match.group(0))

        unique: List[str] = []
        seen: set[str] = set()
        for cand in candidates:
            if not cand:
                continue
            cand = _resolve_player_media_url(html_module.unescape(cand), base_url)
            if not cand or _is_known_player_placeholder(cand):
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
                    is_dash=_is_dash(link, ext),
                    referer=document_url,
                )
            )
        return _HtmlScanResult(
            items, unique_iframes, html, page_title, poster, document_url, base_url
        )


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
        link = _resolve_player_media_url(link, source_url)
        if not link or link in seen:
            return
        if is_direct and _ext_of(link) in {"ts", "m4s"}:
            # A network request for one streaming fragment is not evidence
            # of a complete movie; retain manifests/full media candidates.
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
                is_dash=_is_dash(link, ext) if is_direct else False,
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
