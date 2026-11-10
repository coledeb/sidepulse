"""Keep the test suite away from the developer's real devices and state.

Without this, tests that reach the default device discovery (or spawn the real
status-bar app) write LED programs to whatever SidePulse is mounted under
/Volumes, wearing its flash, and hook tests append events to
~/.local/state/sidepulse. Subprocesses that copy os.environ inherit the same
isolation. Tests that need a mount root or state dir still set their own.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_ISOLATION_ROOT = tempfile.TemporaryDirectory(prefix="sidepulse-tests-")
ISOLATED_MOUNT_ROOT = Path(_ISOLATION_ROOT.name) / "mounts"
ISOLATED_STATE_HOME = Path(_ISOLATION_ROOT.name) / "state"
ISOLATED_MOUNT_ROOT.mkdir()
ISOLATED_STATE_HOME.mkdir()

os.environ["SIDEPULSE_MOUNT_ROOTS"] = str(ISOLATED_MOUNT_ROOT)
os.environ["XDG_STATE_HOME"] = str(ISOLATED_STATE_HOME)


def pytest_sessionfinish(session, exitstatus):
    _ISOLATION_ROOT.cleanup()
