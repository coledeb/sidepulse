"""Start/stop `sidepulse visualize` from the menu-bar app (no AppKit in here)."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from .device_writer import discover_devices, led_lease_holder

VISUALIZER_LEASE_OWNER = "sidepulse visualize"
APP_LIST_REFRESH_SECONDS = 10.0


def visualize_command(style: str, source: str, python: str | None = None, color: str | None = None) -> list[str]:
    command = [python or sys.executable, "-m", "sidepulse", "visualize", "--quiet", "--control-stdin", "--style", style]
    if color:
        command += ["--color", color]
    if source:
        command += ["--app", source]
    return command


def last_log_line(path: Path, limit: int = 160) -> str | None:
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]
    except OSError:
        return None
    lines = [line for line in lines if line and not line.startswith("Visualizing ")]
    if not lines:
        return None
    return lines[-1].removeprefix("sidepulse visualize: ")[:limit]


class VisualizerControl:
    """Owns at most one visualizer child process and caches the list of playing apps.

    A visualizer started from a terminal is detected through its LED lease, so the
    menu shows it as running and Stop can end it too.
    """

    def __init__(self, log_path: Path, *, python: str | None = None,
                 lease_holders: Callable[[], list[dict]] | None = None):
        self.log_path = log_path
        self.python = python
        self.process: subprocess.Popen | None = None
        self.last_error: str | None = None
        self.playing_apps: list[tuple[str, str]] = []  # (display name, bundle ID)
        self._lease_holders = lease_holders or self._discover_lease_holders
        self._apps_lock = threading.Lock()
        self._apps_refreshing = False
        self._apps_refreshed_at = 0.0

    # -- state ---------------------------------------------------------------------

    @staticmethod
    def _discover_lease_holders() -> list[dict]:
        holders = []
        for candidate in discover_devices():
            holder = led_lease_holder(candidate.root)
            if holder is not None:
                holders.append(holder)
        return holders

    def own_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def external_pids(self) -> list[int]:
        own = self.process.pid if self.own_running() else None
        pids = []
        for holder in self._lease_holders():
            try:
                pid = int(holder.get("pid"))
            except (TypeError, ValueError):
                continue
            if holder.get("owner") == VISUALIZER_LEASE_OWNER and pid != own and pid not in pids:
                pids.append(pid)
        return pids

    def running(self) -> bool:
        return self.own_running() or bool(self.external_pids())

    def poll(self) -> None:
        """Notice a child that exited by itself and keep its error for the menu."""
        if self.process is None or self.process.poll() is None:
            return
        code = self.process.returncode
        self.process = None
        if code not in (0, -signal.SIGTERM, -signal.SIGINT):
            self.last_error = last_log_line(self.log_path) or f"exited with status {code}"

    # -- control -------------------------------------------------------------------

    def start(self, style: str, source: str, color: str | None = None) -> None:
        self.stop()
        self.last_error = None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("w", encoding="utf-8") as log:
            self.process = subprocess.Popen(
                visualize_command(style, source, self.python, color),
                stdin=subprocess.PIPE,  # live control; closing it stops the visualizer
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def set_color(self, color: str) -> bool:
        """Change the running visualizer's color without restarting it."""
        if not self.own_running() or self.process.stdin is None:
            return False
        try:
            self.process.stdin.write(f"color {color}\n")
            self.process.stdin.flush()
        except (OSError, ValueError):
            return False
        return True

    def stop(self, *, own_only: bool = False) -> None:
        if self.own_running():
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.process = None
        if own_only:
            return
        for pid in self.external_pids():
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.monotonic() + 3
        while self.external_pids() and time.monotonic() < deadline:
            time.sleep(0.05)

    # -- playing apps --------------------------------------------------------------

    def refresh_apps_async(self, on_done: Callable[[], None] | None = None, *, force: bool = False) -> None:
        with self._apps_lock:
            if self._apps_refreshing:
                return
            if not force and time.monotonic() - self._apps_refreshed_at < APP_LIST_REFRESH_SECONDS:
                return
            self._apps_refreshing = True
        threading.Thread(target=self._refresh_apps, args=(on_done,), daemon=True).start()

    def _refresh_apps(self, on_done: Callable[[], None] | None) -> None:
        try:
            from .visualize import ensure_helper, list_audio_apps

            apps = list_audio_apps(ensure_helper(quiet=True))
            playing: dict[str, str] = {}
            for is_playing, _pid, bundle_id, name in apps:
                if is_playing and bundle_id and bundle_id not in playing:
                    playing[bundle_id] = name or bundle_id
            changed = sorted((name, bundle) for bundle, name in playing.items()) != self.playing_apps
            self.playing_apps = sorted((name, bundle) for bundle, name in playing.items())
        except Exception:
            changed = False
        finally:
            with self._apps_lock:
                self._apps_refreshing = False
                self._apps_refreshed_at = time.monotonic()
        if changed and on_done is not None:
            on_done()

    def source_label(self, source: str) -> str:
        if not source:
            return "All System Audio"
        for name, bundle in self.playing_apps:
            if bundle == source:
                return name
        return source
