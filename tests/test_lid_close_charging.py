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

    def test_awake_close_plays_the_fill_alone(self) -> None:
        self.service.runtime.last_battery_snapshot = CHARGING
        self.service.play_lid_charging_fill()
        self.assertEqual(len(self.writes), 1)
        self.assertIn(BATTERY_CHARGING_MINT, self.writes[0])
        self.assertIsNone(self.service.lid_charging_followup)
        self.assertGreater(self.service.animation_until, 0)

    def test_awake_close_skips_the_fill_unless_charging(self) -> None:
        for snapshot in (BatterySnapshot(percent=40, is_plugged=False), None):
            self.service.runtime.last_battery_snapshot = snapshot
            self.service.play_lid_charging_fill()
        self.enabled = False
        self.service.runtime.last_battery_snapshot = CHARGING
        self.service.play_lid_charging_fill()
        self.assertEqual(self.writes, [])


class ProgramAtPhaseTests(unittest.TestCase):
    IDLE = "brightness 128\noff 2s\noff 26000ms\n2:#9F009F 3:#FF00FF 2s ease\nrepeat"

    def test_hold_is_cut_at_the_elapsed_point(self) -> None:
        from sidepulse.led_status import program_at_phase

        self.assertEqual(
            program_at_phase(self.IDLE, 10).splitlines(),
            ["brightness 128", "off 18000ms", "2:#9F009F 3:#FF00FF 2s ease",
             "off 2s", "off 8000ms", "repeat"],
        )
        # Whole cycles later it is the same point.
        self.assertEqual(program_at_phase(self.IDLE, 40), program_at_phase(self.IDLE, 10))

    def test_transition_starts_at_its_nearest_edge(self) -> None:
        from sidepulse.led_status import program_at_phase

        self.assertEqual(program_at_phase(self.IDLE, 29.5), self.IDLE)
        self.assertEqual(
            program_at_phase(self.IDLE, 28.4).splitlines()[1:3],
            ["2:#9F009F 3:#FF00FF 2s ease", "off 2s"],
        )

    def test_unreadable_or_one_shot_programs_are_unchanged(self) -> None:
        from sidepulse.led_status import program_at_phase

        for program in (
            "off\n#F23819 1.6s pulse\nrepeat",
            "off 320ms\n0:#FF00FF 760ms pulse 0ms; 1:#FF00FF 760ms pulse 95ms\nrepeat",
            "#FF00FF 320ms cosine",
        ):
            self.assertEqual(program_at_phase(program, 5), program)


class StatusResumeTests(unittest.TestCase):
    def test_status_resumes_in_step_after_an_animation(self) -> None:
        from sidepulse.device_writer import DeviceCandidate
        from sidepulse.service import SidePulseService

        service = SidePulseService()
        root = Path("/Volumes/SidePulse")
        service.runtime.devices = [DeviceCandidate(root, root / "LEDS.LED", "mounted")]
        writes: list[str] = []
        idle = ProgramAtPhaseTests.IDLE
        with (
            patch("sidepulse.service.write_led_program",
                  side_effect=lambda program, device_path: writes.append(program)),
            patch("sidepulse.service.program_for_agent_mode", return_value=idle),
            patch("sidepulse.service.led_lease_holder", return_value=None),
            patch("sidepulse.service.time.monotonic", return_value=100.0),
        ):
            service.sync_outputs()
        self.assertEqual(writes, [idle])
        # An animation overwrote the LEDs and dropped the cache.
        service.last_program_by_target.clear()
        with (
            patch("sidepulse.service.write_led_program",
                  side_effect=lambda program, device_path: writes.append(program)),
            patch("sidepulse.service.program_for_agent_mode", return_value=idle),
            patch("sidepulse.service.led_lease_holder", return_value=None),
            patch("sidepulse.service.time.monotonic", return_value=110.0),
        ):
            service.sync_outputs()
        from sidepulse.led_status import program_at_phase

        self.assertEqual(writes[1], program_at_phase(idle, 10))
        self.assertEqual(service.program_started_by_target[f"file:{root / 'LEDS.LED'}"], (idle, 100.0))


if __name__ == "__main__":
    unittest.main()
