from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from sidepulse.antigravity_hook import (
    last_assistant_message,
    normalize_payload,
    protobuf_fields,
)
from sidepulse.collector import AgentMonitor, SourceSpec
from sidepulse.hook import hook_log_main
from sidepulse.install import (
    antigravity_hook_command,
    install_antigravity_hooks,
    uninstall_antigravity_hooks,
)
from sidepulse.models import AgentMode
from sidepulse.providers import (
    detect_antigravity_config,
    t3_antigravity_hook_config_paths,
)

CLI_COMMON = {
    "conversationId": "63e69286-75c9-483b-a316-299899e77d04",
    "workspacePaths": ["/work/project"],
    "transcriptPath": "/nonexistent/transcript_full.jsonl",
    "modelName": "gemini-3.8-flash-low",
    "lastUserInput": "fix the test",
}
ACP_COMMON = {
    "conversationId": "ec436a90-3ca9-4367-a890-555a1f50cc7f",
    "workspacePaths": ["/Users/me/.t3/scratch/thread", "/Users/me/.t3/userdata/attachments"],
    "transcriptPath": "/nonexistent/antigravity-acp/conversations/ec436a90.db",
}


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def _acp_planner_step(text: str) -> bytes:
    # step_payload: field 20 (planner response) { field 6: id, field 1: text }.
    inner = _field(6, b"A4LyapSPMs") + _field(1, text.encode())
    return _varint((3 << 3) | 0) + _varint(7) + _field(20, inner)


class AntigravityEventMapTests(unittest.TestCase):
    def test_cli_turn_maps_first_invocation_and_stop(self) -> None:
        start = normalize_payload("PreInvocation", {**CLI_COMMON, "invocationNum": 0})
        self.assertEqual(start["hook_event_name"], "UserPromptSubmit")
        self.assertEqual(start["session_id"], CLI_COMMON["conversationId"])
        self.assertEqual(start["cwd"], "/work/project")
        self.assertEqual(start["prompt"], "fix the test")
        # protojson leaves out a zero invocationNum.
        self.assertEqual(normalize_payload("PreInvocation", CLI_COMMON)["hook_event_name"], "UserPromptSubmit")
        self.assertIsNone(normalize_payload("PreInvocation", {**CLI_COMMON, "invocationNum": 2}))
        self.assertIsNone(normalize_payload("PostInvocation", {**CLI_COMMON, "invocationNum": 0}))

        stop = normalize_payload("Stop", {**CLI_COMMON, "terminationReason": "NO_TOOL_CALL", "fullyIdle": True})
        self.assertEqual(stop["hook_event_name"], "Stop")
        self.assertNotIn("background_tasks", stop)

    def test_cli_stop_reasons(self) -> None:
        def name(reason: str, **extra) -> str | None:
            mapped = normalize_payload("Stop", {**CLI_COMMON, "terminationReason": reason, **extra})
            return mapped["hook_event_name"] if mapped else None

        self.assertEqual(name("USER_CANCELED"), "Interrupt")
        self.assertEqual(name("ERROR", error="boom"), "StopFailure")
        self.assertEqual(name("MAX_INVOCATIONS"), "StopFailure")
        self.assertEqual(name("TERMINAL_STEP_TYPE"), "Stop")
        self.assertIsNone(name("session_end"))
        busy = normalize_payload("Stop", {**CLI_COMMON, "terminationReason": "NO_TOOL_CALL", "fullyIdle": False})
        self.assertEqual(busy["background_tasks"], [{"status": "running"}])

    def test_acp_turn_uses_invocation_pair(self) -> None:
        start = normalize_payload("PreInvocation", {**ACP_COMMON, "invocationNum": 2})
        self.assertEqual(start["hook_event_name"], "UserPromptSubmit")
        self.assertEqual(start["cwd"], "/Users/me/.t3/scratch/thread")
        end = normalize_payload("PostInvocation", {**ACP_COMMON, "invocationNum": 2})
        self.assertEqual(end["hook_event_name"], "Stop")
        # The ACP harness reports the previous session closing when a session starts.
        self.assertIsNone(normalize_payload("Stop", {**ACP_COMMON, "terminationReason": "session_end"}))

    def test_tool_events(self) -> None:
        pre = normalize_payload(
            "PreToolUse",
            {**CLI_COMMON, "toolCall": {"name": "run_command", "args": {"CommandLine": "npm test"}}},
        )
        self.assertEqual(pre["hook_event_name"], "PreToolUse")
        self.assertEqual(pre["tool_name"], "run_command")
        self.assertEqual(pre["tool_input"], {"CommandLine": "npm test"})
        self.assertNotIn("prompt", pre)
        ok = normalize_payload("PostToolUse", {**CLI_COMMON, "error": "", "toolCall": {"name": "read_file"}})
        self.assertEqual(ok["hook_event_name"], "PostToolUse")
        failed = normalize_payload("PostToolUse", {**CLI_COMMON, "error": "file does not exist"})
        self.assertEqual(failed["hook_event_name"], "PostToolUseFailure")
        self.assertEqual(failed["error"], "file does not exist")


class AntigravityTranscriptTests(unittest.TestCase):
    def test_reads_last_planner_response_from_cli_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript_full.jsonl"
            rows = [
                {"type": "USER_INPUT", "content": "hi"},
                {"type": "PLANNER_RESPONSE", "content": "Working on it."},
                {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command"}]},
                {"type": "GENERIC", "status": "DONE"},
                {"type": "PLANNER_RESPONSE", "content": "Want me to push it?"},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            self.assertEqual(last_assistant_message(str(path)), "Want me to push it?")

    def test_reads_last_planner_response_from_acp_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "conversation.db"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE steps (idx integer primary key, step_type integer, step_payload blob)")
            connection.executemany(
                "INSERT INTO steps VALUES (?, ?, ?)",
                [
                    (0, 14, _field(19, _field(2, b"user prompt"))),
                    (1, 15, _acp_planner_step("First answer.")),
                    (2, 8, _field(14, _field(4, b"file body"))),
                    (3, 15, _acp_planner_step("Anything else?")),
                ],
            )
            connection.commit()
            connection.close()
            self.assertEqual(last_assistant_message(str(path)), "Anything else?")

    def test_missing_or_malformed_transcripts_give_none(self) -> None:
        self.assertIsNone(last_assistant_message(""))
        self.assertIsNone(last_assistant_message("/nonexistent/x.db"))
        self.assertIsNone(last_assistant_message("/nonexistent/x.jsonl"))
        with self.assertRaises(ValueError):
            protobuf_fields(b"\x0a\x10short", 1)


class AntigravityHookProcessTests(unittest.TestCase):
    def test_hook_prints_nothing_and_logs_translated_event(self) -> None:
        # Antigravity reads PreToolUse stdout as a permission decision.
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "antigravity.jsonl"
            payload = {**CLI_COMMON, "toolCall": {"name": "run_command", "args": {}}}
            stdout = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch.dict(
                "os.environ", {"SIDEPULSE_DISABLE_EVENT_SOCKET": "1"}
            ), redirect_stdout(stdout):
                self.assertEqual(hook_log_main("antigravity", log, event="PreToolUse"), 0)
            self.assertEqual(stdout.getvalue(), "")
            row = json.loads(log.read_text().splitlines()[-1])
            self.assertEqual(row["hook_event_name"], "PreToolUse")
            self.assertEqual(row["tool_name"], "run_command")

    def test_dropped_event_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "antigravity.jsonl"
            with mock.patch("sys.stdin", io.StringIO(json.dumps({**CLI_COMMON, "invocationNum": 3}))), mock.patch.dict(
                "os.environ", {"SIDEPULSE_DISABLE_EVENT_SOCKET": "1"}
            ):
                self.assertEqual(hook_log_main("antigravity", log, event="PreInvocation"), 0)
            self.assertFalse(log.exists())

    def test_command_discards_output(self) -> None:
        command = antigravity_hook_command("PreToolUse", Path("/tmp/a.jsonl"), "python3")
        self.assertIn("--event PreToolUse", command)
        self.assertTrue(command.endswith(">/dev/null 2>&1 ; true"))


class AntigravityInstallTests(unittest.TestCase):
    def test_install_writes_global_and_t3_profiles_and_preserves_user_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            global_config = home / ".gemini" / "config" / "hooks.json"
            global_config.parent.mkdir(parents=True)
            global_config.write_text(json.dumps({"lint": {"PostToolUse": [{"matcher": "x", "hooks": []}]}}))
            profile = home / ".t3" / "userdata" / "providers" / "antigravity" / "abc123"
            (profile / "config").mkdir(parents=True)
            t3_paths = t3_antigravity_hook_config_paths(home)
            self.assertEqual(t3_paths, (profile / "config" / "hooks.json",))
            log = home / "state" / "antigravity.jsonl"

            result = install_antigravity_hooks(
                log_path=log,
                config_paths=(global_config, *t3_paths),
                python_executable="python3",
            )
            self.assertTrue(result.changed)
            self.assertEqual(set(result.config_paths), {global_config, *t3_paths})
            data = json.loads(global_config.read_text())
            self.assertIn("lint", data)
            spec = data["sidepulse"]
            self.assertEqual(spec["PreToolUse"][0]["matcher"], "*")
            self.assertIn("--event PreToolUse", spec["PreToolUse"][0]["hooks"][0]["command"])
            self.assertIn("--event Stop", spec["Stop"][0]["command"])
            self.assertEqual(json.loads(t3_paths[0].read_text())["sidepulse"], spec)

            config = detect_antigravity_config(home)
            self.assertTrue(config.hooks_enabled)
            self.assertEqual(config.log_paths[0], log)

            again = install_antigravity_hooks(
                log_path=log,
                config_paths=(global_config, *t3_paths),
                python_executable="python3",
            )
            self.assertFalse(again.changed)

            removed = uninstall_antigravity_hooks(config_paths=(global_config, *t3_paths))
            self.assertTrue(removed.changed)
            self.assertEqual(json.loads(global_config.read_text()), {"lint": {"PostToolUse": [{"matcher": "x", "hooks": []}]}})
            self.assertEqual(json.loads(t3_paths[0].read_text()), {})
            self.assertFalse(detect_antigravity_config(home).hooks_enabled)


class AntigravityCollectorTests(unittest.TestCase):
    def _snapshot(self, *events: dict) -> object:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "antigravity.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text("".join(json.dumps({**event, "logged_at": now}) + "\n" for event in events))
            return AgentMonitor(sources=(SourceSpec("antigravity", log),), stale_after_seconds=3600).snapshot()

    def test_turn_with_tool_completes(self) -> None:
        snapshot = self._snapshot(
            normalize_payload("PreInvocation", {**CLI_COMMON, "invocationNum": 0}),
            normalize_payload("PreToolUse", {**CLI_COMMON, "toolCall": {"name": "run_command"}}),
            normalize_payload("PostToolUse", {**CLI_COMMON, "error": ""}),
            normalize_payload("Stop", {**CLI_COMMON, "terminationReason": "NO_TOOL_CALL"}),
        )
        self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)
        self.assertEqual(snapshot.statuses[0].provider, "antigravity")

    def test_tool_running_while_waiting_on_tool(self) -> None:
        snapshot = self._snapshot(
            normalize_payload("PreInvocation", {**ACP_COMMON, "invocationNum": 1}),
            normalize_payload("PreToolUse", {**ACP_COMMON, "toolCall": {"name": "run_command"}}),
        )
        self.assertEqual(snapshot.aggregate.mode, AgentMode.TOOL_RUNNING)

    def test_closing_question_waits_for_input(self) -> None:
        stop = normalize_payload("PostInvocation", {**ACP_COMMON, "invocationNum": 1})
        stop["last_assistant_message"] = "I've read the file.\n\nDo you need help with anything else?"
        snapshot = self._snapshot(
            normalize_payload("PreInvocation", {**ACP_COMMON, "invocationNum": 1}),
            stop,
        )
        self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)


if __name__ == "__main__":
    unittest.main()
