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

import pytest

from sidepulse import program_status

_ISOLATION_ROOT = tempfile.TemporaryDirectory(prefix="sidepulse-tests-")
ISOLATED_MOUNT_ROOT = Path(_ISOLATION_ROOT.name) / "mounts"
ISOLATED_STATE_HOME = Path(_ISOLATION_ROOT.name) / "state"
ISOLATED_MOUNT_ROOT.mkdir()
ISOLATED_STATE_HOME.mkdir()

os.environ["SIDEPULSE_MOUNT_ROOTS"] = str(ISOLATED_MOUNT_ROOT)
os.environ["XDG_STATE_HOME"] = str(ISOLATED_STATE_HOME)


def pytest_sessionfinish(session, exitstatus):
    _ISOLATION_ROOT.cleanup()



@pytest.fixture(autouse=True)
def isolated_windows_profile(tmp_path, monkeypatch):
    if os.name == 'nt':
        # Windows resolves ~ from USERPROFILE, not HOME. Child processes must
        # never fall back to the signed-in account or its running daemon.
        for key, value in {'USERPROFILE': tmp_path, 'HOME': tmp_path,
                           'XDG_CONFIG_HOME': tmp_path / '.config',
                           'XDG_STATE_HOME': tmp_path / '.local/state',
                           'XDG_DATA_HOME': tmp_path / '.local/share'}.items():
            monkeypatch.setenv(key, str(value))


@pytest.fixture(autouse=True)
def isolated_publisher(tmp_path, monkeypatch):
    original = program_status.installation_publisher
    monkeypatch.setattr(program_status, 'installation_publisher',
                        lambda path=None: original(path or tmp_path / 'publisher-id'))



@pytest.fixture(autouse=True)
def no_real_startup_writes(request, monkeypatch):
    """Never read or write a real card's INIT.LED (the Startup animation).

    Service tests point at /Volumes/SidePulse and can load the developer's
    real settings; with a Startup pick saved there, the sync would write the
    mounted card. Tests of the sync itself opt out with the `startup_io` mark.
    """
    if request.node.get_closest_marker("startup_io"):
        return
    from sidepulse import service
    monkeypatch.setattr(service, "read_startup_program", lambda root, name: None)

    def refuse(root, name, program):
        raise AssertionError(f"test tried to write {root / name}")

    monkeypatch.setattr(service, "write_startup_program", refuse)


@pytest.fixture(autouse=True)
def isolated_devin_config(tmp_path, monkeypatch):
    """Never touch the developer's ~/.config/devin/config.json.

    Its path comes from Path.home(), which the state-dir isolation above does
    not cover, so a test running setup for every agent would write it.
    """
    from sidepulse import install, providers

    def isolated(home=None):
        return (home / ".config" / "devin" / "config.json") if home else tmp_path / "devin" / "config.json"

    monkeypatch.setattr(providers, "default_devin_config_path", isolated)
    monkeypatch.setattr(install, "default_devin_config_path", isolated)


def pytest_configure(config):
    config.addinivalue_line("markers", "startup_io: may read/write INIT.LED in a temp dir")

@pytest.fixture
def program_client():
    from contextlib import contextmanager
    import json
    import socket
    @contextmanager
    def connect(path):
        token = None
        if os.name == 'nt':
            endpoint = json.loads(path.read_text())
            client = socket.create_connection(('127.0.0.1', endpoint['port']), timeout=2)
            token = endpoint['token']
        else:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(2)
            client.connect(str(path))
        class AuthenticatedClient:
            def sendall(self, data):
                if token:
                    try:
                        data = json.dumps({'auth': token, 'report': json.loads(data)}).encode()
                    except ValueError:
                        pass  # Deliberately incomplete or malformed client.
                client.sendall(data)
            def __getattr__(self, name):
                return getattr(client, name)
        try:
            yield AuthenticatedClient()
        finally:
            client.close()
    return connect
