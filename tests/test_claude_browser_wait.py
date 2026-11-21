"""A Claude browser page call still open after 10 seconds shows as Ask (ported from upstream)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from sidepulse import collector
from sidepulse.collector import CLAUDE_BROWSER_WAIT_MESSAGE, LiveAgentMonitor
from sidepulse.daemon_sessions import DaemonSessions
from sidepulse.models import AgentMode, HookEvent
from sidepulse.program_status import ProgramState

START = datetime.now(timezone.utc)


def record(event, *, tool='mcp__Claude_Browser__navigate', call='call', url='https://example.org/',
           provider='claude', at=START, **fields):
    raw = {'session_id': 'browser-session', 'hook_event_name': event, 'tool_name': tool,
           'tool_use_id': call, 'tool_input': {'url': url}, **fields}
    return HookEvent(provider=provider, logged_at=at, event_name=event, raw=raw,
                     session_id='browser-session', tool_name=tool)


def mode_at(monitor, seconds):
    with patch.object(collector, 'datetime') as clock:
        clock.now.return_value = START + timedelta(seconds=seconds)
        clock.fromisoformat = datetime.fromisoformat
        row, = monitor.snapshot().statuses
    return row


def monitor():
    return LiveAgentMonitor(recover_on_init=False)


@pytest.mark.parametrize('tool', ['mcp__Claude_Browser__navigate', 'mcp__Claude_Browser__preview_start'])
def test_open_page_call_asks_after_10_seconds_and_ignores_parallel_completions(tool):
    live = monitor()
    live.ingest_record(record('PreToolUse', tool=tool))
    assert mode_at(live, 9.9).mode == AgentMode.TOOL_RUNNING
    live.ingest_record(record('PostToolUse', tool='Read', call='parallel', at=START + timedelta(seconds=5)))
    row = mode_at(live, 10)
    assert row.mode == AgentMode.WAITING_FOR_INPUT
    assert row.message == CLAUDE_BROWSER_WAIT_MESSAGE
    live.ingest_record(record('PostToolUse', tool=tool, at=START + timedelta(seconds=12)))
    assert mode_at(live, 30).mode == AgentMode.WORKING


@pytest.mark.parametrize('event', ['PostToolUse', 'PostToolUseFailure', 'Stop', 'PermissionRequest'])
def test_a_new_status_for_the_call_or_session_cancels_the_wait(event):
    live = monitor()
    live.ingest_record(record('PreToolUse'))
    live.ingest_record(record(event, at=START + timedelta(seconds=1)))
    assert mode_at(live, 30).message != CLAUDE_BROWSER_WAIT_MESSAGE


@pytest.mark.parametrize('fields,expected', [
    ({'tool_response': {'success': False}}, AgentMode.BLOCKED_ERROR),
    ({'sidepulse_mode': 'waiting_for_input'}, AgentMode.WAITING_FOR_INPUT),
])
def test_a_parallel_error_or_explicit_status_still_applies(fields, expected):
    live = monitor()
    live.ingest_record(record('PreToolUse'))
    live.ingest_record(record('PostToolUse', tool='Bash', call='other', at=START + timedelta(seconds=1), **fields))
    row = mode_at(live, 30)
    assert (row.tool_name, row.mode) == ('Bash', expected)


@pytest.mark.parametrize('overrides', [
    {'url': 'file:///tmp/page.html'},
    {'url': 'about:blank'},
    {'tool': 'mcp__Claude_Browser__read_page'},
    {'provider': 'codex'},
    {'call': ''},
])
def test_only_claude_page_loads_of_web_urls_wait(overrides):
    live = monitor()
    live.ingest_record(record('PreToolUse', **overrides))
    assert mode_at(live, 30).message != CLAUDE_BROWSER_WAIT_MESSAGE


def test_the_ask_reaches_phones_through_the_mirror():
    daemon = DaemonSessions(ProgramState(publisher='mac'), monitor=monitor(), start_t3=False)
    daemon.ingest_record(record('PreToolUse'))
    with patch.object(collector, 'datetime') as clock:
        clock.now.return_value = START + timedelta(seconds=11)
        clock.fromisoformat = datetime.fromisoformat
        daemon.mirror()
    published, = daemon.program_state.statuses()
    assert published.mode == AgentMode.WAITING_FOR_INPUT
