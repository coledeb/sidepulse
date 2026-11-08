from __future__ import annotations

import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from sidepulse.settings import AgentMonitorSettings, load_settings, save_settings
from sidepulse.visualizer_control import (
    VISUALIZER_LEASE_OWNER,
    VisualizerControl,
    last_log_line,
    visualize_command,
)


def fake_python(directory: Path, body: str) -> str:
    """A stand-in for `python -m sidepulse visualize ...` that runs a shell body."""
    script = directory / "fake-python"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


class VisualizerControlTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.log = self.dir / "visualizer.log"
        self.holders: list[dict] = []

    def control(self, body: str) -> VisualizerControl:
        control = VisualizerControl(self.log, python=fake_python(self.dir, body), lease_holders=lambda: self.holders)
        self.addCleanup(control.stop, own_only=True)
        return control

    def test_command_includes_style_and_optional_app(self) -> None:
        self.assertEqual(
            visualize_command("pulse", "", python="py"),
            ["py", "-m", "sidepulse", "visualize", "--quiet", "--control-stdin", "--style", "pulse"],
        )
        self.assertEqual(visualize_command("mono", "com.spotify.client", python="py")[-2:],
                         ["--app", "com.spotify.client"])

    def test_start_and_stop_child(self) -> None:
        control = self.control("exec sleep 30")
        self.assertFalse(control.running())
        control.start("spectrum", "")
        self.assertTrue(control.running())
        control.stop()
        self.assertFalse(control.running())
        control.poll()
        self.assertIsNone(control.last_error)

    def test_failed_child_reports_last_log_line(self) -> None:
        control = self.control('echo "Visualizing all system audio"; echo "sidepulse visualize: no device found" >&2; exit 1')
        control.start("spectrum", "")
        control.process.wait(timeout=5)
        control.poll()
        self.assertFalse(control.running())
        self.assertEqual(control.last_error, "no device found")

    def test_external_visualizer_is_detected_and_stopped(self) -> None:
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        # The lease disappears once its process exits, as led_lease_holder() ignores dead pids.
        live = lambda: [holder for holder in self.all_holders if holder["pid"] != other.pid or other.poll() is None]
        self.all_holders = [
            {"pid": other.pid, "owner": VISUALIZER_LEASE_OWNER},
            {"pid": 1, "owner": "something else"},
        ]
        control = VisualizerControl(self.log, python=fake_python(self.dir, "exit 0"), lease_holders=live)
        self.assertEqual(control.external_pids(), [other.pid])
        self.assertTrue(control.running())

        started = time.monotonic()
        control.stop()
        self.assertIsNotNone(other.poll())
        self.assertFalse(control.running())
        self.assertLess(time.monotonic() - started, 2.5)

    def test_source_label(self) -> None:
        control = self.control("exit 0")
        control.playing_apps = [("Spotify", "com.spotify.client")]
        self.assertEqual(control.source_label(""), "All System Audio")
        self.assertEqual(control.source_label("com.spotify.client"), "Spotify")
        self.assertEqual(control.source_label("chrome"), "chrome")

    def test_last_log_line_skips_banner(self) -> None:
        self.log.write_text("Visualizing all system audio on 8 LEDs\n")
        self.assertIsNone(last_log_line(self.log))
        self.log.write_text("Visualizing x\nsidepulse visualize: boom\n\n")
        self.assertEqual(last_log_line(self.log), "boom")
        self.assertIsNone(last_log_line(self.dir / "missing.log"))


class VisualizerSettingsTests(unittest.TestCase):
    def test_round_trip_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            settings = AgentMonitorSettings().with_visualizer(style="pulse", source=" com.spotify.client ")
            save_settings(settings, path)
            loaded = load_settings(path)
            self.assertEqual(loaded.visualizer_style, "pulse")
            self.assertEqual(loaded.visualizer_source, "com.spotify.client")
            self.assertEqual(loaded.with_visualizer(style="nope").visualizer_style, "pulse")
            self.assertEqual(AgentMonitorSettings().visualizer_style, "spectrum")
            self.assertEqual(AgentMonitorSettings().visualizer_source, "")


if __name__ == "__main__":
    unittest.main()


class VisualizerMenuTests(unittest.TestCase):
    def test_menu_shows_visualizer_section_for_physical_device(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))
        from datetime import datetime, timezone
        from types import SimpleNamespace

        device = status_bar.StatusBarDevice(
            device_id="/Volumes/SidePulse",
            name="SidePulse",
            root=Path("/Volumes/SidePulse"),
            target=Path("/Volumes/SidePulse/LEDS.LED"),
            connected=True,
            display="agent",
            brightness=255,
        )
        with tempfile.TemporaryDirectory() as tmp:
            control = VisualizerControl(Path(tmp) / "v.log", lease_holders=lambda: [])
            control.playing_apps = [("Spotify", "com.spotify.client")]
            target = SimpleNamespace(
                settings=AgentMonitorSettings().with_visualizer(style="pulse", source="com.spotify.client"),
                closed_lid_awake=SimpleNamespace(last_error=None),
                status_bar_devices=lambda: [device],
                visualizer=control,
            )
            menu = status_bar.build_menu(
                SimpleNamespace(statuses=[], collected_at=datetime.now(timezone.utc)), status_bar.STATE_IDLE, target
            )
        items = {menu.itemAtIndex_(i).title(): menu.itemAtIndex_(i) for i in range(menu.numberOfItems())}
        self.assertIn("Music Visualizer", items)
        self.assertIn("Start Visualizer", items)
        self.assertIn("Style: Bass Pulse", items)
        source = items["Source: Spotify"].submenu()
        titles = [source.itemAtIndex_(i).title() for i in range(source.numberOfItems())]
        self.assertEqual([t for t in titles if t], ["All System Audio", "Spotify"])
        checked = [source.itemAtIndex_(i).title() for i in range(source.numberOfItems()) if source.itemAtIndex_(i).state()]
        self.assertEqual(checked, ["Spotify"])


class VisualizerColorTests(unittest.TestCase):
    def test_command_passes_color(self) -> None:
        command = visualize_command("mono", "", python="py", color="#F23819")
        self.assertEqual(command[-2:], ["--color", "#F23819"])

    def test_color_setting_normalizes_and_rejects_bad_values(self) -> None:
        settings = AgentMonitorSettings()
        self.assertEqual(settings.visualizer_color, "#00E5FF")
        self.assertEqual(settings.with_visualizer(color="f23819").visualizer_color, "#F23819")
        self.assertEqual(settings.with_visualizer(color="#zzzzzz").visualizer_color, "#00E5FF")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            save_settings(settings.with_visualizer(color="#123abc"), path)
            self.assertEqual(load_settings(path).visualizer_color, "#123ABC")

    def test_helper_is_hidden_from_app_list(self) -> None:
        from sidepulse.visualize import list_audio_apps

        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "apptap"
            helper.write_text(
                "#!/bin/sh\n"
                "printf 'playing\\t10\\tcom.spotify.client\\tSpotify\\n'\n"
                "printf 'playing\\t11\\tio.sidepulse.apptap\\tapptap\\n'\n"
            )
            helper.chmod(0o755)
            self.assertEqual(list_audio_apps(helper), [(True, 10, "com.spotify.client", "Spotify")])

    def test_color_submenu_only_for_color_styles(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))
        from types import SimpleNamespace

        control = VisualizerControl(Path("/nonexistent/v.log"), lease_holders=lambda: [])
        for style, expected in (("spectrum", False), ("mono", True), ("pulse", True), ("meter", False)):
            target = SimpleNamespace(
                settings=AgentMonitorSettings().with_visualizer(style=style, color="#F23819"), visualizer=control
            )
            titles = [item.title() for item in status_bar.build_visualizer_menu_items(target)]
            self.assertEqual("Color: Ember" in titles, expected, (style, titles))

        target = SimpleNamespace(settings=AgentMonitorSettings().with_visualizer(style="mono", color="#123456"),
                                 visualizer=control)
        item = status_bar.build_visualizer_color_item(target)
        self.assertEqual(item.title(), "Color: #123456")
        submenu = item.submenu()
        last = submenu.itemAtIndex_(submenu.numberOfItems() - 1)
        self.assertEqual(last.title(), "Custom (#123456)...")
        self.assertEqual(last.state(), 1)

    def test_color_hex_round_trip(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))
        for value in ("#00E5FF", "#F23819", "#000000", "#FFFFFF"):
            self.assertEqual(status_bar.hex_from_ns_color(status_bar.ns_color_from_hex(value)), value)


class LiveColorTests(unittest.TestCase):
    def test_control_channel_applies_colors_and_reports_close(self) -> None:
        import io
        from types import SimpleNamespace

        from sidepulse.visualize import ControlChannel

        args = SimpleNamespace(color=(0, 0, 0))
        channel = ControlChannel(args, io.StringIO("color #ff0000\njunk\ncolor nothex\n"))
        self.assertTrue(channel.closed.wait(2))
        self.assertEqual(args.color, (255, 0, 0))

    def test_set_color_reaches_running_child_without_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            received = directory / "stdin.txt"
            control = VisualizerControl(
                directory / "v.log",
                python=fake_python(directory, f'exec cat > "{received}"'),
                lease_holders=lambda: [],
            )
            self.assertFalse(control.set_color("#FF0000"))  # nothing running yet
            control.start("mono", "", "#00E5FF")
            pid = control.process.pid
            self.assertTrue(control.set_color("#F23819"))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not (
                received.exists() and "F23819" in received.read_text()
            ):
                time.sleep(0.02)
            self.assertEqual(received.read_text(), "color #F23819\n")
            self.assertEqual(control.process.pid, pid)
            control.stop()

    def test_hue_conversions_and_slider(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))
        from types import SimpleNamespace

        self.assertEqual(status_bar.hex_from_hue(0.0), "#FF0000")
        self.assertEqual(status_bar.hex_from_hue(1 / 3), "#00FF00")
        self.assertAlmostEqual(status_bar.hue_from_hex("#00FFFF"), 0.5)
        target = SimpleNamespace(settings=AgentMonitorSettings().with_visualizer(style="mono", color="#00FFFF"),
                                 visualizer=None)
        item = status_bar.build_visualizer_color_item(target)
        self.assertIs(target.visualizer_color_menu_items[0], item)
        submenu = item.submenu()
        self.assertEqual(submenu.itemAtIndex_(0).title(), "Hue")
        slider = submenu.itemAtIndex_(1).view().subviews()[0]
        self.assertAlmostEqual(slider.doubleValue(), 180.0)
        self.assertTrue(slider.isContinuous())
        self.assertEqual(slider.action(), "setVisualizerHue:")


class LiveColorMenuTests(unittest.TestCase):
    def test_hue_drag_updates_custom_row_and_checkmarks(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))
        from types import SimpleNamespace

        target = SimpleNamespace(settings=AgentMonitorSettings().with_visualizer(style="mono", color="#FF00A8"),
                                 visualizer=None)
        item = status_bar.build_visualizer_color_item(target)
        submenu = item.submenu()
        custom = submenu.itemAtIndex_(submenu.numberOfItems() - 1)
        cyan = next(submenu.itemAtIndex_(i) for i in range(submenu.numberOfItems())
                    if submenu.itemAtIndex_(i).title() == "Cyan")
        self.assertEqual(custom.title(), "Custom (#FF00A8)...")

        status_bar.update_visualizer_color_menu(target, "#FF0000")
        self.assertEqual(item.title(), "Color: #FF0000")
        self.assertEqual(custom.title(), "Custom (#FF0000)...")
        self.assertEqual(custom.state(), 1)

        status_bar.update_visualizer_color_menu(target, "#00E5FF")
        self.assertEqual(item.title(), "Color: Cyan")
        self.assertEqual(custom.title(), "Custom...")
        self.assertEqual((custom.state(), cyan.state()), (0, 1))
