"""Offline cross-process regressions for application ownership and activation.

Run directly with ``python3 tests/test_single_instance.py``. No application
settings, browser profiles, media, or external network services are used.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QCoreApplication, QTimer
from PyQt6.QtNetwork import QLocalServer

from src.single_instance import InstanceRole, SingleInstance, _allow_windows_foreground


def _helper() -> int:
    config, report, activations, release, gap, timeout = sys.argv[2:]
    app = QCoreApplication([sys.argv[0]])
    instance = SingleInstance(Path(config))
    original_listen = instance._listen

    def delayed_listen() -> InstanceRole:
        # Model a first process paused between acquiring the lock and opening
        # activation IPC. Other processes must wait rather than become owners.
        time.sleep(float(gap))
        return original_listen()

    instance._listen = delayed_listen
    try:
        role = instance.start(timeout_ms=int(timeout))
        Path(report).write_text(json.dumps({"role": role.value, "error": instance.error_message}))
        if role != InstanceRole.PRIMARY:
            return 0 if role == InstanceRole.SECONDARY else 1

        def activated() -> None:
            with Path(activations).open("a", encoding="utf-8") as stream:
                stream.write("activated\n")

        instance.activation_requested.connect(activated)
        poll = QTimer()
        poll.timeout.connect(lambda: app.quit() if Path(release).exists() else None)
        poll.start(20)
        QTimer.singleShot(15000, app.quit)
        return app.exec()
    finally:
        instance.close()


class SingleInstanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="video-scrap-instance-test-")
        self.root = Path(self.directory.name)
        self.config = self.root / "config"
        self.processes: list[subprocess.Popen] = []
        self.releases: list[Path] = []

    def tearDown(self) -> None:
        for release in self.releases:
            release.touch()
        for process in self.processes:
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        self.directory.cleanup()

    def launch(self, *, gap: float = 0, timeout_ms: int = 5000) -> tuple[subprocess.Popen, Path, Path]:
        index = len(self.processes)
        report = self.root / f"report-{index}.json"
        activations = self.root / f"activations-{index}.txt"
        release = self.root / f"release-{index}"
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--helper", str(self.config),
             str(report), str(activations), str(release), str(gap), str(timeout_ms)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.processes.append(process)
        self.releases.append(release)
        return process, report, activations

    def await_path(self, path: Path, timeout: float = 8) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.02)
        details = []
        for process in self.processes:
            if process.poll() is not None:
                out, err = process.communicate()
                details.append(f"exit={process.returncode}: {out} {err}")
        self.fail(f"Timed out waiting for {path}: {details}")

    def role(self, report: Path) -> dict:
        self.await_path(report)
        return json.loads(report.read_text())

    def finish(self, process: subprocess.Popen, index: int) -> None:
        self.releases[index].touch()
        out, err = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, f"{out}\n{err}")

    def test_simultaneous_launches_have_one_owner_and_activate_it(self) -> None:
        launched = [self.launch() for _ in range(5)]
        reports = [self.role(report) for _, report, _ in launched]
        self.assertEqual([r["role"] for r in reports].count("primary"), 1, reports)
        self.assertEqual([r["role"] for r in reports].count("secondary"), 4, reports)
        owner = next(i for i, r in enumerate(reports) if r["role"] == "primary")
        activations = launched[owner][2].read_text().splitlines()
        self.assertEqual(activations, ["activated"] * 4)
        self.finish(launched[owner][0], owner)

    def test_repeat_launch_waits_through_lock_to_socket_startup_gap(self) -> None:
        first, first_report, activations = self.launch(gap=1.0)
        self.await_path(self.config / "instance.lock")
        second, second_report, _ = self.launch()
        self.assertEqual(self.role(first_report)["role"], "primary")
        self.assertEqual(self.role(second_report)["role"], "secondary")
        self.assertEqual(activations.read_text(), "activated\n")
        second.communicate(timeout=5)
        self.assertEqual(second.returncode, 0)
        self.finish(first, 0)

    def test_startup_timeout_never_creates_a_duplicate_owner(self) -> None:
        first, first_report, _ = self.launch(gap=1.0)
        self.await_path(self.config / "instance.lock")
        second, second_report, _ = self.launch(timeout_ms=100)
        result = self.role(second_report)
        self.assertEqual(result["role"], "error", result)
        second.communicate(timeout=5)
        self.assertEqual(second.returncode, 1)
        self.assertEqual(self.role(first_report)["role"], "primary")
        self.finish(first, 0)

    def test_killed_owner_recovers_lock_and_stale_socket(self) -> None:
        first, first_report, _ = self.launch()
        self.assertEqual(self.role(first_report)["role"], "primary")
        first.kill()
        first.communicate(timeout=5)
        self.assertTrue((self.config / "instance.lock").exists())
        second, second_report, _ = self.launch()
        self.assertEqual(self.role(second_report)["role"], "primary")
        self.finish(second, 1)

    def test_clean_exit_releases_lock_and_next_launch_owns_it(self) -> None:
        first, first_report, _ = self.launch()
        self.assertEqual(self.role(first_report)["role"], "primary")
        self.finish(first, 0)
        self.assertFalse((self.config / "instance.lock").exists())
        second, second_report, _ = self.launch()
        self.assertEqual(self.role(second_report)["role"], "primary")
        self.finish(second, 1)

    def test_old_timestamp_does_not_steal_a_running_owner_lock(self) -> None:
        first, first_report, activations = self.launch()
        self.assertEqual(self.role(first_report)["role"], "primary")
        old_time = time.time() - 86400
        os.utime(self.config / "instance.lock", (old_time, old_time))
        second, second_report, _ = self.launch()
        self.assertEqual(self.role(second_report)["role"], "secondary")
        self.assertEqual(activations.read_text(), "activated\n")
        second.communicate(timeout=5)
        self.assertEqual(second.returncode, 0)
        self.finish(first, 0)

    def test_other_live_activation_endpoint_is_not_removed(self) -> None:
        # This endpoint deliberately owns no application lock. A new launch
        # must report conflict without unlinking someone else's live socket.
        self.config.mkdir()
        instance = SingleInstance(self.config)
        endpoint = QLocalServer()
        self.assertTrue(endpoint.listen(instance.server_name), endpoint.errorString())
        try:
            result = instance.start()
            self.assertEqual(result, InstanceRole.ERROR)
            self.assertTrue(endpoint.isListening())
            instance.close()
        finally:
            endpoint.close()

    def test_configuration_path_that_is_a_file_blocks_start(self) -> None:
        self.config.write_text("not a directory")
        instance = SingleInstance(self.config)
        self.assertEqual(instance.start(), InstanceRole.ERROR)
        self.assertIn("settings directory", instance.error_message)
        self.assertFalse(instance._lock.isLocked())

    def test_foreground_permission_is_a_noop_outside_windows(self) -> None:
        with mock.patch("src.single_instance.sys.platform", "darwin"), mock.patch(
            "ctypes.WinDLL", create=True,
        ) as load_library:
            _allow_windows_foreground(12345)
        load_library.assert_not_called()

    def test_windows_foreground_permission_uses_typed_specific_owner_pid(self) -> None:
        from ctypes import wintypes

        library = mock.Mock()
        library.AllowSetForegroundWindow.return_value = 1
        with mock.patch("src.single_instance.sys.platform", "win32"), mock.patch(
            "ctypes.WinDLL", return_value=library, create=True,
        ) as load_library:
            _allow_windows_foreground(12345)
            # Never grant focus to all processes or pass an invalid DWORD PID.
            for invalid_pid in (-1, 0, 0xFFFFFFFF, 0x100000000):
                _allow_windows_foreground(invalid_pid)
        load_library.assert_called_once_with("user32", use_last_error=True)
        allow = library.AllowSetForegroundWindow
        self.assertEqual(allow.argtypes, [wintypes.DWORD])
        self.assertIs(allow.restype, wintypes.BOOL)
        allow.assert_called_once_with(12345)

    def test_windows_foreground_denial_still_activates_existing_instance(self) -> None:
        first, first_report, activations = self.launch()
        self.assertEqual(self.role(first_report)["role"], "primary")
        secondary = SingleInstance(self.config)
        library = mock.Mock()
        library.AllowSetForegroundWindow.return_value = 0
        with mock.patch("src.single_instance.sys.platform", "win32"), mock.patch(
            "ctypes.WinDLL", return_value=library, create=True,
        ):
            self.assertEqual(secondary.start(), InstanceRole.SECONDARY)
        library.AllowSetForegroundWindow.assert_called_once_with(first.pid)
        self.assertEqual(activations.read_text(), "activated\n")
        self.assertFalse(secondary._lock.isLocked())
        self.finish(first, 0)

    def test_windows_foreground_api_failure_does_not_raise(self) -> None:
        with mock.patch("src.single_instance.sys.platform", "win32"), mock.patch(
            "ctypes.WinDLL", side_effect=OSError("API unavailable"), create=True,
        ):
            _allow_windows_foreground(12345)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--helper":
        sys.exit(_helper())
    app = QCoreApplication([])
    unittest.main()
