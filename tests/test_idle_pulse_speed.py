from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sidepulse.led_status import (
    AgentLedController,
    program_for_agent_mode,
    add_dark_hold,
)
from sidepulse.models import AgentMode
from sidepulse.settings import (
    DEFAULT_IDLE_PULSE_SECONDS,
    AgentMonitorSettings,
    load_settings,
    save_settings,
)


class IdlePulseSpeedTests(unittest.TestCase):
    def test_default_idle_is_unchanged_at_default_speed(self) -> None:
        plain = program_for_agent_mode(AgentMode.IDLE_READY)
        timed = program_for_agent_mode(
            AgentMode.IDLE_READY, idle_pulse_seconds=DEFAULT_IDLE_PULSE_SECONDS
        )
        self.assertEqual(plain, timed)

    def test_longer_cycle_keeps_breath_speed_and_holds_dark(self) -> None:
        for style in ("default", "idle-pulse", "ember-idle", "purple-idle"):
            for led_count in (2, 8):
                program = program_for_agent_mode(
                    AgentMode.IDLE_READY,
                    led_count=led_count,
                    animation_style=style,
                    idle_pulse_seconds=12,
                )
                lines = program.splitlines()
                self.assertEqual(lines[0], "off 2s", style)
                self.assertEqual(lines[1], "off 8000ms", style)
                self.assertTrue(lines[2].endswith(" 2s ease"), style)
                self.assertEqual(lines[3], "repeat", style)

    def test_non_idle_animations_ignore_idle_pulse(self) -> None:
        for mode, style in (
            (AgentMode.WORKING, "default"),
            (AgentMode.WAITING_FOR_INPUT, "default"),
            (AgentMode.IDLE_READY, "cyan-roll"),
            (AgentMode.IDLE_READY, "kitt"),
        ):
            self.assertEqual(
                program_for_agent_mode(mode, animation_style=style),
                program_for_agent_mode(mode, animation_style=style, idle_pulse_seconds=30),
            )

    def test_custom_animation_ignores_idle_pulse(self) -> None:
        custom = "off 2s\n#FF00FF 2s ease\nrepeat"
        self.assertEqual(
            program_for_agent_mode(
                AgentMode.IDLE_READY,
                animation_style="custom",
                custom_program=custom,
                idle_pulse_seconds=30,
            ),
            custom,
        )

    def test_dark_hold_goes_after_fade_out_and_splits_long_holds(self) -> None:
        self.assertEqual(
            add_dark_hold("brightness 128\noff 2s\n#00ff00 2s ease\nrepeat", 70000),
            "brightness 128\noff 2s\noff 65535ms\noff 4465ms\n#00ff00 2s ease\nrepeat",
        )
        self.assertEqual(add_dark_hold("off 2s\nrepeat", 0), "off 2s\nrepeat")

    def test_controller_rewrites_when_idle_pulse_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulsePro"
            device.mkdir()
            controller = AgentLedController(device_path=device)
            first = controller.sync_mode(AgentMode.IDLE_READY, idle_pulse_seconds=4)
            same = controller.sync_mode(AgentMode.IDLE_READY, idle_pulse_seconds=4)
            slower = controller.sync_mode(AgentMode.IDLE_READY, idle_pulse_seconds=20)
            self.assertTrue(first.changed)
            self.assertFalse(same.changed)
            self.assertTrue(slower.changed)
            self.assertIn("off 16000ms", (device / "LEDS.LED").read_text())

    def test_setting_round_trips_and_clamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            save_settings(AgentMonitorSettings().with_idle_pulse_seconds(7.5), path)
            self.assertEqual(load_settings(path).idle_pulse_seconds, 7.5)
        settings = AgentMonitorSettings()
        self.assertEqual(settings.with_idle_pulse_seconds(1).idle_pulse_seconds, 4.0)
        self.assertEqual(settings.with_idle_pulse_seconds(500).idle_pulse_seconds, 120.0)
        self.assertEqual(
            settings.with_idle_pulse_seconds(float("nan")).idle_pulse_seconds,
            DEFAULT_IDLE_PULSE_SECONDS,
        )

    def test_longest_idle_pulse_fits_firmware_limits(self) -> None:
        program = program_for_agent_mode(AgentMode.IDLE_READY, idle_pulse_seconds=120)
        self.assertIn("off 65535ms\noff 50465ms", program)
        self.assertLessEqual(len(program.encode()), 512)
        self.assertLessEqual(len(program.splitlines()), 20)


if __name__ == "__main__":
    unittest.main()
