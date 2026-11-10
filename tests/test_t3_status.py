from __future__ import annotations

import http.server
import json
import os
import stat
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sidepulse import t3_status
from sidepulse.collector import LiveAgentMonitor
from sidepulse.models import AgentMode
from sidepulse.origin import is_relayed_origin, is_t3code_origin
from sidepulse.session_actions import SESSION_OPEN_APP, available_session_open_actions

NOW = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)
KARLY_WSL = "f74a5dc9-341d-418b-a818-5a9f607b7ce8"


def thread(thread_id: str, status: str, *, settled: bool = False, title: str = "Fix the build",
           provider: str = "claudeAgent", updated: datetime = NOW, snoozed: bool = False) -> dict:
    return {
        "threadId": thread_id,
        "title": title,
        "status": status,
        "settled": settled,
        "snoozed": snoozed,
        "providerInstanceId": provider,
        "updatedAt": updated.isoformat().replace("+00:00", "Z"),
    }


class FakeClient:
    def __init__(self) -> None:
        self.links = [{"environmentId": KARLY_WSL, "label": "KARLY-WSL", "status": "reachable"}]
        self.threads: dict[str, list[dict]] = {KARLY_WSL: []}
        self.projects: dict[str, list[dict]] = {KARLY_WSL: []}
        self.fail: dict[str, Exception] = {}
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        if name in self.fail:
            raise self.fail[name]
        if name == "t3_environment_links":
            return {"environmentId": "local", "links": self.links}
        if name == "t3_thread_list":
            environment = arguments["environmentId"]
            if environment in self.fail:
                raise self.fail[environment]
            assert arguments["settled"] is False and arguments["allProjects"] is True
            return {"threads": list(self.threads.get(environment, [])), "nextCursor": None}
        if name == "t3_project_list":
            return {"projects": list(self.projects.get(arguments["environmentId"], [])), "nextCursor": None}
        raise AssertionError(name)


class Clock:
    def __init__(self) -> None:
        self.monotonic = 1000.0
        self.wall = NOW

    def advance(self, seconds: float) -> None:
        self.monotonic += seconds
        self.wall += timedelta(seconds=seconds)


def make_poller(client: FakeClient, clock: Clock, records: list) -> t3_status.T3StatusPoller:
    return t3_status.T3StatusPoller(
        records.append,
        client_factory=lambda: client,
        monotonic=lambda: clock.monotonic,
        wall_clock=lambda: clock.wall,
    )


class MappingTests(unittest.TestCase):
    def test_thread_status_mapping(self) -> None:
        cases = [
            (thread("t", "running"), "working"),
            (thread("t", "starting"), "working"),
            (thread("t", "queued"), "working"),
            (thread("t", "preparing"), "working"),
            (thread("t", "waiting"), "waiting"),
            (thread("t", "completed"), "completed"),
            (thread("t", "completed", settled=True), "idle"),
            (thread("t", "completed", snoozed=True), "idle"),
            (thread("t", "failed"), "error"),
            (thread("t", "failed", settled=True), "idle"),
            (thread("t", "interrupted"), "idle"),
            (thread("t", "cancelled"), "idle"),
            (thread("t", "rolled_back"), "idle"),
            (thread("t", "idle"), "idle"),
        ]
        for item, expected in cases:
            with self.subTest(status=item["status"], settled=item["settled"], snoozed=item["snoozed"]):
                self.assertEqual(t3_status.sidepulse_status_for_thread(item), expected)

    def test_provider_mapping(self) -> None:
        self.assertEqual(t3_status.provider_for_t3_instance("claudeAgent"), "claude")
        self.assertEqual(t3_status.provider_for_t3_instance("codex"), "codex")
        self.assertEqual(t3_status.provider_for_t3_instance("cursorAgent"), "cursor")
        self.assertEqual(t3_status.provider_for_t3_instance(None), "t3code")

    def test_record_is_a_relayed_t3_session(self) -> None:
        record = t3_status.t3_thread_record(
            thread("abc12345-0000", "running"),
            environment_id=KARLY_WSL,
            environment_label="KARLY-WSL",
            status="working",
            observed_at=NOW,
        )
        self.assertEqual(record.provider, "claude")
        self.assertEqual(record.origin, "Claude in T3 Code · KARLY-WSL")
        self.assertTrue(is_t3code_origin(record.origin))
        self.assertTrue(is_relayed_origin(record.origin))
        self.assertEqual(record.session_id, "abc12345-0000")
        self.assertEqual(record.raw["t3code_environment_id"], KARLY_WSL)


class PollerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeClient()
        self.clock = Clock()
        self.records: list = []
        self.poller = make_poller(self.client, self.clock, self.records)

    def statuses(self) -> list[str]:
        return [record.raw["sidepulse_status"] for record in self.records]

    def test_reports_each_change_once(self) -> None:
        self.client.threads[KARLY_WSL] = [thread("t1", "running")]
        self.poller.poll_once()
        self.poller.poll_once()
        self.assertEqual(self.statuses(), ["working"])
        self.assertEqual(self.poller.state, t3_status.STATE_CONNECTED)
        self.assertTrue(self.poller.any_active)
        self.assertEqual(self.poller.next_interval(), t3_status.T3_ACTIVE_POLL_SECONDS)

        self.client.threads[KARLY_WSL] = [thread("t1", "waiting")]
        self.poller.poll_once()
        self.client.threads[KARLY_WSL] = [thread("t1", "completed")]
        self.poller.poll_once()
        self.assertEqual(self.statuses(), ["working", "waiting", "completed"])
        self.assertFalse(self.poller.any_active)
        self.assertEqual(self.poller.next_interval(), t3_status.T3_IDLE_POLL_SECONDS)

    def test_rows_name_their_project_on_the_linked_machine(self) -> None:
        self.client.projects[KARLY_WSL] = [
            {"id": "p1", "title": "Sidepulse (KARLY-WSL)", "workspaceRoot": "/home/karly/Sidepulse"},
        ]
        self.client.threads[KARLY_WSL] = [
            {**thread("t1", "running"), "projectId": "p1"},
            {**thread("t2", "running"), "projectId": "unknown"},
        ]
        self.poller.poll_once()
        self.assertEqual([record.cwd for record in self.records], ["/home/karly/Sidepulse", None])

        # A project T3 doesn't list is retried, but not on every poll.
        def project_fetches() -> int:
            return sum(1 for name, _args in self.client.calls if name == "t3_project_list")

        self.clock.advance(10)
        self.poller.poll_once()
        self.assertEqual(project_fetches(), 1)
        self.clock.advance(t3_status.T3_PROJECTS_REFRESH_SECONDS)
        self.poller.poll_once()
        self.assertEqual(project_fetches(), 2)

    def test_project_list_failure_still_reports_the_thread(self) -> None:
        self.client.fail["t3_project_list"] = t3_status.T3Error("timed out")
        self.client.threads[KARLY_WSL] = [{**thread("t1", "running"), "projectId": "p1"}]
        self.poller.poll_once()
        self.assertEqual([(record.session_id, record.cwd) for record in self.records], [("t1", None)])
        self.assertEqual(self.poller.state, t3_status.STATE_CONNECTED)

    def test_a_thread_seen_in_t3_clears_with_its_own_provider(self) -> None:
        self.client.threads[KARLY_WSL] = [thread("t1", "completed", provider="codex")]
        self.poller.poll_once()
        self.client.threads[KARLY_WSL] = []  # the user opened it: settled
        self.poller.poll_once()
        self.poller.poll_once()
        self.assertEqual(self.statuses(), ["completed", "idle"])
        cleared = self.records[-1]
        self.assertEqual(cleared.provider, "codex")
        self.assertEqual(cleared.session_id, "t1")
        self.assertEqual(cleared.origin, "Codex in T3 Code · KARLY-WSL")

    def test_old_unseen_threads_are_history(self) -> None:
        self.client.threads[KARLY_WSL] = [
            thread("old", "completed", updated=NOW - timedelta(days=3)),
            thread("interrupted", "interrupted"),
            thread("recent", "failed", updated=NOW - timedelta(minutes=5)),
        ]
        self.poller.poll_once()
        self.assertEqual([record.session_id for record in self.records], ["recent"])
        self.assertEqual(self.statuses(), ["error"])

    def test_an_active_thread_is_reported_again_before_it_could_go_stale(self) -> None:
        self.client.threads[KARLY_WSL] = [thread("t1", "running")]
        self.poller.poll_once()
        self.clock.advance(30)
        self.poller.poll_once()
        self.clock.advance(31)
        self.poller.poll_once()
        self.assertEqual(self.statuses(), ["working", "working"])
        self.assertEqual(self.records[-1].logged_at, self.clock.wall)

    def test_finished_threads_keep_their_own_finish_time(self) -> None:
        finished = NOW - timedelta(minutes=4)
        self.client.threads[KARLY_WSL] = [thread("t1", "completed", updated=finished)]
        self.poller.poll_once()
        self.assertEqual(self.records[0].logged_at, finished)

    def test_unreachable_environment_keeps_others_going(self) -> None:
        mini = "bfc71ba6-18d1-4f88-8959-e8ea140ca923"
        self.client.links.append({"environmentId": mini, "label": "Mac Mini", "status": "reachable"})
        self.client.links.append({"environmentId": "gone", "label": "Old", "status": "unreachable"})
        self.client.threads[mini] = [thread("m1", "running")]
        self.client.fail[KARLY_WSL] = t3_status.T3Error("timed out")
        self.poller.poll_once()
        self.assertEqual([record.session_id for record in self.records], ["m1"])
        self.assertEqual(self.poller.state, t3_status.STATE_CONNECTED)
        self.assertIn("KARLY-WSL", self.poller.last_error)
        listed = [args["environmentId"] for name, args in self.client.calls if name == "t3_thread_list"]
        self.assertNotIn("gone", listed)

    def test_revoked_credential_asks_for_reconnect(self) -> None:
        self.client.fail["t3_environment_links"] = t3_status.T3AuthError("revoked")
        self.poller.poll_once()
        self.assertEqual(self.poller.state, t3_status.STATE_NEEDS_RECONNECT)
        self.assertIsNone(self.poller.client)

    def test_disconnecting_or_expiring_clears_remote_rows(self) -> None:
        self.client.threads[KARLY_WSL] = [thread("t1", "running")]
        self.poller.poll_once()
        self.client.fail["t3_thread_list"] = t3_status.T3AuthError("expired")
        self.poller.poll_once()
        self.assertEqual(self.statuses(), ["working", "idle"])
        self.assertEqual(self.records[-1].origin, "Claude in T3 Code · KARLY-WSL")

    def test_unlinked_environment_rows_clear(self) -> None:
        self.client.threads[KARLY_WSL] = [thread("t1", "waiting")]
        self.poller.poll_once()
        self.client.links = []
        self.clock.advance(t3_status.T3_LINKS_REFRESH_SECONDS)
        self.poller.poll_once()
        self.assertEqual(self.statuses(), ["waiting", "idle"])

    def test_t3_quitting_keeps_rows_until_it_is_back(self) -> None:
        self.client.threads[KARLY_WSL] = [thread("t1", "running")]
        self.poller.poll_once()
        self.client.fail["t3_environment_links"] = t3_status.T3Error("connection refused")
        self.clock.advance(t3_status.T3_LINKS_REFRESH_SECONDS)
        self.poller.poll_once()
        self.assertEqual(self.statuses(), ["working"])
        self.assertEqual(self.poller.state, t3_status.STATE_UNAVAILABLE)

    def test_a_surprise_does_not_end_polling(self) -> None:
        self.client.fail["t3_environment_links"] = KeyError("links")
        self.poller.poll_once()
        self.assertEqual(self.poller.state, t3_status.STATE_UNAVAILABLE)
        del self.client.fail["t3_environment_links"]
        self.poller.poll_once()
        self.assertEqual(self.poller.state, t3_status.STATE_CONNECTED)

    def test_t3_not_running_is_unavailable(self) -> None:
        self.client.fail["t3_environment_links"] = t3_status.T3Error("connection refused")
        self.poller.poll_once()
        self.assertEqual(self.poller.state, t3_status.STATE_UNAVAILABLE)

    def test_no_credentials_is_disconnected(self) -> None:
        poller = t3_status.T3StatusPoller(self.records.append, client_factory=lambda: None)
        poller.poll_once()
        self.assertEqual(poller.state, t3_status.STATE_DISCONNECTED)

    def test_changed_credentials_rebuild_the_client(self) -> None:
        signature = ["first"]
        built = []

        def factory():
            built.append(signature[0])
            return self.client

        poller = t3_status.T3StatusPoller(
            self.records.append,
            client_factory=factory,
            credentials_signature=lambda: signature[0],
        )
        poller.poll_once()
        poller.poll_once()
        signature[0] = "second"
        poller.poll_once()
        self.assertEqual(built, ["first", "second"])

    def test_environment_links_are_refreshed_once_a_minute(self) -> None:
        for _ in range(3):
            self.poller.poll_once()
            self.clock.advance(5)
        self.clock.advance(60)
        self.poller.poll_once()
        names = [name for name, _ in self.client.calls]
        self.assertEqual(names.count("t3_environment_links"), 2)


class MonitorIntegrationTests(unittest.TestCase):
    def test_threads_show_with_title_origin_and_clear_when_seen(self) -> None:
        client = FakeClient()
        clock = Clock()
        clock.wall = datetime.now(timezone.utc)
        monitor = LiveAgentMonitor()
        poller = make_poller(client, clock, [])
        poller.ingest = monitor.ingest_record

        client.threads[KARLY_WSL] = [thread("abcdef12-3456", "waiting", title="Deploy the TorBox fix")]
        poller.poll_once()
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)
        status = snapshot.statuses[0]
        self.assertEqual(status.display_name, "Deploy the TorBox fix (abcdef12)")
        self.assertEqual(status.origin, "Claude in T3 Code · KARLY-WSL")
        self.assertEqual(available_session_open_actions(status), (SESSION_OPEN_APP,))

        client.threads[KARLY_WSL] = [thread("abcdef12-3456", "completed", title="Deploy the TorBox fix",
                                            updated=clock.wall)]
        poller.poll_once()
        self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.COMPLETED)

        client.threads[KARLY_WSL] = []
        poller.poll_once()
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot.statuses, ())
        self.assertEqual(snapshot.aggregate.mode, AgentMode.IDLE_READY)

    def test_t3_renamed_thread_takes_the_new_title(self) -> None:
        client = FakeClient()
        clock = Clock()
        clock.wall = datetime.now(timezone.utc)
        monitor = LiveAgentMonitor()
        poller = make_poller(client, clock, [])
        poller.ingest = monitor.ingest_record
        client.threads[KARLY_WSL] = [thread("abcdef12-3456", "running", title="New thread")]
        poller.poll_once()
        client.threads[KARLY_WSL] = [thread("abcdef12-3456", "waiting", title="Check the WSL firewall")]
        poller.poll_once()
        self.assertEqual(monitor.snapshot().statuses[0].display_name, "Check the WSL firewall (abcdef12)")


class CredentialTests(unittest.TestCase):
    def test_saved_credentials_are_private_and_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "t3-code.json"
            t3_status.save_t3_credentials(t3_status.T3Credentials("secret", 2000.0), path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            loaded = t3_status.load_t3_credentials(path)
            self.assertEqual(loaded, t3_status.T3Credentials("secret", 2000.0))
            self.assertTrue(loaded.expired(now=2000.0))
            self.assertFalse(loaded.expired(now=1999.0))
            self.assertTrue(t3_status.delete_t3_credentials(path))
            self.assertIsNone(t3_status.load_t3_credentials(path))
            self.assertFalse(t3_status.delete_t3_credentials(path))

    def test_server_origin_comes_from_the_runtime_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertIsNone(t3_status.t3_server_origin(home))
            (home / "userdata").mkdir()
            (home / "userdata" / "server-runtime.json").write_text(
                json.dumps({"origin": "http://127.0.0.1:3773/", "port": 3773})
            )
            self.assertEqual(t3_status.t3_server_origin(home), "http://127.0.0.1:3773")
            self.assertTrue(t3_status.t3_installed(home))


class FakeT3Server:
    """A stand-in for T3's OAuth and MCP endpoints."""

    def __init__(self) -> None:
        self.sessions_initialized = 0
        self.expire_session_once = False
        self.token = "issued-token"
        self.last_authorize: dict[str, str] = {}
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _json(self, code: int, body: dict, headers: dict | None = None) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode()
                if self.path == "/oauth/mcp/register":
                    body = json.loads(raw)
                    assert body["client_name"] == "SidePulse"
                    return self._json(201, {"client_id": "client-1", "redirect_uris": body["redirect_uris"]})
                if self.path == "/oauth/mcp/token":
                    form = dict(urllib.parse.parse_qsl(raw))
                    assert form["code"] == "code-1" and form["code_verifier"]
                    return self._json(200, {"access_token": outer.token, "token_type": "Bearer",
                                            "expires_in": 2592000, "scope": "orchestration:read"})
                if self.path == "/mcp":
                    if self.headers.get("Authorization") != f"Bearer {outer.token}":
                        return self._json(401, {"error": "invalid_mcp_credential"})
                    message = json.loads(raw)
                    if message["method"] == "initialize":
                        outer.sessions_initialized += 1
                        return self._json(200, {"jsonrpc": "2.0", "id": message["id"], "result": {}},
                                          {"Mcp-Session-Id": f"s{outer.sessions_initialized}"})
                    if message["method"] == "notifications/initialized":
                        self.send_response(202)
                        self.end_headers()
                        return
                    if outer.expire_session_once:
                        outer.expire_session_once = False
                        return self._json(404, {"error": "unknown session"})
                    result = {"structuredContent": {"links": [], "echo": message["params"]["name"]}}
                    data = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result})
                    payload = f"event: message\ndata: {data}\n\n".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self._json(404, {})

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.t3 = FakeT3Server()
        self.addCleanup(self.t3.close)

    def test_mcp_client_reads_streamed_results_and_renews_a_lost_session(self) -> None:
        client = t3_status.T3McpClient(self.t3.origin, "issued-token")
        self.assertEqual(client.call_tool("t3_environment_links", {})["echo"], "t3_environment_links")
        self.assertEqual(client.session_id, "s1")
        self.t3.expire_session_once = True
        self.assertEqual(client.call_tool("t3_thread_list", {})["echo"], "t3_thread_list")
        self.assertEqual(client.session_id, "s2")

    def test_mcp_client_reports_a_refused_credential(self) -> None:
        client = t3_status.T3McpClient(self.t3.origin, "wrong")
        with self.assertRaises(t3_status.T3AuthError):
            client.call_tool("t3_environment_links", {})

    def test_connect_runs_the_oauth_flow_and_saves_a_private_token(self) -> None:
        def approve(url: str) -> None:
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            self.assertEqual(query["scope"], "orchestration:read")
            self.assertEqual(query["code_challenge_method"], "S256")
            self.assertEqual(query["resource"], f"{self.t3.origin}/mcp")
            callback = query["redirect_uri"] + "?" + urllib.parse.urlencode(
                {"code": "code-1", "state": query["state"]}
            )
            threading.Thread(target=lambda: urllib.request.urlopen(callback, timeout=5).read(), daemon=True).start()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t3-code.json"
            credentials = t3_status.connect_t3(
                origin=self.t3.origin, open_url=approve, timeout_seconds=10, credentials_path=path
            )
            self.assertEqual(credentials.token, "issued-token")
            self.assertEqual(t3_status.load_t3_credentials(path).token, "issued-token")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_connect_rejects_a_denied_approval(self) -> None:
        def deny(url: str) -> None:
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            callback = query["redirect_uri"] + "?" + urllib.parse.urlencode(
                {"error": "access_denied", "state": query["state"]}
            )
            threading.Thread(target=lambda: urllib.request.urlopen(callback, timeout=5).read(), daemon=True).start()

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(t3_status.T3Error) as caught:
                t3_status.connect_t3(origin=self.t3.origin, open_url=deny, timeout_seconds=10,
                                     credentials_path=Path(tmp) / "t3-code.json")
            self.assertIn("access_denied", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
