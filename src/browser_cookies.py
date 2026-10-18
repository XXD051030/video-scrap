"""Scoped browser-cookie snapshots shared by preview and download requests.

Never turn a browser snapshot into a literal Cookie header.  Requests normally
merges jars into a fresh jar with its default (permissive host-only) policy, so
the session below reapplies the scope policy for every network hop, including
redirects.
"""

from __future__ import annotations

import copy
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from typing import Iterable, Mapping, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter


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


def build_scoped_session(pool: int = 16) -> ScopedCookieSession:
    session = ScopedCookieSession()
    pool = max(4, int(pool))
    adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
