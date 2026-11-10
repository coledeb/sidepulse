"""Shared write path for the local JSONL logs.

These logs record whole hook payloads, so they carry tool inputs: shell
commands, and the full contents of every file an agent writes. Two properties
keep that from becoming a liability, and both belong here rather than at each
call site:

* owner-only permissions, repaired in place for logs written by older versions;
* a size cap, so a long-lived install cannot grow an unbounded plaintext copy
  of everything the agents did.

Standard library only: the hook process imports this on every agent event and
must keep working with PyObjC absent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3
FILE_MODE = 0o600
DIR_MODE = 0o700


def secure_path_mode(path: Path, mode: int) -> None:
    """Tighten permissions in place, best effort."""
    try:
        if (path.stat().st_mode & 0o777) == mode:
            return
        path.chmod(mode)
    except OSError:
        pass


def backup_path(path: Path, index: int) -> Path:
    """Name the ``index``-th rotated generation of ``path``.

    Readers need this too: a record that rotation moved out of the live log is
    still retained, so a reader that wants the whole retention window has to
    know where rotation put it.
    """
    return path.with_name(f"{path.name}.{index}")


def rotate_log(
    path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> None:
    """Roll ``path`` over once it reaches ``max_bytes``, keeping N backups.

    Rotation runs before the append rather than after, so a record is never
    split across two files; the live log may exceed the cap by one record.

    ``backup_count=0`` rotates by discarding: the live log is removed and the
    next append starts an empty one. Use it for a log whose reader only ever
    opens the live file, so that the bytes kept on disk are the bytes the
    feature can actually show. ``max_bytes=0`` disables rotation entirely.

    Rotation is not atomic against itself. Two hook processes from parallel
    agent sessions can both see the log oversized and interleave the shuffle
    below, which strands a just-written record in a backup and can drop a
    generation earlier than ``backup_count`` says. A lock here would have to
    hold in a process that must exit 0 whatever happens, so the trade is
    deliberate: readers that care about the whole window read the backups too.
    """
    if max_bytes <= 0 or backup_count < 0:
        return

    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return

    if backup_count == 0:
        try:
            path.unlink()
        except OSError:
            pass
        return

    def backup(index: int) -> Path:
        return backup_path(path, index)

    try:
        backup(backup_count).unlink()
    except OSError:
        pass
    for index in range(backup_count - 1, 0, -1):
        try:
            backup(index).replace(backup(index + 1))
        except OSError:
            continue
    try:
        path.replace(backup(1))
    except OSError:
        return
    secure_path_mode(backup(1), FILE_MODE)


def append_jsonl_line(
    path: Path,
    record: dict[str, Any],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    secure_parent: bool = True,
) -> None:
    """Append one JSON record as a single physical line, privately and bounded.

    ``secure_parent`` tightens the containing directory to 0700 as well, and
    repairs it in place for installs from older versions. Pass ``False`` when the
    caller does not own the directory: the provider log path is user-configurable
    (``--claude-log``, ``--log-dir``), so its parent may be $HOME, and re-permissioning
    a directory the user chose is not this function's business.
    """
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if secure_parent:
        secure_path_mode(path.parent, DIR_MODE)
    rotate_log(path, max_bytes=max_bytes, backup_count=backup_count)

    payload = (
        json.dumps(record, separators=(",", ":"), ensure_ascii=False, default=str) + "\n"
    )
    # O_APPEND puts the file offset at EOF for each write(2), so two appenders
    # never overwrite each other. It does not make a record atomic: the loop
    # below can take several write(2) calls, and another process can append
    # between two of them. O_RDWR rather than O_WRONLY so the repair below can
    # pread the last byte. The mode argument applies only when this call creates
    # the file, hence the chmod.
    data = payload.encode("utf-8")
    handle = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, FILE_MODE)
    try:
        # Repair before appending, not after failing. A write that dies part-way
        # through -- ENOSPC, EDQUOT, EIO, a return of 0, or the process being
        # killed -- leaves a record with no trailing newline, and the next append
        # would share that physical line so `parse_log_line` drops both records.
        # Terminating the partial record at failure time cannot work for the
        # commonest cause: on a full volume the terminating write fails too.
        # Closing the line here does, because this append is the next write that
        # can succeed, and a record written after it lands on its own line. Two
        # appenders racing here write two newlines, which costs one blank line
        # and no record.
        _close_dangling_line(handle)
        # os.write is one write(2) and may return short, so keep writing until
        # the payload is out. This covers a short write followed by a successful
        # one; the record being written when the volume fills is still lost.
        written = 0
        while written < len(data):
            count = os.write(handle, data[written:])
            if count <= 0:
                break
            written += count
    finally:
        os.close(handle)
    secure_path_mode(path, FILE_MODE)


def _close_dangling_line(handle: int) -> None:
    """Terminate a truncated record left by an earlier append, if there is one.

    Silent on every error: this is hygiene on a path whose caller is a hook that
    must exit 0, and a log that cannot be read is not worth failing an agent
    session over.
    """
    try:
        size = os.fstat(handle).st_size
        if size <= 0 or os.pread(handle, 1, size - 1) == b"\n":
            return
        os.write(handle, b"\n")
    except OSError:
        pass
