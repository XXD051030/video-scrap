"""Scoped browser-cookie snapshots shared by preview and download requests.

Never turn a browser snapshot into a literal Cookie header.  Requests normally
merges jars into a fresh jar with its default (permissive host-only) policy, so
the session below reapplies the scope policy for every network hop, including
redirects.
"""

from __future__ import annotations

import copy
import threading
import socket
import weakref
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from typing import Iterable, Mapping, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ReadTimeoutError

from .http_limits import origin_key

# Only these public request metadata headers may leave the original origin.
# Custom extractor headers are credentials unless explicitly known otherwise.
PUBLIC_CROSS_ORIGIN_HEADERS = frozenset({
    "accept", "accept-encoding", "accept-language", "user-agent", "referer",
    "origin", "range", "if-range", "if-match", "if-unmodified-since",
    "cache-control", "pragma", "connection", "content-length", "content-type",
})


def scoped_headers(headers: Mapping[str, str], source_url: str, target_url: str) -> dict:
    if origin_key(source_url) == origin_key(target_url):
        return dict(headers)
    return {
        key: value for key, value in headers.items()
        if key.lower() in PUBLIC_CROSS_ORIGIN_HEADERS
    }


class _BrowserCookiePolicy(DefaultCookiePolicy):
    def __init__(self) -> None:
        super().__init__(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain)

    def return_ok_domain(self, cookie, request) -> bool:
        if not cookie.domain_specified:
            host = (urlparse(request.get_full_url()).hostname or "").lower()
            return host == cookie.domain.lower().lstrip(".")
        return super().return_ok_domain(cookie, request)


def browser_cookiejar(
    records: Optional[Iterable[Mapping]] = None,
    *, base: Optional[CookieJar] = None,
) -> CookieJar:
    """Build a private jar without widening domain, path or secure constraints.

    ``host_only`` is explicit when exported by the browser.  Older snapshots
    without it are treated as host-only unless their domain starts with a dot.
    Invalid/incomplete records are ignored rather than creating domainless
    cookies that requests would send to every host.
    """
    jar = CookieJar(policy=_BrowserCookiePolicy())
    if base is not None:
        for cookie in base:
            if cookie.domain:
                jar.set_cookie(copy.copy(cookie))
    for record in records or ():
        if not isinstance(record, Mapping):
            continue
        name, value = record.get("name"), record.get("value")
        domain = record.get("domain")
        path = record.get("path") or "/"
        if not all(isinstance(part, str) for part in (name, value, domain, path)):
            continue
        if (
            not name or not domain or not path.startswith("/")
            or any(char in name for char in "\r\n\t;= ")
            or any(char in value for char in "\r\n")
            or any(char in domain for char in "/\\:@ \r\n\t")
        ):
            continue
        domain = domain.lower()
        host_only = record.get("host_only", record.get("hostOnly"))
        if host_only is None:
            host_only = not domain.startswith(".")
        expires = record.get("expires")
        if expires is not None:
            try:
                expires = int(expires)
            except (ValueError, TypeError, OverflowError):
                continue
            # Qt represents a session cookie with no expiration, not zero.
            if expires <= 0:
                expires = 0
        jar.set_cookie(Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain.lstrip(".") if host_only else domain,
            domain_specified=not bool(host_only),
            domain_initial_dot=not bool(host_only) and domain.startswith("."),
            path=path, path_specified=True, secure=bool(record.get("secure", False)),
            expires=expires, discard=expires is None, comment=None,
            comment_url=None, rest={"HttpOnly": bool(record.get("http_only", False))},
            rfc2109=False,
        ))
    return jar


class ScopedCookieSession(requests.Session):
    """A pooled requests session enforcing browser scope on every send."""

    def send(self, request, **kwargs):
        jar = getattr(request, "_cookies", None)
        if isinstance(jar, CookieJar):
            jar.set_policy(_BrowserCookiePolicy())
            request.headers.pop("Cookie", None)
            request.prepare_cookies(jar)
        return super().send(request, **kwargs)

    def rebuild_auth(self, prepared_request, response):
        super().rebuild_auth(prepared_request, response)
        previous = getattr(getattr(response, "request", None), "url", "")
        if origin_key(previous) != origin_key(prepared_request.url):
            for name in list(prepared_request.headers):
                if name.lower() not in PUBLIC_CROSS_ORIGIN_HEADERS | {"cookie"}:
                    prepared_request.headers.pop(name, None)
        # Cookie is rebuilt separately by requests and re-scoped in send().


class _CancelableAdapter(HTTPAdapter):
    """Own pooled sockets, including requests still waiting for headers."""

    HEADER_DEADLINE_SECONDS = 30.0

    def __init__(self, **kwargs):
        self._active_connections = set()
        self._connections_lock = threading.RLock()
        self._closed = False
        owner = self

        class OwnedConnection:
            def __init__(self, *args, **kwargs):
                self._lease_lock = threading.RLock()
                self._lease_generation = 0
                super().__init__(*args, **kwargs)

            def request(self, *args, **kwargs):
                # Taking a pooled connection gives it a new owner before any
                # request bytes are sent. A previous response may already have
                # released the connection while its reader is still unwinding.
                with self._lease_lock:
                    self._lease_generation += 1
                return super().request(*args, **kwargs)

            def connect(self):
                # HTTPS validation may connect before request() is entered.
                with self._lease_lock:
                    self._lease_generation += 1
                super().connect()
                with owner._connections_lock:
                    closed = owner._closed
                    if not closed:
                        owner._active_connections.add(self)
                if closed:
                    self.close()
                    raise requests.ConnectionError("Session was closed")

            def getresponse(self):
                with self._lease_lock:
                    connection = self.sock
                    generation = self._lease_generation
                expired = threading.Event()
                lifecycle_lock = threading.Lock()
                active = True
                def abort_headers():
                    # cancel() cannot revoke a Timer callback that has already
                    # passed its internal cancellation check. Completion and
                    # shutdown therefore share an explicit ownership lock.
                    with lifecycle_lock:
                        if not active:
                            return
                        expired.set()
                        if connection is not None:
                            try:
                                connection.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                timer = threading.Timer(owner.HEADER_DEADLINE_SECONDS, abort_headers)
                timer.daemon = True
                timer.start()
                try:
                    response = super().getresponse()
                    with lifecycle_lock:
                        active = False
                        rejected = expired.is_set() or owner._closed
                    if rejected:
                        response.close()
                        raise ReadTimeoutError(None, self.host, "Response header deadline or session cancellation")
                    response_ref = weakref.ref(response)
                    def abort_owned_response():
                        # EOF can return this connection to urllib3's pool
                        # inside read1(), before the reader's finally block.
                        # request()/connect() and this check/shutdown must share
                        # ownership, including close() of the response wrapper.
                        with self._lease_lock:
                            current = response_ref()
                            if (current is None or current.closed
                                    or generation != self._lease_generation
                                    or (self.sock is not None and self.sock is not connection)):
                                return False
                            if connection is not None:
                                try:
                                    connection.shutdown(socket.SHUT_RDWR)
                                except OSError:
                                    pass
                            self.close()
                            current.close()
                            return True
                    response._abort_owned_response = abort_owned_response
                    return response
                finally:
                    with lifecycle_lock:
                        active = False
                    timer.cancel()

            def close(self):
                # A normal HTTP/1.0 close detaches the connection while its
                # response still owns a buffered socket. Do not shutdown here.
                with self._lease_lock:
                    with owner._connections_lock:
                        owner._active_connections.discard(self)
                    super().close()

        class OwnedHTTPConnection(OwnedConnection, HTTPConnection):
            pass
        class OwnedHTTPSConnection(OwnedConnection, HTTPSConnection):
            pass
        class OwnedHTTPPool(HTTPConnectionPool):
            ConnectionCls = OwnedHTTPConnection
        class OwnedHTTPSPool(HTTPSConnectionPool):
            ConnectionCls = OwnedHTTPSConnection
        self._pool_classes = {"http": OwnedHTTPPool, "https": OwnedHTTPSPool}
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = dict(self._pool_classes)

    def proxy_manager_for(self, *args, **kwargs):
        manager = super().proxy_manager_for(*args, **kwargs)
        manager.pool_classes_by_scheme = dict(self._pool_classes)
        return manager

    def close(self):
        with self._connections_lock:
            self._closed = True
            connections = list(self._active_connections)
        for connection in connections:
            active_socket = getattr(connection, "sock", None)
            if active_socket is not None:
                try:
                    active_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()
        super().close()


def build_scoped_session(pool: int = 16) -> ScopedCookieSession:
    session = ScopedCookieSession()
    pool = max(4, int(pool))
    adapter = _CancelableAdapter(pool_connections=pool, pool_maxsize=pool, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
