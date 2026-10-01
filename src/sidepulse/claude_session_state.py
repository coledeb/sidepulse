"""Read Claude's explicit local session state without reading conversations."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path


MAX_METADATA_BYTES = 32 * 1024
MAX_SESSION_DIRS = 16
MAX_SESSION_FILES = 128


def session_directories() -> tuple[Path, ...]:
    home = Path.home()
    roots = [home / ".claude", *sorted(home.glob(".claude-*"))]
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        roots.append(Path(configured).expanduser())
    directories: list[Path] = []
    for root in roots:
        directory = root / "sessions"
        if root.is_symlink() or directory.is_symlink() or not directory.is_dir():
            continue
        directory = directory.resolve()
        if directory not in directories:
            directories.append(directory)
    if len(directories) > MAX_SESSION_DIRS:
        return ()
    return tuple(directories)


def process_matches(pid: int, proc_start: str) -> bool:
    """Bind registry evidence to the same live process, including PID reuse."""
    if pid <= 0 or not proc_start.strip():
        return False
    try:
        os.kill(pid, 0)
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=0.5,
            # Claude records procStart in UTC, independently of the desktop's
            # local timezone or the LaunchAgent's inherited environment.
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.split() == proc_start.split()


def read_metadata(path: Path) -> tuple[dict, os.stat_result] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size > MAX_METADATA_BYTES
                or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
            ):
                return None
            raw = os.read(fd, MAX_METADATA_BYTES + 1)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        if before.st_mtime_ns != after.st_mtime_ns or before.st_size != after.st_size:
            return None
        data = json.loads(raw)
        return (data, after) if isinstance(data, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def confirmed_idle_at(
    session_id: str,
    *,
    after: datetime,
    now: datetime,
) -> datetime | None:
    """Require newer, unambiguous idle evidence from a live interactive session."""
    confirmed: list[datetime] = []
    try:
        paths: list[Path] = []
        for directory in session_directories():
            paths.extend(
                path for path in directory.iterdir()
                if re.fullmatch(r"[1-9][0-9]*\.json", path.name)
            )
            if len(paths) > MAX_SESSION_FILES:
                return None
        for path in paths:
            entry = read_metadata(path)
            if entry is None:
                return None
            data, read_stat = entry
            if data.get("sessionId") != session_id:
                continue
            pid = data.get("pid")
            timestamp = data.get("statusUpdatedAt")
            proc_start = data.get("procStart")
            if (
                type(pid) is not int
                or str(pid) != path.stem
                or not isinstance(proc_start, str)
                or data.get("kind") != "interactive"
                or data.get("status") != "idle"
                or type(timestamp) not in (int, float)
                or not math.isfinite(timestamp)
                or not process_matches(pid, proc_start)
            ):
                return None
            idle_at = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
            if not after < idle_at <= now:
                return None
            current = path.stat(follow_symlinks=False)
            if (
                current.st_ino != read_stat.st_ino
                or current.st_mtime_ns != read_stat.st_mtime_ns
                or current.st_size != read_stat.st_size
            ):
                return None
            confirmed.append(idle_at)
    except (OSError, ValueError, OverflowError):
        return None
    return max(confirmed) if confirmed else None
