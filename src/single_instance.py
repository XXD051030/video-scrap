"""Keep one application process per user and activate it on repeat launches."""

from __future__ import annotations

from enum import Enum
import hashlib
import os
from pathlib import Path
import sys
import time

from PyQt6.QtCore import QObject, QLockFile, QThread, QTimer, pyqtSignal
from PyQt6.QtNetwork import QAbstractSocket, QLocalServer, QLocalSocket


class InstanceRole(Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    ERROR = "error"


def _allow_windows_foreground(owner_pid: int) -> None:
    """Hand a user-initiated launch's focus permission to the existing owner.

    Windows can otherwise only flash the background application's taskbar
    entry. Permission transfer is best-effort and cannot change ownership.
    """
    if sys.platform != "win32" or not 0 < owner_pid < 0xFFFFFFFF:
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        allow = user32.AllowSetForegroundWindow
        allow.argtypes = [wintypes.DWORD]
        allow.restype = wintypes.BOOL
        allow(owner_pid)
    except (AttributeError, OSError, TypeError, ValueError):
        # Foreground policy, unavailable APIs, or access restrictions must not
        # prevent activation IPC or cause a second application to be started.
        pass


class SingleInstance(QObject):
    """Own the process lock before constructing any shared application state.

    A local socket handles activation only; the lock is the ownership decision.
    In particular, Windows permits multiple servers on one named pipe, so a
    successful listen alone cannot establish that this is the first process.
    """

    activation_requested = pyqtSignal()
    _ACTIVATE = b"activate\n"
    _ACK = b"activated\n"

    def __init__(self, config_dir: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config_dir = Path(config_dir).resolve()
        identity = os.path.normcase(str(self.config_dir)).encode("utf-8")
        self.server_name = "video-scraper-" + hashlib.sha256(identity).hexdigest()[:32]
        self.lock_path = self.config_dir / "instance.lock"
        self._lock = QLockFile(str(self.lock_path))
        # A running desktop application is not stale merely because it has
        # been open for a long time. Qt still recovers locks of dead processes.
        self._lock.setStaleLockTime(0)
        self._server: QLocalServer | None = None
        self.error_message = ""

    def start(self, timeout_ms: int = 5000) -> InstanceRole:
        """Acquire ownership or request activation, with a bounded startup wait.

        If the first process is still starting or cannot receive activation,
        the second process must never proceed to create another MainWindow.
        """
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.error_message = f"Cannot access the application settings directory: {exc}"
            return InstanceRole.ERROR

        deadline = time.monotonic() + max(0, timeout_ms) / 1000
        while True:
            if self._lock.tryLock(0):
                return self._listen()
            if self._lock.error() != QLockFile.LockError.LockFailedError:
                self.error_message = (
                    "Cannot create the application instance lock. "
                    "Check that the settings directory is writable."
                )
                return InstanceRole.ERROR

            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                self.error_message = (
                    "Video Scraper is already running or still starting, but could "
                    "not be brought to the front. Please use the existing window "
                    "or wait for it to finish starting."
                )
                return InstanceRole.ERROR
            if self._activate_existing(min(500, remaining_ms)):
                return InstanceRole.SECONDARY
            QThread.msleep(min(50, max(1, remaining_ms)))

    def _listen(self) -> InstanceRole:
        # Check before listen as well: implementations may allow an existing
        # endpoint to be replaced or (on Windows) to have multiple listeners.
        # A live endpoint without our lock is a conflict, not stale cleanup.
        probe = QLocalSocket()
        probe.connectToServer(self.server_name)
        reachable = probe.waitForConnected(100)
        probe.abort()
        if reachable:
            self.error_message = (
                "Another process is already listening for application activation. "
                "Please close the existing Video Scraper window and try again."
            )
            self._lock.unlock()
            return InstanceRole.ERROR
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self.server_name):
            # A Unix process killed without cleanup can leave its socket file.
            # Only the lock owner may remove it, and never if it is reachable.
            if server.serverError() == QAbstractSocket.SocketError.AddressInUseError:
                probe = QLocalSocket()
                probe.connectToServer(self.server_name)
                reachable = probe.waitForConnected(100)
                probe.abort()
                if not reachable:
                    QLocalServer.removeServer(self.server_name)
                    if server.listen(self.server_name):
                        self._server = server
                        server.newConnection.connect(self._accept_connections)
                        return InstanceRole.PRIMARY
            self.error_message = f"Cannot listen for application activation: {server.errorString()}"
            server.close()
            server.deleteLater()
            self._lock.unlock()
            return InstanceRole.ERROR
        self._server = server
        server.newConnection.connect(self._accept_connections)
        return InstanceRole.PRIMARY

    def _activate_existing(self, timeout_ms: int) -> bool:
        socket = QLocalSocket()
        deadline = time.monotonic() + timeout_ms / 1000
        try:
            socket.connectToServer(self.server_name)
            if not socket.waitForConnected(timeout_ms):
                return False
            if sys.platform == "win32":
                known_owner, owner_pid, _hostname, _appname = self._lock.getLockInfo()
                if known_owner:
                    _allow_windows_foreground(owner_pid)
            socket.write(self._ACTIVATE)
            socket.flush()
            response = bytearray()
            while time.monotonic() < deadline:
                response.extend(bytes(socket.readAll()))
                if b"\n" in response:
                    return bytes(response) == self._ACK
                remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                if not socket.waitForReadyRead(remaining_ms):
                    response.extend(bytes(socket.readAll()))
                    return bytes(response) == self._ACK
            return False
        finally:
            socket.abort()

    def _accept_connections(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            socket.setReadBufferSize(128)
            timer = QTimer(socket)
            timer.setSingleShot(True)
            timer.timeout.connect(socket.abort)
            timer.start(2000)
            socket.disconnected.connect(socket.deleteLater)
            socket.readyRead.connect(lambda client=socket: self._read_activation(client))
            self._read_activation(socket)

    def _read_activation(self, socket: QLocalSocket) -> None:
        if socket.canReadLine():
            if bytes(socket.readLine(128)) == self._ACTIVATE:
                self.activation_requested.emit()
                socket.write(self._ACK)
                socket.flush()
            socket.disconnectFromServer()
        elif socket.bytesAvailable() >= 128:
            socket.abort()

    def close(self) -> None:
        """Release activation and ownership after the main window shuts down."""
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None
        if self._lock.isLocked():
            self._lock.unlock()
