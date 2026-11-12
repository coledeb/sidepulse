"""Agent sessions owned by the daemon.

Hooks hand their raw lines to the daemon (``hook_event``). The daemon keeps
SidePulse's stateful session monitor (closing questions, permission prompts,
T3 Code settling, project names, recovery from the hook logs) and follows
linked T3 Code environments. Its rows are mirrored into ``ProgramState`` as
OSC-style updates so phone sync and the bridge relay carry them, reduced to
agent status only: no prompts, messages, titles, paths or hostname.
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
from datetime import datetime
from typing import Callable

from .collector import LiveAgentMonitor, MonitorSnapshot, default_sources
from .ipc import default_latest_state_path
from .models import AgentMode, AgentStatus
from .providers import parse_log_line
from .settings import load_settings

# Scopes of mirrored agent rows. ProgramState shows its own rows for OSC
# programs and remote machines; these are already shown from the monitor.
MIRROR_SCOPE_PREFIX = "agent-"
MIRROR_STATES = {
    AgentMode.IDLE_READY: "idle",
    AgentMode.WORKING: "working",
    AgentMode.TOOL_RUNNING: "working",
    AgentMode.LONG_TASK_PROGRESS: "working",
    AgentMode.WAITING_FOR_INPUT: "blocked",
    AgentMode.BLOCKED_ERROR: "error",
    AgentMode.COMPLETED: "done",
}
MIRROR_CONTEXT_LIMIT = 128


def log_sessions(message: str) -> None:
    print(f"sidepulse service: {message}", file=sys.stderr)


def mirror_scope(agent_id: str) -> str:
    return MIRROR_SCOPE_PREFIX + hashlib.sha256(agent_id.encode()).hexdigest()[:32]


def is_mirror_scope(scope: str) -> bool:
    return scope.startswith(MIRROR_SCOPE_PREFIX)


def mirrored_update(status: AgentStatus, source_name: str) -> dict | None:
    """The OSC-style update other machines and phones see for one agent row."""
    state = MIRROR_STATES.get(status.mode)
    if state is None:
        return None
    app = "claude-code" if status.provider == "claude" else status.provider
    body: dict[str, object] = {"state": state, "app": app[:32]}
    if state == "blocked":
        body["kind"] = "permission" if "permission" in status.event_name.lower() else "question"
    context = {
        "provider": status.provider,
        "session_id": status.session_id,
        "mode": status.mode.value,
        "event_name": status.event_name,
        "origin": status.origin,
        "source_name": source_name,
    }
    return {
        "v": 1,
        "type": "program_status",
        "scope": mirror_scope(status.agent_id),
        "status": body,
        "context": {
            key: str(value)[:MIRROR_CONTEXT_LIMIT]
            for key, value in context.items()
            if value
        },
    }


def should_mirror(status: AgentStatus) -> bool:
    from .origin import is_relayed_origin
    from .t3_status import T3_EVENT_NAME

    # Threads read from linked T3 environments run on other machines, which
    # report them themselves.
    return status.event_name != T3_EVENT_NAME and not is_relayed_origin(status.origin)


class DaemonSessions:
    def __init__(
        self,
        program_state,
        *,
        monitor: LiveAgentMonitor | None = None,
        start_t3: bool = True,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        settings = load_settings()
        self.program_state = program_state
        self.on_change = on_change
        self.monitor = monitor or LiveAgentMonitor(
            recovery_sources=default_sources(settings),
            stale_after_seconds=settings.idle_timeout_seconds,
            completed_visible_seconds=settings.completed_visible_seconds,
            latest_state_path=default_latest_state_path(),
            recover_on_init=False,
        )
        self.monitor.extra_statuses = self.program_statuses
        self.mirror_lock = threading.Lock()
        self.mirrored: dict[str, str] = {}
        self.t3_poller = None
        self.start_t3 = start_t3

    # lifecycle

    def start(self) -> None:
        threading.Thread(target=self._recover, name="sidepulse-recovery", daemon=True).start()
        if self.start_t3:
            from .t3_status import T3StatusPoller, t3_installed

            self.t3_poller = T3StatusPoller(
                self.ingest_record,
                on_state_change=lambda state: log_sessions(f"t3 state={state}"),
                local_waiting=self.monitor.t3_settle_candidates,
                on_local_settled=self.settle,
            )
            if t3_installed():
                self.t3_poller.start()

    def close(self) -> None:
        if self.t3_poller is not None:
            self.t3_poller.stop()

    def _recover(self) -> None:
        try:
            self.monitor.reconcile_recovery_sources()
        except Exception as exc:
            log_sessions(f"history recovery error: {exc}")
        self.changed()

    # input

    def ingest_line(self, provider: str, line: dict) -> None:
        record = parse_log_line(provider, json.dumps(line, separators=(",", ":"), ensure_ascii=False))
        if record is not None:
            self.ingest_record(record)

    def ingest_record(self, record) -> None:
        self.monitor.ingest_record(record)
        self.changed()

    def settle(self, row, settled_at: datetime) -> None:
        if self.monitor.settle_waiting_session(row, settled_at):
            log_sessions(f"t3 settled agent={row.agent_id} thread={row.t3_thread_id or 'by-timing'}")
            self.changed()

    def reconnect_t3(self) -> None:
        if self.t3_poller is None:
            return
        self.t3_poller.reconnect()
        self.t3_poller.start()

    def t3_info(self) -> dict:
        poller = self.t3_poller
        if poller is None:
            return {}
        return {"state": poller.state, "error": poller.last_error}

    def apply_settings(self, settings) -> None:
        self.monitor.stale_after_seconds = settings.idle_timeout_seconds
        self.monitor.completed_visible_seconds = settings.completed_visible_seconds

    # output

    def program_statuses(self) -> tuple[AgentStatus, ...]:
        """ProgramState rows that are not mirrors of this monitor's own rows."""
        return tuple(
            status for status in self.program_state.statuses(skip_scope=is_mirror_scope)
        )

    def snapshot(self, include_stale: bool = False) -> MonitorSnapshot:
        return self.monitor.snapshot(include_stale=include_stale)

    def changed(self) -> None:
        try:
            self.mirror()
        except Exception as exc:
            log_sessions(f"session mirror failed: {exc}")
        if self.on_change is not None:
            self.on_change()

    def mirror(self) -> None:
        """Bring ProgramState's agent mirrors in line with the visible rows."""
        from .relay import DEFAULT_RELAY_SOURCE_NAME, load_relay_config

        source_name = load_relay_config().machine_name or DEFAULT_RELAY_SOURCE_NAME
        snapshot = self.monitor.snapshot(include_stale=False, with_extra=False)
        wanted: dict[str, dict] = {}
        for status in snapshot.statuses:
            if not should_mirror(status):
                continue
            update = mirrored_update(status, source_name)
            if update is not None:
                wanted[update["scope"]] = update
        with self.mirror_lock:
            for scope, update in wanted.items():
                signature = json.dumps([update["status"], update["context"]], sort_keys=True)
                if self.mirrored.get(scope) == signature:
                    continue
                self.program_state.submit(update)
                self.mirrored[scope] = signature
            for scope in set(self.mirrored) - set(wanted):
                self.program_state.submit(
                    {"v": 1, "type": "program_status", "scope": scope, "status": {"state": "clear"}}
                )
                del self.mirrored[scope]

