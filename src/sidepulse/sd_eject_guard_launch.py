from __future__ import annotations

import hashlib
import os
import plistlib
import re
import subprocess
import time
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, Callable, Literal

from .providers import default_state_dir

SD_EJECT_GUARD_LABEL = "io.sidepulse.sdejectguard"
SD_EJECT_GUARD_FILENAME = f"{SD_EJECT_GUARD_LABEL}.plist"
SD_EJECT_GUARD_DISPLAY_NAME = "SidePulse Pro Eject Prevention"
SD_EJECT_GUARD_BINARY_NAME = SD_EJECT_GUARD_DISPLAY_NAME
SD_EJECT_GUARD_LEGACY_BINARY_NAMES = ("sd_eject_guard",)
SD_EJECT_GUARD_SCOPES = ("auto", "system", "user")
SD_EJECT_GUARD_LOG_MAX_BYTES = 10 * 1024 * 1024

SdEjectGuardScope = Literal["auto", "system", "user"]
ResolvedSdEjectGuardScope = Literal["system", "user"]


class SdEjectGuardInstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class SdEjectGuardPaths:
    scope: ResolvedSdEjectGuardScope
    plist_path: Path
    binary_path: Path
    stdout_path: Path
    stderr_path: Path


@dataclass(frozen=True)
class SdEjectGuardResult:
    label: str
    scope: ResolvedSdEjectGuardScope
    plist_path: Path
    binary_path: Path
    changed: bool
    compiled: bool = False
    started: bool = False
    dry_run: bool = False
    cleanup_removed: Path | None = None
    cleanup_skipped: str | None = None
    legacy_removed: tuple[Path, ...] = ()


@dataclass(frozen=True)
class SdEjectGuardStopResult:
    label: str
    scope: ResolvedSdEjectGuardScope
    plist_path: Path
    stopped: bool
    skipped: str | None = None


@dataclass(frozen=True)
class SdEjectGuardUninstallResult:
    label: str
    scope: ResolvedSdEjectGuardScope
    plist_path: Path
    binary_paths: tuple[Path, ...]
    removed_paths: tuple[Path, ...]
    stopped: bool = False
    skipped: str | None = None
    dry_run: bool = False


def install_sd_eject_guard(
    *,
    scope: SdEjectGuardScope = "auto",
    dry_run: bool = False,
    start: bool = True,
    source_path: Path | None = None,
    user_paths: SdEjectGuardPaths | None = None,
    system_paths: SdEjectGuardPaths | None = None,
) -> SdEjectGuardResult:
    resolved_scope = resolve_sd_eject_guard_scope(scope)
    if scope == "system" and not dry_run and not can_install_system_scope():
        raise SdEjectGuardInstallError(
            "system SD eject guard install requires root privileges; "
            "rerun with sudo or use --sd-eject-guard-scope user"
        )

    paths = paths_for_scope(resolved_scope, user_paths=user_paths, system_paths=system_paths)
    plist = build_sd_eject_guard_plist(paths)
    data = plistlib.dumps(plist, sort_keys=False)
    existing = paths.plist_path.read_bytes() if paths.plist_path.exists() else None
    plist_changed = existing != data
    compiled = False
    cleanup_removed = None
    cleanup_skipped = None
    legacy_removed: tuple[Path, ...] = ()

    source = source_path or packaged_sd_eject_guard_source()
    source_context = as_file(source) if not isinstance(source, Path) else _path_context(source)
    with source_context as resolved_source:
        needs_compile = binary_needs_compile(paths.binary_path, resolved_source)
        if not dry_run:
            paths.binary_path.parent.mkdir(parents=True, exist_ok=True)
            paths.plist_path.parent.mkdir(parents=True, exist_ok=True)
            paths.stdout_path.parent.mkdir(parents=True, exist_ok=True)
            paths.stderr_path.parent.mkdir(parents=True, exist_ok=True)
            truncate_log_if_large(paths.stdout_path)

            if needs_compile:
                compile_sd_eject_guard(resolved_source, paths.binary_path)
                compiled = True

            paths.binary_path.chmod(0o755)
            if resolved_scope == "system":
                apply_system_ownership(paths.binary_path)
                apply_system_ownership(source_stamp_path(paths.binary_path))

            if plist_changed:
                paths.plist_path.write_bytes(data)
                paths.plist_path.chmod(0o644)
                if resolved_scope == "system":
                    apply_system_ownership(paths.plist_path)

            cleanup = cleanup_other_scope(
                resolved_scope,
                user_paths=user_paths,
                system_paths=system_paths,
            )
            cleanup_removed = cleanup.removed
            cleanup_skipped = cleanup.skipped

            if start:
                restart_sd_eject_guard(paths.plist_path, resolved_scope)
            legacy_removed = cleanup_legacy_binaries(paths)

    return SdEjectGuardResult(
        label=SD_EJECT_GUARD_LABEL,
        scope=resolved_scope,
        plist_path=paths.plist_path,
        binary_path=paths.binary_path,
        changed=(
            plist_changed
            or compiled
            or cleanup_removed is not None
            or bool(legacy_removed)
            or (dry_run and needs_compile)
        ),
        compiled=compiled,
        started=start and not dry_run,
        dry_run=dry_run,
        cleanup_removed=cleanup_removed,
        cleanup_skipped=cleanup_skipped,
        legacy_removed=legacy_removed,
    )


def ensure_sd_eject_guard_binary(
    *,
    scope: SdEjectGuardScope = "auto",
    source_path: Path | None = None,
    user_paths: SdEjectGuardPaths | None = None,
    system_paths: SdEjectGuardPaths | None = None,
) -> SdEjectGuardPaths:
    resolved_scope = resolve_sd_eject_guard_scope(scope)
    if resolved_scope == "system" and not can_install_system_scope():
        raise SdEjectGuardInstallError(
            "system SD eject guard run requires root privileges; "
            "rerun with sudo or use --scope user"
        )

    paths = paths_for_scope(resolved_scope, user_paths=user_paths, system_paths=system_paths)
    source = source_path or packaged_sd_eject_guard_source()
    source_context = as_file(source) if not isinstance(source, Path) else _path_context(source)
    with source_context as resolved_source:
        paths.binary_path.parent.mkdir(parents=True, exist_ok=True)
        if binary_needs_compile(paths.binary_path, resolved_source):
            compile_sd_eject_guard(resolved_source, paths.binary_path)
        paths.binary_path.chmod(0o755)
        if resolved_scope == "system":
            apply_system_ownership(paths.binary_path)
            apply_system_ownership(source_stamp_path(paths.binary_path))
    return paths


def run_sd_eject_guard_interactive(
    *,
    scope: SdEjectGuardScope = "auto",
    source_path: Path | None = None,
    user_paths: SdEjectGuardPaths | None = None,
    system_paths: SdEjectGuardPaths | None = None,
) -> int:
    paths = ensure_sd_eject_guard_binary(
        scope=scope,
        source_path=source_path,
        user_paths=user_paths,
        system_paths=system_paths,
    )
    try:
        return subprocess.run([str(paths.binary_path)], check=False).returncode
    except KeyboardInterrupt:
        return 130


def stop_sd_eject_guard(
    *,
    scope: SdEjectGuardScope = "auto",
    user_paths: SdEjectGuardPaths | None = None,
    system_paths: SdEjectGuardPaths | None = None,
    dry_run: bool = False,
) -> tuple[SdEjectGuardStopResult, ...]:
    scopes: tuple[ResolvedSdEjectGuardScope, ...]
    if scope == "auto":
        scopes = ("user", "system")
    elif scope in ("user", "system"):
        scopes = (scope,)
    else:
        raise SdEjectGuardInstallError(f"unknown SD eject guard scope: {scope}")

    results: list[SdEjectGuardStopResult] = []
    for resolved_scope in scopes:
        paths = paths_for_scope(resolved_scope, user_paths=user_paths, system_paths=system_paths)
        if not paths.plist_path.exists():
            results.append(
                SdEjectGuardStopResult(
                    label=SD_EJECT_GUARD_LABEL,
                    scope=resolved_scope,
                    plist_path=paths.plist_path,
                    stopped=False,
                )
            )
            continue

        if resolved_scope == "system" and not can_install_system_scope():
            message = f"missing permissions to stop {paths.plist_path}"
            if scope == "system":
                raise SdEjectGuardInstallError(message)
            results.append(
                SdEjectGuardStopResult(
                    label=SD_EJECT_GUARD_LABEL,
                    scope=resolved_scope,
                    plist_path=paths.plist_path,
                    stopped=False,
                    skipped=message,
                )
            )
            continue

        if not dry_run:
            bootout_sd_eject_guard(paths.plist_path, resolved_scope)
        results.append(
            SdEjectGuardStopResult(
                label=SD_EJECT_GUARD_LABEL,
                scope=resolved_scope,
                plist_path=paths.plist_path,
                stopped=True,
            )
        )
    return tuple(results)


def uninstall_sd_eject_guard(
    *,
    scope: SdEjectGuardScope = "auto",
    user_paths: SdEjectGuardPaths | None = None,
    system_paths: SdEjectGuardPaths | None = None,
    dry_run: bool = False,
) -> tuple[SdEjectGuardUninstallResult, ...]:
    scopes: tuple[ResolvedSdEjectGuardScope, ...]
    if scope == "auto":
        scopes = ("user", "system")
    elif scope in ("user", "system"):
        scopes = (scope,)
    else:
        raise SdEjectGuardInstallError(f"unknown SD eject guard scope: {scope}")

    results: list[SdEjectGuardUninstallResult] = []
    for resolved_scope in scopes:
        paths = paths_for_scope(resolved_scope, user_paths=user_paths, system_paths=system_paths)
        binary_paths = sd_eject_guard_binary_paths(paths)
        artifacts = [paths.plist_path, *binary_paths, source_stamp_path(paths.binary_path)]
        existing = [path for path in artifacts if path.exists()]
        if not existing:
            results.append(
                SdEjectGuardUninstallResult(
                    label=SD_EJECT_GUARD_LABEL,
                    scope=resolved_scope,
                    plist_path=paths.plist_path,
                    binary_paths=binary_paths,
                    removed_paths=(),
                    dry_run=dry_run,
                )
            )
            continue

        if resolved_scope == "system" and not can_install_system_scope():
            message = f"missing permissions to uninstall {paths.plist_path}"
            if scope == "system":
                raise SdEjectGuardInstallError(message)
            results.append(
                SdEjectGuardUninstallResult(
                    label=SD_EJECT_GUARD_LABEL,
                    scope=resolved_scope,
                    plist_path=paths.plist_path,
                    binary_paths=binary_paths,
                    removed_paths=(),
                    skipped=message,
                    dry_run=dry_run,
                )
            )
            continue

        removed: list[Path] = []
        if not dry_run:
            if paths.plist_path.exists():
                bootout_sd_eject_guard(paths.plist_path, resolved_scope)
            for path in artifacts:
                if path.exists():
                    path.unlink()
                    removed.append(path)
            remove_empty_parents(paths.binary_path.parent)
        else:
            removed = existing

        results.append(
            SdEjectGuardUninstallResult(
                label=SD_EJECT_GUARD_LABEL,
                scope=resolved_scope,
                plist_path=paths.plist_path,
                binary_paths=binary_paths,
                removed_paths=tuple(removed),
                stopped=paths.plist_path in existing,
                dry_run=dry_run,
            )
        )
    return tuple(results)


def sd_eject_guard_problems(
    scope: ResolvedSdEjectGuardScope = "system",
    *,
    source_path: Path | None = None,
    user_paths: SdEjectGuardPaths | None = None,
    system_paths: SdEjectGuardPaths | None = None,
    running: Callable[[ResolvedSdEjectGuardScope], bool] | None = None,
) -> list[str]:
    """What an install would change or start; empty when the guard is current and running.

    Needs no root: the daemon's plist, binary and stamp are world-readable and
    launchctl reports system jobs to any user. Setup uses it to skip sudo.
    """
    paths = paths_for_scope(scope, user_paths=user_paths, system_paths=system_paths)
    problems = []
    expected = plistlib.dumps(build_sd_eject_guard_plist(paths), sort_keys=False)
    try:
        if paths.plist_path.read_bytes() != expected:
            problems.append(f"{paths.plist_path} is out of date")
    except FileNotFoundError:
        problems.append(f"{paths.plist_path} is missing")
    except OSError as exc:
        problems.append(f"{paths.plist_path} is unreadable: {exc}")
    source = source_path or packaged_sd_eject_guard_source()
    source_context = as_file(source) if not isinstance(source, Path) else _path_context(source)
    with source_context as resolved_source:
        if binary_needs_compile(paths.binary_path, resolved_source):
            problems.append(f"{paths.binary_path} is missing or out of date")
    problems.extend(f"{legacy} is left over" for legacy in legacy_binary_paths(paths) if legacy.exists())
    if not problems and not (running or sd_eject_guard_running)(scope):
        problems.append(f"{SD_EJECT_GUARD_LABEL} is not running")
    return problems


def sd_eject_guard_running(scope: ResolvedSdEjectGuardScope) -> bool:
    result = subprocess.run(
        ["launchctl", "print", f"{launch_domain(scope)}/{SD_EJECT_GUARD_LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    # The job's own state is the first line indented by one tab; nested
    # endpoint states are indented further.
    match = re.search(r"^\tstate = (\S+)", result.stdout, re.MULTILINE)
    return result.returncode == 0 and match is not None and match.group(1) == "running"


def wait_for_sd_eject_guard_running(
    scope: ResolvedSdEjectGuardScope,
    timeout: float,
    *,
    running: Callable[[ResolvedSdEjectGuardScope], bool] = sd_eject_guard_running,
    interval: float = 0.5,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if running(scope):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def resolve_sd_eject_guard_scope(scope: SdEjectGuardScope) -> ResolvedSdEjectGuardScope:
    if scope == "auto":
        return "system" if can_install_system_scope() else "user"
    if scope in ("system", "user"):
        return scope
    raise SdEjectGuardInstallError(f"unknown SD eject guard scope: {scope}")


def can_install_system_scope() -> bool:
    return os.geteuid() == 0


def paths_for_scope(
    scope: ResolvedSdEjectGuardScope,
    *,
    user_paths: SdEjectGuardPaths | None = None,
    system_paths: SdEjectGuardPaths | None = None,
) -> SdEjectGuardPaths:
    if scope == "system":
        return system_paths or system_sd_eject_guard_paths()
    return user_paths or user_sd_eject_guard_paths()


def sd_eject_guard_installed(
    scope: SdEjectGuardScope = "auto",
    *,
    user_paths: SdEjectGuardPaths | None = None,
    system_paths: SdEjectGuardPaths | None = None,
) -> bool:
    if scope == "auto":
        return any(
            paths_for_scope(resolved_scope, user_paths=user_paths, system_paths=system_paths)
            .plist_path
            .exists()
            for resolved_scope in ("user", "system")
        )
    if scope in ("user", "system"):
        return paths_for_scope(
            scope,
            user_paths=user_paths,
            system_paths=system_paths,
        ).plist_path.exists()
    raise SdEjectGuardInstallError(f"unknown SD eject guard scope: {scope}")


def user_sd_eject_guard_paths(home: Path | None = None) -> SdEjectGuardPaths:
    base = home or Path.home()
    data_dir = default_user_data_dir(home) / "sidepulse" / "sd-eject-guard"
    state_dir = default_state_dir(home)
    return SdEjectGuardPaths(
        scope="user",
        plist_path=base / "Library" / "LaunchAgents" / SD_EJECT_GUARD_FILENAME,
        binary_path=data_dir / SD_EJECT_GUARD_BINARY_NAME,
        stdout_path=state_dir / "sd-eject-guard.out.log",
        stderr_path=state_dir / "sd-eject-guard.err.log",
    )


def system_sd_eject_guard_paths() -> SdEjectGuardPaths:
    return SdEjectGuardPaths(
        scope="system",
        plist_path=Path("/Library/LaunchDaemons") / SD_EJECT_GUARD_FILENAME,
        binary_path=Path("/Library/Application Support/SidePulse/sd-eject-guard")
        / SD_EJECT_GUARD_BINARY_NAME,
        stdout_path=Path("/Library/Logs/SidePulse/sd-eject-guard.out.log"),
        stderr_path=Path("/Library/Logs/SidePulse/sd-eject-guard.err.log"),
    )


def default_user_data_dir(home: Path | None = None) -> Path:
    if home is None:
        xdg_data_home = os.environ.get("XDG_DATA_HOME")
        if xdg_data_home:
            return Path(xdg_data_home).expanduser()
    base = home or Path.home()
    return base / ".local" / "share"


def build_sd_eject_guard_plist(paths: SdEjectGuardPaths) -> dict[str, Any]:
    return {
        "Label": SD_EJECT_GUARD_LABEL,
        "ProgramArguments": [str(paths.binary_path)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(paths.stdout_path),
        "StandardErrorPath": str(paths.stderr_path),
        "WorkingDirectory": "/",
    }


def log_paths_for_scope(scope: ResolvedSdEjectGuardScope) -> tuple[Path, Path]:
    paths = paths_for_scope(scope)
    return paths.stdout_path, paths.stderr_path


def log_paths_for_requested_scope(scope: SdEjectGuardScope = "auto") -> tuple[Path, ...]:
    if scope == "auto":
        scopes: tuple[ResolvedSdEjectGuardScope, ...] = ("user", "system")
    elif scope in ("user", "system"):
        scopes = (scope,)
    else:
        raise SdEjectGuardInstallError(f"unknown SD eject guard scope: {scope}")

    paths: list[Path] = []
    seen: set[Path] = set()
    for resolved_scope in scopes:
        for path in log_paths_for_scope(resolved_scope):
            if path in seen:
                continue
            seen.add(path)
            paths.append(path)
    return tuple(paths)


def read_log_tail(path: Path, line_count: int = 80) -> str:
    if line_count <= 0:
        return ""
    try:
        truncate_log_if_large(path)
        lines = read_recent_lines(path, line_count)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"(unreadable: {exc})"
    return "\n".join(lines)


def truncate_log_if_large(
    path: Path,
    max_bytes: int = SD_EJECT_GUARD_LOG_MAX_BYTES,
) -> bool:
    try:
        if path.stat().st_size <= max_bytes:
            return False
        with path.open("r+b") as handle:
            handle.truncate(0)
        return True
    except (FileNotFoundError, OSError):
        return False


def read_recent_lines(path: Path, max_lines: int) -> list[str]:
    chunk_size = 8192
    chunks: list[bytes] = []
    newline_count = 0
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        while position > 0 and newline_count <= max_lines:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")

    data = b"".join(reversed(chunks))
    return data.decode("utf-8", errors="replace").splitlines()[-max_lines:]


def packaged_sd_eject_guard_source():
    return files("sidepulse.resources").joinpath("sd_eject_guard.c")


def source_stamp_path(binary_path: Path) -> Path:
    return binary_path.with_name(f"{binary_path.name}.source-sha256")


def source_digest(source_path: Path) -> str:
    return hashlib.sha256(source_path.read_bytes()).hexdigest()


def binary_needs_compile(binary_path: Path, source_path: Path) -> bool:
    # Compare the source's content, not its mtime: every SidePulse install
    # rewrites the packaged source with a fresh mtime, which would rebuild the
    # guard (and need sudo for the system daemon) on every update.
    if not binary_path.exists():
        return True
    try:
        return source_stamp_path(binary_path).read_text().strip() != source_digest(source_path)
    except OSError:
        return True


def compile_sd_eject_guard(source_path: Path, binary_path: Path) -> None:
    tmp_binary = binary_path.with_name(f"{binary_path.name}.tmp")
    subprocess.run(
        [
            "clang",
            "-O2",
            "-o",
            str(tmp_binary),
            str(source_path),
            "-framework",
            "DiskArbitration",
            "-framework",
            "CoreFoundation",
        ],
        check=True,
    )
    tmp_binary.chmod(0o755)
    tmp_binary.replace(binary_path)
    source_stamp_path(binary_path).write_text(source_digest(source_path) + "\n")


def restart_sd_eject_guard(
    plist_path: Path,
    scope: ResolvedSdEjectGuardScope,
) -> None:
    domain = launch_domain(scope)
    bootout_sd_eject_guard(plist_path, scope)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"{domain}/{SD_EJECT_GUARD_LABEL}"],
        check=False,
    )


def bootout_sd_eject_guard(
    plist_path: Path,
    scope: ResolvedSdEjectGuardScope,
) -> None:
    subprocess.run(
        ["launchctl", "bootout", launch_domain(scope), str(plist_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def launch_domain(scope: ResolvedSdEjectGuardScope) -> str:
    if scope == "system":
        return "system"
    return f"gui/{os.getuid()}"


@dataclass(frozen=True)
class CleanupResult:
    removed: Path | None = None
    skipped: str | None = None


def cleanup_other_scope(
    installed_scope: ResolvedSdEjectGuardScope,
    *,
    user_paths: SdEjectGuardPaths | None = None,
    system_paths: SdEjectGuardPaths | None = None,
) -> CleanupResult:
    other_scope: ResolvedSdEjectGuardScope = "user" if installed_scope == "system" else "system"
    paths = paths_for_scope(other_scope, user_paths=user_paths, system_paths=system_paths)
    if not paths.plist_path.exists():
        return CleanupResult()
    if other_scope == "system" and not can_install_system_scope():
        return CleanupResult(
            skipped=f"missing permissions to remove {paths.plist_path}",
        )

    bootout_sd_eject_guard(paths.plist_path, other_scope)
    paths.plist_path.unlink()
    return CleanupResult(removed=paths.plist_path)


def sd_eject_guard_binary_paths(paths: SdEjectGuardPaths) -> tuple[Path, ...]:
    return (paths.binary_path, *legacy_binary_paths(paths))


def legacy_binary_paths(paths: SdEjectGuardPaths) -> tuple[Path, ...]:
    legacy_paths = []
    for name in SD_EJECT_GUARD_LEGACY_BINARY_NAMES:
        legacy = paths.binary_path.with_name(name)
        if legacy != paths.binary_path:
            legacy_paths.append(legacy)
    return tuple(legacy_paths)


def cleanup_legacy_binaries(paths: SdEjectGuardPaths) -> tuple[Path, ...]:
    removed: list[Path] = []
    for legacy in legacy_binary_paths(paths):
        try:
            legacy.unlink()
        except FileNotFoundError:
            continue
        removed.append(legacy)
    return tuple(removed)


def remove_empty_parents(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        return


def apply_system_ownership(path: Path) -> None:
    os.chown(path, 0, 0)


class _path_context:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *_exc: object) -> None:
        return None
