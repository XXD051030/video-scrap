"""Active transfer time for an independently suspended download process.

The supervisor alone writes the shared nanosecond offset before resuming any
worker. Readers never acquire a cross-process lock: suspending a process while
it owns such a lock must not block its supervisor's resume operation. Supported
desktop builds are 64-bit and the offset is one aligned 64-bit scalar.
"""
from __future__ import annotations

import threading
import time

_pause_offset = None


def configure_pause_offset(offset) -> None:
    global _pause_offset
    _pause_offset = offset


def monotonic() -> float:
    offset = _pause_offset
    if offset is None:
        return time.monotonic()
    # Suspension can occur between sampling wall time and reading the shared
    # offset. Never combine a pre-pause sample with post-resume compensation.
    # The supervisor is the only writer and its offset only increases.
    while True:
        before = offset.value
        now = time.monotonic()
        if before == offset.value:
            return now - before / 1_000_000_000


def deadline_timer(seconds, callback):
    """Retain ordinary Timer behavior outside an owned download child."""
    if _pause_offset is None:
        return threading.Timer(seconds, callback)

    class ActiveTimer(threading.Thread):
        def __init__(self):
            super().__init__(name="DownloadHeaderDeadline", daemon=True)
            self.finished = threading.Event()
            self.deadline = monotonic() + max(0.0, float(seconds))

        def cancel(self):
            self.finished.set()

        def run(self):
            while not self.finished.is_set():
                remaining = self.deadline - monotonic()
                if remaining <= 0:
                    if not self.finished.is_set():
                        callback()
                    return
                self.finished.wait(min(.05, remaining))

    return ActiveTimer()
