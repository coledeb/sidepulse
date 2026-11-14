from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from sidepulse.cli import build_sidepulse_parser
from sidepulse.collector import AgentMonitor, SourceSpec
from sidepulse.install import (
    install_opencode_hooks,
    opencode_plugin_source,
    uninstall_opencode_hooks,
)
from sidepulse.models import AgentMode, AgentStatus
from sidepulse.origin import (
    ProcessInfo,
    T3CODE_THREAD_KEY,
    annotate_payload_with_origin,
    detect_agent_origin,
    is_t3code_origin,
)
from sidepulse.providers import (
    HOOK_PROVIDERS,
    OPENCODE_EVENTS,
    default_opencode_plugin_path,
    detect_opencode_config,
)
from sidepulse.session_actions import session_resume_command

T3_SERVER = ProcessInfo(
    pid=20,
    ppid=1,
    comm="T3 Code (Nightly)",
    command="/Applications/T3 Code (Nightly).app/Contents/MacOS/T3 Code (Nightly) server",
)
OPENCODE_SERVER = ProcessInfo(
    pid=30,
    ppid=20,
    comm="opencode",
    command="/Users/me/.opencode/bin/opencode serve --hostname=127.0.0.1 --port=4242",
)
BACKGROUND_SERVICE = ProcessInfo(
    pid=40,
    ppid=1,
    comm="opencode",
    command="/Users/me/.opencode/bin/opencode serve --service",
)


class OpenCodeInstallTests(unittest.TestCase):
    def test_plugin_lives_in_its_own_directory_under_the_config_dir(self) -> None:
        home = Path("/Users/someone")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                default_opencode_plugin_path(home),
                home / ".config" / "opencode" / "plugins" / "sidepulse" / "index.js",
            )
        with mock.patch.dict(os.environ, {"OPENCODE_CONFIG_DIR": "/tmp/oc"}, clear=True):
            self.assertEqual(
                default_opencode_plugin_path(),
                Path("/tmp/oc/plugins/sidepulse/index.js"),
            )

    def test_install_detect_and_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = default_opencode_plugin_path(home)
            log = home / "state" / "opencode.jsonl"

            result = install_opencode_hooks(
                config_path=config, log_path=log, python_executable="python3"
            )

            self.assertTrue(result.changed)
            self.assertIsNone(result.backup_path)
            text = config.read_text()
            self.assertTrue(text.startswith("// sidepulse-opencode-plugin"))
            self.assertNotIn("__SIDEPULSE_HOOK_COMMAND__", text)
            self.assertIn("--provider opencode", text)
            self.assertTrue(log.exists())

            detected = detect_opencode_config(home)
            self.assertTrue(detected.hooks_enabled)
            self.assertEqual(detected.hook_events, OPENCODE_EVENTS)
            self.assertEqual(detected.log_paths, (log,))

            again = install_opencode_hooks(
                config_path=config, log_path=log, python_executable="python3"
            )
            self.assertFalse(again.changed)

            removed = uninstall_opencode_hooks(config_path=config, log_path=log)
            self.assertTrue(removed.changed)
            self.assertFalse(config.exists())
            self.assertFalse(config.parent.exists())
            self.assertFalse(detect_opencode_config(home).hooks_enabled)

    def test_install_refuses_to_replace_a_plugin_it_did_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "plugins" / "sidepulse" / "index.js"
            config.parent.mkdir(parents=True)
            config.write_text("export default { id: 'mine', setup() {} }\n")

            with self.assertRaises(ValueError):
                install_opencode_hooks(config_path=config, log_path=Path(tmp) / "o.jsonl")
            result = uninstall_opencode_hooks(config_path=config, log_path=Path(tmp) / "o.jsonl")

            self.assertFalse(result.changed)
            self.assertTrue(config.exists())

    def test_cli_knows_opencode(self) -> None:
        self.assertIn("opencode", HOOK_PROVIDERS)
        args = build_sidepulse_parser().parse_args(["setup", "opencode"])
        self.assertEqual(args.provider, "opencode")


class OpenCodeOriginTests(unittest.TestCase):
    def test_t3_code_server_ancestry_marks_the_session_as_t3_hosted(self) -> None:
        with mock.patch(
            "sidepulse.origin.process_ancestry", return_value=(OPENCODE_SERVER, T3_SERVER)
        ):
            origin = detect_agent_origin("opencode", env={})

        self.assertEqual(origin.label, "OpenCode in T3 Code")
        self.assertTrue(is_t3code_origin(origin.label))

    def test_server_environment_never_decides_the_host(self) -> None:
        # A background service started from a T3 terminal inherits its thread id,
        # then serves plain terminal sessions.
        env = {
            "T3CODE_THREAD_ID": "thread-from-whoever-started-the-service",
            "__CFBundleIdentifier": "com.t3tools.t3code",
            "TERM_PROGRAM": "ghostty",
        }
        with mock.patch(
            "sidepulse.origin.process_ancestry", return_value=(BACKGROUND_SERVICE,)
        ):
            origin = detect_agent_origin("opencode", env=env)
            payload = annotate_payload_with_origin("opencode", {"session_id": "ses_1"}, env=env)

        self.assertEqual(origin.label, "OpenCode")
        self.assertEqual(payload["agent_origin"], "OpenCode")
        self.assertNotIn(T3CODE_THREAD_KEY, payload)


class OpenCodeStatusTests(unittest.TestCase):
    def snapshot(self, rows: list[dict]) -> object:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log = Path(tmp.name) / "opencode.jsonl"
        log.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return AgentMonitor(
            sources=(SourceSpec("opencode", log),), stale_after_seconds=3600
        ).snapshot()

    def row(self, event: str, **fields: object) -> dict:
        return {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "hook_event_name": event,
            "session_id": "ses_abc12345",
            "cwd": "/tmp/project",
            "agent_origin": "OpenCode",
            **fields,
        }

    def test_lifecycle_maps_to_sidepulse_states(self) -> None:
        cases = [
            ([self.row("UserPromptSubmit", prompt="fix it")], AgentMode.WORKING),
            ([self.row("PreToolUse", tool_name="shell")], AgentMode.TOOL_RUNNING),
            ([self.row("PermissionRequest", tool_name="shell")], AgentMode.WAITING_FOR_INPUT),
            (
                [self.row("PermissionRequest", tool_name="question", sidepulse_status="waiting_for_input")],
                AgentMode.WAITING_FOR_INPUT,
            ),
            ([self.row("PostToolUseFailure", tool_name="read")], AgentMode.BLOCKED_ERROR),
            ([self.row("Stop", last_assistant_message="All fixed.")], AgentMode.COMPLETED),
            ([self.row("StopFailure", error_details="rate limited")], AgentMode.BLOCKED_ERROR),
        ]
        for rows, expected in cases:
            with self.subTest(event=rows[-1]["hook_event_name"]):
                snapshot = self.snapshot(rows)
                self.assertEqual(snapshot.statuses[0].mode, expected)
                self.assertEqual(snapshot.statuses[0].provider, "opencode")

    def test_an_interrupted_run_goes_idle(self) -> None:
        snapshot = self.snapshot([self.row("UserPromptSubmit"), self.row("Interrupt")])
        # Idle sessions leave the menu at once.
        self.assertEqual(snapshot.statuses, ())

    def test_a_fresh_permission_request_is_already_a_prompt(self) -> None:
        # Unlike Codex, OpenCode never reports auto-approved permissions, so the
        # Codex settle window does not hold the Ask back as Working.
        snapshot = self.snapshot([self.row("PermissionRequest", tool_name="shell")])
        self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)

    def test_an_answered_permission_resumes_the_tool(self) -> None:
        snapshot = self.snapshot(
            [
                self.row("PermissionRequest", tool_name="shell", message="shell: ls"),
                self.row("PreToolUse", tool_name="shell"),
            ]
        )
        self.assertEqual(snapshot.statuses[0].mode, AgentMode.TOOL_RUNNING)

    def test_session_resumes_in_opencode(self) -> None:
        status = AgentStatus(
            provider="opencode",
            agent_id="opencode:session:ses_1",
            display_name="OpenCode session",
            mode=AgentMode.COMPLETED,
            updated_at=datetime.now(timezone.utc),
            event_name="Stop",
            session_id="ses_1",
            cwd="/tmp/project path",
        )
        self.assertEqual(
            session_resume_command(status),
            "cd '/tmp/project path' && opencode --session ses_1",
        )


HARNESS = textwrap.dedent(
    """
    const plugin = (await import(process.argv[2])).default
    const state = () => globalThis[Symbol.for("sidepulse.opencode.monitor")]
    const events = JSON.parse(process.argv[3])
    const sessions = JSON.parse(process.argv[4] || "{}")
    let subscriptions = 0
    function makeCtx(directory) {
      return {
        location: { directory },
        session: {
          async get({ sessionID }) {
            if (!sessions[sessionID]) throw new Error("not found")
            return sessions[sessionID]
          },
        },
        event: {
          async *subscribe({ signal }) {
            subscriptions += 1
            const first = subscriptions === 1
            if (first) for (const event of events) yield event
            await new Promise((resolve) => signal.addEventListener("abort", resolve))
          },
        },
      }
    }
    const tick = () => new Promise((resolve) => setTimeout(resolve, 20))
    const disposeA = plugin.setup(makeCtx("/work/a"))
    plugin.setup(makeCtx("/work/b"))
    await tick()
    await state().queue
    const before = subscriptions
    disposeA()
    await tick()
    await state().queue
    console.log(JSON.stringify({ before, after: subscriptions }))
    process.exit(0)
    """
)


# A one-shot `opencode run`: the process exits as soon as its events are out,
# with hooks still queued and, here, one session still mid-run.
EXIT_HARNESS = textwrap.dedent(
    """
    const plugin = (await import(process.argv[2])).default
    const events = JSON.parse(process.argv[3])
    plugin.setup({
      location: { directory: "/work/a" },
      session: { async get() { throw new Error("not found") } },
      event: {
        async *subscribe({ signal }) {
          for (const event of events) yield event
          await new Promise((resolve) => signal.addEventListener("abort", resolve))
        },
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 20))
    process.exit(0)
    """
)


def opencode_event(type_: str, data: dict, directory: str | None = "/work/a") -> dict:
    event = {"id": f"evt_{type_}", "type": type_, "data": data}
    if directory:
        event["location"] = {"directory": directory}
    return event


@unittest.skipUnless(shutil.which("node"), "node is needed to run the OpenCode plugin")
class OpenCodePluginTests(unittest.TestCase):
    def run_plugin(self, events: list[dict], sessions: dict | None = None) -> tuple[dict, list[dict]]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "hook.jsonl"
            command = f"{{ cat; echo; }} >> {shlex.quote(str(out))}"
            plugin = root / "plugin.mjs"
            plugin.write_text(opencode_plugin_source(command))
            harness = root / "harness.mjs"
            harness.write_text(HARNESS)

            completed = subprocess.run(
                ["node", str(harness), plugin.as_uri(), json.dumps(events), json.dumps(sessions or {})],
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            rows = (
                [json.loads(line) for line in out.read_text().splitlines() if line]
                if out.exists()
                else []
            )
            return json.loads(completed.stdout), rows

    def test_subagents_report_on_the_session_they_work_for(self) -> None:
        parent, child, grandchild = "ses_parent", "ses_child", "ses_grandchild"
        events = [
            opencode_event("session.execution.started", {"sessionID": parent}),
            opencode_event("session.renamed", {"sessionID": parent, "title": "Parent work"}),
            opencode_event("session.tool.input.started", {"sessionID": parent, "id": "c1", "name": "subagent"}),
            opencode_event("session.tool.called", {"sessionID": parent, "id": "c1", "input": {"agent": "general"}}),
            opencode_event(
                "session.created",
                {"sessionID": child, "parentID": parent, "title": "Child", "location": {"directory": "/work/a"}},
            ),
            opencode_event("session.execution.started", {"sessionID": child}, directory=None),
            opencode_event("session.renamed", {"sessionID": child, "title": "Child title"}),
            opencode_event("session.tool.input.started", {"sessionID": child, "id": "c2", "name": "shell"}),
            opencode_event("session.tool.called", {"sessionID": child, "id": "c2", "input": {"command": "ls"}}),
            opencode_event(
                "permission.asked",
                {"sessionID": child, "id": "p2", "action": "shell", "resources": ["ls"]},
            ),
            opencode_event("permission.replied", {"sessionID": child, "requestID": "p2", "reply": "once"}),
            opencode_event(
                "session.tool.failed",
                {"sessionID": child, "id": "c2", "error": {"type": "tool.error", "message": "boom"}},
            ),
            # Created before the plugin loaded: only a lookup knows its parent.
            opencode_event("session.execution.started", {"sessionID": grandchild}, directory=None),
            opencode_event("form.created", {"sessionID": grandchild, "id": "f1"}),
            opencode_event("form.replied", {"sessionID": grandchild, "id": "f1"}),
            opencode_event("session.execution.succeeded", {"sessionID": grandchild}, directory=None),
            opencode_event("session.text.ended", {"sessionID": child, "text": "Child summary?"}),
            opencode_event("session.execution.succeeded", {"sessionID": child}, directory=None),
            opencode_event("session.tool.success", {"sessionID": parent, "id": "c1"}),
            opencode_event("session.text.ended", {"sessionID": parent, "text": "All done."}),
            opencode_event("session.execution.succeeded", {"sessionID": parent}, directory=None),
        ]
        sessions = {grandchild: {"id": grandchild, "parentID": child}}

        _, rows = self.run_plugin(events, sessions)

        self.assertEqual(
            [(row["hook_event_name"], row.get("tool_name")) for row in rows],
            [
                ("UserPromptSubmit", None),
                ("PreToolUse", "subagent"),
                ("PreToolUse", "shell"),
                ("PermissionRequest", "shell"),
                ("PreToolUse", "shell"),
                ("PostToolUse", "shell"),
                ("PermissionRequest", "question"),
                ("PostToolUse", "question"),
                ("PostToolUse", "subagent"),
                ("Stop", None),
            ],
        )
        self.assertTrue(all(row["session_id"] == parent for row in rows))
        self.assertTrue(all(row.get("sidepulse_title") in (None, "Parent work") for row in rows))
        self.assertEqual(rows[-1]["last_assistant_message"], "All done.")

    def test_t3_sessions_report_their_t3_thread(self) -> None:
        thread_a = "39ddb903-e905-4793-af8e-5cd82718a244"
        thread_b = "6408fcb8-3c51-42f9-a64c-b5b9a2942160"

        def t3_rules(thread_id: str) -> list[dict]:
            return [
                {"action": "shell", "resource": "*", "effect": "ask"},
                {"action": "t3-code-*", "resource": "*", "effect": "deny"},
                {"action": f"t3-code-{thread_id}_*", "resource": "*", "effect": "allow"},
            ]

        events = [
            # Created while the plugin ran, with its rules on the event.
            opencode_event("session.created", {"sessionID": "ses_a", "permission": t3_rules(thread_a)}),
            opencode_event("session.execution.started", {"sessionID": "ses_a"}, directory=None),
            # Its subagent reports on ses_a, with ses_a's thread.
            opencode_event("session.created", {"sessionID": "ses_a_sub", "parentID": "ses_a"}),
            opencode_event("form.created", {"sessionID": "ses_a_sub", "id": "f1"}),
            opencode_event("session.execution.succeeded", {"sessionID": "ses_a"}, directory=None),
            # Created before the plugin loaded: the lookup carries the rules.
            opencode_event("session.execution.started", {"sessionID": "ses_b"}, directory=None),
            # Created on an event without rules: looked up once.
            opencode_event("session.created", {"sessionID": "ses_c"}),
            opencode_event("session.execution.started", {"sessionID": "ses_c"}, directory=None),
            # Not a T3 session.
            opencode_event("session.created", {"sessionID": "ses_cli", "permission": t3_rules(thread_a)[:2]}),
            opencode_event("session.execution.started", {"sessionID": "ses_cli"}, directory=None),
        ]
        sessions = {
            # The shape ctx.session.get returns in OpenCode 2.2.6.
            "ses_b": {"id": "ses_b", "permissions": t3_rules(thread_b)},
            "ses_c": {"id": "ses_c", "permissions": t3_rules(thread_b)},
            "ses_a_sub": {"id": "ses_a_sub", "parentID": "ses_a"},
        }

        _, rows = self.run_plugin(events, sessions)

        self.assertEqual(
            [(row["session_id"], row["hook_event_name"], row.get("t3code_thread_id")) for row in rows],
            [
                ("ses_a", "UserPromptSubmit", thread_a),
                ("ses_a", "PermissionRequest", thread_a),
                ("ses_a", "Stop", thread_a),
                ("ses_b", "UserPromptSubmit", thread_b),
                ("ses_c", "UserPromptSubmit", thread_b),
                ("ses_cli", "UserPromptSubmit", None),
            ],
        )

    def test_plugin_translates_events_once_per_server(self) -> None:
        sid = "ses_plugin1"
        events = [
            opencode_event("provider.updated", {}),
            opencode_event(
                "session.inbox.enqueued",
                {"sessionID": sid, "item": {"type": "user", "payload": {"text": "fix it"}}},
            ),
            opencode_event("session.renamed", {"sessionID": sid, "title": "Fix it"}),
            opencode_event("session.execution.started", {"sessionID": sid}, directory=None),
            opencode_event("session.tool.input.started", {"sessionID": sid, "id": "c1", "name": "shell"}),
            opencode_event("session.tool.called", {"sessionID": sid, "id": "c1", "input": {"command": "ls"}}),
            opencode_event(
                "permission.asked",
                {"sessionID": sid, "id": "p1", "action": "shell", "resources": ["ls"]},
            ),
            opencode_event("permission.replied", {"sessionID": sid, "requestID": "p1", "reply": "once"}),
            opencode_event("session.tool.success", {"sessionID": sid, "id": "c1"}),
            opencode_event(
                "session.tool.failed",
                {"sessionID": sid, "id": "c2", "error": {"type": "permission.rejected", "message": "no"}},
            ),
            opencode_event("session.text.ended", {"sessionID": sid, "text": "Fixed. Want me to commit?"}),
            opencode_event("session.execution.succeeded", {"sessionID": sid}, directory=None),
        ]
        counts, rows = self.run_plugin(events)
        # Two directories share one subscription, which moves when its owner unloads.
        self.assertEqual(counts, {"before": 1, "after": 2})

        self.assertEqual(
            [row["hook_event_name"] for row in rows],
            [
                "UserPromptSubmit",
                "PreToolUse",
                "PermissionRequest",
                "PreToolUse",
                "PostToolUse",
                "PostToolUse",
                "Stop",
            ],
        )
        self.assertTrue(all(row["session_id"] == sid for row in rows))
        self.assertTrue(all(row["cwd"] == "/work/a" for row in rows))
        self.assertEqual(rows[0]["prompt"], "fix it")
        self.assertEqual(rows[0]["sidepulse_title"], "Fix it")
        self.assertEqual(rows[1]["tool_name"], "shell")
        self.assertEqual(rows[1]["tool_input"], {"command": "ls"})
        self.assertEqual(rows[2]["message"], "shell: ls")
        self.assertNotIn("tool_input", rows[2])
        self.assertEqual(rows[-1]["last_assistant_message"], "Fixed. Want me to commit?")

    def test_hooks_still_queued_at_exit_are_delivered_in_order(self) -> None:
        done, cut = "ses_done", "ses_cut"
        events = [
            opencode_event("session.execution.started", {"sessionID": done}),
            opencode_event("session.text.ended", {"sessionID": done, "text": "hi"}),
            opencode_event("session.execution.succeeded", {"sessionID": done}),
            opencode_event("session.execution.started", {"sessionID": cut}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "hook.jsonl"
            # Slow enough that the process exits with most hooks still queued.
            command = f"sleep 0.2; {{ cat; echo; }} >> {shlex.quote(str(out))}"
            plugin = root / "plugin.mjs"
            plugin.write_text(opencode_plugin_source(command))
            harness = root / "harness.mjs"
            harness.write_text(EXIT_HARNESS)
            completed = subprocess.run(
                ["node", str(harness), plugin.as_uri(), json.dumps(events)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            rows: list[dict] = []
            deadline = time.monotonic() + 10
            while len(rows) < 4 and time.monotonic() < deadline:
                time.sleep(0.1)
                if out.exists():
                    rows = [json.loads(line) for line in out.read_text().splitlines() if line]

        self.assertEqual(
            [(row["session_id"], row["hook_event_name"]) for row in rows],
            [
                (done, "UserPromptSubmit"),
                (done, "Stop"),
                (cut, "UserPromptSubmit"),
                (cut, "Interrupt"),
            ],
        )
        self.assertEqual(rows[1]["last_assistant_message"], "hi")


if __name__ == "__main__":
    unittest.main()
