"""Immutable, task-private HLS fallback input with validated HTTP resources.

Backends see only random loopback routes. Playlists never refresh upstream;
every referenced child playlist must already have an inspected snapshot. No
upstream URI can opt into a local file/data protocol through a rewritten list.
"""
from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import secrets
import socket
import threading
import tempfile
from pathlib import Path
from urllib.parse import urljoin

from .http_limits import resource_identity_url

MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_PLAYLISTS = 1024
MAX_SNAPSHOT_RESOURCES = 200_000
MAX_RESOURCE_BYTES = 64 * 1024 * 1024
_URI = re.compile(r'(?<![A-Z0-9-])URI=("[^"]*"|[^,]*)')


@dataclass(frozen=True)
class ManifestSnapshot:
    base_url: str
    text: str


@dataclass(frozen=True)
class _Resource:
    url: str
    kind: str
    key_url: str | None = None
    iv: bytes | None = None


class HlsSnapshotServer:
    def __init__(self, snapshots, fetch, validate_aes, parse_iv, fatal_types,
                 close_fetch=None):
        self.snapshots = dict(snapshots)
        self.fetch = fetch
        self.validate_aes = validate_aes
        self.parse_iv = parse_iv
        self.fatal_types = fatal_types
        self.close_fetch = close_fetch
        self._server = self._thread = None
        self._stopped = threading.Event()
        self._slots = threading.BoundedSemaphore(4)
        self._lock = threading.Lock()
        self._key_lock = threading.Lock()
        self._failure = None
        self._keys = {}
        self._routes = {}
        self._manifest_paths = {}
        self._resource_paths = {}
        self._resource_locks = {}
        self._files = {}
        self._temporary = None
        self._clients = set()

    def __enter__(self):
        unique = set(self.snapshots.values())
        if (len(unique) > MAX_SNAPSHOT_PLAYLISTS or
                sum(len(item.text.encode('utf-8')) for item in unique) > MAX_SNAPSHOT_BYTES):
            raise ValueError('HLS fallback snapshot exceeds its playlist budget')
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(5)
                super().setup()
                with owner._lock:
                    owner._clients.add(self.connection)

            def finish(self):
                try:
                    super().finish()
                finally:
                    with owner._lock:
                        owner._clients.discard(self.connection)

            def log_message(self, *_args):
                pass

            def do_GET(self):
                owner._serve(self, body=True)

            def do_HEAD(self):
                owner._serve(self, body=False)

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._server.daemon_threads = False
        self._server.block_on_close = True
        self.base_url = f'http://127.0.0.1:{self._server.server_port}'
        try:
            self._temporary = tempfile.TemporaryDirectory(prefix='video-scrap-hls-snapshot-')
            paths = {item: '/' + secrets.token_urlsafe(18) + '.m3u8' for item in unique}
            for url, item in self.snapshots.items():
                self._manifest_paths[self._absolute(url, url)] = paths[item]
            for item, path in paths.items():
                self._routes[path] = self._rewrite(item).encode('utf-8')
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            name='HlsFallbackSnapshot', daemon=True)
            self._thread.start()
            return self
        except BaseException:
            self._server.server_close()
            self._server = None
            if self._temporary is not None:
                self._temporary.cleanup()
            raise

    def __exit__(self, *_args):
        self._stopped.set()
        if self.close_fetch is not None:
            self.close_fetch()
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(1)
        self._routes.clear()
        self._keys.clear()
        if self._temporary is not None:
            self._temporary.cleanup()

    @staticmethod
    def _absolute(base, uri):
        return resource_identity_url(None, urljoin(base, uri))

    def url(self, upstream):
        return self.base_url + self._manifest_paths[self._absolute(upstream, upstream)]

    def raise_if_failed(self):
        with self._lock:
            failure = self._failure
        if failure is not None:
            kind, message = failure
            raise kind(message)

    def _resource(self, base, uri, kind, key_url=None, iv=None):
        absolute = self._absolute(base, uri)
        if kind == 'manifest':
            if absolute not in self._manifest_paths:
                raise ValueError('HLS fallback referenced an uninspected child playlist')
            return self.base_url + self._manifest_paths[absolute]
        resource = _Resource(absolute, kind, key_url, iv)
        path = self._resource_paths.get(resource)
        if path is None:
            if len(self._resource_paths) >= MAX_SNAPSHOT_RESOURCES:
                raise ValueError('HLS fallback exceeds its resource budget')
            extension = '.key' if kind == 'key' else '.mp4' if kind == 'map' else '.ts'
            path = '/' + secrets.token_urlsafe(18) + extension
            self._resource_paths[resource] = path
            self._routes[path] = resource
            self._resource_locks[resource] = threading.Lock()
        return self.base_url + path

    def _rewrite(self, snapshot):
        lines = []
        has_segments = False
        pending_variant = False
        key_url = iv_text = None
        sequence = 0
        for raw in snapshot.text.splitlines():
            line = raw.strip()
            if line.startswith('#EXTINF:'):
                has_segments = True
            if (line.startswith('#EXT-X-BYTERANGE:') or
                    (line.startswith('#EXT-X-MAP:') and 'BYTERANGE=' in line)):
                raise self.fatal_types[0]('Ranged playlists must use the validated native HLS path')
            if line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
                sequence = int(line.split(':', 1)[1])
                if not 0 <= sequence < 2 ** 128:
                    raise ValueError('Invalid HLS media sequence')
            if line.startswith('#EXT-X-KEY:'):
                method = re.search(r'(?:^|,)METHOD=([^,]+)', line.split(':', 1)[1])
                uri = _URI.search(line)
                iv = re.search(r'(?:^|,)IV=([^,]+)', line.split(':', 1)[1])
                if method is None:
                    raise ValueError('HLS key method is missing')
                if method[1] == 'AES-128':
                    if uri is None:
                        raise ValueError('AES-128 key URI is missing')
                    key_url = self._absolute(snapshot.base_url, uri[1].strip('"'))
                    iv_text = iv[1] if iv else None
                else:
                    key_url = iv_text = None
            if line.startswith('#EXT-X-STREAM-INF:'):
                pending_variant = True
            if line.startswith('#'):
                if line.startswith(('#EXT-X-MEDIA:', '#EXT-X-I-FRAME-STREAM-INF:')):
                    kind = 'manifest'
                elif line.startswith(('#EXT-X-KEY:', '#EXT-X-SESSION-KEY:')):
                    kind = 'key'
                elif line.startswith('#EXT-X-MAP:'):
                    kind = 'map'
                else:
                    kind = 'media'
                if kind == 'map' and key_url is not None and iv_text is None:
                    raise ValueError('An AES-128 HLS initialization section requires an explicit IV')
                iv = self.parse_iv(iv_text, sequence) if key_url is not None and kind == 'map' else None
                line = _URI.sub(lambda match: 'URI="' + self._resource(
                    snapshot.base_url, match[1].strip('"'), kind,
                    key_url if kind == 'map' else None, iv,
                ) + '"', line)
            elif line:
                iv = self.parse_iv(iv_text, sequence) if key_url is not None else None
                line = self._resource(snapshot.base_url, line,
                                      'manifest' if pending_variant else 'media', key_url, iv)
                if not pending_variant:
                    sequence += 1
                pending_variant = False
            lines.append(line)
        # The native backend downloads the inspected live window as a finite
        # task. A frozen fallback playlist must terminate at that same window,
        # rather than asking FFmpeg to reload an immutable live list forever.
        if has_segments and '#EXT-X-ENDLIST' not in lines:
            lines.append('#EXT-X-ENDLIST')
        return '\n'.join(lines) + '\n'

    def _key(self, url):
        with self._key_lock:
            if self._stopped.is_set():
                raise InterruptedError('HLS fallback cancelled')
            key = self._keys.get(url)
            if key is None:
                key = self.fetch(url, 16, self._stopped.is_set)
                if len(key) != 16:
                    raise self.fatal_types[0]('AES-128 key must contain exactly 16 bytes')
                self._keys[url] = key
            return key

    def _resource_file(self, resource):
        # A resumed backend request must see the same whole response as its
        # first request. Store one validated representation, not separate
        # upstream responses for each backend Range/retry.
        with self._resource_locks[resource]:
            if self._stopped.is_set():
                raise InterruptedError('HLS fallback cancelled')
            path = self._files.get(resource)
            if path is not None:
                return path
            payload = (self._key(resource.url) if resource.kind == 'key'
                       else self.fetch(resource.url, MAX_RESOURCE_BYTES, self._stopped.is_set))
            if not payload:
                raise self.fatal_types[0]('HLS fallback resource contains no bytes')
            if resource.key_url is not None:
                self.validate_aes(payload, self._key(resource.key_url), resource.iv)
            path = Path(self._temporary.name) / secrets.token_hex(18)
            try:
                with path.open('xb') as target:
                    target.write(payload)
                if self._stopped.is_set():
                    raise InterruptedError('HLS fallback cancelled')
                self._files[resource] = path
                return path
            except BaseException:
                path.unlink(missing_ok=True)
                raise

    def _serve(self, request, *, body):
        route = self._routes.get(request.path)
        if route is None or self._stopped.is_set():
            request.send_error(404)
            return
        if not self._slots.acquire(timeout=.1):
            request.send_error(503, 'HLS fallback resource slots are busy')
            return
        payload = None
        stream = None
        headers_sent = False
        try:
            if isinstance(route, bytes):
                payload, mime = route, 'application/vnd.apple.mpegurl'
                total = len(payload)
            else:
                path = self._resource_file(route)
                total = path.stat().st_size
                stream = path.open('rb')
                mime = 'application/octet-stream'
            start, end, status = 0, total - 1, 200
            span = request.headers.get('Range')
            if span:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', span)
                if (match is None or not any(match.groups()) or
                        any(len(part) > 20 for part in match.groups())):
                    request.send_error(416)
                    return
                if match[1]:
                    start = int(match[1])
                    end = min(end, int(match[2])) if match[2] else end
                else:
                    count = int(match[2])
                    start = max(0, total - count)
                    if count <= 0:
                        request.send_error(416)
                        return
                if start > end:
                    request.send_error(416)
                    return
                status = 206
            request.send_response(status)
            request.send_header('Content-Type', mime)
            request.send_header('Content-Length', str(end - start + 1))
            if status == 206:
                request.send_header('Content-Range', f'bytes {start}-{end}/{total}')
            request.end_headers()
            headers_sent = True
            if body:
                if stream is not None:
                    stream.seek(start)
                for offset in range(start, end + 1, 64 * 1024):
                    if self._stopped.is_set():
                        return
                    length = min(64 * 1024, end + 1 - offset)
                    chunk = stream.read(length) if stream is not None else payload[offset:offset + length]
                    if len(chunk) != length:
                        raise OSError('HLS fallback resource was truncated')
                    request.wfile.write(chunk)
        except Exception as exc:
            if isinstance(exc, self.fatal_types):
                with self._lock:
                    # Keep diagnostic data, not a traceback retaining a large
                    # payload or the response that produced it.
                    self._failure = (type(exc), str(exc))
            if not headers_sent:
                try:
                    request.send_error(502, 'HLS fallback resource could not be validated')
                except OSError:
                    pass
        finally:
            if stream is not None:
                stream.close()
            payload = None
            self._slots.release()
