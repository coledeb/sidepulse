"""Remount a SidePulse drive so the computer rereads its filesystem.

A SidePulse Pro rewrites its own FAT12 drive (for example, after applying a
firmware update), but macOS keeps the drive's file table cached while it stays
mounted, so the change only shows after the card is reinserted. Unmounting and
mounting again in software has the same effect without touching the card.

This unmounts rather than ejects: an eject of the built-in SD reader removes
the card until it is reinserted, and SidePulse Pro Eject Prevention vetoes it.
"""
from __future__ import annotations

import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path

from .device_writer import device_write_lock

UNMOUNT_ATTEMPTS = 3


class RemountError(RuntimeError):
    pass


def remount_drive(root: Path, *, timeout: float = 15) -> Path:
    """Unmount and remount the SidePulse drive at ``root``; return its new mount point."""
    root = root.expanduser()
    if not (root / "STATUS.TXT").is_file():
        raise RemountError(f"{root} is not a mounted SidePulse drive.")
    if sys.platform == "darwin":
        return _remount_macos(root, timeout)
    if sys.platform.startswith("linux"):
        return _remount_linux(root, timeout)
    raise RemountError("Remounting the drive requires macOS or Linux.")


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=30)


def _error_text(result: subprocess.CompletedProcess) -> str:
    return (result.stderr or result.stdout).strip() or f"exit status {result.returncode}"


def _diskutil_info(target: str) -> dict:
    result = subprocess.run(
        ["/usr/sbin/diskutil", "info", "-plist", target], capture_output=True, timeout=10,
    )
    if result.returncode != 0:
        return {}
    return plistlib.loads(result.stdout)


def _unmount(command: list[str]) -> None:
    # A brief open file (Spotlight, another reader) can make the first try fail.
    for attempt in range(UNMOUNT_ATTEMPTS):
        result = _run(command)
        if result.returncode == 0:
            return
        if attempt + 1 < UNMOUNT_ATTEMPTS:
            time.sleep(1)
    raise RemountError(f"Could not unmount the drive: {_error_text(result)}")


def _remount_macos(root: Path, timeout: float) -> Path:
    info = _diskutil_info(str(root))
    mount = info.get("MountPoint")
    disk = info.get("DeviceIdentifier")
    if not mount or not disk or Path(mount).resolve() != root.resolve():
        raise RemountError(f"{root} is not a mounted volume.")
    with device_write_lock(root):
        _unmount(["/usr/sbin/diskutil", "unmount", str(root)])
        deadline = time.monotonic() + timeout
        error = ""
        while True:
            result = _run(["/usr/sbin/diskutil", "mount", disk])
            if result.returncode == 0:
                mount = _diskutil_info(disk).get("MountPoint")
                if mount:
                    return Path(mount)
            else:
                error = _error_text(result)
            if time.monotonic() >= deadline:
                raise RemountError(
                    f"The drive was unmounted but did not mount again ({error or 'no mount point'}). "
                    f"Run `diskutil mount {disk}` or reconnect the device."
                )
            time.sleep(1)


def _remount_linux(root: Path, timeout: float) -> Path:
    result = _run(["findmnt", "-n", "-o", "SOURCE", "--target", str(root)])
    source = result.stdout.strip()
    if result.returncode != 0 or not source.startswith("/dev/"):
        raise RemountError(f"{root} is not a mounted volume.")
    with device_write_lock(root):
        _unmount(["udisksctl", "unmount", "-b", source])
        deadline = time.monotonic() + timeout
        while True:
            result = _run(["udisksctl", "mount", "-b", source])
            match = re.search(r" at (.+?)\.?$", result.stdout.strip())
            if result.returncode == 0 and match:
                return Path(match.group(1))
            if time.monotonic() >= deadline:
                raise RemountError(
                    f"The drive was unmounted but did not mount again ({_error_text(result)}). "
                    f"Run `udisksctl mount -b {source}` or reconnect the device."
                )
            time.sleep(1)
