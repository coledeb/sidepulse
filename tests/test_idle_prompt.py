from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sidepulse.collector import AgentMonitor, LiveAgentMonitor, SourceSpec, status_from_event
from sidepulse.models import AgentMode
from sidepulse.providers import parse_log_line


class ClaudeIdlePromptTests(unittest.TestCase):
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

    def idle_notification(self, session_id: str, timestamp: datetime):
        return self.event(
            "Notification",
            session_id,
            timestamp,
            notification_type="idle_prompt",
            message="Claude is waiting for your input",
        )

    def replay(self, records: list[object]):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "claude.jsonl"
            log.write_text(
                "\n".join(json.dumps(record.raw | {"logged_at": record.logged_at.isoformat()}) for record in records)
                + "\n"
            )
            return AgentMonitor(
                sources=(SourceSpec("claude", log),),
                stale_after_seconds=3600,
            ).snapshot()

    def test_live_start_then_repeated_idle_prompts_remains_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            session_id = "fresh-claude-session"
            monitor = LiveAgentMonitor(
                latest_state_path=Path(tmp) / "latest.json",
                stale_after_seconds=3600,
            )

            monitor.ingest_record(self.event("SessionStart", session_id, now))
            monitor.ingest_record(self.idle_notification(session_id, now + timedelta(seconds=1)))
            monitor.ingest_record(self.idle_notification(session_id, now + timedelta(seconds=2)))

            snapshot = monitor.snapshot()
            self.assertEqual(snapshot.aggregate.mode, AgentMode.IDLE_READY)
            self.assertEqual(snapshot.aggregate.active_count, 0)
            self.assertEqual(snapshot.statuses[0].mode, AgentMode.IDLE_READY)
            self.assertEqual(snapshot.statuses[0].updated_at, now + timedelta(seconds=2))

    def test_replay_start_then_repeated_idle_prompts_remains_inactive(self) -> None:
        now = datetime.now(timezone.utc)
        session_id = "replayed-claude-session"
        snapshot = self.replay(
            [
                self.event("SessionStart", session_id, now),
                self.idle_notification(session_id, now + timedelta(seconds=1)),
                self.idle_notification(session_id, now + timedelta(seconds=2)),
            ]
        )

        self.assertEqual(snapshot.aggregate.mode, AgentMode.IDLE_READY)
        self.assertEqual(snapshot.aggregate.active_count, 0)
        self.assertEqual(snapshot.statuses[0].updated_at, now + timedelta(seconds=2))

    def test_recovery_replaces_same_timestamp_persisted_ask_with_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            latest = base / "latest.json"
            log = base / "claude.jsonl"
            now = datetime.now(timezone.utc) - timedelta(seconds=5)
            session_id = "recovered-claude-session"
            notification_at = now + timedelta(seconds=1)
            latest.write_text(
                json.dumps(
                    {
                        "updated_at": notification_at.isoformat(),
                        "statuses": [
                            {
                                "provider": "claude",
                                "agent_id": f"claude:session:{session_id}",
                                "display_name": "project: stale ask",
                                "mode": AgentMode.WAITING_FOR_INPUT.value,
                                "updated_at": notification_at.isoformat(),
                                "event_name": "Notification",
                                "session_id": session_id,
                                "cwd": "/tmp/project",
                            }
                        ],
                    }
                )
                + "\n"
            )
            records = [
                self.event("SessionStart", session_id, now),
                self.idle_notification(session_id, notification_at),
            ]
            log.write_text(
                "\n".join(json.dumps(record.raw | {"logged_at": record.logged_at.isoformat()}) for record in records)
                + "\n"
            )

            monitor = LiveAgentMonitor(
                recovery_sources=(SourceSpec("claude", log),),
                latest_state_path=latest,
                stale_after_seconds=3600,
            )

            snapshot = monitor.snapshot()
            self.assertEqual(snapshot.aggregate.mode, AgentMode.IDLE_READY)
            self.assertEqual(snapshot.aggregate.active_count, 0)
            persisted = json.loads(latest.read_text())["statuses"][0]
            self.assertEqual(persisted["mode"], AgentMode.IDLE_READY.value)
            self.assertEqual(persisted["updated_at"], notification_at.isoformat())

    def test_idle_prompt_after_real_work_stays_ask(self) -> None:
        now = datetime.now(timezone.utc)
        session_id = "working-claude-session"
        snapshot = self.replay(
            [
                self.event("SessionStart", session_id, now),
                self.event("UserPromptSubmit", session_id, now + timedelta(seconds=1)),
                self.idle_notification(session_id, now + timedelta(seconds=2)),
            ]
        )

        self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)
        self.assertEqual(snapshot.aggregate.active_count, 1)

    def test_permission_prompt_permission_request_and_explicit_ask_are_not_suppressed(self) -> None:
        now = datetime.now(timezone.utc)
        session_id = "asking-claude-session"
        permission = self.event(
            "PermissionRequest",
            session_id,
            now + timedelta(seconds=1),
            tool_name="Bash",
            tool_input={"command": "safe-command"},
        )
        explicit = self.event(
            "Notification",
            session_id,
            now + timedelta(seconds=2),
            notification_type="idle_prompt",
            message="Claude is waiting for your input",
            sidepulse_mode="ask",
        )
        permission_prompt = self.event(
            "Notification",
            session_id,
            now + timedelta(seconds=3),
            notification_type="permission_prompt",
            message="Claude is waiting for your input",
        )

        permission_snapshot = self.replay([self.event("SessionStart", session_id, now), permission])
        explicit_snapshot = self.replay([self.event("SessionStart", session_id, now), explicit])
        permission_prompt_snapshot = self.replay(
            [self.event("SessionStart", session_id, now), permission_prompt]
        )

        self.assertEqual(permission_snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)
        self.assertEqual(explicit_snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)
        self.assertEqual(permission_prompt_snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)

    def test_non_idle_input_notification_after_start_stays_ask(self) -> None:
        now = datetime.now(timezone.utc)
        session_id = "unknown-notification-session"
        snapshot = self.replay(
            [
                self.event("SessionStart", session_id, now),
                self.event(
                    "Notification",
                    session_id,
                    now + timedelta(seconds=1),
                    notification_type="attention_needed",
                    message="Claude is waiting for your input",
                ),
            ]
        )

        self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)

    def test_idle_prompt_without_a_prior_status_stays_ask(self) -> None:
        record = self.idle_notification("no-prior-session", datetime.now(timezone.utc))

        status = status_from_event(record)

        self.assertIsNotNone(status)
        self.assertEqual(status.mode, AgentMode.WAITING_FOR_INPUT)


if __name__ == "__main__":
    unittest.main()
