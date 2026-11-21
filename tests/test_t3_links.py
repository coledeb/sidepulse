"""T3 Code rows open their thread with T3's t3code://threads/<env>/<thread> link."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from sidepulse import origin
from sidepulse.collector import agent_status_from_dict, status_from_event
from sidepulse.models import HookEvent
from sidepulse.origin import t3_environment_id, t3_link_for_thread, valid_t3_link
from sidepulse.session_actions import SESSION_OPEN_APP, session_open_action_label, session_open_target
from sidepulse.t3_status import t3_thread_record

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def hook(event: str, *, origin_label: str, thread_id: str | None = "thread-1", session_id: str = "s1") -> HookEvent:
    raw = {"hook_event_name": event, "session_id": session_id, "agent_origin": origin_label}
    if thread_id:
        raw["t3code_thread_id"] = thread_id
    return HookEvent(provider="claude", logged_at=NOW, event_name=event, raw=raw,
                     session_id=session_id, origin=origin_label)


def test_links_are_built_and_validated():
    assert t3_link_for_thread("env 1", "thread/2") == "t3code://threads/env%201/thread%2F2"
    assert t3_link_for_thread(None, "thread") is None
    assert valid_t3_link("t3code://threads/../thread") is None
    assert valid_t3_link("https://example.com") is None


def test_environment_id_is_read_from_t3_home(tmp_path):
    (tmp_path / "userdata").mkdir()
    (tmp_path / "userdata" / "environment-id").write_text("env-local\n")
    assert t3_environment_id({"T3CODE_HOME": str(tmp_path)}) == "env-local"
    assert t3_environment_id({"T3CODE_HOME": str(tmp_path / "missing")}) is None


def test_local_t3_hook_links_its_thread_and_keeps_it_on_bare_events():
    with patch.object(origin, "t3_environment_id", return_value="env-local"):
        first = status_from_event(hook("UserPromptSubmit", origin_label="Claude in T3 Code"))
        bare = status_from_event(hook("Stop", origin_label="Claude in T3 Code", thread_id=None), previous=first)
    assert first.t3_link == "t3code://threads/env-local/thread-1"
    assert bare.t3_link == first.t3_link
    assert agent_status_from_dict(bare.to_dict()).t3_link == first.t3_link


def test_hooks_outside_t3_or_relayed_from_another_computer_get_no_link():
    with patch.object(origin, "t3_environment_id", return_value="env-local"):
        terminal = status_from_event(hook("UserPromptSubmit", origin_label="Claude CLI"))
        relayed = status_from_event(hook("UserPromptSubmit", origin_label="Claude in T3 Code · KARLY-WSL"))
    assert terminal.t3_link is None
    assert relayed.t3_link is None


def test_linked_environment_rows_link_with_their_own_environment():
    record = t3_thread_record(
        {"threadId": "remote-thread", "providerInstanceId": "codex", "title": "Fix it"},
        environment_id="env-remote", environment_label="Mac Mini", status="working", observed_at=NOW,
    )
    with patch.object(origin, "t3_environment_id", side_effect=AssertionError("not this Mac's id")):
        status = status_from_event(record)
    assert status.t3_link == "t3code://threads/env-remote/remote-thread"
    assert session_open_target(status, SESSION_OPEN_APP) == ("url", status.t3_link)
    assert session_open_action_label(status, SESSION_OPEN_APP) == "Open in T3 Code"


def test_t3_rows_without_a_link_still_bring_t3_forward():
    with patch.object(origin, "t3_environment_id", return_value=None):
        status = status_from_event(hook("UserPromptSubmit", origin_label="Claude in T3 Code"))
    assert status.t3_link is None
    assert session_open_target(status, SESSION_OPEN_APP) == ("app", origin.T3CODE_BUNDLE_ID)

