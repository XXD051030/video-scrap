"""FFmpeg discovery regressions using local files and no external services.

Run with ``build/.venv/bin/python tests/test_ffmpeg_resolver.py``. These checks
model source and frozen installations without executing or downloading FFmpeg.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import ffmpeg


@contextmanager
def _working_directory(directory: Path):
    previous = Path.cwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(previous)


class FFmpegResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ffmpeg-discovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.bundle = self.root / "bundle"
        self.project.mkdir()
        self.bundle.mkdir()
        self.source_mac = self.project / "build" / "app-assets" / "bin" / "ffmpeg"
        self.source_windows = (
            self.project / "build" / "windows-app-assets" / "bin" / "ffmpeg.exe"
        )
        self.fake_imageio = SimpleNamespace(get_ffmpeg_exe=mock.Mock(return_value=""))
        self.start_patch(mock.patch.object(ffmpeg, "__file__", str(self.project / "src" / "ffmpeg.py")))
        self.which = self.start_patch(mock.patch("shutil.which", return_value=None))
        self.start_patch(mock.patch.object(sys, "platform", "darwin"))
        self.start_patch(mock.patch.object(sys, "frozen", False, create=True))
        self.start_patch(mock.patch.object(sys, "_MEIPASS", str(self.bundle), create=True))
        self.start_patch(mock.patch.dict(sys.modules, {"imageio_ffmpeg": self.fake_imageio}))

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    @staticmethod
    def executable(path: Path, *, executable: bool = True) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"local test fixture; never executed\n")
        path.chmod(0o700 if executable else 0o600)
        return path

    def imageio_binary(self, path: Path) -> Path:
        self.executable(path)
        self.fake_imageio.get_ffmpeg_exe.return_value = str(path)
        return path

    def test_existing_path_binary_has_priority_over_all_fallbacks(self) -> None:
        system = self.executable(self.root / "system" / "ffmpeg")
        self.which.return_value = str(system)
        self.executable(self.bundle / "bin" / "ffmpeg")
        self.executable(self.source_mac)
        self.imageio_binary(self.root / "imageio" / "ffmpeg")
        with mock.patch.object(sys, "frozen", True):
            self.assertEqual(ffmpeg.resolve_ffmpeg(), str(system.resolve()))
        self.which.assert_called_once_with("ffmpeg")
        self.fake_imageio.get_ffmpeg_exe.assert_not_called()

    def test_frozen_bundle_is_used_before_source_and_imageio(self) -> None:
        packaged = self.executable(self.bundle / "bin" / "ffmpeg")
        self.executable(self.source_mac)
        self.imageio_binary(self.root / "imageio" / "ffmpeg")
        with mock.patch.object(sys, "frozen", True):
            self.assertEqual(ffmpeg.resolve_ffmpeg(), str(packaged.resolve()))
        self.fake_imageio.get_ffmpeg_exe.assert_not_called()

    def test_frozen_windows_bundle_uses_exe_without_posix_execute_bit(self) -> None:
        packaged = self.executable(self.bundle / "bin" / "ffmpeg.exe", executable=False)
        self.executable(self.bundle / "bin" / "ffmpeg")
        self.executable(self.source_windows)
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(sys, "frozen", True):
            self.assertEqual(ffmpeg.resolve_ffmpeg(), str(packaged.resolve()))

    def test_non_frozen_source_ignores_bundle_directory(self) -> None:
        self.executable(self.bundle / "bin" / "ffmpeg")
        source = self.executable(self.source_mac)
        self.assertEqual(ffmpeg.resolve_ffmpeg(), str(source.resolve()))

    def test_macos_source_asset_is_found_outside_project_cwd(self) -> None:
        source = self.executable(self.source_mac)
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        self.executable(unrelated / "build" / "app-assets" / "bin" / "ffmpeg")
        with _working_directory(unrelated):
            self.assertEqual(ffmpeg.resolve_ffmpeg(), str(source.resolve()))
        self.fake_imageio.get_ffmpeg_exe.assert_not_called()

    def test_windows_source_uses_windows_asset_without_posix_execute_bit(self) -> None:
        source = self.executable(self.source_windows, executable=False)
        self.executable(self.source_mac)
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(ffmpeg.resolve_ffmpeg(), str(source.resolve()))
        self.fake_imageio.get_ffmpeg_exe.assert_not_called()

    def test_wrong_platform_source_asset_is_ignored(self) -> None:
        fallback = self.imageio_binary(self.root / "imageio" / "ffmpeg")
        self.executable(self.source_windows)
        self.assertEqual(ffmpeg.resolve_ffmpeg(), str(fallback.resolve()))
        self.source_windows.unlink()
        self.executable(self.source_mac)
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(ffmpeg.resolve_ffmpeg(), str(fallback.resolve()))

    def test_linux_does_not_use_macos_or_windows_source_asset(self) -> None:
        self.executable(self.source_mac)
        self.executable(self.source_windows)
        fallback = self.imageio_binary(self.root / "imageio" / "ffmpeg")
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(ffmpeg.resolve_ffmpeg(), str(fallback.resolve()))

    def test_missing_local_assets_use_installed_imageio_binary(self) -> None:
        fallback = self.imageio_binary(self.root / "imageio" / "ffmpeg")
        self.assertEqual(ffmpeg.resolve_ffmpeg(), str(fallback.resolve()))
        self.fake_imageio.get_ffmpeg_exe.assert_called_once_with()

    def test_no_installed_imageio_or_local_binary_returns_none(self) -> None:
        with mock.patch.dict(sys.modules, {"imageio_ffmpeg": None}):
            self.assertIsNone(ffmpeg.resolve_ffmpeg())

    def test_imageio_discovery_error_returns_none(self) -> None:
        self.fake_imageio.get_ffmpeg_exe.side_effect = RuntimeError("No packaged binary")
        self.assertIsNone(ffmpeg.resolve_ffmpeg())

    def test_missing_imageio_binary_returns_none(self) -> None:
        self.fake_imageio.get_ffmpeg_exe.return_value = str(self.root / "missing")
        self.assertIsNone(ffmpeg.resolve_ffmpeg())

    def test_directory_candidates_are_rejected_and_discovery_continues(self) -> None:
        directory = self.root / "not-a-binary"
        directory.mkdir()
        self.which.return_value = str(directory)
        self.source_mac.mkdir(parents=True)
        fallback = self.imageio_binary(self.root / "imageio" / "ffmpeg")
        self.assertEqual(ffmpeg.resolve_ffmpeg(), str(fallback.resolve()))

    def test_unexecutable_posix_candidates_are_rejected_and_discovery_continues(self) -> None:
        system = self.executable(self.root / "system" / "ffmpeg", executable=False)
        self.which.return_value = str(system)
        self.executable(self.source_mac, executable=False)
        fallback = self.imageio_binary(self.root / "imageio" / "ffmpeg")
        inaccessible = {system.resolve(), self.source_mac.resolve()}
        with mock.patch("os.access", side_effect=lambda path, mode: Path(path).resolve() not in inaccessible):
            self.assertEqual(ffmpeg.resolve_ffmpeg(), str(fallback.resolve()))

    def test_unexecutable_imageio_candidate_returns_none(self) -> None:
        candidate = self.executable(self.root / "imageio" / "ffmpeg", executable=False)
        self.fake_imageio.get_ffmpeg_exe.return_value = str(candidate)
        with mock.patch("os.access", return_value=False):
            self.assertIsNone(ffmpeg.resolve_ffmpeg())

    def test_candidate_filesystem_error_does_not_hide_valid_fallback(self) -> None:
        inaccessible = self.root / "inaccessible" / "ffmpeg"
        self.which.return_value = str(inaccessible)
        fallback = self.imageio_binary(self.root / "imageio" / "ffmpeg")
        real_is_file = Path.is_file

        def checked_is_file(path: Path) -> bool:
            if path == inaccessible:
                raise OSError("simulated inaccessible directory")
            return real_is_file(path)

        with mock.patch.object(Path, "is_file", checked_is_file):
            self.assertEqual(ffmpeg.resolve_ffmpeg(), str(fallback.resolve()))

    def test_relative_path_binary_is_returned_as_absolute_path(self) -> None:
        binary = self.executable(self.root / "relative" / "ffmpeg")
        self.which.return_value = "relative/ffmpeg"
        with _working_directory(self.root):
            result = ffmpeg.resolve_ffmpeg()
        self.assertEqual(result, str(binary.resolve()))
        self.assertTrue(Path(result).is_absolute())

    def test_discovery_does_not_modify_process_path(self) -> None:
        self.executable(self.source_mac)
        sentinel = "unchanged-path-from-test"
        with mock.patch.dict(os.environ, {"PATH": sentinel}):
            self.assertEqual(ffmpeg.resolve_ffmpeg(), str(self.source_mac.resolve()))
            self.assertEqual(os.environ["PATH"], sentinel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
