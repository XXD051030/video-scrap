"""Offline checks for byte-range validation in the playback cache.

Run directly with ``.venv/bin/python tests/test_media_proxy_ranges.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.media_proxy import MediaProxyServer, _Entry, _HlsPrefetcher, _Prefetcher


class FakeResponse:
    def __init__(
        self,
        status: int,
        chunks: list[bytes],
        content_range: str | None = None,
        content_length: int | None = None,
        content_encoding: str | None = None,
    ) -> None:
        self.status_code = status
        self.headers: dict[str, str] = {}
        if content_range is not None:
            self.headers["Content-Range"] = content_range
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        if content_encoding is not None:
            self.headers["Content-Encoding"] = content_encoding
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size: int):
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.ranges: list[str | None] = []
        self.accept_encodings: list[str | None] = []

    def get(self, *_args, **kwargs) -> FakeResponse:
        self.ranges.append(kwargs["headers"].get("Range"))
        self.accept_encodings.append(kwargs["headers"].get("Accept-Encoding"))
        return self.response

    def close(self) -> None:
        pass


def _with_entry(check) -> None:
    proxy = MediaProxyServer()
    entry = _Entry("https://example.test/video.mp4", {}, proxy.cache_dir)
    original_session = proxy._session
    try:
        check(proxy, entry)
    finally:
        entry.close()
        proxy._session = original_session
        proxy.stop()


def test_player_rejects_invalid_206_ranges_without_caching() -> None:
    cases = [
        "bytes 0-3/16",     # Starts before the requested offset.
        "bytes 8-12/16",    # Extends beyond the requested end.
        "bytes 8-11/11",    # The declared total is too small.
        "bytes 11-8/16",    # Reversed interval.
        "bytes 8-11/16 junk",  # Trailing malformed text.
        None,                 # 206 without Content-Range.
    ]
    for content_range in cases:
        def check(proxy: MediaProxyServer, entry: _Entry) -> None:
            response = FakeResponse(206, [b"ABCD"], content_range)
            session = FakeSession(response)
            proxy._session = session
            assert b"".join(proxy._fetch_range(entry, 8, 11)) == b""
            assert entry.cached_bytes() == 0
            assert entry.total_size is None
            assert session.ranges == ["bytes=8-11"]
            assert session.accept_encodings == ["identity"]
            assert response.closed

        _with_entry(check)


def test_player_short_or_oversized_206_caches_only_verified_prefix() -> None:
    for body, expected in [([b"AB"], b"AB"), ([b"ABC", b"DE"], b"ABCD")]:
        def check(proxy: MediaProxyServer, entry: _Entry) -> None:
            response = FakeResponse(206, body, "bytes 8-11/16")
            proxy._session = FakeSession(response)
            assert b"".join(proxy._fetch_range(entry, 8, 11)) == expected
            assert entry.read_at(8, len(expected)) == expected
            assert entry._intervals == [(8, 8 + len(expected))]
            assert entry.cached_run_end(8 + len(expected)) == 8 + len(expected)
            assert entry.total_size == 16
            assert response.closed

        _with_entry(check)


def test_player_valid_206_and_200_fallback() -> None:
    def check(proxy: MediaProxyServer, entry: _Entry) -> None:
        response = FakeResponse(206, [b"AB", b"CD"], "bytes 8-11/16")
        proxy._session = FakeSession(response)
        assert b"".join(proxy._fetch_range(entry, 8, 11)) == b"ABCD"
        assert entry.read_at(8, 4) == b"ABCD"
        assert entry._intervals == [(8, 12)]
        assert entry.total_size == 16

        changed = FakeResponse(200, [b"0123456789"], content_length=10)
        proxy._session = FakeSession(changed)
        assert b"".join(proxy._fetch_range(entry, 3, 5)) == b""
        assert entry._intervals == [(8, 12)]

        full = FakeResponse(200, [b"0123456789ABCDEF"], content_length=16)
        proxy._session = FakeSession(full)
        assert b"".join(proxy._fetch_range(entry, 3, 5)) == b"345"
        assert entry.cached_run_end(0) >= 6

    _with_entry(check)


def test_prefetch_rejects_invalid_206_range() -> None:
    cases = ["bytes 0-3/16", "bytes 8-12/16", "bytes 8-11/16 junk"]
    for content_range in cases:
        def check(proxy: MediaProxyServer, entry: _Entry) -> None:
            response = FakeResponse(206, [b"ABCD"], content_range)
            session = FakeSession(response)
            prefetch = _Prefetcher(entry, lambda: 128, session)
            prefetch._fetch_into_cache(8, 11)
            assert entry.cached_bytes() == 0
            assert entry.total_size is None
            assert session.ranges == ["bytes=8-11"]
            assert session.accept_encodings == ["identity"]
            assert response.closed

        _with_entry(check)


def test_prefetch_valid_206_and_200_fallback() -> None:
    def check(proxy: MediaProxyServer, entry: _Entry) -> None:
        response = FakeResponse(206, [b"AB", b"CD"], "bytes 8-11/16")
        session = FakeSession(response)
        prefetch = _Prefetcher(entry, lambda: 128, session)
        prefetch._fetch_into_cache(8, 11)
        assert entry.read_at(8, 4) == b"ABCD"
        assert entry._intervals == [(8, 12)]
        assert entry.total_size == 16

        changed = FakeResponse(200, [b"0123456789"], content_length=10)
        prefetch.session = FakeSession(changed)
        prefetch._fetch_into_cache(0, 3)
        assert entry._intervals == [(8, 12)]

        full = FakeResponse(200, [b"0123456789ABCDEF"], content_length=16)
        prefetch.session = FakeSession(full)
        prefetch._fetch_into_cache(0, 3)
        assert entry.cached_run_end(0) == 16

    _with_entry(check)


def test_200_without_content_length_keeps_streaming_fallback() -> None:
    def check(proxy: MediaProxyServer, entry: _Entry) -> None:
        entry.total_size = 10
        response = FakeResponse(200, [b"0123456789"])
        proxy._session = FakeSession(response)
        assert b"".join(proxy._fetch_range(entry, 3, 5)) == b"345"
        assert entry.cached_run_end(0) == 10

    _with_entry(check)


def test_prefetch_short_or_oversized_206_caches_only_verified_prefix() -> None:
    for body, expected in [([b"AB"], b"AB"), ([b"ABC", b"DE"], b"ABCD")]:
        def check(proxy: MediaProxyServer, entry: _Entry) -> None:
            response = FakeResponse(206, body, "bytes 8-11/16")
            prefetch = _Prefetcher(entry, lambda: 128, FakeSession(response))
            prefetch._fetch_into_cache(8, 11)
            assert entry.read_at(8, len(expected)) == expected
            assert entry._intervals == [(8, 8 + len(expected))]
            assert entry.cached_run_end(8 + len(expected)) == 8 + len(expected)

        _with_entry(check)


def test_non_identity_206_and_changed_total_are_rejected() -> None:
    def check(proxy: MediaProxyServer, entry: _Entry) -> None:
        entry.total_size = 20
        for response in [
            FakeResponse(206, [b"ABCD"], "bytes 8-11/16"),
            FakeResponse(206, [b"ABCD"], "bytes 8-11/20", content_encoding="gzip"),
        ]:
            proxy._session = FakeSession(response)
            assert b"".join(proxy._fetch_range(entry, 8, 11)) == b""
            prefetch = _Prefetcher(entry, lambda: 128, FakeSession(response))
            prefetch._fetch_into_cache(8, 11)
            assert entry.cached_bytes() == 0

    _with_entry(check)


def test_hls_prefetch_rejects_unrequested_partial_response() -> None:
    def check(proxy: MediaProxyServer, entry: _Entry) -> None:
        response = FakeResponse(206, [b"ABCD"], "bytes 8-11/16")
        session = FakeSession(response)
        prefetch = _HlsPrefetcher(entry, lambda: 60, session)
        prefetch._fetch_segment(entry)
        assert session.ranges == [None]
        assert session.accept_encodings == ["identity"]
        assert entry.cached_bytes() == 0

    _with_entry(check)


def test_encoded_200_body_is_not_used_as_byte_addressed_cache() -> None:
    def check(proxy: MediaProxyServer, entry: _Entry) -> None:
        response = FakeResponse(
            200, [b"ABCD"], content_length=4, content_encoding="gzip"
        )
        proxy._session = FakeSession(response)
        assert b"".join(proxy._fetch_range(entry, 0, 3)) == b""

        prefetch = _Prefetcher(entry, lambda: 128, FakeSession(response))
        prefetch._fetch_into_cache(0, 3)
        proxy._probe_total_size(entry)

        hls = _HlsPrefetcher(entry, lambda: 60, FakeSession(response))
        hls._fetch_segment(entry)
        assert entry.cached_bytes() == 0
        assert entry.total_size is None

    _with_entry(check)


def test_head_error_or_encoded_length_cannot_set_cache_size() -> None:
    class HeadSession:
        def __init__(self, response: FakeResponse) -> None:
            self.response = response
            self.accept_encoding = None

        def request(self, **kwargs) -> FakeResponse:
            self.accept_encoding = kwargs["headers"].get("Accept-Encoding")
            return self.response

    class HeadRequest:
        def send_response(self, _status: int) -> None:
            pass

        def send_header(self, _key: str, _value: str) -> None:
            pass

        def end_headers(self) -> None:
            pass

    for status, encoding, expected in [
        (403, None, None),
        (200, "gzip", None),
        (200, None, 16),
    ]:
        def check(proxy: MediaProxyServer, entry: _Entry) -> None:
            session = HeadSession(
                FakeResponse(status, [], content_length=16, content_encoding=encoding)
            )
            proxy._session = session
            proxy._serve_head(HeadRequest(), entry)
            assert entry.total_size == expected
            assert session.accept_encoding == "identity"

        _with_entry(check)

    def negative_length(proxy: MediaProxyServer, entry: _Entry) -> None:
        response = FakeResponse(200, [], content_length=-1)
        proxy._session = HeadSession(response)
        proxy._serve_head(HeadRequest(), entry)
        assert entry.total_size is None

    _with_entry(negative_length)


def test_probe_uses_only_complete_zero_byte_206_range() -> None:
    for content_range, chunks, expected in [
        ("bytes 5-5/16", [b"A"], None),
        ("bytes 0-0/0", [b"A"], None),
        ("bytes 0-0/16", [], None),
        ("bytes 0-0/16", [b"A"], 16),
    ]:
        def check(proxy: MediaProxyServer, entry: _Entry) -> None:
            session = FakeSession(FakeResponse(206, chunks, content_range))
            proxy._session = session
            proxy._probe_total_size(entry)
            assert entry.total_size == expected
            assert session.accept_encodings == ["identity"]

        _with_entry(check)


def test_player_retries_only_the_uncached_tail_after_short_206() -> None:
    class SequenceSession:
        def __init__(self) -> None:
            self.responses = [
                FakeResponse(206, [b"AB"], "bytes 0-3/4"),
                FakeResponse(206, [b"CD"], "bytes 2-3/4"),
            ]
            self.ranges: list[str] = []

        def get(self, *_args, **kwargs) -> FakeResponse:
            self.ranges.append(kwargs["headers"]["Range"])
            return self.responses.pop(0)

    def check(proxy: MediaProxyServer, entry: _Entry) -> None:
        entry.total_size = 4
        session = SequenceSession()
        proxy._session = session
        assert b"".join(proxy._iterate_bytes(entry, 0, 3)) == b"ABCD"
        assert session.ranges == ["bytes=0-3", "bytes=2-3"]
        assert entry._intervals == [(0, 4)]

    _with_entry(check)


if __name__ == "__main__":
    test_player_rejects_invalid_206_ranges_without_caching()
    test_player_short_or_oversized_206_caches_only_verified_prefix()
    test_player_valid_206_and_200_fallback()
    test_prefetch_rejects_invalid_206_range()
    test_prefetch_valid_206_and_200_fallback()
    test_200_without_content_length_keeps_streaming_fallback()
    test_prefetch_short_or_oversized_206_caches_only_verified_prefix()
    test_non_identity_206_and_changed_total_are_rejected()
    test_hls_prefetch_rejects_unrequested_partial_response()
    test_encoded_200_body_is_not_used_as_byte_addressed_cache()
    test_head_error_or_encoded_length_cannot_set_cache_size()
    test_probe_uses_only_complete_zero_byte_206_range()
    test_player_retries_only_the_uncached_tail_after_short_206()
