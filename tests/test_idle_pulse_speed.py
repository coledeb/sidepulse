from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sidepulse.led_status import (
    AgentLedController,
    program_for_agent_mode,
    retime_program,
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

    def test_slower_idle_pulse_stretches_both_halves(self) -> None:
        for style in ("default", "idle-pulse", "ember-idle", "purple-idle"):
            for led_count in (2, 8):
                program = program_for_agent_mode(
                    AgentMode.IDLE_READY,
                    led_count=led_count,
                    animation_style=style,
                    idle_pulse_seconds=12,
                )
                self.assertIn("off 6000ms", program, style)
                self.assertIn("6000ms ease", program, style)
                self.assertNotIn(" 2s", program, style)

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

    def test_retime_scales_delays_and_leaves_colors_alone(self) -> None:
        self.assertEqual(
            retime_program("brightness 128\n0:#00ff00 0.33s ease-in 250ms; 1:#123456 1s", 2),
            "brightness 128\n0:#00ff00 660ms ease-in 500ms; 1:#123456 2000ms",
        )
        self.assertEqual(retime_program("#ffffff 40s", 3), "#ffffff 65535ms")

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
            self.assertIn("10000ms", (device / "LEDS.LED").read_text())

    def test_setting_round_trips_and_clamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            save_settings(AgentMonitorSettings().with_idle_pulse_seconds(7.5), path)
            self.assertEqual(load_settings(path).idle_pulse_seconds, 7.5)
        settings = AgentMonitorSettings()
        self.assertEqual(settings.with_idle_pulse_seconds(0.2).idle_pulse_seconds, 1.0)
        self.assertEqual(settings.with_idle_pulse_seconds(500).idle_pulse_seconds, 120.0)
        self.assertEqual(
            settings.with_idle_pulse_seconds(float("nan")).idle_pulse_seconds,
            DEFAULT_IDLE_PULSE_SECONDS,
        )

    def test_longest_idle_pulse_fits_firmware_step_limit(self) -> None:
        program = program_for_agent_mode(AgentMode.IDLE_READY, idle_pulse_seconds=120)
        self.assertIn("off 60000ms", program)


if __name__ == "__main__":
    unittest.main()
