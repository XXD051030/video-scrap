"""Small streaming response budgets shared by media and page readers."""

from __future__ import annotations

import time
import socket
import threading
from typing import Callable, Iterator, Optional
from urllib.parse import urlsplit, urlunsplit


def resource_identity_url(response, fallback_url: str) -> str:
    """Identity of the final HTTP resource, independent of a local ETag.

    Do not decode or sort the path/query: signed URLs and distinct resources
    can depend on their exact spelling. Fragments never reach the server.
    """
    value = getattr(response, "url", None) if response is not None else None
    value = value if isinstance(value, str) and value else fallback_url
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if (scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("Media resource identity must be an HTTP(S) URL without credentials")
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    else:
        host = host.encode("idna").decode("ascii")
    port = parsed.port
    if port is not None and port != (443 if scheme == "https" else 80):
        host += f":{port}"
    return urlunsplit((scheme, host, parsed.path or "/", parsed.query, ""))


def abort_response(response) -> None:
    """Interrupt a requests socket read before closing its buffered wrapper.

    Closing only HTTPResponse may wait for the same buffered-reader lock that
    a slow peer is holding. shutdown() wakes the read first. Alternate response
    adapters without a lease guard only provide best-effort socket interruption
    and must still use finite read timeouts. They cannot guarantee pooled-socket
    ownership during cancellation.
    """
    raw = getattr(response, "raw", None)
    # A completed urllib3 response may have returned its connection to the
    # pool. Its old wrapper must never shut down that connection's next user.
    if raw is not None and getattr(raw, "closed", False):
        return
    owned_abort = getattr(raw, "_abort_owned_response", None)
    if callable(owned_abort):
        # The transport owns the check/shutdown/close transaction. A False
        # result means a newer request owns this connection; even close() on
        # the old response wrapper could otherwise affect that newer owner.
        owned_abort()
        return
    candidates = [getattr(getattr(raw, "_connection", None), "sock", None)]
    buffered = getattr(getattr(raw, "_fp", None), "fp", None)
    candidates.append(getattr(getattr(buffered, "raw", None), "_sock", None))
    for connection in candidates:
        if raw is not None and getattr(raw, "closed", False):
            return
        if isinstance(connection, socket.socket):
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    try:
        response.close()
    except Exception:
        pass


def origin_key(url: str) -> tuple[str, str, int | None]:
    """Canonical HTTP origin; scheme and effective port are security boundaries."""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port or (443 if scheme == "https" else 80 if scheme == "http" else None)
        return scheme, host, port
    except (ValueError, TypeError):
        return "", "", None


def iter_limited_response(
    response,
    max_bytes: int,
    deadline_seconds: float,
    cancelled: Optional[Callable[[], bool]] = None,
    chunk_size: int = 64 * 1024,
    *, raw: bool = False,
) -> Iterator[bytes]:
    """Yield a bounded body, checking a wall-clock deadline and cancellation.

    The request must use ``stream=True`` and a finite socket read timeout.
    A watchdog interrupts requests sockets even while a chunk read blocks;
    alternate adapters rely on their close()/timeout behavior. Callers own
    response.close(), including when iteration is cancelled or fails.
    Content-Length is only an early rejection; decoded bytes remain bounded.
    """
    limit = max(0, int(max_bytes))
    deadline = time.monotonic() + max(0.0, float(deadline_seconds))

    def check() -> None:
        if cancelled is not None and cancelled():
            raise InterruptedError("Response read cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Response exceeded its total read deadline")

    check()
    length = (getattr(response, "headers", {}) or {}).get("Content-Length")
    if length is not None:
        try:
            if int(length) > limit:
                raise ValueError(f"Response exceeds the {limit}-byte limit")
        except (TypeError, OverflowError):
            pass
        except ValueError:
            if str(length).strip().isdigit():
                raise
    iterator = getattr(response, "iter_content", None)
    if raw:
        chunks = response.raw.stream(max(1, min(int(chunk_size), 64 * 1024)), decode_content=False)
    elif callable(iterator):
        chunks = iterator(chunk_size=max(1, min(int(chunk_size), 64 * 1024)))
    else:
        # Lightweight offline responses and already buffered library adapters.
        body = getattr(response, "content", None)
        if body is None:
            body = str(getattr(response, "text", "")).encode("utf-8")
        chunks = (body,)
    finished = threading.Event()
    lifecycle_lock = threading.Lock()

    def watch():
        while not finished.wait(min(.1, max(.001, deadline - time.monotonic()))):
            if time.monotonic() >= deadline or (cancelled is not None and cancelled()):
                with lifecycle_lock:
                    if not finished.is_set():
                        abort_response(response)
                return

    watcher = threading.Thread(target=watch, name="ResponseDeadline", daemon=True)
    watcher.start()
    received = 0
    try:
        for chunk in chunks:
            check()
            if not chunk:
                continue
            received += len(chunk)
            if received > limit:
                raise ValueError(f"Response exceeds the {limit}-byte limit")
            yield chunk
        check()
    finally:
        with lifecycle_lock:
            finished.set()


def read_limited_response(
    response, max_bytes: int, deadline_seconds: float,
    cancelled: Optional[Callable[[], bool]] = None,
) -> bytes:
    return b"".join(iter_limited_response(response, max_bytes, deadline_seconds, cancelled))


def iter_continuous_response(
    response, max_bytes: int, idle_seconds: float,
    cancelled: Optional[Callable[[], bool]] = None,
    chunk_size: int = 64 * 1024, *, raw: bool = False,
) -> Iterator[bytes]:
    """Stream healthy whole media without applying a whole-file wall deadline.

    Use read1 so small incoming packets count as progress before a large
    chunk fills. The idle clock runs only while reading upstream; time spent
    yielding to a slow player is excluded. Cancellation remains active then.
    Finite headers/socket timeouts and caller-owned response.close still apply.
    """
    limit = max(0, int(max_bytes))
    idle_seconds = max(.001, float(idle_seconds))
    chunk_size = max(1, min(int(chunk_size), 64 * 1024))
    length = (getattr(response, "headers", {}) or {}).get("Content-Length")
    if length is not None:
        try:
            declared = int(length)
        except (ValueError, TypeError, OverflowError):
            declared = None
        if declared is not None and (declared < 0 or declared > limit):
            raise ValueError(f"Response exceeds the {limit}-byte limit")
    raw_body = getattr(response, "raw", None)
    read1 = getattr(raw_body, "read1", None)
    if callable(read1):
        def available_chunks():
            while True:
                chunk = read1(chunk_size, decode_content=not raw)
                if not chunk:
                    return
                yield chunk
        chunks = iter(available_chunks())
    elif callable(getattr(raw_body, "stream", None)):
        # Older urllib3 versions do not expose read1. One-byte streaming keeps
        # their progress clock honest instead of waiting to fill a large chunk.
        chunks = iter(raw_body.stream(1, decode_content=not raw))
    elif callable(getattr(response, "iter_content", None)):
        chunks = iter(response.iter_content(chunk_size=chunk_size))
    else:
        chunks = iter((getattr(response, "content", b""),))

    lock = threading.Lock()
    finished = threading.Event()
    reading = False
    read_started = 0.0
    timed_out = False

    def watch():
        nonlocal timed_out
        while not finished.wait(min(.05, idle_seconds / 4)):
            stop = cancelled is not None and cancelled()
            with lock:
                if finished.is_set():
                    return
                expired = reading and time.monotonic() - read_started >= idle_seconds
                if stop or expired:
                    timed_out = bool(expired)
                    abort_response(response)
                    return

    def check():
        if cancelled is not None and cancelled():
            raise InterruptedError("Response read cancelled")
        if timed_out:
            raise TimeoutError("Media response stopped making progress")

    watcher = threading.Thread(target=watch, name="MediaIdleDeadline", daemon=True)
    watcher.start()
    received = 0
    try:
        while True:
            check()
            with lock:
                reading, read_started = True, time.monotonic()
            try:
                chunk = next(chunks)
            except StopIteration:
                break
            except Exception:
                # A concurrent socket abort can surface as a low-level
                # buffered-reader error while urllib3 unwinds. Preserve the
                # intentional timeout/cancellation reason for the caller.
                check()
                raise
            finally:
                with lock:
                    reading = False
            check()
            if chunk:
                received += len(chunk)
                if received > limit:
                    raise ValueError(f"Response exceeds the {limit}-byte limit")
                yield chunk
        check()
    finally:
        with lock:
            finished.set()
