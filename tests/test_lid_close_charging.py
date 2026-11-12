from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from sidepulse.battery import (
    BATTERY_CHARGING_MINT,
    BatterySnapshot,
    charging_fill_cycle,
    program_for_battery,
    program_for_charging_preview,
)
from sidepulse.device_writer import validate_led_text
from sidepulse.settings import LID_ANIMATION_CLOSED, LID_ANIMATION_OPEN

CHARGING = BatterySnapshot(percent=40, is_plugged=True, is_charging=True)


class ChargingPreviewProgramTests(unittest.TestCase):
    def test_not_charging_has_no_preview(self) -> None:
        for snapshot in (
            BatterySnapshot(percent=40, is_plugged=False),
            BatterySnapshot(percent=40, is_plugged=True, is_charging=False),
            BatterySnapshot(percent=100, is_plugged=True, is_charging=True),
            BatterySnapshot(percent=90, is_plugged=True, is_charging=True, is_charged=True),
        ):
            self.assertIsNone(program_for_charging_preview(snapshot, 7))

    def test_preview_repeats_the_fill_for_the_seconds_then_goes_dark(self) -> None:
        program, duration = program_for_charging_preview(CHARGING, 7)
        lines = program.splitlines()
        _cycle_lines, cycle_ms = charging_fill_cycle(CHARGING)
        repeats = round(7000 / cycle_ms)

        self.assertIn(BATTERY_CHARGING_MINT, program)
        self.assertEqual(lines[-2], f"repeat {repeats}")
        self.assertTrue(lines[-1].startswith("off "))
        self.assertNotIn("repeat", lines[:-2])
        self.assertAlmostEqual(duration, 7, delta=cycle_ms / 1000)

    def test_preview_shares_the_fill_with_the_battery_display(self) -> None:
        program, _duration = program_for_charging_preview(CHARGING, 7)
        display = program_for_battery(CHARGING)
        self.assertEqual(program.splitlines()[:-2], display.splitlines()[:-1])

    def test_preview_fits_the_device_limits_at_every_level(self) -> None:
        for percent in range(0, 100, 5):
            snapshot = BatterySnapshot(percent=percent, is_plugged=True, is_charging=True)
            for seconds in (5, 15):
                program, _duration = program_for_charging_preview(
                    snapshot, seconds, brightness=128
                )
                validate_led_text(program)

    def test_short_length_still_plays_once(self) -> None:
        program, _duration = program_for_charging_preview(CHARGING, 0)
        self.assertIn("repeat 1", program)


class LidCloseChargingTests(unittest.TestCase):
    """The daemon follows the lid-close animation with the charging fill."""

    def setUp(self) -> None:
        from sidepulse.device_writer import DeviceCandidate
        from sidepulse.service import SidePulseService
        from sidepulse.settings import AgentMonitorSettings

        self.service = SidePulseService()
        root = Path("/Volumes/SidePulse")
        self.service.runtime.devices = [DeviceCandidate(root, root / "LEDS.LED", "mounted")]
        self.writes: list[str] = []
        self.enabled = True
        self.settings = lambda: AgentMonitorSettings().with_battery_power_change_preview(
            enabled=self.enabled, seconds=6
        )
        for target in ("sidepulse.service.load_settings",):
            patcher = patch(target, side_effect=lambda: self.settings())
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch(
            "sidepulse.service.write_led_program",
            side_effect=lambda program, device_path: self.writes.append(program) or device_path,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def close(self, snapshot=CHARGING):
        self.service.runtime.last_battery_snapshot = snapshot
        self.service.play_lid_animation(LID_ANIMATION_CLOSED)

    def test_close_while_charging_queues_the_fill(self) -> None:
        self.close()
        _due, _wall, battery, seconds = self.service.lid_charging_followup
        self.assertIs(battery, CHARGING)
        self.assertEqual(seconds, 6.0)
        self.assertEqual(len(self.writes), 1)

    def test_fill_follows_the_checkbox_and_charging_state(self) -> None:
        self.enabled = False
        self.close()
        self.assertIsNone(self.service.lid_charging_followup)
        self.enabled = True
        for snapshot in (BatterySnapshot(percent=40, is_plugged=False), None):
            self.close(snapshot)
            self.assertIsNone(self.service.lid_charging_followup)
        self.service.runtime.last_battery_snapshot = CHARGING
        self.service.play_lid_animation(LID_ANIMATION_OPEN)
        self.assertIsNone(self.service.lid_charging_followup)

    def test_fill_is_written_once_the_close_animation_ends(self) -> None:
        self.close()
        with patch("sidepulse.service.time.time", return_value=self.service.lid_charging_followup[1] + 0.2):
            self.service.play_lid_charging_followup()
        self.assertEqual(len(self.writes), 2)
        self.assertIn(BATTERY_CHARGING_MINT, self.writes[1])
        self.assertTrue(self.writes[1].splitlines()[-1].startswith("off "))
        self.assertIsNone(self.service.lid_charging_followup)

    def test_fill_is_skipped_when_the_mac_slept_first(self) -> None:
        self.close()
        with patch("sidepulse.service.time.time", return_value=self.service.lid_charging_followup[1] + 30):
            self.service.play_lid_charging_followup()
        self.assertEqual(len(self.writes), 1)

    def test_reopening_the_lid_cancels_the_fill(self) -> None:
        self.close()
        self.service.play_lid_animation(LID_ANIMATION_OPEN)
        self.assertIsNone(self.service.lid_charging_followup)


if __name__ == "__main__":
    unittest.main()
