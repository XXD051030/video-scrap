"""Native process-container checks for the early orphan discovery window."""
from __future__ import annotations
import os
import multiprocessing
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psutil
from src import process_owner as module
from src.process_owner import ProcessOwner

ROOT_CODE = r'''
import os,sys,time,subprocess
from pathlib import Path
root=Path(sys.argv[1])
if os.name == 'posix': os.setpgid(0, int(sys.argv[2]))
(root/'ready').touch()
until=time.monotonic()+10
while not (root/'release').exists():
    if time.monotonic()>until: sys.exit(2)
    time.sleep(.01)
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],
                       stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
(root/'child').write_text(str(child.pid))
os._exit(0)
'''


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Native ownership fixture did not reach its handshake")
        time.sleep(.01)


def has_exited(process):
    """A process can disappear between the two native status queries."""
    try:
        return not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def quick_exit():
    """Another multiprocessing worker exercises its automatic child reaping."""


def invalid_anchor(connection):
    marker = os.environ.get("VIDEO_SCRAPER_OWNER_TEST_PID_FILE")
    if marker:
        Path(marker).write_text(str(os.getpid()))
    connection.send_bytes(b"invalid")
    time.sleep(60)


class ProcessOwnershipTests(unittest.TestCase):
    def test_container_kills_descendant_after_its_parent_has_exited(self):
        owner = ProcessOwner()
        process = child = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                process = subprocess.Popen([sys.executable, "-c", ROOT_CODE, str(root), str(owner.pgid or 0)])
                wait_for(lambda: (root / "ready").exists())
                owner.attach(process.pid)
                (root / "release").touch()
                wait_for(lambda: (root / "child").exists())
                child = psutil.Process(int((root / "child").read_text()))
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertTrue(child.is_running())
                # No descendants were enumerated before the root exited.
                owner.terminate()
                owner.wait_empty(5)
                self.assertTrue(has_exited(child))
        finally:
            owner.terminate()
            owner.close()
            if child is not None:
                try:
                    if not has_exited(child):
                        child.kill()
                except psutil.NoSuchProcess:
                    pass
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    def test_close_is_idempotent_and_never_targets_a_stale_process_group(self):
        owner = ProcessOwner()
        owner.close(); owner.close(); owner.terminate()

    @unittest.skipUnless(os.name == "posix", "POSIX live group leader")
    def test_live_anchor_survives_root_reaping_and_other_multiprocessing_start(self):
        owner = ProcessOwner()
        root = child = guard = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                root = subprocess.Popen([sys.executable, "-c", ROOT_CODE, directory, str(owner.pgid)])
                wait_for(lambda: (path / "ready").exists())
                owner.attach(root.pid)
                (path / "release").touch()
                wait_for(lambda: (path / "child").exists())
                child = psutil.Process(int((path / "child").read_text()))
                self.assertEqual(root.wait(timeout=5), 0)
                transient = multiprocessing.get_context("spawn").Process(target=quick_exit)
                transient.start()
                transient.join(5)
                self.assertFalse(transient.is_alive())
                transient.close()
                anchor = psutil.Process(owner.anchor_pid)
                self.assertNotEqual(owner.pgid, root.pid)
                self.assertTrue(anchor.is_running())
                self.assertEqual(os.getpgid(anchor.pid), anchor.pid)
                guard = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                # Patching only the supervisor cannot alter the independently
                # spawned anchor. No historical numeric group is signalled here.
                with mock.patch.object(module.os, "killpg", side_effect=AssertionError("Supervisor must not signal a PGID")):
                    owner.terminate()
                    owner.terminate()
                    owner.wait_empty(5)
                    owner.close()
                self.assertIsNone(guard.poll())
                self.assertTrue(has_exited(child))
        finally:
            owner.close()
            if guard is not None:
                guard.terminate(); guard.wait(5)
            if child is not None:
                try:
                    if not has_exited(child):
                        child.kill()
                except psutil.NoSuchProcess:
                    pass
            if root is not None:
                if root.poll() is None:
                    root.kill()
                root.wait(5)

    @unittest.skipUnless(os.name == "posix", "POSIX anchor failure")
    def test_unexpected_anchor_exit_refuses_historical_group_signals(self):
        owner = ProcessOwner()
        psutil.Process(owner.anchor_pid).kill()
        owner._anchor.join(5)
        with mock.patch.object(module.os, "killpg") as killpg:
            with self.assertRaisesRegex(RuntimeError, "exited unexpectedly"):
                owner.terminate()
            with self.assertRaisesRegex(RuntimeError, "ownership cannot be verified"):
                owner.wait_empty()
            with self.assertRaisesRegex(RuntimeError, "exited unexpectedly"):
                owner.close()
            owner.close(); owner.terminate()
        killpg.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX anchor cleanup failure")
    def test_unexpected_anchor_exit_retains_staging_and_reports_cleanup_failure(self):
        from src.download_job import DownloadJob, DownloadCleanupError
        from src.scraper import VideoItem
        owner = ProcessOwner()
        psutil.Process(owner.anchor_pid).kill()
        owner._anchor.join(5)
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "private"
            staging.mkdir()
            (staging / "part").write_bytes(b"retained")
            states = []
            job = DownloadJob(VideoItem("test", "https://example.test/a", "https://example.test/"),
                              Path(directory), on_state=states.append)
            job._state = "cancelled"
            with self.assertRaises(DownloadCleanupError) as caught:
                job._cleanup(process=None, tree=None, receiver=None, reader=None,
                             work_dir=staging, published=None, completed=False, owner=owner)
            self.assertEqual(caught.exception.retained_dir, staging)
            self.assertEqual((staging / "part").read_bytes(), b"retained")
            self.assertEqual(states, ["failed"])
            self.assertEqual(job.state, "failed")
        owner.close()

    @unittest.skipUnless(os.name == "posix", "POSIX anchor setup")
    def test_invalid_handshake_cleans_anchor_and_cancelled_setup_starts_none(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "anchor.pid"
            with mock.patch.dict(os.environ, {"VIDEO_SCRAPER_OWNER_TEST_PID_FILE": str(marker)}), \
                    mock.patch.object(module, "_posix_group_anchor", invalid_anchor):
                with self.assertRaisesRegex(RuntimeError, "Invalid download group owner handshake"):
                    ProcessOwner()
            pid = int(marker.read_text())
            self.assertFalse(psutil.pid_exists(pid))
        with mock.patch.object(module.multiprocessing, "get_context") as context:
            with self.assertRaises(InterruptedError):
                ProcessOwner(cancelled=lambda: True)
        context.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX supervisor pipe EOF")
    def test_supervisor_death_closes_pipe_and_terminates_owned_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            controller = path / "controller.py"
            controller.write_text(
                "import os,sys,time,subprocess\nfrom pathlib import Path\n"
                f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
                "from src.process_owner import ProcessOwner\n"
                "if __name__ == '__main__':\n"
                " owner=ProcessOwner(); p=Path(sys.argv[1])\n"
                " (p/'anchor').write_text(str(owner.anchor_pid))\n"
                f" root=subprocess.Popen([sys.executable,'-c',{ROOT_CODE!r},str(p),str(owner.pgid)])\n"
                " while not (p/'ready').exists(): time.sleep(.01)\n"
                " owner.attach(root.pid); (p/'release').touch()\n"
                " while not (p/'child').exists(): time.sleep(.01)\n"
                " os._exit(0)\n",
            )
            process = subprocess.Popen([sys.executable, str(controller), directory])
            owned = []
            try:
                self.assertEqual(process.wait(10), 0)
                for name in ("anchor", "child"):
                    try:
                        owned.append(psutil.Process(int((path / name).read_text())))
                    except psutil.NoSuchProcess:
                        pass
                def stopped():
                    for member in owned:
                        try:
                            if not has_exited(member):
                                return False
                        except psutil.NoSuchProcess:
                            pass
                    return True
                wait_for(stopped, 5)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(5)
                for member in owned:
                    try:
                        if not has_exited(member):
                            member.kill()
                    except psutil.NoSuchProcess:
                        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
