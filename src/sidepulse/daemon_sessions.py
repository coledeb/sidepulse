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
import urllib.parse
from dataclasses import replace
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
# get_state replies are capped at 1 MiB, so row text shown in the menu is
# clipped here; the monitor still read the whole event.
SNAPSHOT_TEXT_LIMITS = {"message": 2048, "display_name": 512, "tool_name": 256}


def clip_text(value: str | None, limit: int) -> str | None:
    if value is None or len(value.encode("utf-8")) <= limit:
        return value
    return value.encode("utf-8")[: limit - 3].decode("utf-8", errors="ignore") + "…"


def clipped_status(status: AgentStatus, session_link: str | None = None) -> AgentStatus:
    changes = {
        field: clip_text(getattr(status, field), limit)
        for field, limit in SNAPSHOT_TEXT_LIMITS.items()
        if getattr(status, field) is not None
        and len(getattr(status, field).encode("utf-8")) > limit
    }
    if session_link and not status.session_link:
        changes["session_link"] = session_link
    return replace(status, **changes) if changes else status


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


def runs_on_this_machine(status: AgentStatus) -> bool:
    """False for threads read from linked T3 environments and rows relayed
    from other computers: they run elsewhere and report themselves."""
    from .origin import is_relayed_origin
    from .t3_status import T3_EVENT_NAME

    return status.event_name != T3_EVENT_NAME and not is_relayed_origin(status.origin)


def should_mirror(status: AgentStatus) -> bool:
    return runs_on_this_machine(status)


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
        self.recovery_lock = threading.Lock()

    # lifecycle

    def start(self) -> None:
        self.schedule_recovery()
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

    def schedule_recovery(self) -> None:
        threading.Thread(target=self._recover, name="sidepulse-recovery", daemon=True).start()

    def _recover(self) -> None:
        with self.recovery_lock:
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
        sources = default_sources(settings)
        if sources != self.monitor.recovery_sources:
            # A transcript fallback was turned on or off: read the new sources now.
            self.monitor.recovery_sources = sources
            self.schedule_recovery()

    # output

    def program_statuses(self) -> tuple[AgentStatus, ...]:
        """ProgramState rows that are not mirrors of this monitor's own rows."""
        return tuple(
            status for status in self.program_state.statuses(skip_scope=is_mirror_scope)
        )

    def session_link(self, status: AgentStatus) -> str | None:
        scope = mirror_scope(status.agent_id)
        if scope not in self.mirrored:
            return None
        return "sidepulse://sessions?" + urllib.parse.urlencode(
            {"publisher": self.program_state.publisher, "scope": scope},
            quote_via=urllib.parse.quote,
        )

    def local_mode(self) -> AgentMode:
        """Aggregate mode of the agents running on this machine only.

        Remote work keeps going while this Mac sleeps, so it must not keep it awake.
        """
        return self.monitor.snapshot(only=runs_on_this_machine).aggregate.mode

    def snapshot(self, include_stale: bool = False) -> MonitorSnapshot:
        snapshot = self.monitor.snapshot(include_stale=include_stale)
        aggregate = snapshot.aggregate
        if aggregate.representative is not None:
            aggregate = replace(aggregate, representative=clipped_status(aggregate.representative))
        return replace(
            snapshot,
            aggregate=aggregate,
            statuses=tuple(clipped_status(row, self.session_link(row)) for row in snapshot.statuses),
            stale_statuses=tuple(clipped_status(row) for row in snapshot.stale_statuses),
        )

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
        # Read the rows under the lock, so a slower caller cannot publish an
        # older view after a newer one.
        with self.mirror_lock:
            snapshot = self.monitor.snapshot(include_stale=False, with_extra=False)
            wanted: dict[str, tuple[dict, str]] = {}
            for status in snapshot.statuses:
                if not should_mirror(status):
                    continue
                update = mirrored_update(status, source_name)
                if update is not None:
                    # A newer event with the same state still refreshes the mirror.
                    freshness = status.updated_at.isoformat()
                    wanted[update["scope"]] = (update, freshness)
            for scope, (update, freshness) in wanted.items():
                signature = json.dumps([update["status"], update["context"], freshness], sort_keys=True)
                if self.mirrored.get(scope) == signature:
                    continue
                self.program_state.submit(update)
                self.mirrored[scope] = signature
            for scope in set(self.mirrored) - set(wanted):
                self.program_state.submit(
                    {"v": 1, "type": "program_status", "scope": scope, "status": {"state": "clear"}}
                )
                del self.mirrored[scope]

