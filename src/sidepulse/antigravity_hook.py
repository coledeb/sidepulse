"""Adapt Antigravity (agy) lifecycle hooks to SidePulse events.

Antigravity's ``hooks.json`` sends a camelCase payload that does not name its
event, so the installed command passes the event with ``--event``.

Two harnesses send these hooks, and they order events differently:

* The ``agy`` CLI (and the IDE and app, which share ``~/.gemini/config``) run
  one ``PreInvocation``/``PostInvocation`` pair per model call, starting at
  ``invocationNum`` 0, and send ``Stop`` when the turn ends. Its transcript is a
  JSONL file.
* The ACP harness T3 Code runs (``agy_acp_server``) runs one pair per turn,
  numbered from 1, and never sends ``Stop`` at the end of a turn; its only
  ``Stop`` (``session_end``) arrives when a session starts. Its transcript is a
  SQLite database (``antigravity-acp/conversations/<id>.db``).

The hook must never print to stdout: Antigravity reads a ``PreToolUse`` hook's
stdout as a permission decision, and ``{}`` (or any JSON without a usable
decision) denies the tool call. Empty stdout leaves the decision to Antigravity.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

ANTIGRAVITY_NATIVE_EVENTS = (
    "SessionStart",
    "PreInvocation",
    "PostInvocation",
    "PreToolUse",
    "PostToolUse",
    "Stop",
)
# Events grouped under a tool ``matcher`` in hooks.json; the rest are flat lists.
ANTIGRAVITY_TOOL_EVENTS = frozenset({"PreToolUse", "PostToolUse"})

# Stop reasons, as Antigravity names them with the EXECUTOR_TERMINATION_REASON_
# prefix stripped.
INTERRUPT_REASONS = frozenset({"USER_CANCELED", "USER_CANCELLED"})
FAILURE_REASONS = frozenset(
    {
        "ERROR",
        "MAX_INVOCATIONS",
        "MAX_FORCED_INVOCATIONS",
        "MAX_TOKEN_BUDGET_EXCEEDED",
    }
)
# The ACP harness reports a session closing this way, at the start of the next
# session rather than the end of a turn, so it says nothing about the turn.
IGNORED_STOP_REASONS = frozenset({"SESSION_END"})

# The ACP transcript stores each step as a protobuf blob. A planner response is
# step type 15; its text is field 1 of the message in field 20.
ACP_PLANNER_RESPONSE_STEP_TYPE = 15
ACP_PLANNER_RESPONSE_FIELD = 20
ACP_PLANNER_TEXT_FIELD = 1

TRANSCRIPT_TAIL_BYTES = 256 * 1024


def is_acp_payload(payload: dict[str, Any]) -> bool:
    return str(payload.get("transcriptPath") or "").endswith(".db")


def normalize_payload(event_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """The SidePulse hook payload for one Antigravity event, or None to drop it."""
    acp = is_acp_payload(payload)
    sidepulse_event = sidepulse_event_name(event_name, payload, acp=acp)
    if sidepulse_event is None:
        return None

    normalized: dict[str, Any] = {
        "hook_event_name": sidepulse_event,
        "antigravity_event": event_name,
        "session_id": str(payload.get("conversationId") or ""),
    }
    workspace_paths = payload.get("workspacePaths")
    if isinstance(workspace_paths, list) and workspace_paths and isinstance(workspace_paths[0], str):
        normalized["cwd"] = workspace_paths[0]
    for source, target in (
        ("transcriptPath", "transcript_path"),
        ("modelName", "model"),
    ):
        value = payload.get(source)
        if isinstance(value, str) and value:
            normalized[target] = value

    prompt = payload.get("lastUserInput")
    if sidepulse_event == "UserPromptSubmit" and isinstance(prompt, str) and prompt:
        normalized["prompt"] = prompt

    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        if isinstance(tool_call.get("name"), str):
            normalized["tool_name"] = tool_call["name"]
        if isinstance(tool_call.get("args"), dict):
            normalized["tool_input"] = tool_call["args"]
    error = payload.get("error")
    if isinstance(error, str) and error and sidepulse_event in {"PostToolUseFailure", "StopFailure"}:
        normalized["error"] = error
        normalized["error_details"] = error

    if sidepulse_event == "Stop":
        if payload.get("fullyIdle") is False:
            # Shaped like Claude's Stop so the collector shows background work.
            normalized["background_tasks"] = [{"status": "running"}]
        message = last_assistant_message(str(payload.get("transcriptPath") or ""))
        if message:
            normalized["last_assistant_message"] = message
    return normalized


def sidepulse_event_name(event_name: str, payload: dict[str, Any], *, acp: bool) -> str | None:
    if event_name == "SessionStart":
        return "SessionStart"
    if event_name == "PreToolUse":
        return "PreToolUse"
    if event_name == "PostToolUse":
        return "PostToolUseFailure" if payload.get("error") else "PostToolUse"
    if event_name == "PreInvocation":
        # protojson leaves out a zero invocationNum.
        if acp or _int(payload.get("invocationNum")) == 0:
            return "UserPromptSubmit"
        return None
    if event_name == "PostInvocation":
        return "Stop" if acp else None
    if event_name == "Stop":
        reason = str(payload.get("terminationReason") or "").strip().upper()
        if reason in IGNORED_STOP_REASONS:
            return None
        if reason in INTERRUPT_REASONS:
            return "Interrupt"
        if reason in FAILURE_REASONS or payload.get("error"):
            return "StopFailure"
        return "Stop"
    return None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return -1


# --- transcripts -------------------------------------------------------------


def last_assistant_message(transcript_path: str) -> str | None:
    if not transcript_path:
        return None
    path = Path(transcript_path)
    try:
        if path.suffix == ".db":
            return last_acp_planner_text(path)
        return last_jsonl_planner_text(path)
    except Exception:
        return None


def last_jsonl_planner_text(path: Path) -> str | None:
    """The newest planner response text in a CLI ``transcript*.jsonl``."""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
        tail = handle.read().decode("utf-8", errors="replace")
    for line in reversed(tail.splitlines()):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("type") != "PLANNER_RESPONSE":
            continue
        content = row.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return None


def last_acp_planner_text(path: Path) -> str | None:
    """The newest planner response text in an ACP conversation database."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    try:
        rows = connection.execute(
            "SELECT step_payload FROM steps WHERE step_type = ? ORDER BY idx DESC LIMIT 20",
            (ACP_PLANNER_RESPONSE_STEP_TYPE,),
        ).fetchall()
    finally:
        connection.close()
    for (payload,) in rows:
        if not isinstance(payload, (bytes, bytearray)):
            continue
        for response in protobuf_fields(bytes(payload), ACP_PLANNER_RESPONSE_FIELD):
            for text in protobuf_fields(response, ACP_PLANNER_TEXT_FIELD):
                decoded = text.decode("utf-8", errors="replace")
                if decoded.strip():
                    return decoded
    return None


def protobuf_fields(data: bytes, field_number: int) -> list[bytes]:
    """Every length-delimited value of ``field_number`` at the top level of ``data``."""
    values: list[bytes] = []
    index = 0
    length = len(data)
    while index < length:
        key, index = _varint(data, index)
        field, wire_type = key >> 3, key & 7
        if wire_type == 0:
            _, index = _varint(data, index)
        elif wire_type == 1:
            index += 8
        elif wire_type == 2:
            size, index = _varint(data, index)
            if index + size > length:
                raise ValueError("truncated protobuf field")
            if field == field_number:
                values.append(data[index : index + size])
            index += size
        elif wire_type == 5:
            index += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
    return values


def _varint(data: bytes, index: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if index >= len(data) or shift > 63:
            raise ValueError("bad protobuf varint")
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return result, index
