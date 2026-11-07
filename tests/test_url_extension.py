"""File extensions taken from media URLs for scraping and direct downloads."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import downloader
from src.downloader import VideoDownloader
from src.scraper import VideoItem, build_items_from_urls
from src.utils import url_extension


class UrlExtensionTests(unittest.TestCase):
    def test_extension_comes_from_the_path_only(self):
        cases = {
            "https://cdn.example.com/media/clip.mp4": "mp4",
            "https://cdn.example.com/media/clip.WEBM?token=a.b": "webm",
            "https://cdn.example.com/media/clip.mov#t=3": "mov",
            "https://cdn.example.com/media/play.m3u8?x=1#y.ts": "m3u8",
            # The host, query and fragment are not part of the file name.
            "https://cdn.example.com": None,
            "https://cdn.example.com/": None,
            "https://cdn.example.com/stream#clip.webm": None,
            "https://cdn.example.com/get?file=clip.mp4": None,
            # Extensions are short alphanumeric suffixes.
            "https://cdn.example.com/archive.tar.gz": "gz",
            "https://cdn.example.com/clip.verylong": None,
            "https://cdn.example.com/clip.": None,
            "https://cdn.example.com/clip.mp-4": None,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(url_extension(url), expected)


class _RecordingParallel:
    """Stand-in that records the chosen output path and stops the download."""

    def __init__(self, recorded):
        self.recorded = recorded
        self._session = mock.Mock()

    def download(self, *, url, output_path, headers, progress):
        self.recorded.append(output_path)
        raise RuntimeError("stop after naming")

    def close(self):
        pass


class DirectDownloadNameTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.downloader = VideoDownloader(Path(directory.name))
        self.addCleanup(self.downloader.close)

    def saved_name(self, item):
        recorded = []
        with mock.patch.object(downloader, "ParallelDownloader",
                               side_effect=lambda **_kwargs: _RecordingParallel(recorded)), \
                self.assertRaisesRegex(RuntimeError, "stop after naming"):
            self.downloader._download_direct(item, None)
        return recorded[0].name

    def output_name(self, url, ext=None):
        return self.saved_name(VideoItem("Title", url, "https://cdn.example.com/watch",
                                         ext=ext, is_direct=True))

    def test_missing_extension_falls_back_to_mp4_instead_of_host_or_fragment(self):
        # Previously the host suffix or fragment was taken as the extension,
        # producing names such as "Title.com" or "Title.webm" for /stream.
        self.assertEqual(self.output_name("https://cdn.example.com"), "Title.mp4")
        self.assertEqual(self.output_name("https://cdn.example.com/stream#clip.webm"), "Title.mp4")

    def test_path_extension_survives_a_fragment(self):
        self.assertEqual(self.output_name("https://cdn.example.com/clip.MOV#t=3"), "Title.mov")

    def test_scraped_extension_still_takes_precedence(self):
        self.assertEqual(self.output_name("https://cdn.example.com/clip.mov", ext="webm"), "Title.webm")

    def test_audio_and_video_extensions_are_kept(self):
        for ext in ("webm", "mkv", "mov", "ts", "mp3", "m4a", "ogg", "flac"):
            with self.subTest(ext=ext):
                self.assertEqual(self.output_name(f"https://cdn.example.com/clip.{ext.upper()}"),
                                 f"Title.{ext}")

    def test_program_and_script_extensions_are_saved_as_mp4(self):
        # The content check only rejects pages and text, so the extension
        # must not let a downloaded program or script keep its own type.
        for url in ("https://cdn.example.com/files/movie.exe",
                    "https://cdn.example.com/get_video.php?id=1",
                    "https://cdn.example.com/run.bat",
                    "https://cdn.example.com/screen.scr",
                    "https://cdn.example.com/page.html"):
            with self.subTest(url=url):
                self.assertEqual(self.output_name(url), "Title.mp4")
        self.assertEqual(self.output_name("https://cdn.example.com/clip.webm", ext="exe"), "Title.webm")

    def test_page_labelled_executable_downloads_as_mp4(self):
        page = "https://page.example/watch"
        item = build_items_from_urls(["https://cdn.example/files/movie.exe"], [], page, "Cat video")[0]
        self.assertEqual((item.ext, item.is_direct), ("exe", True))
        self.assertEqual(self.saved_name(item), "Cat video - rendered 1.mp4")


if __name__ == "__main__":
    unittest.main(verbosity=2)
