"""Shared ``requests.Session`` factory with a sized connection pool.

A single scrape / download / playback session makes many requests to the
same host -- an HLS manifest fans out into hundreds of same-host segment
fetches, and the byte-range prefetcher hammers one CDN. Reusing a Session
keeps TCP + TLS connections alive (keep-alive) across all of them instead
of paying a fresh handshake every time.

The connection pool supports concurrent requests. Session configuration must
be completed before sharing it and must not be mutated during active requests;
this is not a general guarantee that requests.Session is thread safe.
"""

from __future__ import annotations

from .browser_cookies import ScopedCookieSession, build_scoped_session


def build_session(pool: int = 16) -> ScopedCookieSession:
    """Return a Session whose HTTP(S) adapter pools ``pool`` connections."""
    return build_scoped_session(pool)
