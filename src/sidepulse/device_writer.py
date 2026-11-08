from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_FILE_NAME = "LEDS.LED"
LED_FILE_NAMES = (DEFAULT_FILE_NAME,)
KNOWN_LED_FILE_NAMES = frozenset(name.upper() for name in LED_FILE_NAMES)
MAX_LED_BYTES = 512
MAX_LED_LINES = 20
MOUNT_ROOT = Path("/Volumes")
DEVICE_NAME_HINTS = (
    "sidepulsepro",
    "sidepulsedot",
    "pulsedot",
)
_WRITE_LOCK = threading.Lock()


class DeviceWriteError(RuntimeError):
    pass


class DeviceBusyError(DeviceWriteError):
    """Another process (e.g. `sidepulse visualize`) holds an exclusive LED lease."""


@dataclass(frozen=True)
class DeviceCandidate:
    root: Path
    target: Path
    reason: str


def write_led_program(
    text: str,
    *,
    device_path: Path | None = None,
    file_name: str = DEFAULT_FILE_NAME,
    dry_run: bool = False,
) -> Path:
    program = normalize_led_text(text)
    return write_normalized_led_program(
        program,
        device_path=device_path,
        file_name=file_name,
        dry_run=dry_run,
    )


def write_normalized_led_program(
    program: str,
    *,
    device_path: Path | None = None,
    file_name: str = DEFAULT_FILE_NAME,
    dry_run: bool = False,
) -> Path:
    validate_led_text(program)
    target = resolve_target_path(device_path=device_path, file_name=file_name)

    if dry_run:
        return target

    holder = led_lease_holder(target.parent)
    if holder is not None:
        raise DeviceBusyError(
            f"{target.parent} is in use by {holder.get('owner') or 'another process'} "
            f"(pid {holder.get('pid')}); stop it to restore normal LED updates."
        )

    with device_write_lock(target.parent):
        target.parent.mkdir(parents=True, exist_ok=True)
        write_text_synced(target, program)
    return target


@contextmanager
def device_write_lock(root: Path):
    """Serialize this app's file/USB/read-channel writers across threads and processes."""
    import fcntl

    key = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()
    directory = Path.home() / ".cache" / "sidepulse" / "device-locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _WRITE_LOCK, (directory / key).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _device_key(root: Path) -> str:
    try:
        resolved = root.resolve()
    except OSError:
        resolved = root
    return hashlib.sha256(os.fsencode(resolved)).hexdigest()


def led_lease_path(root: Path) -> Path:
    return Path.home() / ".cache" / "sidepulse" / "device-leases" / f"{_device_key(root)}.json"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def led_lease_holder(root: Path) -> dict | None:
    """Return the live lease on a device held by *another* process, if any.

    Leases left behind by a process that no longer exists are ignored.
    """
    try:
        data = json.loads(led_lease_path(root).read_text(encoding="utf-8"))
        pid = int(data["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if pid == os.getpid() or not _pid_alive(pid):
        return None
    return data


@contextmanager
def hold_led_lease(root: Path, owner: str):
    """Make this process the only LED writer for a device while the context is open.

    Other writers (the background service, the status-bar app, `sidepulse write`)
    get DeviceBusyError and retry on their normal schedule, so they restore their
    display by themselves once the lease is released.
    """
    holder = led_lease_holder(root)
    if holder is not None:
        raise DeviceBusyError(
            f"{root} is already in use by {holder.get('owner') or 'another process'} (pid {holder.get('pid')})."
        )
    path = led_lease_path(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"pid": os.getpid(), "owner": owner, "root": str(root)}), encoding="utf-8")
    os.replace(temporary, path)
    try:
        yield
    finally:
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
                path.unlink()
        except (OSError, ValueError):
            pass


def write_text_synced(target: Path, text: str) -> None:
    """Write text and force it to the device before returning.

    Removable devices buffer writes, so the LED firmware may not see the
    program until the data reaches the disk. Syncing is best effort: the
    bytes are already handed to the OS once the file is closed, so a
    filesystem that refuses to sync must not fail the write.
    """

    is_new = not path_exists(target)
    try:
        handle = target.open("w", encoding="utf-8")
    except OSError as exc:
        if exc.errno not in (errno.EROFS, errno.EACCES, errno.EPERM):
            raise
        from .readonly_control import write_readonly_program

        write_readonly_program(target, text)
        return
    # Only a denied open may change transports. Never replay a partial write.
    with handle:
        handle.write(text)
        handle.flush()
        sync_fd(handle.fileno())

    if is_new:
        sync_directory(target.parent)


def sync_fd(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError:
        pass


def sync_directory(directory: Path) -> None:
    """Flush a new file's directory entry, where the platform supports it."""

    try:
        fd = os.open(directory, getattr(os, "O_DIRECTORY", os.O_RDONLY))
    except OSError:
        return
    try:
        sync_fd(fd)
    finally:
        os.close(fd)


def normalize_led_text(text: str) -> str:
    return decode_simple_escapes(text)


def decode_simple_escapes(text: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\\" or index + 1 >= len(text):
            output.append(char)
            index += 1
            continue

        next_char = text[index + 1]
        if next_char == "n":
            output.append("\n")
            index += 2
        elif next_char == "r":
            output.append("\r")
            index += 2
        elif next_char == "t":
            output.append("\t")
            index += 2
        elif next_char == "\\":
            output.append("\\")
            index += 2
        else:
            output.append(char)
            index += 1
    return "".join(output)


def validate_led_text(text: str) -> None:
    if not text:
        raise DeviceWriteError("LED program is empty.")

    byte_count = len(text.encode("utf-8"))
    if byte_count > MAX_LED_BYTES:
        raise DeviceWriteError(
            f"LED program is {byte_count} bytes; max is {MAX_LED_BYTES}."
        )

    line_count = len(text.splitlines()) or 1
    if line_count > MAX_LED_LINES:
        raise DeviceWriteError(
            f"LED program has {line_count} lines; max is {MAX_LED_LINES}."
        )


def resolve_target_path(
    *,
    device_path: Path | None = None,
    file_name: str = DEFAULT_FILE_NAME,
) -> Path:
    if device_path is not None:
        return target_from_device_path(device_path.expanduser(), file_name)

    candidates = discover_devices(file_name=file_name)
    if not candidates:
        raise DeviceWriteError(
            "No SidePulse Pro or SidePulse Dot device found. "
            "Mount the device, or pass --device /path/to/SidePulseDot."
        )
    if len(candidates) > 1:
        lines = "\n".join(f"  {candidate.root}" for candidate in candidates)
        raise DeviceWriteError(
            "Multiple possible devices found. Pass --device with one of:\n" + lines
        )
    return candidates[0].target


def target_from_device_path(path: Path, file_name: str) -> Path:
    if path.name.upper() in KNOWN_LED_FILE_NAMES:
        return path
    return path / file_name


def discover_devices(
    *,
    mount_root: Path | None = None,
    file_name: str = DEFAULT_FILE_NAME,
) -> list[DeviceCandidate]:
    candidates: list[DeviceCandidate] = []
    seen: set[Path] = set()
    roots = (mount_root,) if mount_root is not None else default_mount_roots()
    for root in roots:
        if not root.exists():
            continue
        for volume in sorted(iter_mounts(root), key=lambda path: path.name.lower()):
            if volume in seen:
                continue
            seen.add(volume)
            target = target_from_device_path(volume, file_name)
            name_matches = is_device_name(volume.name)
            file_exists = path_exists(target)
            if file_exists:
                candidates.append(
                    DeviceCandidate(volume, target, f"contains {target.name}")
                )
            elif name_matches:
                candidates.append(DeviceCandidate(volume, target, "name matches device"))
    return candidates


def default_mount_roots(
    *,
    platform: str | None = None,
    home: Path | None = None,
) -> tuple[Path, ...]:
    configured = os.environ.get("SIDEPULSE_MOUNT_ROOTS")
    if configured is not None:
        return tuple(
            Path(value).expanduser()
            for value in configured.split(os.pathsep)
            if value.strip()
        )

    current_platform = platform or sys.platform
    if current_platform == "darwin":
        return (MOUNT_ROOT,)
    if current_platform.startswith("linux"):
        username = (home or Path.home()).name
        return unique_paths(
            (
                Path("/media") / username,
                Path("/run/media") / username,
                Path("/media"),
                Path("/run/media"),
                Path("/mnt"),
            )
        )
    return (MOUNT_ROOT,)


def unique_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    for path in paths:
        if path not in result:
            result.append(path)
    return tuple(result)


def iter_mounts(mount_root: Path) -> Iterable[Path]:
    try:
        children = list(mount_root.iterdir())
    except OSError:
        return ()

    mounts: list[Path] = []
    for child in children:
        if child.name in {".timemachine", "Macintosh HD"}:
            continue
        try:
            if child.is_dir():
                mounts.append(child)
        except OSError:
            continue
    return mounts


def path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def is_device_name(name: str) -> bool:
    normalized = "".join(char for char in name.lower() if char.isalnum())
    return any(hint in normalized for hint in DEVICE_NAME_HINTS)
