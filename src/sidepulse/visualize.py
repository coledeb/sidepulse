"""`sidepulse visualize`: drive SidePulse LEDs from live audio.

Audio comes from a small Swift helper (`resources/apptap.swift`) that captures
system audio, specific apps (Core Audio process taps, macOS 14.2+), or the
microphone, and streams 8 logarithmic frequency-band levels. This module turns
those levels into LED frames.

Frames are streamed over READ2ME on SidePulse Pro: the program is delivered by
*reading* setup.html, so nothing is written to the device's filesystem and the
firmware never saves filesystem metadata to flash. That caps the rate at ~20 fps
(a 68-byte frame is 200 reads of ~0.25 ms). Writing LEDS.LED instead allows
30-60 fps, but the device then saves metadata to flash about twice a minute.

While running, the visualizer holds an exclusive LED lease so the background
service and status-bar app pause; they restore their display when it exits.
"""
from __future__ import annotations

import argparse
import colorsys
import hashlib
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .device_writer import (
    DeviceWriteError,
    hold_led_lease,
    resolve_target_path,
    validate_led_text,
    write_normalized_led_program,
)
from .led_status import led_count_for_target
from .settings import VISUALIZER_STYLES

HELPER_SOURCE = "apptap.swift"
HELPER_PLIST = "apptap-Info.plist"
BANDS = 8
STYLES = VISUALIZER_STYLES
TRANSPORTS = ("auto", "read2me", "file")
DEFAULT_COLOR = "#00E5FF"  # the app's Cyan profile color
HELPER_BUNDLE_ID = "io.sidepulse.apptap"
SILENCE_RMS = 1e-4
STALE_AUDIO_SECONDS = 0.5
AGC_DECAY_SECONDS = 6.0
AGC_FLOOR = 0.002
READ2ME_FPS = 20.0
FILE_FPS = 30.0


class VisualizerError(RuntimeError):
    pass


# --- audio helper -----------------------------------------------------------------


def helper_cache_dir() -> Path:
    return Path.home() / ".cache" / "sidepulse" / "apptap"


def ensure_helper(*, quiet: bool = False) -> Path:
    """Compile the Swift audio helper once per source revision and return its path."""
    package = resources.files("sidepulse.resources")
    source = package.joinpath(HELPER_SOURCE).read_bytes()
    plist = package.joinpath(HELPER_PLIST).read_bytes()
    digest = hashlib.sha256(source + b"\0" + plist).hexdigest()[:16]
    binary = helper_cache_dir() / digest / "apptap"
    if binary.exists():
        return binary

    swiftc = shutil.which("xcrun")
    if sys.platform != "darwin" or swiftc is None:
        raise VisualizerError("The audio helper needs macOS with Xcode or the Command Line Tools (xcode-select --install).")
    build = binary.parent
    build.mkdir(parents=True, exist_ok=True)
    (build / HELPER_SOURCE).write_bytes(source)
    (build / HELPER_PLIST).write_bytes(plist)
    if not quiet:
        print("Building the SidePulse audio helper (one time, ~30 s)...", file=sys.stderr)
    temporary = build / f"apptap.{os.getpid()}.partial"  # concurrent builds must not collide
    result = subprocess.run(
        [
            swiftc, "swiftc", "-O", "-swift-version", "5", str(build / HELPER_SOURCE), "-o", str(temporary),
            # Embed an Info.plist so macOS can show the audio-capture permission prompt.
            "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist",
            "-Xlinker", str(build / HELPER_PLIST),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = "\n".join(line for line in result.stderr.splitlines() if "error" in line)[:2000]
        raise VisualizerError(
            "Could not build the audio helper. Install Xcode or the Command Line Tools "
            f"(xcode-select --install).\n{detail or result.stderr[-2000:]}"
        )
    temporary.replace(binary)
    return binary


def list_audio_apps(helper: Path) -> list[tuple[bool, int, str, str]]:
    result = subprocess.run([str(helper), "--list"], capture_output=True, text=True, check=True)
    apps = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and parts[1].isdigit():
            if parts[2] == HELPER_BUNDLE_ID:
                continue  # a running visualizer's own capture helper, not a real audio app
            apps.append((parts[0] == "playing", int(parts[1]), parts[2], parts[3]))
    return apps


@dataclass
class AudioFrame:
    rms: float
    bands: list[float]
    received: float


def parse_helper_line(line: str) -> tuple[float, list[float]] | None:
    parts = line.split()
    if len(parts) != BANDS + 2 or parts[0] != "F":
        return None
    try:
        values = [float(value) for value in parts[1:]]
    except ValueError:
        return None
    return values[0], values[1:]


class AudioSource:
    """Runs the helper and keeps the most recent band levels."""

    def __init__(self, helper: Path, args: list[str]):
        self.process = subprocess.Popen(
            [str(helper), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self.lock = threading.Lock()
        self.latest = AudioFrame(0.0, [0.0] * BANDS, 0.0)
        self.messages: list[str] = []
        threading.Thread(target=self._read_stderr, daemon=True).start()
        header = self.process.stdout.readline().split()
        if len(header) != 3 or header[0] != "APPTAP":
            self.process.wait(timeout=5)
            detail = "\n".join(self.messages).strip() or f"exit status {self.process.returncode}"
            raise VisualizerError(f"Audio capture failed to start: {detail}")
        self.sample_rate = int(header[1])
        threading.Thread(target=self._read_stdout, daemon=True).start()

    def _read_stdout(self):
        for line in self.process.stdout:
            parsed = parse_helper_line(line)
            if parsed is not None:
                with self.lock:
                    self.latest = AudioFrame(parsed[0], parsed[1], time.monotonic())

    def _read_stderr(self):
        for line in self.process.stderr:
            self.messages.append(line.removeprefix("apptap: ").rstrip())

    def snapshot(self) -> AudioFrame:
        with self.lock:
            return self.latest

    def alive(self) -> bool:
        return self.process.poll() is None

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()


# --- analysis ---------------------------------------------------------------------


def group_bands(bands: list[float], count: int) -> list[float]:
    """Combine 8 bands into `count` LEDs (e.g. 2 for SidePulse Dot) by taking group maxima."""
    if count >= len(bands):
        return list(bands[:count])
    groups = []
    for index in range(count):
        start = index * len(bands) // count
        end = (index + 1) * len(bands) // count
        groups.append(max(bands[start:end]))
    return groups


class LevelTracker:
    """Per-channel automatic gain plus attack/release smoothing, all in 0..1."""

    def __init__(self, count: int, *, attack: float = 0.75, release: float = 0.22):
        self.peaks = [AGC_FLOOR] * count
        self.levels = [0.0] * count
        self.attack = attack
        self.release = release

    def update(self, values: list[float], interval: float, *, silent: bool) -> list[float]:
        decay = math.exp(-interval / AGC_DECAY_SECONDS)
        for index, value in enumerate(values):
            self.peaks[index] = max(value, self.peaks[index] * decay, AGC_FLOOR)
            target = 0.0 if silent else min(1.0, value / self.peaks[index])
            rate = self.attack if target > self.levels[index] else self.release
            self.levels[index] += (target - self.levels[index]) * rate
        return list(self.levels)


def parse_color(value: str) -> tuple[int, int, int]:
    text = value.strip().lstrip("#")
    if len(text) != 6:
        raise argparse.ArgumentTypeError(f"expected #RRGGBB, got {value!r}")
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected #RRGGBB, got {value!r}") from exc


def scale(rgb: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    amount = max(0.0, min(1.0, amount))
    return tuple(round(channel * amount) for channel in rgb)


def hsv(hue: float, value: float, saturation: float = 1.0) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, saturation, max(0.0, min(1.0, value)))
    return round(r * 255), round(g * 255), round(b * 255)


def perceptual(level: float) -> float:
    """Keep quiet bands dark so peaks read clearly."""
    return max(0.0, level) ** 1.6


def meter_rgb(index: int, count: int) -> tuple[int, int, int]:
    position = index / max(1, count - 1)
    if position <= 0.5:
        return round(255 * position / 0.5), 255, 0
    return 255, round(255 * (1 - (position - 0.5) / 0.5)), 0


def style_colors(style: str, levels: list[float], loudness: float, *, hue: float,
                 color: tuple[int, int, int]) -> list[tuple[int, int, int]]:
    count = len(levels)
    if style == "spectrum":
        span = 0.78  # red (bass) through violet (treble)
        return [hsv(hue + span * i / max(1, count - 1), perceptual(level)) for i, level in enumerate(levels)]
    if style == "mono":
        return [scale(color, perceptual(level)) for level in levels]
    if style == "pulse":
        bass = max(levels[: max(1, count // 4)])
        return [scale(color, 0.06 + 0.94 * perceptual(bass))] * count
    if style == "meter":
        fill = loudness * count
        return [scale(meter_rgb(i, count), 0.05 + 0.95 * max(0.0, min(1.0, fill - i))) for i in range(count)]
    raise ValueError(f"unknown style {style}")


def frame_program(colors: list[tuple[int, int, int]], fade_ms: int) -> str:
    """Compact positional frame. Fixed width: every frame has the same byte length,
    so file transport never changes the LEDS.LED directory entry size."""
    fade_ms = max(10, min(99, int(fade_ms)))
    return " ".join(f"#{r:02X}{g:02X}{b:02X}" for r, g, b in colors) + f" {fade_ms}ms"


def blank_program(count: int) -> str:
    return frame_program([(0, 0, 0)] * count, 99)


def read_restore_program(target: Path) -> str | None:
    """The program to put back on exit: whatever the normal writers last wrote.

    READ2ME streaming never changes LEDS.LED, and other writers are blocked by the
    lease, so the file still holds the pre-visualizer program. Writers that only
    rewrite on a state change compare against this file, so without restoring it
    explicitly the LEDs would stay dark after the visualizer exits.
    """
    try:
        program = target.read_text(encoding="utf-8")
        validate_led_text(program)
    except (OSError, UnicodeError, DeviceWriteError):
        return None
    return program


# --- transports -------------------------------------------------------------------


class FileSink:
    name = "file"

    def __init__(self, target: Path):
        self.target = target

    def send(self, program: str) -> None:
        write_normalized_led_program(program, device_path=self.target)

    def close(self) -> None:
        pass


class Read2MeSink:
    """Fire-and-forget READ2ME streaming: probe once, then send frames without
    waiting for acknowledgements. A lost frame is replaced by the next one."""

    name = "read2me"

    def __init__(self, root: Path):
        from ._vendor.read2me import Channel, frame

        self._frame = frame
        self.channel = Channel(root / "setup.html")
        try:
            self.channel.probe()
            self.transfer_id = self.channel.status()["transfer_id"]
        except BaseException:
            self.channel.close()
            raise

    def send(self, program: str) -> None:
        data = program.encode("ascii")
        self.transfer_id = self.transfer_id % 0xFFFFFFFF + 1
        self.channel.send_frame(self._frame(self.transfer_id, "LEDS.LED", len(data), 0, data))

    def close(self) -> None:
        self.channel.close()


class NullSink:
    name = "dry-run"

    def send(self, program: str) -> None:
        pass

    def close(self) -> None:
        pass


def open_sink(transport: str, target: Path, *, warn) -> FileSink | Read2MeSink:
    root = target.parent
    if transport == "file":
        return FileSink(target)
    if transport == "auto":
        try:
            from .readonly_control import read_device_info

            model, _ = read_device_info(root)
        except Exception:
            model = None
        if model != "Pro":
            return FileSink(target)
    try:
        return Read2MeSink(root)
    except Exception as exc:
        if transport == "read2me":
            raise VisualizerError(f"READ2ME streaming is unavailable: {exc}") from exc
        warn(f"READ2ME streaming unavailable ({exc}); falling back to file writes.")
        return FileSink(target)


# --- main loop --------------------------------------------------------------------


def terminal_line(colors: list[tuple[int, int, int]], levels: list[float]) -> str:
    blocks = " ▁▂▃▄▅▆▇█"
    cells = "".join(
        f"\033[38;2;{max(r, 50)};{max(g, 50)};{max(b, 50)}m{blocks[min(8, int(level * 8.999))] * 2}\033[0m"
        for (r, g, b), level in zip(colors, levels)
    )
    return f"\r{cells} "


def helper_args(args: argparse.Namespace) -> list[str]:
    if args.mic:
        return ["--mic"]
    if args.app:
        return list(args.app)
    return ["--all"]


def describe_source(args: argparse.Namespace) -> str:
    if args.mic:
        return "the microphone"
    if args.app:
        return ", ".join(args.app)
    return "all system audio"


def _stop_on_signal(_signum, _frame):
    raise KeyboardInterrupt


def run(args: argparse.Namespace) -> int:
    # Clean shutdown (blank frame, release the LED lease) on kill / terminal close too,
    # and on Ctrl-C even when started from a shell that ignores SIGINT in the background.
    import signal

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, _stop_on_signal)
    helper = ensure_helper()
    if args.list_apps:
        apps = list_audio_apps(helper)
        if not apps:
            print("No apps with audio found.")
        for playing, pid, bundle_id, name in apps:
            print(f"{'playing' if playing else '       '}  {name or '?':32}  {bundle_id or '-':44}  pid {pid}")
        print("\nUse a name or bundle ID fragment: sidepulse visualize --app spotify")
        return 0

    target = resolve_target_path(device_path=args.device)
    led_count = led_count_for_target(target)
    brightness = max(0.0, min(1.0, args.brightness / 100.0))
    show_meter = not args.quiet and sys.stdout.isatty()

    def warn(message: str) -> None:
        print(f"sidepulse visualize: {message}", file=sys.stderr)

    if args.dry_run:
        return _loop(args, helper, NullSink(), led_count, brightness, show_meter, warn, None)
    with hold_led_lease(target.parent, "sidepulse visualize"):
        restore = read_restore_program(target)
        sink = open_sink(args.transport, target, warn=warn)
        if sink.name == "file":
            warn("streaming with file writes; the device saves metadata to flash about twice a minute.")
        return _loop(args, helper, sink, led_count, brightness, show_meter, warn, restore)


class ControlChannel:
    """Live settings from a parent process over stdin (used by the menu-bar app).

    Lines look like `color #RRGGBB`. When stdin closes (the parent quit or was
    restarted) the visualizer stops, restoring the LEDs, instead of lingering.
    """

    def __init__(self, args: argparse.Namespace, stream=None):
        self.args = args
        self.stream = stream if stream is not None else sys.stdin
        self.closed = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            for line in self.stream:
                self.apply(line)
        except (OSError, ValueError):
            pass
        self.closed.set()

    def apply(self, line: str) -> None:
        parts = line.split()
        if len(parts) == 2 and parts[0] == "color":
            try:
                self.args.color = parse_color(parts[1])  # read by the main loop every frame
            except argparse.ArgumentTypeError:
                pass


def _loop(args, helper, sink, led_count, brightness, show_meter, warn, restore) -> int:
    fps = args.fps or (READ2ME_FPS if sink.name == "read2me" else FILE_FPS)
    interval = 1.0 / max(1.0, fps)
    fade_ms = int(interval * 1000 * 1.5)  # overlap frames so colors blend between updates
    source = AudioSource(helper, helper_args(args))
    for message in source.messages:
        warn(message)
    bands = LevelTracker(led_count)
    loudness = LevelTracker(1, attack=0.6, release=0.15)
    print(f"Visualizing {describe_source(args)} on {led_count} LEDs ({args.style}, {sink.name}, {fps:g} fps). "
          "Ctrl-C to stop.", file=sys.stderr)

    control = ControlChannel(args) if args.control_stdin else None
    last_program = None
    started = next_tick = time.monotonic()
    try:
        while True:
            if control is not None and control.closed.is_set():
                return 0
            if not source.alive():
                detail = "; ".join(source.messages[-3:]) or "audio helper exited"
                raise VisualizerError(detail)
            audio = source.snapshot()
            now = time.monotonic()
            silent = now - audio.received > STALE_AUDIO_SECONDS or audio.rms < SILENCE_RMS
            levels = bands.update(group_bands(audio.bands, led_count), interval, silent=silent)
            level = loudness.update([audio.rms], interval, silent=silent)[0]
            hue = args.hue + args.cycle * (now - started) / 60.0
            colors = style_colors(args.style, levels, level, hue=hue, color=args.color)
            colors = [scale(color, brightness) for color in colors]
            program = frame_program(colors, fade_ms)
            if program != last_program:
                sink.send(program)
                last_program = program
            if show_meter:
                sys.stdout.write(terminal_line(colors, levels))
                sys.stdout.flush()

            next_tick += interval
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()  # fell behind; don't burst to catch up
    except KeyboardInterrupt:
        return 0
    finally:
        source.close()
        try:
            sink.send(restore or blank_program(led_count))
        except Exception as exc:
            warn(f"could not restore the previous LED program: {exc}")
        sink.close()
        if show_meter:
            sys.stdout.write("\n")


def add_visualize_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "visualize",
        help="Turn live audio (all system audio, specific apps, or the mic) into an LED visualizer.",
        description=(
            "Music visualizer: splits live audio into frequency bands, one per LED. "
            "On SidePulse Pro, frames stream over READ2ME (no flash wear, ~20 fps). "
            "Normal status LEDs pause while it runs and come back when it exits."
        ),
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--app", action="append", metavar="NAME",
                        help="Only visualize apps whose name or bundle ID contains NAME (repeatable), e.g. spotify.")
    source.add_argument("--mic", action="store_true", help="Use the microphone instead of system audio.")
    source.add_argument("--list-apps", action="store_true", help="List apps that currently have audio and exit.")
    parser.add_argument("--style", choices=STYLES, default="spectrum",
                        help="spectrum: rainbow bands (default); mono: bands in one --color; "
                             "pulse: whole strip pulses with the bass; meter: loudness bar.")
    parser.add_argument("--color", type=parse_color, default=parse_color(DEFAULT_COLOR),
                        help=f"Color for mono and pulse styles. Default: {DEFAULT_COLOR}.")
    parser.add_argument("--hue", type=float, default=0.0, help="Spectrum start hue 0..1 (0 = red bass).")
    parser.add_argument("--cycle", type=float, default=0.0, help="Rotate spectrum hues, in rotations per minute.")
    parser.add_argument("--brightness", type=float, default=100.0, help="Brightness percent. Default: 100.")
    parser.add_argument("--transport", choices=TRANSPORTS, default="auto",
                        help="auto: READ2ME on SidePulse Pro, file writes otherwise. "
                             "file: smoother but causes periodic flash metadata saves.")
    parser.add_argument("--fps", type=float, help="Frame rate. Default: 20 (READ2ME) or 30 (file).")
    parser.add_argument("--device", type=Path, help="Mounted device folder. Default: auto-detect.")
    parser.add_argument("--quiet", action="store_true", help="No terminal meter.")
    parser.add_argument("--dry-run", action="store_true", help="Analyze audio and show the meter without touching the device.")
    # Internal: the menu-bar app sends live changes (e.g. `color #RRGGBB`) over stdin.
    parser.add_argument("--control-stdin", action="store_true", help=argparse.SUPPRESS)
    parser.set_defaults(func=cmd_visualize)


def cmd_visualize(args: argparse.Namespace) -> int:
    try:
        return run(args)
    except (VisualizerError, DeviceWriteError) as exc:
        print(f"sidepulse visualize: {exc}", file=sys.stderr)
        return 1
