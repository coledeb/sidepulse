from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sidepulse.collector import LiveAgentMonitor, SourceSpec
from sidepulse.models import AgentMode
from sidepulse.providers import parse_log_line


class ClaudeSessionIdleTests(unittest.TestCase):
    proc_start = "Wed Oct  1 01:02:03 2026"

    def test_process_identity_matches_utc_registry_across_local_timezones(self) -> None:
        from sidepulse.claude_session_state import process_matches

        utc_start = "Thu Oct  1 17:22:17 2026"

        def process_start(_command, **kwargs):
            # The observed registry uses UTC even when the desktop uses Phoenix.
            value = utc_start if kwargs["env"].get("TZ") == "UTC" else "Thu Oct  1 10:22:17 2026"
            return SimpleNamespace(returncode=0, stdout=value + "\n")

        with patch("sidepulse.claude_session_state.os.kill"), patch(
            "sidepulse.claude_session_state.subprocess.run", side_effect=process_start
        ), patch.dict(os.environ, {"TZ": "America/Phoenix"}):
            self.assertTrue(process_matches(123, utc_start))
            self.assertFalse(process_matches(123, "Wed Sep 30 17:22:17 2026"))

    def process_matches(self, result: bool = True):
        """Avoid process-table reads while keeping the metadata contract explicit."""
        try:
            from sidepulse import claude_session_state
        except ImportError:
            return nullcontext()
        return patch.object(claude_session_state, "process_matches", return_value=result)

    def event(
        self,
        name: str,
        session_id: str,
        timestamp: datetime,
        **fields: object,
    ):
        record = parse_log_line(
            "claude",
            json.dumps(
                {
                    "logged_at": timestamp.isoformat(),
                    "hook_event_name": name,
                    "session_id": session_id,
                    "cwd": "/tmp/project",
                    **fields,
                }
            ),
        )
        self.assertIsNotNone(record)
        return record

    def milliseconds(self, value: datetime) -> datetime:
        return datetime.fromtimestamp(
            int(value.timestamp() * 1000) / 1000,
            tz=timezone.utc,
        )

    def write_session(
        self,
        directory: Path,
        session_id: str,
        *,
        status: str = "idle",
        status_at: datetime,
        pid: int | None = None,
        proc_start: str | None = None,
        kind: str = "interactive",
        raw: object | None = None,
    ) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{pid if pid is not None else os.getpid()}.json"
        value = raw if raw is not None else {
            "sessionId": session_id,
            "pid": os.getpid() if pid is None else pid,
            "procStart": self.proc_start if proc_start is None else proc_start,
            "kind": kind,
            "status": status,
            "statusUpdatedAt": int(status_at.timestamp() * 1000),
        }
        path.write_text(json.dumps(value) + "\n")
        return path

    def monitor(self, root: Path, latest: Path | None = None) -> LiveAgentMonitor:
        return LiveAgentMonitor(
            sources=(SourceSpec("event-bus", root / "events.sock"),),
            stale_after_seconds=3600,
            latest_state_path=latest,
        )

    def status(self, monitor: LiveAgentMonitor):
        snapshot = monitor.snapshot()
        statuses = snapshot.statuses + snapshot.stale_statuses
        self.assertEqual(len(statuses), 1)
        return statuses[0]

    def test_old_parent_prompt_is_reconciled_to_live_idle_session(self) -> None:
        """The reported Moshi/registry reproduction must clear stale Working."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now(timezone.utc)
            prompt_at = now - timedelta(seconds=6)
            idle_at = self.milliseconds(now - timedelta(seconds=1))
            session_id = "parent-idle"
            self.write_session(root / ".claude" / "sessions", session_id, status_at=idle_at)

            with self.process_matches(), patch("sidepulse.collector.Path.home", return_value=root), patch("sidepulse.claude_session_state.Path.home", return_value=root), patch.dict(
                os.environ, {}, clear=True
            ):
                monitor = self.monitor(root, root / "latest.json")
                monitor.ingest_record(self.event("UserPromptSubmit", session_id, prompt_at))
                status = self.status(monitor)

            self.assertEqual(status.mode, AgentMode.IDLE_READY)
            self.assertEqual(status.event_name, "SessionIdle")
            self.assertEqual(status.updated_at, idle_at)

    def test_persisted_session_idle_survives_registry_cleanup_and_recovery_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now(timezone.utc)
            prompt_at = now - timedelta(seconds=8)
            idle_at = self.milliseconds(now - timedelta(seconds=2))
            session_id = "restart-idle"
            sessions = root / ".claude" / "sessions"
            registry = self.write_session(sessions, session_id, status_at=idle_at)
            latest = root / "latest.json"
            log = root / "claude.jsonl"
            record = self.event("UserPromptSubmit", session_id, prompt_at)
            log.write_text(json.dumps(record.raw | {"logged_at": record.logged_at.isoformat()}) + "\n")

            with self.process_matches(), patch("sidepulse.collector.Path.home", return_value=root), patch("sidepulse.claude_session_state.Path.home", return_value=root), patch.dict(
                os.environ, {}, clear=True
            ):
                monitor = self.monitor(root, latest)
                monitor.ingest_record(record)
                first = self.status(monitor)
                self.assertEqual(first.mode, AgentMode.IDLE_READY)
                self.assertGreater(first.updated_at, prompt_at)
                registry.unlink()

                restarted = LiveAgentMonitor(
                    recovery_sources=(SourceSpec("claude", log),),
                    stale_after_seconds=3600,
                    latest_state_path=latest,
                )
                restored = self.status(restarted)

            self.assertEqual(restored.mode, AgentMode.IDLE_READY)
            self.assertEqual(restored.event_name, "SessionIdle")
            self.assertEqual(restored.updated_at, idle_at)

    def test_newer_prompt_is_not_cleared_by_an_older_idle_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now(timezone.utc)
            session_id = "newer-prompt"
            self.write_session(
                root / ".claude" / "sessions",
                session_id,
                status_at=now - timedelta(seconds=8),
            )
            with self.process_matches(), patch("sidepulse.collector.Path.home", return_value=root), patch("sidepulse.claude_session_state.Path.home", return_value=root), patch.dict(
                os.environ, {}, clear=True
            ):
                monitor = self.monitor(root)
                monitor.ingest_record(
                    self.event("UserPromptSubmit", session_id, now - timedelta(seconds=6))
                )
                status = self.status(monitor)
            self.assertEqual(status.mode, AgentMode.WORKING)
            self.assertEqual(status.event_name, "UserPromptSubmit")

    def test_registry_validation_and_fresh_prompt_leave_working_untouched(self) -> None:
        cases = (
            ("old", {"status_at": datetime.now(timezone.utc) - timedelta(seconds=60)}),
            ("equal", {"status_at": datetime.now(timezone.utc)}),
            ("future", {"status_at": datetime.now(timezone.utc) + timedelta(seconds=60)}),
            ("active", {"status": "active", "status_at": datetime.now(timezone.utc)}),
            ("busy", {"status": "busy", "status_at": datetime.now(timezone.utc)}),
            ("dead", {"pid": 999_999, "status_at": datetime.now(timezone.utc)}),
            ("noninteractive", {"kind": "background", "status_at": datetime.now(timezone.utc)}),
            ("invalid", {"raw": {"not": "session metadata"}, "status_at": datetime.now(timezone.utc)}),
            ("fresh", {"status_at": datetime.now(timezone.utc)}),
        )
        for label, details in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                now = datetime.now(timezone.utc)
                prompt_at = now - timedelta(seconds=3 if label == "fresh" else 6)
                session_id = f"not-idle-{label}"
                if label == "equal":
                    details = {"status_at": prompt_at}
                self.write_session(root / ".claude" / "sessions", session_id, **details)
                with self.process_matches(result=label != "dead"), patch("sidepulse.collector.Path.home", return_value=root), patch("sidepulse.claude_session_state.Path.home", return_value=root), patch.dict(
                    os.environ, {}, clear=True
                ):
                    monitor = self.monitor(root)
                    monitor.ingest_record(self.event("UserPromptSubmit", session_id, prompt_at))
                    status = self.status(monitor)
                self.assertEqual(status.mode, AgentMode.WORKING)
                self.assertEqual(status.event_name, "UserPromptSubmit")

    def test_matching_busy_or_unknown_metadata_blocks_idle_reconciliation(self) -> None:
        for state in ("busy", "unknown"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                now = datetime.now(timezone.utc)
                prompt_at = now - timedelta(seconds=6)
                session_id = f"matched-{state}"
                self.write_session(
                    root / ".claude" / "sessions",
                    session_id,
                    status=state,
                    status_at=now - timedelta(seconds=1),
                )
                with self.process_matches(), patch("sidepulse.collector.Path.home", return_value=root), patch("sidepulse.claude_session_state.Path.home", return_value=root), patch.dict(
                    os.environ, {}, clear=True
                ):
                    monitor = self.monitor(root)
                    monitor.ingest_record(self.event("UserPromptSubmit", session_id, prompt_at))
                    status = self.status(monitor)
                self.assertEqual(status.mode, AgentMode.WORKING)

    def test_contradictory_matching_session_metadata_blocks_idle_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now(timezone.utc)
            session_id = "contradictory"
            self.write_session(
                root / ".claude" / "sessions",
                session_id,
                status="idle",
                status_at=now - timedelta(seconds=1),
            )
            self.write_session(
                root / ".claude-personal" / "sessions",
                session_id,
                status="busy",
                status_at=now - timedelta(seconds=1),
            )
            with self.process_matches(), patch("sidepulse.collector.Path.home", return_value=root), patch("sidepulse.claude_session_state.Path.home", return_value=root), patch.dict(
                os.environ, {}, clear=True
            ):
                monitor = self.monitor(root)
                monitor.ingest_record(
                    self.event("UserPromptSubmit", session_id, now - timedelta(seconds=6))
                )
                status = self.status(monitor)
            self.assertEqual(status.mode, AgentMode.WORKING)

    def test_new_prompt_during_registry_read_wins_over_confirmed_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now(timezone.utc)
            session_id = "registry-race"
            old_prompt_at = now - timedelta(seconds=6)
            idle_at = self.milliseconds(now - timedelta(seconds=1))
            with self.process_matches(), patch("sidepulse.collector.Path.home", return_value=root), patch("sidepulse.claude_session_state.Path.home", return_value=root), patch.dict(
                os.environ, {}, clear=True
            ):
                monitor = self.monitor(root)
                monitor.ingest_record(self.event("UserPromptSubmit", session_id, old_prompt_at))

                def prompt_arrives(*args, **kwargs):
                    monitor.ingest_record(
                        self.event("UserPromptSubmit", session_id, now)
                    )
                    return idle_at

                with patch("sidepulse.collector.confirmed_idle_at", side_effect=prompt_arrives):
                    status = self.status(monitor)
            self.assertEqual(status.mode, AgentMode.WORKING)
            self.assertEqual(status.event_name, "UserPromptSubmit")
            self.assertEqual(status.updated_at, now)

    def test_proc_start_mismatch_blocks_an_otherwise_valid_idle_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now(timezone.utc)
            session_id = "pid-reused"
            self.write_session(
                root / ".claude" / "sessions",
                session_id,
                status_at=now - timedelta(seconds=1),
                proc_start="wrong process start",
            )
            with self.process_matches(False) as matches, patch("sidepulse.collector.Path.home", return_value=root), patch("sidepulse.claude_session_state.Path.home", return_value=root), patch.dict(
                os.environ, {}, clear=True
            ):
                monitor = self.monitor(root)
                monitor.ingest_record(
                    self.event("UserPromptSubmit", session_id, now - timedelta(seconds=6))
                )
                status = self.status(monitor)
            self.assertEqual(status.mode, AgentMode.WORKING)
            self.assertEqual(status.event_name, "UserPromptSubmit")
            matches.assert_called_once_with(os.getpid(), "wrong process start")

    def test_only_parent_claude_prompt_is_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now(timezone.utc)
            old = now - timedelta(seconds=6)
            self.write_session(root / ".claude" / "sessions", "shared", status_at=now - timedelta(seconds=1))
            with self.process_matches(), patch("sidepulse.collector.Path.home", return_value=root), patch("sidepulse.claude_session_state.Path.home", return_value=root), patch.dict(
                os.environ, {}, clear=True
            ):
                for provider, name, fields in (
                    ("claude", "PreToolUse", {}),
                    ("claude", "PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": "x"}}),
                    ("claude", "UserPromptSubmit", {"agent_id": "child"}),
                    ("codex", "UserPromptSubmit", {}),
                ):
                    record = parse_log_line(
                        provider,
                        json.dumps({"logged_at": old.isoformat(), "hook_event_name": name, "session_id": "shared", "cwd": "/tmp/project", **fields}),
                    )
                    self.assertIsNotNone(record)
                    monitor = self.monitor(root)
                    monitor.ingest_record(record)
                    status = self.status(monitor)
                    self.assertNotEqual(status.event_name, "SessionIdle")
                    self.assertNotEqual(status.mode, AgentMode.IDLE_READY)

    def test_default_personal_work_and_custom_session_roots_are_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            custom = root / "custom-claude"
            now = datetime.now(timezone.utc)
            roots = (
                ("default", root / ".claude" / "sessions", {}),
                ("personal", root / ".claude-personal" / "sessions", {}),
                ("work", root / ".claude-work" / "sessions", {}),
                ("custom", custom / "sessions", {"CLAUDE_CONFIG_DIR": str(custom)}),
            )
            for label, sessions, environment in roots:
                with self.subTest(root=label):
                    session_id = f"root-{label}"
                    self.write_session(sessions, session_id, status_at=now - timedelta(seconds=1))
                    with self.process_matches(), patch("sidepulse.collector.Path.home", return_value=root), patch("sidepulse.claude_session_state.Path.home", return_value=root), patch.dict(
                        os.environ, environment, clear=True
                    ):
                        monitor = self.monitor(root)
                        monitor.ingest_record(
                            self.event("UserPromptSubmit", session_id, now - timedelta(seconds=6))
                        )
                        status = self.status(monitor)
                    self.assertEqual(status.mode, AgentMode.IDLE_READY)
                    self.assertEqual(status.event_name, "SessionIdle")


if __name__ == "__main__":
    unittest.main()
