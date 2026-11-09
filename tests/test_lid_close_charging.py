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
from sidepulse.settings import (
    LID_ANIMATION_CLOSED,
    LID_ANIMATION_OPEN,
    LedAnimationSetting,
)

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
    def setUp(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))
        self.status_bar = status_bar
        self.device = status_bar.StatusBarDevice(
            device_id="sd",
            name="SidePulse",
            root=Path("/Volumes/SidePulse"),
            target=Path("/Volumes/SidePulse/LEDS.LED"),
            connected=True,
            display="agent",
        )
        self.animation = LedAnimationSetting(program="off", duration_seconds=0.1)

    def controller(self, *, enabled: bool = True, snapshot=CHARGING):
        controller = self.status_bar.StatusBarController.alloc().init()
        controller.settings = controller.settings.with_battery_power_change_preview(
            enabled=enabled, seconds=6
        )
        controller.last_battery_snapshot = snapshot
        controller.leds_enabled = True
        controller.led_animation_token = 0
        return controller

    def play(self, controller, kind: str):
        with (
            patch("sidepulse.status_bar.log_status_bar"),
            patch.object(controller, "status_bar_devices", return_value=[self.device]),
            patch("sidepulse.status_bar.threading.Thread") as thread,
        ):
            controller.play_lid_animation(kind, animation=self.animation)
        return thread.call_args.kwargs["args"]

    def test_close_while_charging_queues_the_fill(self) -> None:
        args = self.play(self.controller(), LID_ANIMATION_CLOSED)
        self.assertEqual(args[0], LID_ANIMATION_CLOSED)
        self.assertIs(args[4], CHARGING)
        self.assertEqual(args[5], 6.0)

    def test_fill_follows_the_checkbox_and_charging_state(self) -> None:
        cases = (
            (self.controller(enabled=False), LID_ANIMATION_CLOSED),
            (
                self.controller(snapshot=BatterySnapshot(percent=40, is_plugged=False)),
                LID_ANIMATION_CLOSED,
            ),
            (self.controller(snapshot=None), LID_ANIMATION_CLOSED),
            (self.controller(), LID_ANIMATION_OPEN),
        )
        for controller, kind in cases:
            args = self.play(controller, kind)
            self.assertIsNone(args[4])
            self.assertEqual(args[5], 0.0)

    def run_worker(self, controller, *, late: bool):
        writes = []
        clock = iter([1000.0, 1000.0 + (30.0 if late else 0.2)])
        with (
            patch("sidepulse.status_bar.log_status_bar"),
            patch("sidepulse.status_bar.time.sleep"),
            patch("sidepulse.status_bar.time.time", side_effect=lambda: next(clock)),
            patch(
                "sidepulse.status_bar.write_led_program",
                side_effect=lambda program, device_path: writes.append(program) or device_path,
            ),
            patch.object(controller, "performSelectorOnMainThread_withObject_waitUntilDone_"),
        ):
            controller.play_lid_animation_worker(
                LID_ANIMATION_CLOSED, self.animation, [self.device], 0, CHARGING, 6.0
            )
        return writes

    def test_worker_writes_the_fill_after_the_close_animation(self) -> None:
        writes = self.run_worker(self.controller(), late=False)
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0], "off")
        self.assertIn(BATTERY_CHARGING_MINT, writes[1])
        self.assertTrue(writes[1].splitlines()[-1].startswith("off "))

    def test_worker_skips_the_fill_when_the_mac_slept_first(self) -> None:
        writes = self.run_worker(self.controller(), late=True)
        self.assertEqual(writes, ["off"])

    def test_worker_skips_the_fill_after_the_lid_reopens(self) -> None:
        controller = self.controller()
        controller.led_animation_token = 1
        writes = self.run_worker(controller, late=False)
        self.assertEqual(writes, ["off"])


if __name__ == "__main__":
    unittest.main()
