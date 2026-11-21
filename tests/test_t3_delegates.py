from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sidepulse.models import HookEvent
from sidepulse.t3_delegates import T3DelegateLookup

NOW = datetime(2026, 11, 14, 22, 0, tzinfo=timezone.utc)


def event(session_id: str, *, origin: str = "OpenCode in T3 Code", thread_id: str | None = None,
          provider: str = "opencode") -> HookEvent:
    raw = {"hook_event_name": "Stop"}
    if thread_id:
        raw["t3code_thread_id"] = thread_id
    return HookEvent(provider=provider, logged_at=NOW, event_name="Stop", raw=raw,
                     session_id=session_id, origin=origin)


class T3DelegateLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "statev2.sqlite"
        self.connection = sqlite3.connect(self.db)
        self.addCleanup(self.connection.close)
        self.connection.executescript(
            "CREATE TABLE orchestration_v2_projection_threads (thread_id TEXT PRIMARY KEY, payload_json TEXT);"
            "CREATE TABLE orchestration_v2_projection_provider_threads"
            " (provider_thread_id TEXT PRIMARY KEY, thread_id TEXT, updated_at TEXT, payload_json TEXT);"
        )
        self.clock = 0.0
        self.lookup = T3DelegateLookup(self.db, monotonic=lambda: self.clock)

    def add_thread(self, thread_id: str, native_id: str | None, **payload) -> None:
        payload.setdefault("lineage", {"parentThreadId": None, "relationshipToParent": None})
        self.connection.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?, ?)",
                                (thread_id, json.dumps(payload)))
        if native_id:
            self.connection.execute(
                "INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?, ?)",
                (f"pt:{thread_id}", thread_id, "2026-11-14T22:00:00Z",
                 json.dumps({"nativeThreadRef": {"nativeId": native_id}})),
            )
        self.connection.commit()

    def test_remote_and_local_delegates_are_hidden(self) -> None:
        self.add_thread("mcp:launch:remote", "ses_remote",
                        delegatedFrom={"environmentId": "mini", "threadId": "p", "title": "Parent"})
        self.add_thread("thread:delegated-task:x", "ses_local",
                        lineage={"parentThreadId": "p", "relationshipToParent": "subagent"})
        self.assertTrue(self.lookup.is_delegate(event("ses_remote")))
        self.assertTrue(self.lookup.is_delegate(event("ses_local")))

    def test_codex_transcript_records_of_a_delegate_are_hidden_too(self) -> None:
        """Rollout records leave origin empty; the hook's annotation is in the payload."""
        self.add_thread("mcp:launch:codex", "codex-session",
                        delegatedFrom={"environmentId": "mini", "threadId": "p", "title": "Parent"})
        transcript = HookEvent(provider="codex", logged_at=NOW, event_name="Stop",
                               raw={"hook_event_name": "Stop", "agent_origin": "Codex in T3 Code"},
                               session_id="codex-session", origin=None)
        self.assertTrue(self.lookup.is_delegate(transcript))
        bare = HookEvent(provider="codex", logged_at=NOW, event_name="Stop", raw={},
                         session_id="codex-session", origin=None)
        # Once known, a record that names no host at all is hidden as well.
        self.assertTrue(self.lookup.is_delegate(bare))

    def test_requested_launches_forks_and_user_threads_show(self) -> None:
        self.add_thread("mcp:launch:asked", "ses_asked", delegatedFrom=None)
        self.add_thread("fork", "ses_fork", lineage={"parentThreadId": "p", "relationshipToParent": "fork"})
        self.add_thread("user", "ses_user")
        for session in ("ses_asked", "ses_fork", "ses_user"):
            with self.subTest(session=session):
                self.assertFalse(self.lookup.is_delegate(event(session)))

    def test_thread_id_from_the_hook_is_used_first(self) -> None:
        self.add_thread("mcp:launch:claude", None, delegatedFrom={"threadId": "p"})
        record = event("claude-session", origin="Claude in T3 Code", thread_id="mcp:launch:claude",
                       provider="claude")
        self.assertTrue(self.lookup.is_delegate(record))

    def test_only_local_t3_rows_are_checked(self) -> None:
        self.add_thread("mcp:launch:remote", "ses_remote", delegatedFrom={"threadId": "p"})
        self.assertFalse(self.lookup.is_delegate(event("ses_remote", origin="OpenCode")))
        self.assertFalse(self.lookup.is_delegate(event("ses_remote", origin="OpenCode in T3 Code · KARLY")))

    def test_unknown_session_is_rechecked_after_a_wait(self) -> None:
        record = event("ses_late")
        self.assertFalse(self.lookup.is_delegate(record))
        self.add_thread("mcp:launch:late", "ses_late", delegatedFrom={"threadId": "p"})
        self.assertFalse(self.lookup.is_delegate(record))  # still within the wait
        self.clock += 6
        self.assertTrue(self.lookup.is_delegate(record))

    def test_missing_or_unreadable_database_hides_nothing(self) -> None:
        self.assertFalse(T3DelegateLookup(Path(self.tmp.name) / "none.sqlite").is_delegate(event("s")))
        broken = Path(self.tmp.name) / "broken.sqlite"
        broken.write_text("not a database")
        self.assertFalse(T3DelegateLookup(broken).is_delegate(event("s")))

    def test_monitor_drops_a_delegate_row_once_t3_records_it(self) -> None:
        from sidepulse.collector import LiveAgentMonitor

        monitor = LiveAgentMonitor(latest_state_path=Path(self.tmp.name) / "latest.json", recover_on_init=False)
        monitor.t3_delegates = self.lookup
        prompt = HookEvent(provider="opencode", logged_at=NOW, event_name="UserPromptSubmit",
                           raw={"hook_event_name": "UserPromptSubmit", "prompt": "Act as..."},
                           session_id="ses_child", origin="OpenCode in T3 Code")
        monitor.ingest_record(prompt)
        self.assertIn("opencode:session:ses_child", monitor.statuses_by_key)
        self.add_thread("mcp:launch:child", "ses_child", delegatedFrom={"threadId": "p"})
        self.clock += 6
        monitor.ingest_record(event("ses_child"))
        self.assertNotIn("opencode:session:ses_child", monitor.statuses_by_key)

    def test_saved_delegate_rows_go_on_restart(self) -> None:
        from sidepulse import t3_status
        from sidepulse.collector import LiveAgentMonitor

        latest = Path(self.tmp.name) / "latest.json"
        first = LiveAgentMonitor(latest_state_path=latest, recover_on_init=False)
        first.t3_delegates = T3DelegateLookup(Path(self.tmp.name) / "none.sqlite")
        first.ingest_record(event("ses_child"))
        remote = HookEvent(provider="opencode", logged_at=NOW, event_name=t3_status.T3_EVENT_NAME,
                           raw={"hook_event_name": t3_status.T3_EVENT_NAME, "sidepulse_status": "completed"},
                           session_id="mcp:launch:remote", origin="OpenCode in T3 Code · KARLY")
        first.ingest_record(remote)
        self.assertEqual(
            sorted(first.statuses_by_key),
            ["opencode:session:mcp:launch:remote", "opencode:session:ses_child"],
        )

        self.add_thread("mcp:launch:child", "ses_child", delegatedFrom={"threadId": "p"})
        import sidepulse.collector as collector
        original = collector.T3DelegateLookup
        collector.T3DelegateLookup = lambda: self.lookup
        try:
            second = LiveAgentMonitor(latest_state_path=latest, recover_on_init=False)
        finally:
            collector.T3DelegateLookup = original
        self.assertEqual(sorted(second.statuses_by_key), ["opencode:session:mcp:launch:remote"])


if __name__ == "__main__":
    unittest.main()
