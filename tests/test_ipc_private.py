from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from sidepulse.ipc import HookEventServer, directory_is_private, ensure_private_dir, send_hook_event


class PrivateSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_server_makes_its_directory_and_socket_owner_only(self) -> None:
        state = self.root / "state"
        state.mkdir(mode=0o755)
        state.chmod(0o755)
        received: list[tuple[str, dict]] = []
        server = HookEventServer(lambda provider, line: received.append((provider, line)), socket_path=state / "events.sock")
        path = server.start()
        self.addCleanup(server.stop)

        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertTrue(send_hook_event("claude", {"hook_event_name": "Stop"}, socket_path=path))

    def test_sender_skips_a_socket_in_a_shared_directory(self) -> None:
        shared = self.root / "shared"
        shared.mkdir()
        shared.chmod(0o777)
        self.assertFalse(directory_is_private(shared))
        self.assertFalse(send_hook_event("claude", {"hook_event_name": "Stop"}, socket_path=shared / "x.sock"))

    def test_symlinked_directory_is_refused(self) -> None:
        real = self.root / "real"
        real.mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(real)
        with self.assertRaises(OSError):
            ensure_private_dir(link)
        self.assertFalse(directory_is_private(link))


if __name__ == "__main__":
    unittest.main()
