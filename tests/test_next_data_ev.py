"""Tests for the encrypted Next.js ``ev`` HLS extractor (rou.video & clones).

Run directly: ``python3 tests/test_next_data_ev.py`` (no pytest needed).
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper import _decrypt_next_ev, _extract_next_data_ev_hls


def _encrypt(obj: dict, k: int) -> str:
    """Inverse of the player's decode: JSON -> shift each byte up by k -> b64."""
    text = json.dumps(obj)
    shifted = bytes((ord(c) + k) % 256 for c in text)
    return base64.b64encode(shifted).decode("ascii")


def test_decrypt_roundtrip() -> None:
    obj = {
        "videoUrl": "https://v.rn244.xyz/hls/abc/abc-720/index.jpg?auth=tok&exp=9",
        "thumbVTTUrl": "https://v.rn246.xyz/hls/abc/thumbs/thumbnail.vtt",
    }
    ev = {"d": _encrypt(obj, 24), "k": 24}
    assert _decrypt_next_ev(ev) == obj


def test_extract_from_next_data() -> None:
    url = "https://v.rn244.xyz/hls/abc/abc-720/index.jpg?auth=tok"
    ev = {"d": _encrypt({"videoUrl": url}, 13), "k": 13}
    nd = {
        "props": {
            "pageProps": {
                "ev": ev,
                "video": {"name": "Clip", "nameZh": "片名"},
            }
        }
    }
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(nd)
        + "</script>"
    )
    assert _extract_next_data_ev_hls(html) == (url, "片名")


def test_extract_absent_or_malformed() -> None:
    assert _extract_next_data_ev_hls("<html>no next data here</html>") is None
    # __NEXT_DATA__ present but no ev blob
    nd = {"props": {"pageProps": {"video": {"name": "x"}}}}
    html = f'<script id="__NEXT_DATA__">{json.dumps(nd)}</script>'
    assert _extract_next_data_ev_hls(html) is None


def test_decrypt_rejects_bad_input() -> None:
    assert _decrypt_next_ev({"d": "not base64 !!!", "k": 24}) is None
    assert _decrypt_next_ev({"d": "", "k": "notint"}) is None


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'OK' if failures == 0 else 'FAILURES: ' + str(failures)}")
    sys.exit(1 if failures else 0)
