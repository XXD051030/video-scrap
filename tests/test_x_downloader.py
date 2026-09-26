"""Offline checks for selecting and naming X post downloads."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.downloader import VideoDownloader
from src.scraper import VideoItem


def _x_item(index: int, *, browser: str | None = None) -> VideoItem:
    url = "https://x.com/example/status/123456789/video/2?share=1"
    return VideoItem(
        title="Shared title",
        url=url,
        source_url=url,
        x_playlist_index=index,
        x_auth_browser=browser,
    )


def test_x_download_selects_media_and_reports_final_merged_file(tmp_path: Path) -> None:
    captured: dict = {}

    class FakeYDL:
        def __init__(self, opts: dict) -> None:
            captured["opts"] = opts

        def __enter__(self) -> FakeYDL:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def download(self, urls: list[str]) -> int:
            captured["urls"] = urls
            opts = captured["opts"]
            final = Path(opts["outtmpl"].replace("%(ext)s", "mp4"))
            temporary = str(final).replace(".mp4", ".f137.mp4")
            opts["progress_hooks"][0]({"status": "finished", "filename": temporary})
            final.write_bytes(b"merged file")
            opts["post_hooks"][0](str(final))
            return 0

    updates = []
    with patch("src.downloader.yt_dlp.YoutubeDL", FakeYDL):
        downloader = VideoDownloader(tmp_path)
        try:
            result = downloader.download(_x_item(1, browser="safari"), on_progress=updates.append)
        finally:
            downloader.close()

    assert captured["urls"] == ["https://x.com/example/status/123456789"]
    assert captured["opts"]["playlist_items"] == "1"
    assert captured["opts"]["cookiesfrombrowser"] == ("safari",)
    assert captured["opts"]["overwrites"] is False
    assert result.name == "Shared title_x_123456789_video_1.mp4"
    assert result.read_bytes() == b"merged file"
    assert updates[-1].status == "completed"
    assert updates[-1].filename == str(result)


def test_x_media_in_one_post_have_distinct_names(tmp_path: Path) -> None:
    outputs = []

    class FakeYDL:
        def __init__(self, opts: dict) -> None:
            self.opts = opts

        def __enter__(self) -> FakeYDL:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def download(self, _urls: list[str]) -> int:
            index = self.opts["playlist_items"]
            final = Path(self.opts["outtmpl"].replace("%(ext)s", "mp4"))
            final.write_text(index)
            self.opts["post_hooks"][0](str(final))
            outputs.append(final)
            return 0

    with patch("src.downloader.yt_dlp.YoutubeDL", FakeYDL):
        downloader = VideoDownloader(tmp_path)
        try:
            first = downloader.download(_x_item(1))
            second = downloader.download(_x_item(2))
        finally:
            downloader.close()

    assert first != second
    assert outputs == [first, second]
    assert first.read_text() == "1"
    assert second.read_text() == "2"


def test_lookalike_domain_keeps_generic_download_behavior(tmp_path: Path) -> None:
    captured: dict = {}

    class FakeYDL:
        def __init__(self, opts: dict) -> None:
            captured["opts"] = opts

        def __enter__(self) -> FakeYDL:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def download(self, urls: list[str]) -> int:
            captured["urls"] = urls
            captured["opts"]["progress_hooks"][0](
                {"status": "finished", "filename": str(tmp_path / "Shared title.mp4")}
            )
            return 0

    item = VideoItem(
        title="Shared title",
        url="https://x.com.evil.example/example/status/123456789",
        source_url="https://x.com.evil.example/example/status/123456789",
        x_playlist_index=2,
        x_auth_browser="safari",
    )
    with patch("src.downloader.yt_dlp.YoutubeDL", FakeYDL):
        downloader = VideoDownloader(tmp_path)
        try:
            result = downloader.download(item)
        finally:
            downloader.close()

    assert captured["urls"] == [item.url]
    assert "playlist_items" not in captured["opts"]
    assert "cookiesfrombrowser" not in captured["opts"]
    assert "post_hooks" not in captured["opts"]
    assert result == tmp_path / "Shared title.mp4"


def test_x_download_without_finished_file_is_failure(tmp_path: Path) -> None:
    class FakeYDL:
        def __init__(self, _opts: dict) -> None:
            pass

        def __enter__(self) -> FakeYDL:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def download(self, _urls: list[str]) -> int:
            return 0

    with patch("src.downloader.yt_dlp.YoutubeDL", FakeYDL):
        downloader = VideoDownloader(tmp_path)
        try:
            try:
                downloader.download(_x_item(1))
            except RuntimeError as exc:
                assert "did not produce a finished file" in str(exc)
            else:
                raise AssertionError("Expected missing X output to fail")
        finally:
            downloader.close()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                with tempfile.TemporaryDirectory() as directory:
                    fn(Path(directory))
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'OK' if failures == 0 else 'FAILURES: ' + str(failures)}")
    sys.exit(1 if failures else 0)
