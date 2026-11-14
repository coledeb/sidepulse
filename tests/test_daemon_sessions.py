"""The daemon owns SidePulse's stateful agent sessions and mirrors them as OSC updates."""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from sidepulse.collector import LiveAgentMonitor
from sidepulse.daemon_sessions import DaemonSessions, is_mirror_scope, mirrored_update
from sidepulse.models import AgentMode, AgentStatus
from sidepulse.program_status import ProgramState, bridge_update
from sidepulse.program_transport import ProgramServer, daemon_command, send_hook_event


def sessions(state=None):
    state = state or ProgramState(publisher='mac')
    return DaemonSessions(state, monitor=LiveAgentMonitor(recover_on_init=False), start_t3=False)


def status(**fields):
    values = dict(provider='claude', agent_id='claude:session:s1', display_name='repo',
                  mode=AgentMode.WORKING, updated_at=datetime.now(timezone.utc),
                  event_name='UserPromptSubmit', session_id='s1', origin='T3 Code',
                  cwd='/Users/someone/private-project', message='secret message')
    values.update(fields)
    return AgentStatus(**values)


def stop_line(message, session='s1'):
    return {'hook_event_name': 'Stop', 'session_id': session, 'cwd': '/tmp/project',
            'last_assistant_message': message}


def test_hook_lines_feed_the_stateful_monitor_and_mirror_into_program_state():
    daemon = sessions()
    daemon.ingest_line('claude', {'hook_event_name': 'UserPromptSubmit', 'session_id': 's1',
                                  'cwd': '/tmp/project', 'prompt': 'do it'})
    rows = daemon.snapshot().statuses
    assert [row.mode for row in rows] == [AgentMode.WORKING]
    assert rows[0].agent_id.startswith('claude:')
    # The mirror exists for phones and the bridge, but is not shown twice.
    assert len(daemon.program_state.statuses()) == 1
    assert daemon.program_statuses() == ()


def test_closing_question_stays_waiting_like_the_old_menu_bar_monitor():
    daemon = sessions()
    daemon.ingest_line('claude', stop_line('Done. Should I push it?'))
    assert daemon.snapshot().aggregate.mode == AgentMode.WAITING_FOR_INPUT
    mirrored = daemon.program_state.statuses()[0]
    assert mirrored.mode == AgentMode.WAITING_FOR_INPUT


def test_mirror_clears_rows_the_monitor_no_longer_shows():
    daemon = sessions()
    daemon.ingest_line('claude', stop_line('All done.'))
    assert daemon.program_state.statuses()
    daemon.monitor.completed_visible_seconds = 0
    with patch('sidepulse.collector.datetime') as clock:
        clock.now.return_value = datetime.now(timezone.utc) + timedelta(seconds=5)
        clock.fromisoformat = datetime.fromisoformat
        daemon.mirror()
    assert not daemon.program_state.statuses()


def test_osc_program_rows_are_shown_beside_agent_rows():
    daemon = sessions()
    daemon.program_state.submit({'v': 1, 'type': 'program_status', 'scope': 'build',
                                 'status': {'state': 'working', 'app': 'make'}})
    daemon.ingest_line('claude', stop_line('Done. Should I push it?'))
    providers = sorted(row.provider for row in daemon.snapshot().statuses)
    assert providers == ['claude', 'make']


def test_mirrored_update_carries_status_but_no_content_or_hostname():
    update = mirrored_update(status(mode=AgentMode.WAITING_FOR_INPUT, event_name='PermissionRequest'),
                             'Remote computer')
    assert is_mirror_scope(update['scope'])
    assert update['status'] == {'state': 'blocked', 'app': 'claude-code', 'kind': 'permission'}
    encoded = json.dumps(update)
    for secret in ('private-project', 'secret message', 'repo', os.uname().nodename):
        assert secret not in encoded


def test_bridge_relay_drops_messages_titles_and_paths_from_any_update():
    state = ProgramState(publisher='mac')
    state.submit({'v': 1, 'type': 'program_status', 'scope': 'build',
                  'status': {'state': 'working', 'msg': 'secret message', 'title': 'secret title'},
                  'context': {'provider': 'make', 'cwd': '/Users/someone/private-project',
                              'title': 'secret title', 'source_name': 'build-vm'}})
    _number, update = state.pending_events('bridge')[0]
    encoded = json.dumps(bridge_update(update))
    for secret in ('secret message', 'secret title', 'private-project'):
        assert secret not in encoded
    assert 'build-vm' in encoded


def test_linked_t3_threads_and_relayed_rows_are_not_mirrored():
    from sidepulse.t3_status import T3_EVENT_NAME
    daemon = sessions()
    with daemon.monitor.lock:
        daemon.monitor.statuses_by_key['t3'] = status(agent_id='t3', event_name=T3_EVENT_NAME)
        daemon.monitor.statuses_by_key['relay'] = status(agent_id='relay', origin='T3 Code · build-vm')
    daemon.mirror()
    assert not daemon.program_state.statuses()


@pytest.mark.skipif(os.name == 'nt', reason='AF_UNIX hook socket')
def test_hook_event_reaches_the_daemon_monitor_over_its_socket():
    daemon = sessions()
    with tempfile.TemporaryDirectory(prefix='sp-', dir='/tmp') as directory:
        path = Path(directory) / 'status.sock'
        server = ProgramServer(daemon.program_state, path, on_hook_event=daemon.ingest_line,
                               snapshot=daemon.snapshot)
        server.start()
        try:
            # Hook payloads can be far larger than an OSC status report.
            big = {'hook_event_name': 'PostToolUse', 'session_id': 's1', 'cwd': '/tmp/project',
                   'tool_name': 'Bash', 'tool_response': 'x' * 200_000}
            with patch('sidepulse.program_transport.status_socket_path', return_value=path), \
                    patch('sidepulse.program_transport.candidate_state_dirs', return_value=()):
                assert send_hook_event('claude', big, timeout=2)
            reply = daemon_command({'type': 'get_state'}, path=path)
        finally:
            server.stop()
    assert [row['provider'] for row in reply['snapshot']['statuses']] == ['claude']


def test_huge_prompts_do_not_push_get_state_past_its_reply_cap():
    daemon = sessions()
    for index in range(3):
        daemon.ingest_line('claude', {'hook_event_name': 'UserPromptSubmit', 'session_id': f's{index}',
                                      'cwd': '/tmp/project', 'prompt': 'p' * 600_000})
    encoded = json.dumps(daemon.snapshot(include_stale=True).to_dict())
    assert len(encoded.encode()) < 64 * 1024
    assert all(len((row.message or '').encode()) <= 2048 for row in daemon.snapshot().statuses)


def test_osc_rows_keep_their_explicit_state_past_the_hook_idle_timeout():
    daemon = sessions()
    daemon.monitor.stale_after_seconds = 60
    daemon.program_state.submit({'v': 1, 'type': 'program_status', 'scope': 'build',
                                 'status': {'state': 'working', 'app': 'make'}})
    with patch('sidepulse.collector.datetime') as clock:
        clock.now.return_value = datetime.now(timezone.utc) + timedelta(hours=2)
        snapshot = daemon.snapshot()
    assert [row.provider for row in snapshot.statuses] == ['make']
    assert snapshot.aggregate.mode == AgentMode.WORKING


def test_a_newer_event_in_the_same_state_refreshes_the_mirror():
    daemon = sessions()
    line = {'hook_event_name': 'PermissionRequest', 'session_id': 's1', 'cwd': '/tmp/project',
            'tool_name': 'Bash', 'tool_input': {'command': 'ls'}}
    daemon.ingest_line('claude', line)
    first = daemon.program_state.pending_events('phone')
    later = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    daemon.ingest_line('claude', {**line, 'logged_at': later})
    assert daemon.program_state.pending_events('phone') != first


def test_turning_on_a_transcript_fallback_recovers_from_it_right_away():
    from sidepulse.settings import AgentMonitorSettings
    daemon = sessions()
    with patch.object(daemon, 'schedule_recovery') as recover:
        daemon.apply_settings(AgentMonitorSettings(codex_transcripts_enabled=True))
        recover.assert_called_once()
        daemon.apply_settings(AgentMonitorSettings(codex_transcripts_enabled=True))
        recover.assert_called_once()


def test_local_rows_carry_their_mobile_session_link():
    daemon = sessions()
    daemon.ingest_line('claude', stop_line('Done. Should I push it?'))
    (row,) = daemon.snapshot().statuses
    assert row.session_link and row.session_link.startswith('sidepulse://sessions?publisher=mac&scope=agent-')


def test_daemon_rewrites_leds_once_a_killed_visualizer_lease_is_gone():
    from sidepulse.device_writer import DeviceCandidate
    from sidepulse.service import SidePulseService
    service = SidePulseService()
    root = Path('/Volumes/SidePulse')
    service.runtime.devices = [DeviceCandidate(root, root / 'LEDS.LED', 'mounted')]
    writes = []
    holder = {'pid': 1}
    with patch('sidepulse.service.write_led_program', side_effect=lambda program, device_path: writes.append(program)), \
            patch('sidepulse.service.led_lease_holder', side_effect=lambda _root: holder.get('pid') and holder):
        holder.clear()
        service.sync_outputs()
        assert len(writes) == 1
        holder['pid'] = 1  # the visualizer takes over and paints its own frames
        service.sync_outputs()
        holder.clear()  # it is killed without restoring the agent program
        service.sync_outputs()
    assert len(writes) == 2 and writes[0] == writes[1]


def remote_t3_working(daemon):
    from sidepulse.t3_status import t3_thread_record
    record = t3_thread_record({'threadId': 'remote-thread', 'providerInstanceId': 'codex', 'status': 'running'},
                              environment_id='env-remote', environment_label='Mac Mini',
                              status='working', observed_at=datetime.now(timezone.utc))
    daemon.ingest_record(record)


def test_local_mode_ignores_remote_t3_threads():
    # A remote thread keeps running while this Mac sleeps, so it must not keep it awake.
    daemon = sessions()
    remote_t3_working(daemon)
    assert daemon.snapshot().aggregate.mode == AgentMode.WORKING
    assert daemon.local_mode() == AgentMode.IDLE_READY


def test_local_mode_sees_local_done_hidden_behind_a_remote_working_thread():
    daemon = sessions()
    daemon.ingest_line('claude', stop_line('All done.'))
    remote_t3_working(daemon)
    assert [row.mode for row in daemon.snapshot().statuses] == [AgentMode.WORKING]
    assert daemon.local_mode() == AgentMode.COMPLETED


def test_local_mode_ignores_rows_relayed_from_other_computers():
    daemon = sessions()
    daemon.program_state.receive_update('relay:pc', {'v': 1, 'type': 'program_status', 'scope': 'agent-x',
                                                     'status': {'state': 'working', 'app': 'codex'},
                                                     'context': {'origin': 'Codex CLI', 'source_name': 'PC'},
                                                     'publisher': 'pc', 'seq': 1,
                                                     'updated_at': int(datetime.now(timezone.utc).timestamp() * 1000)})
    daemon.program_state.submit({'v': 1, 'type': 'program_status', 'scope': 'build',
                                 'status': {'state': 'working', 'app': 'make'}})
    assert len(daemon.snapshot().statuses) == 2
    assert daemon.local_mode() == AgentMode.WORKING
