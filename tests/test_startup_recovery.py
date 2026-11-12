from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sidepulse.collector import AgentMonitor, LiveAgentMonitor, SourceSpec, status_from_event
from sidepulse.models import AgentMode, HookEvent


class StartupRecoveryTests(unittest.TestCase):
    def test_recovery_updates_cached_state_with_equal_timestamp(self):
        now = datetime.now(timezone.utc)
        monitor = LiveAgentMonitor(
            recovery_sources=(SourceSpec("codex", Path("unused.jsonl")),),
            recover_on_init=False,
        )
        monitor.ingest_record(HookEvent(
            "codex", now, "UserPromptSubmit", {}, session_id="cached",
        ))
        recovered = status_from_event(HookEvent(
            "codex", now, "Stop", {}, session_id="cached",
        ))
        with patch.object(AgentMonitor, "_latest_statuses", return_value={recovered.agent_id: recovered}):
            monitor.reconcile_recovery_sources()
        self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.COMPLETED)

    def test_deferred_recovery_loads_cached_state_without_reading_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest = Path(tmp) / "latest.json"
            original = LiveAgentMonitor(latest_state_path=latest)
            original.ingest_record(HookEvent(
                "codex", datetime.now(timezone.utc), "UserPromptSubmit", {},
                session_id="cached",
            ))
            with patch.object(AgentMonitor, "_latest_statuses") as recover:
                monitor = LiveAgentMonitor(
                    latest_state_path=latest,
                    recovery_sources=(SourceSpec("codex", Path(tmp) / "hook.jsonl"),),
                    recover_on_init=False,
                )
                recover.assert_not_called()
            self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.WORKING)

    def test_recovery_does_not_block_ingestion_or_overwrite_live_events(self):
        for offset in (0, -1):
            with self.subTest(recovery_timestamp_offset=offset), tempfile.TemporaryDirectory() as tmp:
                latest = Path(tmp) / "latest.json"
                monitor = LiveAgentMonitor(
                    latest_state_path=latest,
                    recovery_sources=(SourceSpec("codex", Path(tmp) / "hook.jsonl"),),
                    recover_on_init=False,
                )
                now = datetime.now(timezone.utc)
                historical = status_from_event(HookEvent(
                    "codex", now + timedelta(seconds=offset), "Stop", {},
                    session_id="live",
                ))
                missed = status_from_event(HookEvent(
                    "codex", now, "Stop", {}, session_id="missed",
                ))
                entered = threading.Event()
                release = threading.Event()
                errors = []

                def read_history():
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("test did not release history reader")
                    return {s.agent_id: s for s in (historical, missed)}

                def recover():
                    try:
                        monitor.reconcile_recovery_sources()
                    except Exception as exc:
                        errors.append(exc)

                with patch.object(AgentMonitor, "_latest_statuses", side_effect=read_history):
                    worker = threading.Thread(target=recover, daemon=True)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(5))
                        monitor.ingest_record(HookEvent(
                            "codex", now, "UserPromptSubmit", {}, session_id="live",
                        ))
                        self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.WORKING)
                    finally:
                        release.set()
                        worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(monitor.statuses_by_key[historical.agent_id].mode, AgentMode.WORKING)
                self.assertEqual(monitor.statuses_by_key[missed.agent_id].mode, AgentMode.COMPLETED)
                persisted = {s["agent_id"]: s for s in json.loads(latest.read_text())["statuses"]}
                self.assertEqual(persisted[historical.agent_id]["mode"], "working")
                self.assertEqual(persisted[missed.agent_id]["mode"], "completed")


@unittest.skipUnless(sys.platform == "darwin", "requires AppKit")
class StatusBarStartupTests(unittest.TestCase):
    def test_accessory_policy_is_set_before_controller_initialization(self):
        from sidepulse import status_bar

        calls = Mock()
        with (
            patch.object(status_bar, "NSApplication") as application,
            patch.object(status_bar, "StatusBarController") as controller,
        ):
            app = application.sharedApplication.return_value
            calls.attach_mock(app.setActivationPolicy_, "hide_dock_icon")
            calls.attach_mock(controller.alloc.return_value.init, "initialize")
            status_bar.run_status_bar()
            self.assertEqual([call[0] for call in calls.mock_calls], ["hide_dock_icon", "initialize"])
            app.run.assert_called_once()

    def test_daemon_sessions_defer_history_to_a_background_thread(self):
        from sidepulse import daemon_sessions
        from sidepulse.program_status import ProgramState

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(daemon_sessions, "default_latest_state_path", return_value=Path(tmp) / "latest.json"),
            patch.object(daemon_sessions, "default_sources", return_value=(SourceSpec("codex", Path(tmp) / "hook.jsonl"),)),
            patch.object(AgentMonitor, "_latest_statuses", return_value={}) as read_history,
            patch.object(daemon_sessions.threading, "Thread") as thread,
        ):
            sessions = daemon_sessions.DaemonSessions(ProgramState(publisher="mac"), start_t3=False)
            read_history.assert_not_called()
            sessions.start()
            self.assertTrue(thread.call_args.kwargs["daemon"])
            thread.return_value.start.assert_called_once()
            thread.call_args.kwargs["target"]()
            read_history.assert_called_once()
