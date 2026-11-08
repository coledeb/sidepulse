from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sidepulse.battery import BatteryLedController
from sidepulse.cli import build_sidepulse_parser
from sidepulse.device_writer import (
    DeviceBusyError,
    hold_led_lease,
    led_lease_holder,
    led_lease_path,
    validate_led_text,
    write_normalized_led_program,
)
from sidepulse.led_status import AgentLedController
from sidepulse.models import AgentMode
from sidepulse.visualize import (
    BANDS,
    LevelTracker,
    STYLES,
    blank_program,
    frame_program,
    group_bands,
    parse_color,
    parse_helper_line,
    read_restore_program,
    style_colors,
)


class FrameTests(unittest.TestCase):
    def test_frames_have_fixed_length_and_parse(self) -> None:
        dark = frame_program([(0, 0, 0)] * 8, 75)
        bright = frame_program([(255, 128, 1)] * 8, 75)
        self.assertEqual(len(dark), len(bright))
        self.assertEqual(len(dark), 68)
        self.assertTrue(bright.startswith("#FF8001 #FF8001"))
        self.assertTrue(bright.endswith(" 75ms"))
        validate_led_text(bright)

    def test_fade_is_clamped_to_two_digits(self) -> None:
        self.assertTrue(frame_program([(0, 0, 0)] * 2, 5).endswith(" 10ms"))
        self.assertTrue(frame_program([(0, 0, 0)] * 2, 500).endswith(" 99ms"))
        self.assertEqual(len(blank_program(2)), len(frame_program([(9, 9, 9)] * 2, 50)))

    def test_every_style_yields_one_color_per_led(self) -> None:
        for count in (2, 8):
            levels = [index / count for index in range(count)]
            for style in STYLES:
                colors = style_colors(style, levels, 0.5, hue=0.0, color=(0, 200, 255))
                self.assertEqual(len(colors), count, style)
                for color in colors:
                    self.assertTrue(all(0 <= channel <= 255 for channel in color), (style, color))

    def test_silence_is_dark_in_spectrum_and_mono(self) -> None:
        for style in ("spectrum", "mono"):
            colors = style_colors(style, [0.0] * 8, 0.0, hue=0.3, color=(255, 255, 255))
            self.assertEqual(colors, [(0, 0, 0)] * 8)

    def test_spectrum_runs_red_bass_to_violet_treble(self) -> None:
        colors = style_colors("spectrum", [1.0] * 8, 1.0, hue=0.0, color=(0, 0, 0))
        self.assertEqual(colors[0], (255, 0, 0))
        red, green, blue = colors[-1]
        self.assertGreater(blue, green)
        self.assertGreater(red, green)


class AnalysisTests(unittest.TestCase):
    def test_group_bands_takes_group_maxima_for_dot(self) -> None:
        bands = [0.1, 0.9, 0.2, 0.3, 0.0, 0.4, 0.5, 0.2]
        self.assertEqual(group_bands(bands, 2), [0.9, 0.5])
        self.assertEqual(group_bands(bands, 8), bands)

    def test_level_tracker_normalizes_and_silences(self) -> None:
        tracker = LevelTracker(2)
        for _ in range(20):
            levels = tracker.update([0.5, 0.05], 0.05, silent=False)
        self.assertAlmostEqual(levels[0], 1.0, places=3)
        self.assertAlmostEqual(levels[1], 1.0, places=3)  # per-band gain
        for _ in range(40):
            levels = tracker.update([0.5, 0.05], 0.05, silent=True)
        self.assertLess(max(levels), 0.01)

    def test_parse_helper_line(self) -> None:
        line = "F 0.01 " + " ".join(["0.5"] * BANDS)
        self.assertEqual(parse_helper_line(line), (0.01, [0.5] * BANDS))
        self.assertIsNone(parse_helper_line("F 0.1 0.2"))
        self.assertIsNone(parse_helper_line("APPTAP 48000 8"))
        self.assertIsNone(parse_helper_line("F x " + " ".join(["0"] * BANDS)))

    def test_parse_color(self) -> None:
        self.assertEqual(parse_color("#00C8FF"), (0, 200, 255))
        self.assertEqual(parse_color("ff3a00"), (255, 58, 0))
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_color("#fff")


class LeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name) / "home"
        home.mkdir()
        patcher = patch("sidepulse.device_writer.Path.home", return_value=home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.device = Path(self.tmp.name) / "SidePulsePro"
        self.device.mkdir()
        (self.device / "LEDS.LED").write_text("off")

    def write_foreign_lease(self, pid: int) -> None:
        path = led_lease_path(self.device)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": pid, "owner": "sidepulse visualize"}))

    def foreign_process(self) -> subprocess.Popen:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def test_owner_can_write_and_lease_is_released(self) -> None:
        with hold_led_lease(self.device, "test"):
            self.assertTrue(led_lease_path(self.device).exists())
            self.assertIsNone(led_lease_holder(self.device))  # own lease does not block
            write_normalized_led_program("#010203", device_path=self.device)
        self.assertFalse(led_lease_path(self.device).exists())
        self.assertEqual((self.device / "LEDS.LED").read_text(), "#010203")

    def test_other_process_lease_blocks_writes(self) -> None:
        self.write_foreign_lease(self.foreign_process().pid)
        with self.assertRaises(DeviceBusyError):
            write_normalized_led_program("#ffffff", device_path=self.device)
        self.assertEqual((self.device / "LEDS.LED").read_text(), "off")
        with self.assertRaises(DeviceBusyError):
            with hold_led_lease(self.device, "second"):
                pass

    def test_stale_lease_from_dead_process_is_ignored(self) -> None:
        process = self.foreign_process()
        process.kill()
        process.wait()
        self.write_foreign_lease(process.pid)
        self.assertIsNone(led_lease_holder(self.device))
        write_normalized_led_program("#ffffff", device_path=self.device)

    def test_dry_run_ignores_lease(self) -> None:
        self.write_foreign_lease(self.foreign_process().pid)
        write_normalized_led_program("#ffffff", device_path=self.device, dry_run=True)

    def test_controllers_retry_quickly_while_busy(self) -> None:
        self.write_foreign_lease(self.foreign_process().pid)
        agent = AgentLedController(device_path=self.device)
        result = agent.sync_mode(AgentMode.WORKING)
        self.assertIsNotNone(result.error)
        self.assertTrue(agent.last_error_busy)
        self.assertEqual(agent.retry_seconds(), 1.0)

        battery = BatteryLedController(device_path=self.device)
        self.assertEqual(battery.retry_seconds(), 10.0)
        battery.last_error_busy = True
        self.assertEqual(battery.retry_seconds(), 1.0)


class RestoreTests(unittest.TestCase):
    def test_restore_program_reads_valid_file_and_rejects_bad(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "LEDS.LED"
            self.assertIsNone(read_restore_program(target))
            target.write_text("off\n#ff00ff 1s pulse\nrepeat")
            self.assertEqual(read_restore_program(target), "off\n#ff00ff 1s pulse\nrepeat")
            target.write_text("x" * 600)
            self.assertIsNone(read_restore_program(target))


class ParserTests(unittest.TestCase):
    def test_visualize_subcommand_is_registered(self) -> None:
        args = build_sidepulse_parser().parse_args(
            ["visualize", "--app", "spotify", "--app", "music", "--style", "pulse", "--color", "#ff0000"]
        )
        self.assertEqual(args.app, ["spotify", "music"])
        self.assertEqual(args.style, "pulse")
        self.assertEqual(args.color, (255, 0, 0))
        self.assertEqual(args.transport, "auto")


if __name__ == "__main__":
    unittest.main()
