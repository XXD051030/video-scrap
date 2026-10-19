"""OS-owned download descendants, established before backend code starts.

POSIX workers join a private group whose persistent leader owns its lifetime.
Only that live leader signals its own group, never a supervisor holding an old
numeric PGID. Windows workers enter a kill-on-close Job Object.
"""
from __future__ import annotations

import ctypes
import multiprocessing
import os
import signal
import sys
import time
import psutil


def _posix_group_anchor(connection) -> None:
    """Keep the group leader alive until teardown, including supervisor EOF."""
    os.setpgid(0, 0)

    def stop_group(*_args):
        # The caller is the living group leader. Its PID cannot have been
        # recycled while this code executes, unlike a historical parent PGID.
        if os.getpgrp() != os.getpid():
            os._exit(2)
        os.killpg(os.getpgrp(), signal.SIGKILL)
        os._exit(2)

    # multiprocessing terminates daemon children at ordinary interpreter exit.
    # The same group teardown must happen then, not just on command/pipe EOF.
    signal.signal(signal.SIGTERM, stop_group)
    try:
        connection.send_bytes(b"ready")
        try:
            connection.recv_bytes(32)
        except (EOFError, OSError):
            pass
    finally:
        connection.close()
        stop_group()


class ProcessOwner:
    def __init__(self, cancelled=None) -> None:
        self.pid = None
        self.job = None
        self.api = None
        self.pgid = None
        self._anchor = None
        self._control = None
        self._termination_requested = False
        if sys.platform == "win32":
            self._create_job()
        else:
            self._create_anchor(cancelled)

    @property
    def anchor_pid(self):
        return self._anchor.pid if self._anchor is not None else None

    def _create_anchor(self, cancelled) -> None:
        if cancelled is not None and cancelled():
            raise InterruptedError("Download ownership setup cancelled")
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        anchor = context.Process(target=_posix_group_anchor, args=(child,),
                                 name="VideoScraperProcessOwner", daemon=True)
        self._anchor, self._control = anchor, parent
        try:
            try:
                anchor.start()
            finally:
                child.close()
            deadline = time.monotonic() + 20
            while not parent.poll(.05):
                if cancelled is not None and cancelled():
                    raise InterruptedError("Download ownership setup cancelled")
                if not anchor.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("Download group owner handshake failed")
            if parent.recv_bytes(32) != b"ready" or not anchor.is_alive():
                raise RuntimeError("Invalid download group owner handshake")
            self.pgid = anchor.pid
            if os.getpgid(anchor.pid) != self.pgid:
                raise RuntimeError("Download group owner is not its group leader")
        except BaseException:
            # No worker can have joined yet. Close wakes an already initialized
            # anchor; terminate also handles one still importing during spawn.
            parent.close()
            self._control = None
            if anchor.pid is not None:
                if anchor.is_alive():
                    anchor.terminate()
                anchor.join(3)
                if anchor.is_alive():
                    anchor.kill()
                    anchor.join(3)
            anchor.close()
            self._anchor = None
            self.pgid = None
            raise

    def _create_job(self) -> None:
        from ctypes import wintypes as w
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", w.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", w.DWORD), ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        api.CreateJobObjectW.restype = w.HANDLE
        api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        api.SetInformationJobObject.restype = w.BOOL
        api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        api.OpenProcess.restype = w.HANDLE
        api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        api.AssignProcessToJobObject.restype = w.BOOL
        api.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        api.TerminateJobObject.restype = w.BOOL
        api.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
        api.QueryInformationJobObject.restype = w.BOOL
        api.CloseHandle.argtypes = [w.HANDLE]
        api.CloseHandle.restype = w.BOOL
        self.api = api
        self.job = api.CreateJobObjectW(None, None)
        if not self.job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE, no breakaway.
        if not api.SetInformationJobObject(self.job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach(self, pid: int) -> None:
        if self.job is not None:
            handle = self.api.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not self.api.AssignProcessToJobObject(self.job, handle):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self.api.CloseHandle(handle)
        else:
            if (self._anchor is None or self._termination_requested
                    or not self._anchor.is_alive()):
                raise RuntimeError("Download group owner exited before worker attachment")
            if os.getpgid(pid) != self.pgid:
                raise RuntimeError("Download worker did not establish a private process group")
        self.pid = pid

    def terminate(self) -> None:
        if self.job is not None:
            if not self.api.TerminateJobObject(self.job, 1):
                raise ctypes.WinError(ctypes.get_last_error())
        elif self._anchor is not None and not self._termination_requested:
            if not self._anchor.is_alive():
                raise RuntimeError("Download group owner exited unexpectedly; refusing to signal a historical PGID")
            self._termination_requested = True
            try:
                self._control.send_bytes(b"terminate")
            finally:
                self._control.close()
                self._control = None

    def close(self) -> None:
        if self.job is not None:
            handle, self.job = self.job, None
            if not self.api.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        elif self._anchor is not None:
            anchor = self._anchor
            try:
                self.terminate()
            finally:
                if self._control is not None:
                    self._control.close()
                    self._control = None
                anchor.join(3)
                if not anchor.is_alive():
                    anchor.close()
                    self._anchor = None
                else:
                    raise RuntimeError("Download group owner did not finish after teardown")
        self.pid = None
        self.pgid = None

    def wait_empty(self, timeout: float = 3) -> None:
        deadline = time.monotonic() + timeout
        while self.pid is not None or self._anchor is not None:
            active = False
            if self.job is not None:
                class Accounting(ctypes.Structure):
                    _fields_ = [(name, ctypes.c_longlong) for name in
                                ("TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")] + [
                                (name, ctypes.c_ulong) for name in
                                ("TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]
                info = Accounting()
                if not self.api.QueryInformationJobObject(self.job, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                active = bool(info.ActiveProcesses)
            else:
                if self._anchor is None:
                    return
                if not self._termination_requested and not self._anchor.is_alive():
                    raise RuntimeError("Download group owner exited unexpectedly; ownership cannot be verified")
                active = self._anchor.is_alive()
                for process in psutil.process_iter(("pid", "status")):
                    try:
                        group = os.getpgid(process.pid)
                    except (ProcessLookupError, PermissionError, psutil.Error):
                        continue
                    if group != self.pgid:
                        continue
                    try:
                        active = process.status() != psutil.STATUS_ZOMBIE
                    except psutil.NoSuchProcess:
                        continue
                    except psutil.Error as exc:
                        raise RuntimeError("Cannot verify an owned download subprocess exited") from exc
                    if active:
                        break
            if not active:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("An owned download subprocess did not exit")
            time.sleep(0.02)
