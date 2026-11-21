from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from hook_assertions import command_text
from sidepulse.collector import AgentMonitor, SourceSpec
from sidepulse.hook import hook_log_main, is_devin_claude_compat_payload
from sidepulse.install import install_devin_hooks, uninstall_devin_hooks
from sidepulse.models import AgentMode
from sidepulse.origin import detect_agent_origin
from sidepulse.providers import DEVIN_EVENTS, detect_devin_config
from sidepulse.session_actions import session_resume_command
from sidepulse.t3_status import provider_for_t3_instance

# Captured from `devin acp` 3000.14 (T3 Code's Devin provider). Devin's own
# hooks and the Claude Code hooks it also runs get this same payload.
DEVIN_PROJECT = "/Users/me/Projects/app"
DEVIN_ENV = {"DEVIN_PROJECT_DIR": DEVIN_PROJECT, "SIDEPULSE_DISABLE_EVENT_SOCKET": "1"}


def devin_event(name: str, **fields) -> dict:
    payload = {"hook_event_name": name, "session_id": "glowing-caraway"}
    if name != "SessionStart":
        payload["prompt_id"] = "05697515-2558-4745-bb9f-ac6ca51b1c97"
    payload.update(fields)
    return payload


def run_hook(provider: str, log: Path, payload: dict, env: dict) -> int:
    with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch.dict(
        "os.environ", env, clear=False
    ):
        return hook_log_main(provider, log)


class DevinClaudeCompatTests(unittest.TestCase):
    def test_devin_payload_is_recognized_only_with_devin_env(self) -> None:
        text = json.dumps(devin_event("Stop", last_assistant_message="done"))
        self.assertTrue(is_devin_claude_compat_payload(text, DEVIN_ENV))
        self.assertFalse(is_devin_claude_compat_payload(text, {}))

    def test_claude_code_payload_is_left_to_claude(self) -> None:
        # Claude Code always sends a transcript, even if DEVIN_PROJECT_DIR leaked in.
        text = json.dumps(
            {"hook_event_name": "Stop", "session_id": "s", "transcript_path": "/x.jsonl"}
        )
        self.assertFalse(is_devin_claude_compat_payload(text, DEVIN_ENV))

    def test_claude_hook_from_devin_logs_as_devin_with_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            claude_log = Path(tmp) / "claude.jsonl"
            devin_log = Path(tmp) / "devin.jsonl"
            with mock.patch("sidepulse.hook.detect_log_path", return_value=devin_log), mock.patch(
                "sidepulse.hook.devin_native_hooks_installed", return_value=False
            ):
                payload = devin_event("PreToolUse", tool_name="read", tool_input={"file_path": "a"})
                self.assertEqual(run_hook("claude", claude_log, payload, DEVIN_ENV), 0)
            self.assertFalse(claude_log.exists())
            row = json.loads(devin_log.read_text().splitlines()[-1])
            self.assertEqual(row["hook_event_name"], "PreToolUse")
            self.assertEqual(row["cwd"], DEVIN_PROJECT)
            self.assertNotEqual(row.get("agent_origin_kind", ""), "claude_t3code")

    def test_t3_hosted_devin_gets_t3_origin_and_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            devin_log = Path(tmp) / "devin.jsonl"
            env = {**DEVIN_ENV, "T3CODE_THREAD_ID": "thread-1"}
            with mock.patch("sidepulse.hook.detect_log_path", return_value=devin_log), mock.patch(
                "sidepulse.hook.devin_native_hooks_installed", return_value=False
            ):
                run_hook("claude", Path(tmp) / "claude.jsonl", devin_event("UserPromptSubmit"), env)
            row = json.loads(devin_log.read_text().splitlines()[-1])
            self.assertEqual(row["agent_origin"], "Devin in T3 Code")
            self.assertEqual(row["t3code_thread_id"], "thread-1")
            # T3 runs a `devin acp` per thread that can exit between turns, so
            # its pid must not idle a finished row.
            self.assertNotIn("agent_pid", row)

    def test_claude_copy_is_dropped_when_devin_hooks_are_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            claude_log = Path(tmp) / "claude.jsonl"
            devin_log = Path(tmp) / "devin.jsonl"
            with mock.patch("sidepulse.hook.detect_log_path", return_value=devin_log), mock.patch(
                "sidepulse.hook.devin_native_hooks_installed", return_value=True
            ):
                run_hook("claude", claude_log, devin_event("UserPromptSubmit"), DEVIN_ENV)
            self.assertFalse(claude_log.exists())
            self.assertFalse(devin_log.exists())

    def test_session_end_is_dropped(self) -> None:
        # T3's Devin ends its session after every turn, which would turn a
        # closing question into Done.
        with tempfile.TemporaryDirectory() as tmp:
            devin_log = Path(tmp) / "devin.jsonl"
            run_hook("devin", devin_log, devin_event("SessionEnd", reason="other"), DEVIN_ENV)
            self.assertFalse(devin_log.exists())


class DevinStatusTests(unittest.TestCase):
    def snapshot(self, tmp: str, *events: dict):
        log = Path(tmp) / "devin.jsonl"
        now = datetime.now(timezone.utc).isoformat()
        rows = [{**event, "cwd": DEVIN_PROJECT, "logged_at": now} for event in events]
        log.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return AgentMonitor(sources=(SourceSpec("devin", log),), stale_after_seconds=3600).snapshot()

    def test_prompt_then_tool_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = self.snapshot(
                tmp,
                devin_event("SessionStart", source="startup"),
                devin_event("UserPromptSubmit", prompt="fix it"),
                devin_event("PreToolUse", tool_name="exec", tool_input={"command": "ls"}),
            )
            self.assertEqual(snapshot.aggregate.mode, AgentMode.TOOL_RUNNING)
            self.assertEqual(snapshot.statuses[0].provider, "devin")

    def test_stop_is_done_and_closing_question_waits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            done = self.snapshot(
                tmp,
                devin_event("UserPromptSubmit", prompt="fix it"),
                devin_event("Stop", stop_hook_active=False, last_assistant_message="Fixed the test."),
            )
            self.assertEqual(done.aggregate.mode, AgentMode.COMPLETED)
        with tempfile.TemporaryDirectory() as tmp:
            asking = self.snapshot(
                tmp,
                devin_event("UserPromptSubmit", prompt="fix it"),
                devin_event(
                    "Stop",
                    stop_hook_active=False,
                    last_assistant_message="Should I also update the docs?",
                ),
            )
            self.assertEqual(asking.aggregate.mode, AgentMode.WAITING_FOR_INPUT)


class DevinInstallTests(unittest.TestCase):
    def test_install_keeps_user_config_and_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / ".config" / "devin" / "config.json"
            config.parent.mkdir(parents=True)
            user_hook = {"matcher": "exec", "hooks": [{"type": "command", "command": "./check.sh"}]}
            config.write_text(json.dumps({"version": 1, "hooks": {"PreToolUse": [user_hook]}}))
            log = home / "state" / "devin.jsonl"

            result = install_devin_hooks(config_path=config, log_path=log, python_executable="python3")

            self.assertTrue(result.changed)
            data = json.loads(config.read_text())
            self.assertEqual(data["version"], 1)
            self.assertEqual(data["hooks"]["PreToolUse"][0], user_hook)
            self.assertEqual(set(data["hooks"]), set(DEVIN_EVENTS))
            ours = data["hooks"]["Stop"][-1]
            self.assertEqual(ours["matcher"], "")
            self.assertIn("--provider devin", command_text(ours["hooks"][0]["command"]))
            detected = detect_devin_config(home)
            self.assertTrue(detected.hooks_enabled)
            self.assertEqual(detected.log_paths, (log,))

            again = install_devin_hooks(config_path=config, log_path=log, python_executable="python3")
            self.assertFalse(again.changed)

            uninstall_devin_hooks(config_path=config, log_path=log)
            data = json.loads(config.read_text())
            self.assertEqual(data, {"version": 1, "hooks": {"PreToolUse": [user_hook]}})
            self.assertFalse(detect_devin_config(home).hooks_enabled)

    def test_missing_config_is_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            detected = detect_devin_config(Path(tmp))
            self.assertFalse(detected.exists)
            self.assertFalse(detected.hooks_enabled)


class DevinPresentationTests(unittest.TestCase):
    def test_t3_registry_instance_maps_to_devin(self) -> None:
        self.assertEqual(provider_for_t3_instance("acpRegistry_devin"), "devin")
        self.assertEqual(provider_for_t3_instance("claudeAgent"), "claude")

    def test_devin_desktop_origin(self) -> None:
        origin = detect_agent_origin(
            "devin",
            env={"__CFBundleIdentifier": "com.exafunction.windsurf", "VSCODE_PID": "1"},
        )
        self.assertEqual(origin.label, "Devin Desktop")

    def test_devin_cli_origin(self) -> None:
        from sidepulse.origin import ProcessInfo

        processes = (ProcessInfo(pid=10, ppid=1, comm="devin", command="/usr/local/bin/devin"),)
        with mock.patch("sidepulse.origin.process_ancestry", return_value=processes):
            origin = detect_agent_origin("devin", env={})
        self.assertEqual(origin.label, "Devin CLI")

    def test_resume_command(self) -> None:
        status = mock.Mock(provider="devin", session_id="glowing-caraway", cwd="/tmp/app")
        self.assertEqual(
            session_resume_command(status), "cd /tmp/app && devin --resume glowing-caraway"
        )


if __name__ == "__main__":
    unittest.main()
