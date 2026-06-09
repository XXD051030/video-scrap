"""Shared ``requests.Session`` factory with a sized connection pool.

A single scrape / download / playback session makes many requests to the
same host -- an HLS manifest fans out into hundreds of same-host segment
fetches, and the byte-range prefetcher hammers one CDN. Reusing a Session
keeps TCP + TLS connections alive (keep-alive) across all of them instead
of paying a fresh handshake every time.

``requests.Session`` is safe to share across threads for issuing requests;
we just size the pool to the component's concurrency so urllib3 doesn't
discard and re-open connections under load.
"""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter


def build_session(pool: int = 16) -> requests.Session:
    """Return a Session whose HTTP(S) adapter pools ``pool`` connections."""
    pool = max(4, int(pool))
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=pool,
        pool_maxsize=pool,
        max_retries=0,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
