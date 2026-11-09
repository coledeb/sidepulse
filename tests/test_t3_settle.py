"""Settling a T3 thread on this Mac clears its hook row's closing-question wait."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sidepulse import t3_status
from sidepulse.collector import T3_SETTLED_EVENT_NAME, LiveAgentMonitor
from sidepulse.models import AgentMode, HookEvent
from sidepulse.t3_status import LocalWaitingRow, settled_run_matches

STOP = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
OPENCODE_THREAD = "39ddb903-e905-4793-af8e-5cd82718a244"
CODEX_THREAD = "779f53ec-3461-4533-ac04-1bce659205ab"
OTHER_CODEX_THREAD = "02fc90f4-58e3-4a29-8991-222a41e7cb8c"


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def detail(
    thread_id: str,
    *,
    completed: datetime,
    settled_at: datetime | None,
    pending: int = 0,
    status: str = "completed",
    provider: str = "opencode",
) -> dict:
    return {
        "thread": {
            "threadId": thread_id,
            "providerInstanceId": provider,
            "status": status,
            "settled": settled_at is not None,
            "settledAt": iso(settled_at) if settled_at else None,
            "pendingRequestCount": pending,
            "activeRunId": None,
        },
        "recentRuns": [
            {"ordinal": 2, "status": "completed", "completedAt": iso(completed)},
            {"ordinal": 1, "status": "completed", "completedAt": iso(completed - timedelta(minutes=5))},
        ],
    }


class FakeClient:
    def __init__(self) -> None:
        self.details: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        if name == "t3_environment_links":
            return {"links": []}
        if name == "t3_thread_list":
            assert arguments == {
                "allProjects": True,
                "settled": True,
                "includeSubagents": False,
                "limit": t3_status.T3_SETTLED_SCAN_LIMIT,
            }
            threads = [
                {**item["thread"], "title": "t"}
                for item in self.details.values()
                if item["thread"]["settled"]
            ]
            return {"threads": threads}
        if name == "t3_thread_read":
            return self.details[arguments["threadId"]]
        raise AssertionError(name)

    def reads(self) -> int:
        return sum(1 for name, _ in self.calls if name == "t3_thread_read")


def hook(provider: str, event: str, session_id: str, *, at: datetime, thread_id: str | None = None,
         origin: str | None = None, **fields) -> HookEvent:
    origin = origin or f"{ {'opencode': 'OpenCode', 'codex': 'Codex', 'claude': 'Claude'}[provider] } in T3 Code"
    raw = {"hook_event_name": event, "session_id": session_id, "agent_origin": origin, **fields}
    if thread_id:
        raw["t3code_thread_id"] = thread_id
    return HookEvent(provider=provider, logged_at=at, event_name=event, raw=raw,
                     session_id=session_id, origin=origin)


def question_turn(monitor: LiveAgentMonitor, provider: str, session_id: str, *,
                  thread_id: str | None = None, origin: str | None = None) -> None:
    monitor.ingest_record(hook(provider, "UserPromptSubmit", session_id, at=STOP - timedelta(seconds=4),
                               thread_id=thread_id, origin=origin, prompt="hi"))
    monitor.ingest_record(hook(provider, "Stop", session_id, at=STOP, thread_id=thread_id, origin=origin,
                               last_assistant_message="Hello! Second test received. What's next?"))


class MatchTests(unittest.TestCase):
    row = LocalWaitingRow("opencode:session:ses_1", "opencode", STOP, OPENCODE_THREAD)

    def test_settled_after_the_latest_run(self) -> None:
        settled = STOP + timedelta(seconds=90)
        found = settled_run_matches(
            detail(OPENCODE_THREAD, completed=STOP + timedelta(milliseconds=30), settled_at=settled),
            self.row, exact=True,
        )
        self.assertEqual(found, settled)

    def test_not_settled_pending_or_running(self) -> None:
        later = STOP + timedelta(seconds=90)
        for item in (
            detail(OPENCODE_THREAD, completed=STOP, settled_at=None),
            detail(OPENCODE_THREAD, completed=STOP, settled_at=later, pending=1),
            detail(OPENCODE_THREAD, completed=STOP, settled_at=later, status="running"),
            # Settled before the latest run finished: that settle was for an earlier turn.
            detail(OPENCODE_THREAD, completed=later, settled_at=STOP + timedelta(seconds=30)),
            None,
        ):
            with self.subTest(item=item and item["thread"]):
                self.assertIsNone(settled_run_matches(item, self.row, exact=True))

    def test_timing_match_needs_the_run_to_end_with_the_stop(self) -> None:
        settled = STOP + timedelta(seconds=90)
        near = detail(CODEX_THREAD, completed=STOP + timedelta(milliseconds=936), settled_at=settled)
        far = detail(CODEX_THREAD, completed=STOP + timedelta(seconds=20), settled_at=settled)
        self.assertEqual(settled_run_matches(near, self.row, exact=False), settled)
        self.assertIsNone(settled_run_matches(far, self.row, exact=False))
        # A known thread id needs no timing.
        self.assertEqual(settled_run_matches(far, self.row, exact=True), settled)


class SettleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeClient()
        self.monitor = LiveAgentMonitor()
        self.poller = t3_status.T3StatusPoller(
            lambda record: None,
            client_factory=lambda: self.client,
            local_waiting=self.monitor.t3_settle_candidates,
            on_local_settled=self.settled,
        )
        self.settles: list[tuple[LocalWaitingRow, datetime]] = []

    def settled(self, row: LocalWaitingRow, settled_at: datetime) -> None:
        self.settles.append((row, settled_at))
        self.monitor.settle_waiting_session(row, settled_at)

    def mode(self, agent_id: str) -> AgentMode:
        return self.monitor.statuses_by_key[agent_id].mode

    def test_opencode_row_clears_when_its_thread_is_settled(self) -> None:
        question_turn(self.monitor, "opencode", "ses_1", thread_id=OPENCODE_THREAD)
        agent_id = "opencode:session:ses_1"
        self.assertEqual(self.mode(agent_id), AgentMode.WAITING_FOR_INPUT)
        self.assertEqual(self.monitor.t3_settle_candidates()[0].t3_thread_id, OPENCODE_THREAD)

        self.client.details[OPENCODE_THREAD] = detail(
            OPENCODE_THREAD, completed=STOP + timedelta(milliseconds=30), settled_at=None,
        )
        self.poller.poll_once()
        self.assertEqual(self.mode(agent_id), AgentMode.WAITING_FOR_INPUT)
        self.assertTrue(self.poller.any_active)  # keep checking quickly while it waits

        settled_at = STOP + timedelta(seconds=100)
        self.client.details[OPENCODE_THREAD] = detail(
            OPENCODE_THREAD, completed=STOP + timedelta(milliseconds=30), settled_at=settled_at,
        )
        self.poller.poll_once()
        status = self.monitor.statuses_by_key[agent_id]
        self.assertEqual(status.mode, AgentMode.IDLE_READY)
        self.assertEqual(status.event_name, T3_SETTLED_EVENT_NAME)
        self.assertEqual(status.updated_at, settled_at)
        self.assertEqual(self.monitor.t3_settle_candidates(), [])
        self.poller.poll_once()
        self.assertFalse(self.poller.any_active)

    def test_codex_row_matches_its_thread_by_run_timing(self) -> None:
        question_turn(self.monitor, "codex", "01a1b9d2-db41")
        settled_at = STOP + timedelta(seconds=60)
        self.client.details[OTHER_CODEX_THREAD] = detail(
            OTHER_CODEX_THREAD, completed=STOP - timedelta(minutes=9), settled_at=settled_at, provider="codex",
        )
        self.client.details[CODEX_THREAD] = detail(
            CODEX_THREAD, completed=STOP + timedelta(milliseconds=936), settled_at=settled_at, provider="codex",
        )
        self.poller.poll_once()
        self.assertEqual(self.mode("codex:session:01a1b9d2-db41"), AgentMode.IDLE_READY)
        self.assertEqual([row.t3_thread_id for row, _ in self.settles], [None])

    def test_two_threads_ending_together_are_left_alone(self) -> None:
        question_turn(self.monitor, "codex", "01a1b9d2-db41")
        for thread_id in (CODEX_THREAD, OTHER_CODEX_THREAD):
            self.client.details[thread_id] = detail(
                thread_id, completed=STOP + timedelta(milliseconds=400),
                settled_at=STOP + timedelta(seconds=60), provider="codex",
            )
        self.poller.poll_once()
        self.assertEqual(self.mode("codex:session:01a1b9d2-db41"), AgentMode.WAITING_FOR_INPUT)

    def test_other_providers_threads_never_match(self) -> None:
        question_turn(self.monitor, "codex", "01a1b9d2-db41")
        self.client.details[OPENCODE_THREAD] = detail(
            OPENCODE_THREAD, completed=STOP, settled_at=STOP + timedelta(seconds=60), provider="opencode",
        )
        self.poller.poll_once()
        self.assertEqual(self.mode("codex:session:01a1b9d2-db41"), AgentMode.WAITING_FOR_INPUT)
        self.assertEqual(self.client.reads(), 0)

    def test_a_thread_that_did_not_match_is_read_once(self) -> None:
        question_turn(self.monitor, "codex", "01a1b9d2-db41")
        self.client.details[CODEX_THREAD] = detail(
            CODEX_THREAD, completed=STOP + timedelta(seconds=30),
            settled_at=STOP + timedelta(seconds=60), provider="codex",
        )
        self.poller.poll_once()
        self.poller.poll_once()
        self.assertEqual(self.client.reads(), 1)
        self.assertEqual(self.mode("codex:session:01a1b9d2-db41"), AgentMode.WAITING_FOR_INPUT)

    def test_only_closing_questions_on_this_mac_are_checked(self) -> None:
        # A permission prompt still blocks the agent, settled or not.
        self.monitor.ingest_record(hook("opencode", "PermissionRequest", "ses_perm", at=STOP,
                                        thread_id=OPENCODE_THREAD, tool_name="shell"))
        # A relayed or linked-machine row is not this Mac's to settle.
        question_turn(self.monitor, "claude", "remote-1", origin="Claude in T3 Code · KARLY-WSL")
        # Not hosted by T3.
        question_turn(self.monitor, "codex", "cli-1", origin="Codex CLI")
        self.assertEqual(self.monitor.t3_settle_candidates(), [])

    def test_a_row_that_moved_on_is_not_settled(self) -> None:
        question_turn(self.monitor, "opencode", "ses_1", thread_id=OPENCODE_THREAD)
        row = self.monitor.t3_settle_candidates()[0]
        self.monitor.ingest_record(hook("opencode", "UserPromptSubmit", "ses_1", at=STOP + timedelta(seconds=5),
                                        thread_id=OPENCODE_THREAD, prompt="more"))
        self.assertFalse(self.monitor.settle_waiting_session(row, STOP + timedelta(seconds=9)))
        self.assertEqual(self.mode("opencode:session:ses_1"), AgentMode.WORKING)


if __name__ == "__main__":
    unittest.main()
