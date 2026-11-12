from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
from pathlib import Path
from typing import Callable

from .providers import candidate_state_dirs, default_state_dir


MAX_EVENT_BYTES = 1024 * 1024
HOOK_EVENT_SEND_TIMEOUT_SECONDS = 0.2
PRIVATE_DIR_MODE = 0o700
PRIVATE_SOCKET_MODE = 0o600


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (new directories 0700) and refuse one this user does not own.

    Hook payloads hold prompts and tool input, so the sockets that carry them
    live in owner-only directories. A directory someone else created, or a
    symlink planted in a shared location, is rejected rather than used.
    """
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError(f"{path} is not a directory owned by this user")
    if info.st_mode & 0o777 != PRIVATE_DIR_MODE:
        path.chmod(PRIVATE_DIR_MODE)
    return path


def directory_is_private(path: Path) -> bool:
    """True when ``path`` is a real directory owned by this user that only it can enter."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and info.st_mode & 0o077 == 0
    )


def default_event_socket_path() -> Path:
    return default_state_dir() / "events.sock"


def candidate_event_socket_paths() -> tuple[Path, ...]:
    """Socket paths a listening app may have bound, most likely first."""
    return tuple(directory / "events.sock" for directory in candidate_state_dirs())


def default_latest_state_path() -> Path:
    return default_state_dir() / "latest.json"


def request_settings_window(*, socket_path: Path | None = None) -> bool:
    """Ask a running UI to open settings, requiring an acknowledgement."""
    targets = (socket_path,) if socket_path is not None else candidate_event_socket_paths()
    for target in targets:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            try:
                client.connect(str(target.expanduser()))
                client.sendall(b'{"command":"open-settings"}')
                client.shutdown(socket.SHUT_WR)
                if client.recv(2) == b"ok":
                    return True
            except OSError:
                continue
    return False


class UICommandServer:
    def __init__(
        self,
        *,
        socket_path: Path | None = None,
        on_open_settings: Callable[[], None] | None = None,
    ) -> None:
        self.on_open_settings = on_open_settings
        self.socket_path = (socket_path or default_event_socket_path()).expanduser()
        self.socket: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.running = False

    def start(self) -> Path:
        ensure_private_dir(self.socket_path.parent)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        # The 0700 parent already keeps other users out; this covers a socket
        # left behind if the directory is ever loosened.
        os.chmod(self.socket_path, PRIVATE_SOCKET_MODE)
        server.listen(16)
        server.settimeout(0.5)
        self.socket = server
        self.running = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        return self.socket_path

    def stop(self) -> None:
        self.running = False
        server = self.socket
        self.socket = None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _serve(self) -> None:
        while self.running:
            server = self.socket
            if server is None:
                return
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return

            with connection:
                self._handle_connection(connection)

    def _handle_connection(self, connection: socket.socket) -> None:
        # Each client gets one second in total, so a slow or stalled client cannot
        # block the accept loop. At the deadline, parse the bytes that arrived.
        deadline = time.monotonic() + 1.0
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            connection.settimeout(remaining)
            try:
                chunk = connection.recv(65536)
            except TimeoutError:
                break
            except OSError:
                return
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_EVENT_BYTES:
                return
            chunks.append(chunk)

        try:
            message = json.loads(b"".join(chunks).decode("utf-8"))
        except Exception:
            return

        if not isinstance(message, dict):
            return
        if message.get("command") == "open-settings":
            if self.on_open_settings is not None:
                self.on_open_settings()
                try:
                    connection.sendall(b"ok")
                except OSError:
                    pass
            return
