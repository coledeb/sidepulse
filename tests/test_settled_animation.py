"""The Settled animation plays as one step with the settle that triggers it."""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from sidepulse.settings import AgentMonitorSettings


class SettledAnimationTests(unittest.TestCase):
    def setUp(self) -> None:
        from sidepulse.device_writer import DeviceCandidate
        from sidepulse.service import SidePulseService

        self.service = SidePulseService()
        root = Path("/Volumes/SidePulse")
        self.service.runtime.devices = [DeviceCandidate(root, root / "LEDS.LED", "mounted")]
        self.events: list[str] = []
        for target, kwargs in (
            ("sidepulse.service.load_settings", {"side_effect": AgentMonitorSettings}),
            ("sidepulse.service.led_lease_holder", {"return_value": None}),
            ("sidepulse.service.write_led_program",
             {"side_effect": lambda program, device_path: self.events.append("write") or device_path}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_settle_runs_under_the_output_lock_before_the_animation(self) -> None:
        def apply() -> bool:
            # The main loop's status write takes this lock, so it cannot land
            # between the settle and the animation.
            held = []
            thread = threading.Thread(target=lambda: held.append(self.service.output_lock.acquire(blocking=False)))
            thread.start()
            thread.join()
            self.assertEqual(held, [False])
            self.events.append("settle")
            return True

        self.assertTrue(self.service.play_settled_animation(apply))
        self.assertEqual(self.events, ["settle", "write"])
        self.assertGreater(self.service.animation_until, 0)

    def test_nothing_plays_when_the_settle_does_not_apply(self) -> None:
        self.assertFalse(self.service.play_settled_animation(lambda: False))
        self.assertEqual(self.events, [])
        self.assertEqual(self.service.animation_until, 0)

    def test_a_running_animation_is_not_interrupted(self) -> None:
        self.service.play_settled_animation()
        self.events.clear()
        self.assertTrue(self.service.play_settled_animation(lambda: self.events.append("settle") or True))
        self.assertEqual(self.events, ["settle"])


if __name__ == "__main__":
    unittest.main()
