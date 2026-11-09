from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sidepulse.battery import DEFAULT_POWER_CHANGE_PREVIEW_SECONDS
from sidepulse.settings import (
    MAX_POWER_CHANGE_PREVIEW_SECONDS,
    MIN_POWER_CHANGE_PREVIEW_SECONDS,
    AgentMonitorSettings,
    load_settings,
    save_settings,
)


class BatteryPreviewSecondsTests(unittest.TestCase):
    def test_setting_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            settings = AgentMonitorSettings().with_battery_power_change_preview(seconds=10)
            save_settings(settings, path)
            self.assertEqual(load_settings(path).battery_power_change_preview_seconds, 10.0)

    def test_setting_is_clamped_to_allowed_window(self) -> None:
        settings = AgentMonitorSettings()
        self.assertEqual(
            settings.with_battery_power_change_preview(seconds=1).battery_power_change_preview_seconds,
            MIN_POWER_CHANGE_PREVIEW_SECONDS,
        )
        self.assertEqual(
            settings.with_battery_power_change_preview(seconds=60).battery_power_change_preview_seconds,
            MAX_POWER_CHANGE_PREVIEW_SECONDS,
        )
        self.assertEqual(
            settings.with_battery_power_change_preview(
                seconds=float("nan")
            ).battery_power_change_preview_seconds,
            DEFAULT_POWER_CHANGE_PREVIEW_SECONDS,
        )

    def test_out_of_range_saved_value_is_clamped_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            save_settings(AgentMonitorSettings(), path)
            data = json.loads(path.read_text())
            data["battery_monitoring"]["power_change_preview_seconds"] = 90
            path.write_text(json.dumps(data))
            self.assertEqual(
                load_settings(path).battery_power_change_preview_seconds,
                MAX_POWER_CHANGE_PREVIEW_SECONDS,
            )

    def test_toggling_keeps_seconds(self) -> None:
        settings = AgentMonitorSettings().with_battery_power_change_preview(seconds=12)
        toggled = settings.with_battery_power_change_preview(enabled=False)
        self.assertFalse(toggled.battery_show_on_power_change)
        self.assertEqual(toggled.battery_power_change_preview_seconds, 12.0)


if __name__ == "__main__":
    unittest.main()
