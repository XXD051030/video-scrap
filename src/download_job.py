"""Independently controlled downloads, isolated in spawned child processes.

Suspending one process tree pauses its existing requests and temporary files;
it does not replay requests or share a suspended lock with another download.
Only the supervising parent publishes a successful result to the output folder.
"""

from __future__ import annotations

from dataclasses import asdict
from collections import deque
import json
import multiprocessing
from multiprocessing.connection import Connection
import os
from pathlib import Path
import queue
import stat
import sys
import tempfile
import threading
import time
from typing import Callable, Optional

import psutil

from .downloader import DownloadLaneLimiter, DownloadProgress, VideoDownloader
from .parallel_downloader import _publish_unique
from .scraper import VideoItem
from .process_owner import ProcessOwner
from .temp_ownership import create_owned_tempdir, add_temp_owner, remove_owned_tempdir
from .transfer_clock import configure_pause_offset


class DownloadCancelled(RuntimeError):
    """The user cancelled this job before its result was committed."""


class DownloadCleanupError(RuntimeError):
    """Cleanup failed; a retained private folder may still have an owner."""

    def __init__(self, message: str, retained_dir: Optional[Path] = None) -> None:
        super().__init__(message)
        self.retained_dir = retained_dir


_ITEM_FIELDS = (
    "title", "url", "source_url", "referer", "duration", "ext", "is_direct",
    "is_hls", "x_playlist_index", "x_auth_browser", "hls_png_wrapped", "is_dash",
    "browser_cookies",
)
_TERMINAL_STATES = {"cancelled", "completed", "failed"}
_MESSAGE_LIMIT = 4096


def _item_payload(item: VideoItem) -> dict:
    """Do not pickle extractor results, browser CookieJars, or Qt objects."""
    return {name: getattr(item, name) for name in _ITEM_FIELDS}


def _send_message(connection: Connection, message: dict) -> None:
    encoded = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > _MESSAGE_LIMIT:
        raise ValueError("Download worker message exceeds its size limit")
    connection.send_bytes(encoded)


def _download_child(
    connection: Connection,
    payload: dict,
    work_dir: str,
    quality: str,
    audio_format: str,
    parallel_connections: int,
    owner_ready=None,
    owner_release=None,
    owner_pgid=None,
    pause_offset=None,
) -> None:
    """Spawn target; imported normally by Python and the frozen executable."""
    configure_pause_offset(pause_offset)
    if owner_ready is not None:
        if sys.platform != "win32":
            if owner_pgid is None:
                raise RuntimeError("Download worker is missing its private group owner")
            os.setpgid(0, owner_pgid)
        owner_ready.set()
        if not owner_release.wait(30):
            connection.close()
            return
    root = Path(work_dir)
    temporary = root / "temporary"
    temporary.mkdir(exist_ok=True)
    tempfile.tempdir = str(temporary)
    for variable in ("TMPDIR", "TMP", "TEMP"):
        os.environ[variable] = str(temporary)
    # Windowed Windows bundles may not have console streams. IPC never relies
    # on stdout; libraries can still safely print incidental diagnostic text.
    for stream_name in ("stdout", "stderr"):
        if getattr(sys, stream_name) is None:
            setattr(sys, stream_name, open(os.devnull, "w", encoding="utf-8"))

    send_lock = threading.Lock()  # Child-local, never acquired by the parent.
    last_progress = 0.0
    last_status = ""

    def relay(progress: DownloadProgress) -> None:
        nonlocal last_progress, last_status
        now = time.monotonic()
        with send_lock:
            if (progress.status == "downloading" and last_status == "downloading"
                    and now - last_progress < 0.1):
                return
            last_progress = now
            last_status = progress.status
            event = asdict(progress)
            # Windows paths and backend diagnostics must remain small enough
            # for a supervisor to consume without a partially received packet.
            event["filename"] = str(event["filename"] or "")[-256:] or None
            event["message"] = str(event["message"] or "")[:192]
            _send_message(connection, {"kind": "progress", "value": event})

    downloader = None
    try:
        output = root / "output"
        output.mkdir()
        downloader = VideoDownloader(
            output, parallel_connections=parallel_connections,
            lane_limiter=DownloadLaneLimiter(parallel_connections),
        )
        try:
            result = downloader.download(
                VideoItem(**payload), quality=quality,
                audio_format=audio_format, on_progress=relay,
            )
        finally:
            downloader.close()
        _send_message(connection, {"kind": "result", "path": str(result.relative_to(root))})
    except BaseException as exc:
        try:
            _send_message(connection, {
                "kind": "error", "message": f"{type(exc).__name__}: {exc}"[:400],
            })
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


class _ProcessTree:
    """Own precisely one spawned worker and descendants identified beneath it."""

    def __init__(self, pid: int) -> None:
        self.root = psutil.Process(pid)
        self.known: dict[int, psutil.Process] = {pid: self.root}
        self.suspended: dict[int, psutil.Process] = {}
        self.owner = None

    @staticmethod
    def alive(process: psutil.Process) -> bool:
        try:
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    def discover(self) -> list[psutil.Process]:
        """Retain handles to descendants even if their parent exits later."""
        try:
            children = self.root.children(recursive=True)
        except psutil.NoSuchProcess:
            children = []
        for process in children:
            self.known[process.pid] = process
        return [process for process in self.known.values() if self.alive(process)]

    @staticmethod
    def _wait_stopped(process: psutil.Process) -> None:
        deadline = time.monotonic() + 1.0
        while True:
            try:
                if not process.is_running() or process.status() in {
                    psutil.STATUS_STOPPED, psutil.STATUS_ZOMBIE,
                }:
                    return
            except psutil.NoSuchProcess:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("The operating system did not confirm download suspension")
            time.sleep(0.005)

    def suspend(self) -> None:
        """Freeze the root first, then close spawning races in descendants."""
        suspended_now: list[psutil.Process] = []
        try:
            if not self.alive(self.root):
                return
            self.root.suspend()
            suspended_now.append(self.root)
            self.suspended[self.root.pid] = self.root
            self._wait_stopped(self.root)
            # A descendant could spawn between enumeration and suspension.
            # Once a complete pass finds no new process, all known ancestors
            # are frozen and no ordinary descendant can create another child.
            for _ in range(32):
                found_new = False
                for process in self.discover():
                    if process.pid in self.suspended:
                        continue
                    try:
                        process.suspend()
                    except psutil.NoSuchProcess:
                        continue
                    self.suspended[process.pid] = process
                    suspended_now.append(process)
                    self._wait_stopped(process)
                    found_new = True
                if not found_new:
                    return
            raise RuntimeError("Could not stabilize the download process tree")
        except BaseException:
            # Never leave an incompletely suspended tree or claim it is paused.
            for process in reversed(suspended_now):
                try:
                    process.resume()
                except psutil.NoSuchProcess:
                    pass
                except psutil.Error:
                    pass
                self.suspended.pop(process.pid, None)
            raise

    def resume(self) -> None:
        # Resume descendants before their parent so it cannot run ahead of
        # still-frozen FFmpeg output pipes or other subprocesses.
        errors = []
        processes = list(self.suspended.values())
        processes.sort(key=lambda process: process.pid == self.root.pid)
        for process in processes:
            try:
                process.resume()
            except psutil.NoSuchProcess:
                pass
            except psutil.Error as exc:
                errors.append(exc)
                continue
            self.suspended.pop(process.pid, None)
        if errors:
            raise RuntimeError(f"Could not resume the complete download process tree: {errors[0]}")

    def terminate(self) -> None:
        """Kill paused workers too, and wait only in the supervising thread."""
        owner = getattr(self, "owner", None)
        if owner is not None:
            # Includes descendants whose parent exited before discover().
            owner.terminate()
        try:
            self.suspend()
        except (psutil.Error, RuntimeError):
            # Kill is still attempted when suspension is unavailable.
            self.discover()
        processes = list(self.known.values())
        processes.sort(key=lambda process: process.pid == self.root.pid)
        errors = []
        for process in processes:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
            except psutil.Error as exc:
                errors.append(exc)
        # multiprocessing owns waitpid/reaping of the root. psutil must not
        # consume that child's exit status before Process.join can retrieve it.
        descendants = [process for process in processes if process.pid != self.root.pid]
        _, alive = psutil.wait_procs(descendants, timeout=3)
        alive = [process for process in alive if self.alive(process)]
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
            except psutil.Error as exc:
                errors.append(exc)
        if alive:
            _, alive = psutil.wait_procs(alive, timeout=3)
        # Never consume the root's waitpid status, but do confirm that its
        # kill actually succeeded. In particular an AccessDenied root must
        # not be mistaken for successful descendant-only cleanup.
        deadline = time.monotonic() + 1.0
        while self.alive(self.root) and time.monotonic() < deadline:
            time.sleep(0.01)
        remaining = [process for process in self.known.values() if self.alive(process)]
        if remaining:
            detail = str(errors[0]) if errors else "the operating system did not terminate it"
            raise RuntimeError(f"Could not terminate a download subprocess: {detail}")
        self.suspended.clear()


class DownloadJob:
    """One download with quick, thread-safe pause/resume/cancel requests.

    ``run`` belongs in a background thread. State/progress callbacks may run
    in that thread or the caller of a request; GUI callers should use signals.
    Pausing preserves the existing child, requests, and private staging files.
    The short publication commit resolves a completion/cancellation race:
    cancellation accepted before the commit discards this job's output; after
    ``completed`` has been committed cancellation is a no-op.
    """

    def __init__(
        self, item: VideoItem, output_dir: Path, quality: str = "best",
        audio_format: str = "original", parallel_connections: int = 16,
        on_progress: Optional[Callable[[DownloadProgress], None]] = None,
        on_state: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.item = item
        self.output_dir = Path(output_dir)
        self.quality = quality
        self.audio_format = audio_format
        self.parallel_connections = min(16, max(1, int(parallel_connections)))
        self.on_progress = on_progress
        self.on_state = on_state
        self._lock = threading.Lock()
        self._wake = threading.Event()  # Parent-local, never passed to child.
        self._cancelled = False
        self._want_paused = False
        self._running = False
        self._state = "starting"
        self._state_events = deque()
        self._dispatching_state = False
        self._process: Optional[multiprocessing.Process] = None
        self._pid: Optional[int] = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def is_paused(self) -> bool:
        return self.state == "paused"

    @property
    def pid(self) -> Optional[int]:
        with self._lock:
            return self._pid

    def _emit_state(self, state: str) -> None:
        # Events are enqueued while the state lock is held. A single caller
        # drains them outside the lock, including reentrant control requests.
        with self._lock:
            if self._dispatching_state:
                return
            self._dispatching_state = True
        try:
            while True:
                with self._lock:
                    if not self._state_events:
                        self._dispatching_state = False
                        return
                    event = self._state_events.popleft()
                if self.on_state is not None:
                    self.on_state(event)
        except BaseException:
            with self._lock:
                self._dispatching_state = False
            raise

    def _set_state_locked(self, state: str) -> None:
        self._state = state
        self._state_events.append(state)

    def _state_to(self, state: str) -> bool:
        with self._lock:
            if self._state in _TERMINAL_STATES:
                return False
            if self._cancelled and state not in {"cancelling", "cancelled", "failed"}:
                return False
            if state == "paused" and not self._want_paused:
                return False
            if state in {"starting", "downloading"} and self._want_paused:
                return False
            if self._state == state:
                return False
            self._set_state_locked(state)
        self._emit_state(state)
        return True

    def pause(self) -> bool:
        with self._lock:
            if self._cancelled or self._state in _TERMINAL_STATES | {"finalizing"}:
                return False
            if self._want_paused:
                return True
            self._want_paused = True
            self._set_state_locked("pausing")
        self._wake.set()
        self._emit_state("pausing")
        return True

    def resume(self) -> bool:
        with self._lock:
            if self._cancelled or self._state in _TERMINAL_STATES | {"finalizing"}:
                return False
            if not self._want_paused:
                return False
            self._want_paused = False
            self._set_state_locked("resuming")
        self._wake.set()
        self._emit_state("resuming")
        return True

    def cancel(self) -> bool:
        with self._lock:
            if self._state in _TERMINAL_STATES:
                return False
            if self._cancelled:
                return True
            self._cancelled = True
            self._set_state_locked("cancelling")
        self._wake.set()
        self._emit_state("cancelling")
        return True

    def _requests(self) -> tuple[bool, bool]:
        with self._lock:
            return self._cancelled, self._want_paused

    def _update_progress_state(self, event: DownloadProgress) -> None:
        """A fallback may resume network transfers after a failed merge."""
        if event.status == "finalizing":
            with self._lock:
                self._want_paused = False
            self._state_to("finalizing")
        elif event.status == "downloading" and self.state == "finalizing":
            self._state_to("downloading")

    @staticmethod
    def _validate_result(result: str, work_dir: Path) -> Path:
        path = Path(result)
        root = work_dir.resolve(strict=True)
        resolved = path.resolve(strict=True)
        if root not in resolved.parents or not stat.S_ISREG(resolved.stat().st_mode):
            raise RuntimeError("Download worker returned an invalid finished file")
        current = path.absolute()
        while current != root:
            if current.is_symlink():
                raise RuntimeError("Download worker returned a symlinked file")
            if current.parent == current:
                raise RuntimeError("Download worker returned a file outside its staging folder")
            current = current.parent
        return resolved

    def _cleanup(
        self, *, process, tree, receiver, reader, work_dir, published, completed,
        owner=None,
    ) -> None:
        """Attempt every cleanup step; never remove a live worker's files.

        This runs only in the supervisor thread. A failure in the tree helper
        must not prevent fallback owner-only termination, multiprocessing
        reaping, or closing the IPC handles. All failures remain observable.
        """
        errors: list[str] = []

        def attempt(description, action):
            try:
                return action()
            except BaseException as exc:
                errors.append(f"{description}: {type(exc).__name__}: {exc}")
                return None

        container = getattr(tree, "owner", None) if tree is not None else owner
        if tree is not None:
            attempt("terminate process tree", tree.terminate)
            # The handles were discovered under precisely this worker. Do not
            # enumerate global processes, create handles from unrelated PIDs,
            # or bypass psutil's process-identity protection on these handles.
            owners = list(tree.known.values())
            owners.sort(key=lambda owner: owner.pid == tree.root.pid)
            for owner in owners:
                alive = attempt("inspect owned subprocess", lambda owner=owner: tree.alive(owner))
                if alive is not False:
                    attempt("kill owned subprocess", owner.kill)
        elif container is not None:
            attempt("terminate process ownership", container.terminate)

        if process is not None:
            started = attempt("inspect worker PID", lambda: process.pid)
            alive = attempt("inspect worker", process.is_alive)
            if alive is not False:
                attempt("kill worker", process.kill)
            if started is not None or alive is not False:
                attempt("join worker", lambda: process.join(timeout=3))

        if reader is not None:
            attempt("join IPC reader", lambda: reader.join(timeout=1))
        if receiver is not None:
            attempt("close IPC receiver", receiver.close)
        if reader is not None:
            if attempt("inspect IPC reader", reader.is_alive):
                attempt("finish IPC reader", lambda: reader.join(timeout=1))
            if attempt("inspect IPC reader", reader.is_alive):
                errors.append("IPC reader did not finish after the receiver was closed")

        all_exited = True
        if container is not None:
            before = len(errors)
            attempt("verify process ownership empty", container.wait_empty)
            all_exited = len(errors) == before
            attempt("close process ownership", container.close)
        if process is not None:
            if attempt("verify worker exit", process.is_alive) is not False:
                all_exited = False
        if tree is not None:
            for owner in tree.known.values():
                if attempt("verify owned subprocess exit", lambda owner=owner: tree.alive(owner)) is not False:
                    all_exited = False
        # close is always attempted, even for an unstarted process or a failed
        # tree helper. multiprocessing intentionally rejects closing a live
        # process; that rejection is reported and its staging is retained.
        if process is not None:
            attempt("close worker handle", process.close)
        if published is not None and not completed:
            attempt("discard uncommitted output", lambda: published.unlink(missing_ok=True))

        retained_dir = None
        if work_dir is not None:
            if all_exited:
                attempt("remove private staging", lambda: remove_owned_tempdir(work_dir))
                if attempt("verify private staging removal", work_dir.exists) is not False:
                    retained_dir = work_dir
            else:
                retained_dir = work_dir
                errors.append("An owned subprocess is still running or its exit could not be verified")
        if errors:
            # Cleanup is part of successful completion/cancellation. Explicitly
            # override an earlier terminal callback if its cleanup later fails.
            with self._lock:
                self._set_state_locked("failed")
            attempt("report cleanup failure", lambda: self._emit_state("failed"))
            detail = "; ".join(errors)
            if retained_dir is not None:
                detail += f"; private staging retained at {retained_dir}"
            raise DownloadCleanupError(f"Download cleanup failed: {detail}", retained_dir)

    def run(self) -> Path:
        with self._lock:
            if self._running or self._state in _TERMINAL_STATES:
                raise RuntimeError("A download job can only run once")
            self._running = True
        work_dir = None
        process = None
        tree = None
        receiver = None
        reader = None
        published = None
        completed = False
        owner = None
        try:
            if self._requests()[0]:
                raise DownloadCancelled("Download cancelled")
            self._state_to("starting")
            destination = self.output_dir.resolve()
            destination.mkdir(parents=True, exist_ok=True)
            work_dir = create_owned_tempdir(".video-scrap-job-", destination)
            context = multiprocessing.get_context("spawn")
            # Only this supervisor writes; no lock may be held by an OS-paused
            # child when its clock is compensated before resume.
            pause_offset = context.RawValue("q", 0)
            pause_started = None

            def resume_tree():
                nonlocal pause_started
                if pause_started is not None:
                    pause_offset.value += max(0, time.monotonic_ns() - pause_started)
                    pause_started = None
                tree.resume()
            owner = ProcessOwner(cancelled=lambda: self._requests()[0])
            if owner.anchor_pid is not None:
                add_temp_owner(work_dir, owner.anchor_pid, group=owner.pgid)
            owner_ready, owner_release = context.Event(), context.Event()
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(
                target=_download_child,
                args=(sender, _item_payload(self.item), str(work_dir), self.quality,
                      self.audio_format, self.parallel_connections, owner_ready, owner_release,
                      owner.pgid, pause_offset),
                name="VideoScraperDownload",
            )
            with self._lock:
                self._process = process
            if self._requests()[0]:
                sender.close()
                raise DownloadCancelled("Download cancelled")
            try:
                process.start()
            finally:
                sender.close()
            with self._lock:
                self._pid = process.pid
            deadline = time.monotonic() + 20
            while not owner_ready.wait(0.05):
                if self._requests()[0]:
                    raise DownloadCancelled("Download cancelled")
                if not process.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("Download worker ownership handshake failed")
            owner.attach(process.pid)
            add_temp_owner(work_dir, process.pid)
            tree = _ProcessTree(process.pid)
            tree.owner = owner
            owner_release.set()
            incoming: queue.Queue = queue.Queue()

            def read_messages() -> None:
                # A partial low-level pipe write cannot prevent the supervisor
                # from responding to pause/cancel. EOF follows worker cleanup.
                try:
                    while True:
                        message = json.loads(receiver.recv_bytes(_MESSAGE_LIMIT))
                        incoming.put(message)
                except EOFError:
                    pass
                except (OSError, ValueError, UnicodeError) as exc:
                    incoming.put({"kind": "error", "message": f"Invalid worker message: {exc}"})
                finally:
                    incoming.put({"kind": "eof"})

            reader = threading.Thread(target=read_messages, name="DownloadJobPipe", daemon=True)
            reader.start()
            result = None
            worker_error = None
            pipe_eof = False
            self._state_to("downloading")
            while True:
                while True:
                    try:
                        message = incoming.get_nowait()
                    except queue.Empty:
                        break
                    kind = message.get("kind")
                    if kind == "progress":
                        event = DownloadProgress(**message["value"])
                        self._update_progress_state(event)
                        if (
                            event.status not in {"completed", "partial"}
                            and not self._requests()[0]
                            and self.state not in {"pausing", "paused", "resuming"}
                        ):
                            if self.on_progress is not None:
                                self.on_progress(event)
                    elif kind == "result":
                        result = str(work_dir / message["path"])
                    elif kind == "error":
                        worker_error = message["message"]
                    elif kind == "eof":
                        pipe_eof = True
                cancelled, want_paused = self._requests()
                if cancelled:
                    raise DownloadCancelled("Download cancelled")
                tree.discover()
                if want_paused and not tree.suspended and process.is_alive():
                    pause_started = time.monotonic_ns()
                    tree.suspend()
                    # A resume/cancel request can arrive during OS suspension.
                    cancelled, want_paused = self._requests()
                    if cancelled:
                        raise DownloadCancelled("Download cancelled")
                    if want_paused:
                        self._state_to("paused")
                    else:
                        resume_tree()
                        self._state_to("downloading")
                elif not want_paused and tree.suspended:
                    resume_tree()
                    self._state_to("downloading")
                if not process.is_alive() and pipe_eof:
                    break
                self._wake.wait(0.03)
                self._wake.clear()
            process.join(timeout=1)
            if worker_error:
                raise RuntimeError(worker_error)
            if process.exitcode != 0 or result is None:
                raise RuntimeError(f"Download worker exited without a result (exit {process.exitcode})")
            staged = self._validate_result(result, work_dir)
            self._state_to("finalizing")
            if self._requests()[0]:
                raise DownloadCancelled("Download cancelled")
            # No UI-facing lock is held during filesystem publication.
            published = _publish_unique(staged, destination / staged.name)
            with self._lock:
                if self._cancelled:
                    raise DownloadCancelled("Download cancelled")
                self._set_state_locked("completed")
                completed = True
            self._emit_state("completed")
            if self.on_progress is not None:
                size = published.stat().st_size
                self.on_progress(DownloadProgress(
                    status="completed", downloaded_bytes=size, total_bytes=size,
                    filename=str(published), message="Download complete",
                ))
            return published
        except DownloadCancelled:
            self._state_to("cancelled")
            raise
        except BaseException as exc:
            if self._requests()[0] and not completed:
                self._state_to("cancelled")
                raise DownloadCancelled("Download cancelled") from exc
            self._state_to("failed")
            raise
        finally:
            try:
                self._cleanup(
                    process=process, tree=tree, receiver=receiver, reader=reader,
                    work_dir=work_dir, published=published, completed=completed,
                    owner=owner,
                )
            finally:
                if owner is not None:
                    try:
                        owner.terminate()
                    finally:
                        owner.close()
