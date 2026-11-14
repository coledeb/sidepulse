from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
from unittest.mock import patch

from sidepulse.collector import AGENT_PROCESS_CHECK_SECONDS, LiveAgentMonitor, SourceSpec
from sidepulse.models import AgentMode
from sidepulse.origin import (
    AGENT_PID_KEY,
    AGENT_PID_STARTED_KEY,
    PROCESS_ALIVE,
    PROCESS_GONE,
    PROCESS_UNKNOWN,
    ProcessInfo,
    annotate_payload_with_origin,
    process_state,
)
from sidepulse.providers import parse_log_line

STARTED = "Fri Nov 13 23:20:00 2026"
AGENT = ProcessInfo(pid=4242, ppid=1, comm="opencode", command="opencode run hi", started=STARTED)


class ExitedAgentProcessTests(unittest.TestCase):
    """A session whose agent process is gone stops counting as working."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.monitor = LiveAgentMonitor(
            sources=(SourceSpec("event-bus", Path(self.tmp.name) / "events.sock"),),
            stale_after_seconds=3600,
        )
        self.checks: list[tuple[int, str]] = []
        self.state = PROCESS_GONE

        def process_state(pid: int, started: str) -> str:
            self.checks.append((pid, started))
            return self.state

        self.monitor.process_state = process_state

    def ingest(self, name: str, *, provider: str = "opencode", session: str = "ses_1", **fields) -> None:
        payload = {
            "hook_event_name": name,
            "session_id": session,
            "cwd": "/tmp",
            "timestamp": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            **fields,
        }
        self.monitor.ingest_record(parse_log_line(provider, json.dumps(payload)))

    def only_status(self):
        snapshot = self.monitor.snapshot(with_extra=False)
        statuses = snapshot.statuses + snapshot.stale_statuses
        self.assertEqual(len(statuses), 1)
        return statuses[0]

    def with_pid(self, **fields) -> dict:
        return {AGENT_PID_KEY: 4242, AGENT_PID_STARTED_KEY: STARTED, **fields}

    def test_a_working_session_whose_process_exited_goes_idle(self) -> None:
        self.ingest("UserPromptSubmit", prompt="hi", **self.with_pid())
        status = self.only_status()
        self.assertEqual(status.mode, AgentMode.IDLE_READY)
        self.assertEqual(status.event_name, "ProcessExited")
        self.assertEqual(self.checks, [(4242, STARTED)])
        self.assertEqual(self.monitor.snapshot(with_extra=False).aggregate.active_count, 0)

    def test_a_live_or_uncheckable_process_keeps_the_session_working(self) -> None:
        for state in (PROCESS_ALIVE, PROCESS_UNKNOWN):
            with self.subTest(state=state):
                self.state = state
                self.ingest("UserPromptSubmit", session=f"ses_{state}", prompt="hi", **self.with_pid())
                snapshot = self.monitor.snapshot(with_extra=False)
                modes = {status.session_id: status.mode for status in snapshot.statuses}
                self.assertEqual(modes[f"ses_{state}"], AgentMode.WORKING)

    def test_each_row_is_checked_at_most_once_per_interval(self) -> None:
        self.state = PROCESS_ALIVE
        self.ingest("UserPromptSubmit", prompt="hi", **self.with_pid())
        self.monitor.snapshot(with_extra=False)
        self.monitor.snapshot(with_extra=False)
        self.assertEqual(len(self.checks), 1)
        with patch("sidepulse.collector.time.monotonic", return_value=10**9):
            self.monitor.snapshot(with_extra=False)
        self.assertEqual(len(self.checks), 2)
        self.assertGreater(AGENT_PROCESS_CHECK_SECONDS, 0)

    def test_finished_sessions_are_not_checked(self) -> None:
        self.ingest("UserPromptSubmit", prompt="hi", **self.with_pid())
        self.ingest("Stop", last_assistant_message="done")
        self.assertEqual(self.only_status().mode, AgentMode.COMPLETED)
        self.assertEqual(self.checks, [])

    def test_a_later_hook_without_a_pid_keeps_the_known_process(self) -> None:
        self.state = PROCESS_ALIVE
        self.ingest("UserPromptSubmit", prompt="hi", **self.with_pid())
        self.ingest("PreToolUse", tool_name="shell")
        self.monitor.snapshot(with_extra=False)
        self.assertEqual(self.checks, [(4242, STARTED)])

    def test_sessions_without_a_pid_or_from_another_computer_are_left_alone(self) -> None:
        self.ingest("UserPromptSubmit", session="ses_plain", prompt="hi")
        self.ingest(
            "UserPromptSubmit",
            session="ses_remote",
            prompt="hi",
            agent_origin="OpenCode · KARLY",
            **self.with_pid(),
        )
        snapshot = self.monitor.snapshot(with_extra=False)
        self.assertEqual({status.mode for status in snapshot.statuses}, {AgentMode.WORKING})
        self.assertEqual(len(snapshot.statuses), 2)
        self.assertEqual(self.checks, [])


class HookRecordsAgentProcessTests(unittest.TestCase):
    def annotate(self, provider: str, payload: dict | None = None) -> dict:
        with patch("sidepulse.origin.agent_process", return_value=AGENT), patch(
            "sidepulse.origin.process_ancestry", return_value=()
        ):
            return annotate_payload_with_origin(provider, payload or {"hook_event_name": "Stop"}, env={})

    def test_known_agents_record_their_process(self) -> None:
        for provider in ("claude", "codex", "opencode"):
            with self.subTest(provider=provider):
                result = self.annotate(provider)
                self.assertEqual(result[AGENT_PID_KEY], 4242)
                self.assertEqual(result[AGENT_PID_STARTED_KEY], STARTED)

    def test_other_agents_and_relayed_payloads_do_not(self) -> None:
        self.assertNotIn(AGENT_PID_KEY, self.annotate("cursor"))
        relayed = self.annotate("opencode", {"hook_event_name": "Stop", "agent_origin": "OpenCode · KARLY"})
        self.assertNotIn(AGENT_PID_KEY, relayed)


class ProcessStateTests(unittest.TestCase):
    def state(self, kill_error: BaseException | None, lstart: str) -> str:
        completed = subprocess.CompletedProcess([], 0, stdout=lstart + "\n")
        with patch("sidepulse.origin.os.kill", side_effect=kill_error), patch(
            "sidepulse.origin.subprocess.run", return_value=completed
        ):
            return process_state(4242, STARTED)

    def test_another_users_process_is_judged_by_its_start_time(self) -> None:
        # Hooks replayed or run under sudo record a root-owned ancestor.
        self.assertEqual(self.state(PermissionError(), STARTED), PROCESS_ALIVE)
        self.assertEqual(self.state(PermissionError(), "Sat Nov 14 01:00:00 2026"), PROCESS_GONE)

    def test_a_missing_pid_is_gone(self) -> None:
        self.assertEqual(self.state(ProcessLookupError(), STARTED), PROCESS_GONE)


if __name__ == "__main__":
    unittest.main()
