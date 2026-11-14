"""Spot hook rows from T3 Code subagents running on this Mac.

A ``delegate_task`` child runs as an ordinary agent process, so its hooks look
like any other T3-hosted session. T3's own state database knows better: the
thread is a subagent (``lineage.relationshipToParent``) when delegated on the
same machine, or carries ``delegatedFrom`` when another machine sent it. This
mirrors T3's ``isDelegatedAppThread``. Threads an agent opened on request with
``t3_thread_launch`` have neither, so they still show.

The database is T3's internal projection, read-only; if its shape changes,
nothing is hidden.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from .models import HookEvent
from .origin import T3CODE_THREAD_KEY, is_relayed_origin, is_t3code_origin

T3_STATE_DB = Path.home() / ".t3" / "userdata" / "statev2.sqlite"
# A session T3 hasn't recorded yet (its first hook can beat the projection) is
# looked up again after this long.
T3_UNKNOWN_RECHECK_SECONDS = 5.0

_THREAD_BY_ID = "SELECT payload_json FROM orchestration_v2_projection_threads WHERE thread_id = ?"
_THREAD_BY_NATIVE_SESSION = (
    "SELECT t.payload_json FROM orchestration_v2_projection_provider_threads p"
    " JOIN orchestration_v2_projection_threads t ON t.thread_id = p.thread_id"
    " WHERE json_extract(p.payload_json, '$.nativeThreadRef.nativeId') = ?"
    " ORDER BY p.updated_at DESC LIMIT 1"
)


def is_delegated_thread(payload: dict[str, Any]) -> bool:
    lineage = payload.get("lineage")
    subagent = isinstance(lineage, dict) and lineage.get("relationshipToParent") == "subagent"
    return subagent or payload.get("delegatedFrom") is not None


class T3DelegateLookup:
    def __init__(
        self,
        state_db: Path | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state_db = state_db or T3_STATE_DB
        self.monotonic = monotonic
        self.known: dict[str, bool] = {}
        self.unknown_checked_at: dict[str, float] = {}

    def is_delegate(self, record: HookEvent) -> bool:
        """Whether ``record`` comes from a T3 subagent on this Mac."""
        if not is_t3code_origin(record.origin) or is_relayed_origin(record.origin):
            return False
        thread_id = record.raw.get(T3CODE_THREAD_KEY)
        thread_id = thread_id if isinstance(thread_id, str) and thread_id else None
        session_id = record.session_id
        if not thread_id and not session_id:
            return False
        key = f"{record.provider}:{session_id or thread_id}"
        if key in self.known:
            return self.known[key]
        now = self.monotonic()
        checked_at = self.unknown_checked_at.get(key)
        if checked_at is not None and now - checked_at < T3_UNKNOWN_RECHECK_SECONDS:
            return False
        payload = self._thread_payload(thread_id, session_id)
        if payload is None:
            self.unknown_checked_at[key] = now
            return False
        self.unknown_checked_at.pop(key, None)
        self.known[key] = is_delegated_thread(payload)
        return self.known[key]

    def _thread_payload(self, thread_id: str | None, session_id: str | None) -> dict[str, Any] | None:
        if not self.state_db.exists():
            return None
        try:
            connection = sqlite3.connect(f"file:{self.state_db}?mode=ro", uri=True, timeout=0.5)
            try:
                row = None
                if thread_id:
                    row = connection.execute(_THREAD_BY_ID, (thread_id,)).fetchone()
                if row is None and session_id:
                    row = connection.execute(_THREAD_BY_NATIVE_SESSION, (session_id,)).fetchone()
            finally:
                connection.close()
            payload = json.loads(row[0]) if row else None
        except (sqlite3.Error, ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None
