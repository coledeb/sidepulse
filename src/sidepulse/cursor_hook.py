"""Adapt Cursor lifecycle hooks to SidePulse events."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .hook import routed_hook_payload, write_hook_line, write_hook_status_audit
from .program_transport import send_hook_event
from .origin import (
    T3CODE_SURFACE,
    T3CODE_THREAD_ENV,
    T3CODE_THREAD_KEY,
    clean_label,
    is_t3code_bundle_id,
    origin_from_processes,
    process_ancestry,
    surface_origin,
)


EVENT_ALIASES = {
    "sessionStart": "SessionStart",
    "sessionEnd": "SessionEnd",
    "beforeSubmitPrompt": "UserPromptSubmit",
    "preToolUse": "PreToolUse",
    "beforeShellExecution": "PreToolUse",
    "afterShellExecution": "PostToolUse",
    "afterFileEdit": "PostToolUse",
    "postToolUse": "PostToolUse",
    "postToolUseFailure": "PostToolUseFailure",
    "stop": "Stop",
}


# T3 Code runs Cursor through Cursor's agent SDK inside the T3 server process,
# so every T3 thread's hooks share the server's environment and carry no thread
# id. T3 names each agent after its thread and stores the agent id in its state
# database, which is how a hook finds its thread.
T3_STATE_DB = Path.home() / ".t3" / "userdata" / "statev2.sqlite"
T3_THREAD_QUERY = (
    "SELECT thread_id FROM orchestration_v2_projection_provider_threads"
    " WHERE driver = 'cursor'"
    " AND json_extract(payload_json, '$.nativeThreadRef.nativeId') = ?"
    " ORDER BY updated_at DESC LIMIT 1"
)
# T3 Code adds its own instructions to the prompt Cursor reports, and wraps a
# thread's first prompt in <user_request>. Only the user's words make a title.
T3_PROMPT_BLOCKS = re.compile(
    r"<(t3_code_orchestration_instructions|runtime_info|pull_request_linking)>.*?</\1>",
    re.DOTALL,
)
USER_REQUEST_BLOCK = re.compile(r"<user_request>(.*?)</user_request>", re.DOTALL)
# A Cursor transcript ends with the turn's last message, so only its tail is read.
TRANSCRIPT_TAIL_BYTES = 256 * 1024


@dataclass(frozen=True)
class CursorHost:
    """Who runs a Cursor session: the Cursor app (the default) or T3 Code."""

    label: str = "Cursor"
    kind: str = "cursor_app"
    source: str = "hook:cursor"
    t3_thread_id: str | None = None


def detect_cursor_host(
    payload: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    parent_pid: int | None = None,
    state_db: Path | None = None,
) -> CursorHost:
    active_env = os.environ if env is None else env
    source: str | None = None
    if clean_label(active_env.get(T3CODE_THREAD_ENV)):
        source = f"env:{T3CODE_THREAD_ENV}"
    elif is_t3code_bundle_id(active_env.get("__CFBundleIdentifier")):
        source = "env:__CFBundleIdentifier"
    else:
        processes = process_ancestry(parent_pid or os.getppid())
        origin = origin_from_processes("cursor", processes)
        if origin is not None and origin.kind == f"cursor_{T3CODE_SURFACE}":
            source = origin.source
    if source is None:
        return CursorHost()
    origin = surface_origin("cursor", T3CODE_SURFACE, source)
    conversation_id = str(payload.get("conversation_id") or payload.get("conversationId") or "")
    thread_id = t3_thread_for_agent(conversation_id, state_db or T3_STATE_DB)
    return CursorHost(origin.label, origin.kind, origin.source, thread_id)


def t3_thread_for_agent(agent_id: str, state_db: Path) -> str | None:
    """The T3 thread running Cursor agent ``agent_id``, from T3's state database."""
    if not agent_id or not state_db.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=0.5)
        try:
            row = connection.execute(T3_THREAD_QUERY, (agent_id,)).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return clean_label(row[0]) if row else None


def user_prompt_text(prompt: str) -> str:
    """The user's own words from a Cursor prompt, without what T3 Code adds."""
    stripped = T3_PROMPT_BLOCKS.sub("", prompt)
    request = USER_REQUEST_BLOCK.search(stripped)
    if request:
        stripped = request.group(1)
    return stripped.strip() or prompt


def last_assistant_message(transcript_path: str) -> str | None:
    """The text of the last assistant message in a Cursor agent transcript."""
    if not transcript_path:
        return None
    try:
        with open(transcript_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("role") != "assistant":
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        texts = [
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        text = "\n".join(item for item in texts if item.strip()).strip()
        if text:
            return text
    return None


def normalize_payload(
    event_name: str,
    payload: dict[str, Any],
    host: CursorHost | None = None,
) -> dict[str, Any] | None:
    """SidePulse's event for a Cursor hook, or None when it reports nothing new."""
    if event_name == "sessionEnd" and payload.get("reason") == "completed":
        # Cursor's agent SDK (T3 Code) ends its session after every turn, just
        # after the turn's stop, which already reported how the turn ended.
        return None
    host = host or CursorHost()
    normalized = dict(payload)
    normalized["hook_event_name"] = EVENT_ALIASES.get(event_name, event_name)
    normalized["session_id"] = str(
        payload.get("conversation_id") or payload.get("conversationId") or ""
    )
    normalized["agent_origin"] = host.label
    normalized["agent_origin_kind"] = host.kind
    normalized["agent_origin_source"] = host.source
    normalized["agent_origin_confidence"] = "explicit"
    if host.t3_thread_id:
        normalized[T3CODE_THREAD_KEY] = host.t3_thread_id

    workspace_roots = payload.get("workspace_roots") or payload.get("workspaceRoots")
    if isinstance(workspace_roots, list) and workspace_roots:
        normalized["cwd"] = str(workspace_roots[0])
    elif payload.get("workspace_root"):
        normalized["cwd"] = str(payload["workspace_root"])

    prompt = payload.get("prompt")
    if isinstance(prompt, str):
        normalized["prompt"] = user_prompt_text(prompt)

    if event_name in {"beforeShellExecution", "preToolUse"}:
        normalized["tool_name"] = str(payload.get("tool_name") or "shell")
        normalized["tool_input"] = payload.get("tool_input") or {
            "command": payload.get("command")
        }
    elif event_name in {
        "afterShellExecution",
        "afterFileEdit",
        "postToolUse",
        "postToolUseFailure",
    }:
        normalized["tool_name"] = str(payload.get("tool_name") or event_name)
        normalized["tool_response"] = {
            "status": payload.get("status"),
            "exit_code": payload.get("exit_code"),
            "error": payload.get("error_message"),
        }

    if event_name == "postToolUseFailure" and payload.get("error_message"):
        normalized["message"] = str(payload["error_message"])

    status = payload.get("status")
    if event_name == "stop" and "last_assistant_message" not in normalized:
        # Cursor's stop payload has no reply text; the question check needs it.
        message = last_assistant_message(str(payload.get("transcript_path") or ""))
        if message:
            normalized["last_assistant_message"] = message
    if event_name == "stop" and isinstance(status, str):
        normalized["message"] = f"Cursor stopped: {status}"
        if status == "error":
            # Cursor has no separate failure hook; an agent loop that ended in
            # an error reports it as the stop status.
            normalized["hook_event_name"] = "StopFailure"

    return normalized


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--event", required=True)
    parser.add_argument("--log", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        raw = json.loads(sys.stdin.read() or "{}")
        payload = raw if isinstance(raw, dict) else {}
        normalized = normalize_payload(args.event, payload, detect_cursor_host(payload))
        if normalized is None:
            sys.stdout.write("{}\n")
            return 0
        provider, log_path, line = routed_hook_payload(
            "cursor",
            args.log.expanduser(),
            json.dumps(normalized, separators=(",", ":"), ensure_ascii=False),
        )
        try:
            write_hook_line(log_path, line)
        except Exception:
            pass
        try:
            write_hook_status_audit(provider, line)
        except Exception:
            pass
        try:
            send_hook_event(provider, line)
        except Exception:
            pass
    except Exception:
        pass

    sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
