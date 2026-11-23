from __future__ import annotations

import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sidepulse import remount


def completed(command, returncode=0, stdout='', stderr=''):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class MacRemountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'SidePulse'
        self.root.mkdir()
        (self.root / 'STATUS.TXT').write_text('release_version 1.1.17\n')
        self.commands = []
        self.mounted = True
        self.unmount_failures = 0
        self.mount_failures = 0
        patch('sys.platform', 'darwin').start()
        patch('time.sleep').start()
        patch('sidepulse.remount.device_write_lock').start()
        self.addCleanup(patch.stopall)

    def fake_run(self, command, **kwargs):
        self.commands.append(command[1:3])
        verb, target = command[1], command[-1]
        if verb == 'info':
            info = {'DeviceIdentifier': 'disk6'}
            if self.mounted:
                info['MountPoint'] = str(self.root)
            return completed(command, stdout=plistlib.dumps(info))
        if verb == 'unmount':
            if self.unmount_failures:
                self.unmount_failures -= 1
                return completed(command, 1, stderr='Volume busy')
            self.mounted = False
            return completed(command)
        if verb == 'mount':
            self.assertEqual(target, 'disk6')
            if self.mount_failures:
                self.mount_failures -= 1
                return completed(command, 1, stderr='not ready')
            self.mounted = True
            return completed(command)
        raise AssertionError(command)

    def run_remount(self, **kwargs):
        with patch('subprocess.run', side_effect=self.fake_run):
            return remount.remount_drive(self.root, **kwargs)

    def test_unmounts_then_mounts_the_same_disk_without_ejecting(self):
        self.assertEqual(self.run_remount(), self.root)
        verbs = [command[0] for command in self.commands]
        self.assertEqual(verbs, ['info', 'unmount', 'mount', 'info'])
        self.assertNotIn('eject', verbs)

    def test_retries_a_busy_unmount_and_a_slow_mount(self):
        self.unmount_failures = 2
        self.mount_failures = 2
        self.assertEqual(self.run_remount(), self.root)
        verbs = [command[0] for command in self.commands]
        self.assertEqual(verbs.count('unmount'), 3)
        self.assertEqual(verbs.count('mount'), 3)

    def test_busy_drive_stays_mounted_with_an_error(self):
        self.unmount_failures = remount.UNMOUNT_ATTEMPTS
        with self.assertRaisesRegex(remount.RemountError, 'Volume busy'):
            self.run_remount()
        self.assertTrue(self.mounted)

    def test_failed_mount_names_the_recovery_command(self):
        self.mount_failures = 100
        with self.assertRaisesRegex(remount.RemountError, 'diskutil mount disk6'):
            self.run_remount(timeout=0)

    def test_rejects_drives_without_status_file(self):
        (self.root / 'STATUS.TXT').unlink()
        with self.assertRaisesRegex(remount.RemountError, 'not a mounted SidePulse drive'):
            self.run_remount()
        self.assertEqual(self.commands, [])
