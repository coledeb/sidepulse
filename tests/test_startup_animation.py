"""The Startup animation is written to the card's INIT.LED only when it changes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from sidepulse.device_writer import DeviceCandidate
from sidepulse.settings import (
    AGENT_ANIMATION_CYAN_STARTUP,
    AGENT_ANIMATION_IMMEDIATE_OFF,
    AGENT_ANIMATION_STARTUP_FILL,
    ANIMATION_STATE_STARTUP,
    AgentMonitorSettings,
    builtin_agent_animation_profiles,
)

FACTORY_INIT = (
    Path(__file__).resolve().parents[1]
    / "src/sidepulse/resources/animations/startup-fill-8.LED"
).read_text()


@pytest.mark.startup_io
class StartupAnimationTests(unittest.TestCase):
    def setUp(self) -> None:
        from sidepulse.service import SidePulseService

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "INIT.LED").write_text(FACTORY_INIT)
        self.service = SidePulseService()
        self.service.runtime.devices = [
            DeviceCandidate(self.root, self.root / "LEDS.LED", "mounted")
        ]
        self.writes: list[str] = []
        real_write = __import__(
            "sidepulse.device_writer", fromlist=["write_startup_program"]
        ).write_startup_program

        def record(root, file_name, program):
            self.writes.append(program)
            return real_write(root, file_name, program)

        for target, kwargs in (
            ("sidepulse.device_writer.led_lease_holder", {"return_value": None}),
            ("sidepulse.service.write_startup_program", {"side_effect": record}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def settings(self, style: str | None) -> AgentMonitorSettings:
        settings = AgentMonitorSettings()
        if style is not None:
            settings = settings.with_agent_animation(ANIMATION_STATE_STARTUP, style)
        return settings

    def test_an_untouched_setting_never_writes_the_card(self) -> None:
        (self.root / "INIT.LED").write_text("off\n")
        self.assertFalse(self.service.sync_startup_programs(self.settings(None)))
        self.assertEqual(self.writes, [])
        self.assertEqual((self.root / "INIT.LED").read_text(), "off\n")

    def test_the_factory_fill_matches_a_factory_card(self) -> None:
        settings = self.settings(AGENT_ANIMATION_STARTUP_FILL)
        self.assertFalse(self.service.sync_startup_programs(settings))
        self.assertEqual(self.writes, [])

    def test_a_new_pick_is_written_once(self) -> None:
        settings = self.settings(AGENT_ANIMATION_IMMEDIATE_OFF)
        self.assertTrue(self.service.sync_startup_programs(settings))
        self.assertFalse(self.service.sync_startup_programs(settings))
        self.assertEqual(self.writes, ["off"])
        self.assertEqual((self.root / "INIT.LED").read_text(), "off")

        cyan = self.settings(AGENT_ANIMATION_CYAN_STARTUP)
        self.assertTrue(self.service.sync_startup_programs(cyan))
        self.assertIn("#00E5FF", (self.root / "INIT.LED").read_text())
        self.assertEqual(len(self.writes), 2)

    def test_a_failed_write_is_not_retried_every_tick(self) -> None:
        settings = self.settings(AGENT_ANIMATION_IMMEDIATE_OFF)
        with patch("sidepulse.service.write_startup_program", side_effect=OSError("read-only")) as failing:
            self.assertFalse(self.service.sync_startup_programs(settings))
            self.assertFalse(self.service.sync_startup_programs(settings))
        self.assertEqual(failing.call_count, 1)

    def test_status_is_rewritten_after_a_startup_write(self) -> None:
        settings = self.settings(AGENT_ANIMATION_IMMEDIATE_OFF)
        key = f"file:{self.root / 'LEDS.LED'}"
        self.service.last_program_by_target[key] = "cached"
        self.service.sync_startup_programs(settings)
        self.assertNotIn(key, self.service.last_program_by_target)

    def test_built_in_profiles_pick_a_startup(self) -> None:
        for profile in builtin_agent_animation_profiles().values():
            with self.subTest(profile=profile.name):
                self.assertIn(ANIMATION_STATE_STARTUP, profile.animations)


if __name__ == "__main__":
    unittest.main()
