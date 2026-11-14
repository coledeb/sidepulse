from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .models import provider_label

T3CODE_BUNDLE_ID = "com.t3tools.t3code"
T3CODE_THREAD_ENV = "T3CODE_THREAD_ID"
T3CODE_THREAD_KEY = "t3code_thread_id"
T3CODE_SURFACE = "t3code"
# These agents run every session inside one long-lived server, and SidePulse's
# hook runs as that server's child. The server's environment belongs to
# whatever first started it (OpenCode's background service can be started from
# a T3 Code terminal and then serve plain terminal sessions), so only the
# server's process ancestry says who hosts it: T3 Code starts its own server.
SERVER_HOSTED_PROVIDERS = frozenset({"opencode"})
# relay.annotate_remote_line joins an origin and its computer with this.
RELAY_ORIGIN_SEPARATOR = " · "
# The agent process a hook runs under, so the daemon can tell a session whose
# process is gone (a run killed or exited before its Stop) from a quiet one.
AGENT_PID_KEY = "agent_pid"
AGENT_PID_STARTED_KEY = "agent_pid_started"
# Agents known to run their hooks from the long-lived agent process itself, not
# from a short-lived helper that exits after each hook.
AGENT_PID_PROVIDERS = frozenset({"claude", "codex", "opencode"})
# lstart is printed in the caller's timezone and locale; pin both so the hook
# and the daemon (a LaunchAgent with its own environment) agree.
PS_ENV_OVERRIDES = {"LC_ALL": "C", "TZ": "UTC"}


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int | None
    comm: str
    command: str
    # ps's lstart: with the pid, names the process even after the pid is reused.
    started: str | None = None


@dataclass(frozen=True)
class AgentOrigin:
    kind: str
    label: str
    source: str
    confidence: str = "inferred"

    def to_payload(self) -> dict[str, str]:
        return {
            "agent_origin": self.label,
            "agent_origin_kind": self.kind,
            "agent_origin_source": self.source,
            "agent_origin_confidence": self.confidence,
        }


def detect_agent_origin(
    provider: str,
    *,
    env: Mapping[str, str] | None = None,
    parent_pid: int | None = None,
) -> AgentOrigin:
    active_env = os.environ if env is None else env
    normalized_provider = provider.lower()

    explicit = explicit_origin_from_env(normalized_provider, active_env)
    if explicit is not None:
        return explicit

    server_hosted = normalized_provider in SERVER_HOSTED_PROVIDERS
    if not server_hosted:
        env_origin = origin_from_environment(normalized_provider, active_env)
        if env_origin is not None:
            return env_origin

    processes = process_ancestry(parent_pid or os.getppid())
    process_origin = origin_from_processes(normalized_provider, processes)
    if process_origin is not None:
        return process_origin

    if server_hosted:
        return AgentOrigin(
            kind=f"{normalized_provider}_server",
            label=provider_label(normalized_provider),
            source="process:server",
        )

    if terminal_environment(active_env):
        source = 'env:TERM_PROGRAM' if active_env.get('TERM_PROGRAM') else 'env:WT_SESSION'
        return surface_origin(normalized_provider, "cli", source)

    return AgentOrigin(
        kind=f"{normalized_provider}_unknown",
        label=provider_label(normalized_provider),
        source="fallback:provider",
        confidence="unknown",
    )


def explicit_origin_from_env(provider: str, env: Mapping[str, str]) -> AgentOrigin | None:
    label = clean_label(env.get("SIDEPULSE_AGENT_ORIGIN"))
    if not label:
        return None
    kind = clean_label(env.get("SIDEPULSE_AGENT_ORIGIN_KIND")) or normalize_kind(label)
    return AgentOrigin(
        kind=kind,
        label=label,
        source="env:SIDEPULSE_AGENT_ORIGIN",
        confidence="explicit",
    )


def origin_from_environment(provider: str, env: Mapping[str, str]) -> AgentOrigin | None:
    # T3 Code hosts other agents' runtimes; it is checked first because a hosted
    # session can also inherit terminal or editor variables from wherever T3 Code
    # itself was launched.
    if clean_label(env.get(T3CODE_THREAD_ENV)):
        return surface_origin(provider, T3CODE_SURFACE, f"env:{T3CODE_THREAD_ENV}")
    if is_t3code_bundle_id(env.get("__CFBundleIdentifier")):
        return surface_origin(provider, T3CODE_SURFACE, "env:__CFBundleIdentifier")

    term_program = str(env.get("TERM_PROGRAM") or "").strip().lower()
    if term_program == "vscode" or any(key.startswith("VSCODE_") for key in env):
        return surface_origin(provider, "vscode", "env:VSCODE")

    bundle_id = str(env.get("__CFBundleIdentifier") or "").strip().lower()
    if bundle_id:
        if provider == "codex" and any(part in bundle_id for part in ("openai", "chatgpt", "codex")):
            return surface_origin(provider, "app", "env:__CFBundleIdentifier")
        if provider == "claude" and "anthropic" in bundle_id:
            return surface_origin(provider, "app", "env:__CFBundleIdentifier")
        if provider == "grok" and "grok" in bundle_id:
            return surface_origin(provider, "app", "env:__CFBundleIdentifier")
        if provider == "junie" and "jetbrains" in bundle_id:
            return surface_origin(provider, "ide", "env:__CFBundleIdentifier")

    return None


def origin_from_processes(
    provider: str,
    processes: tuple[ProcessInfo, ...],
) -> AgentOrigin | None:
    haystack = "\n".join(f"{info.comm}\n{info.command}" for info in processes).lower()

    # "T3 Code.app" and "T3 Code (Nightly).app" both run as ".../T3 Code...app/".
    if re.search(r"/t3 code[^/]*\.app/", haystack):
        return surface_origin(provider, T3CODE_SURFACE, "process:T3 Code")

    if any(token in haystack for token in ("visual studio code.app", "code helper", "vscode")):
        return surface_origin(provider, "vscode", "process:Visual Studio Code")
    if "cursor.app" in haystack or "cursor helper" in haystack:
        return surface_origin(provider, "cursor", "process:Cursor")
    if "windsurf.app" in haystack or "windsurf helper" in haystack:
        return surface_origin(provider, "windsurf", "process:Windsurf")

    if provider == "codex" and any(
        token in haystack for token in ("codex.app", "chatgpt.app")
    ):
        return surface_origin(provider, "app", "process:Codex.app")
    if provider == "claude" and "claude.app" in haystack:
        return surface_origin(provider, "app", "process:Claude.app")
    if provider == "opencode" and "opencode.app" in haystack:
        return surface_origin(provider, "app", "process:OpenCode.app")
    if provider == "antigravity" and "antigravity.app" in haystack:
        return surface_origin(provider, "app", "process:Antigravity.app")
    if provider == "grok" and "grok.app" in haystack:
        return surface_origin(provider, "app", "process:Grok.app")
    if provider == "junie" and any(
        token in haystack
        for token in (
            "intellij idea.app",
            "pycharm.app",
            "webstorm.app",
            "goland.app",
            "phpstorm.app",
            "rubymine.app",
            "rustrover.app",
            "rider.app",
            "clion.app",
        )
    ):
        return surface_origin(provider, "ide", "process:JetBrains IDE")

    for info in processes:
        basename = process_basename(info)
        if provider == "codex" and basename == "codex":
            return surface_origin(provider, "cli", "process:codex")
        if provider == "claude" and basename in {"claude", "claude-code"}:
            return surface_origin(provider, "cli", "process:claude")
        if provider == "grok" and basename == "grok":
            return surface_origin(provider, "cli", "process:grok")
        if provider == "junie" and basename == "junie":
            return surface_origin(provider, "cli", "process:junie")
        if provider == "antigravity" and basename == "agy":
            return surface_origin(provider, "cli", "process:agy")

    return None


def surface_origin(provider: str, surface: str, source: str) -> AgentOrigin:
    labels = {
        ("codex", "app"): "Codex UI",
        ("codex", "cli"): "Codex CLI",
        ("codex", "vscode"): "Codex in VS Code",
        ("codex", "cursor"): "Codex in Cursor",
        ("codex", "windsurf"): "Codex in Windsurf",
        ("codex", "transcript"): "Codex Transcript",
        ("claude", "app"): "Claude App",
        ("claude", "cli"): "Claude Code CLI",
        ("claude", "vscode"): "Claude in VS Code",
        ("claude", "cursor"): "Claude in Cursor",
        ("claude", "windsurf"): "Claude in Windsurf",
        ("claude", "transcript"): "Claude Transcript",
        ("grok", "app"): "Grok App",
        ("grok", "cli"): "Grok CLI",
        ("grok", "vscode"): "Grok in VS Code",
        ("grok", "cursor"): "Grok in Cursor",
        ("grok", "windsurf"): "Grok in Windsurf",
        ("grok", "transcript"): "Grok Transcript",
        ("junie", "cli"): "Junie CLI",
        ("junie", "ide"): "Junie in JetBrains IDE",
        ("junie", "transcript"): "Junie Transcript",
        ("opencode", "app"): "OpenCode Desktop",
        ("antigravity", "app"): "Antigravity App",
        ("antigravity", "cli"): "Antigravity CLI",
    }
    if surface == T3CODE_SURFACE:
        label = f"{provider_label(provider)} in T3 Code"
    else:
        label = labels.get((provider, surface), provider_label(provider))
    return AgentOrigin(
        kind=f"{provider}_{surface}",
        label=label,
        source=source,
    )


def process_ancestry(pid: int, *, limit: int = 10) -> tuple[ProcessInfo, ...]:
    seen: set[int] = set()
    result: list[ProcessInfo] = []
    current = pid
    while current > 1 and current not in seen and len(result) < limit:
        seen.add(current)
        info = process_info(current)
        if info is None:
            break
        result.append(info)
        if info.ppid is None:
            break
        current = info.ppid
    return tuple(result)


def process_info(pid: int) -> ProcessInfo | None:
    try:
        completed = subprocess.run(
            [
                "/bin/ps", "-p", str(pid),
                "-o", "pid=", "-o", "ppid=", "-o", "lstart=", "-o", "comm=", "-o", "command=",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.4,
            env={**os.environ, **PS_ENV_OVERRIDES},
        )
    except Exception:
        return None
    line = completed.stdout.strip()
    if completed.returncode != 0 or not line:
        return None

    # lstart is always five words: "Fri Nov 13 17:46:10 2026".
    parts = line.split(None, 8)
    if len(parts) < 8:
        return None
    try:
        parsed_pid = int(parts[0])
        parsed_ppid = int(parts[1])
    except ValueError:
        return None
    started = " ".join(parts[2:7])
    comm = parts[7]
    command = parts[8] if len(parts) > 8 else comm
    return ProcessInfo(parsed_pid, parsed_ppid, comm, command, started)


PROCESS_ALIVE = "alive"
PROCESS_GONE = "gone"
PROCESS_UNKNOWN = "unknown"


def process_state(pid: int, started: str) -> str:
    """Whether the process process_info saw as (pid, started) still runs.

    Unknown when ps can't answer (a timeout under load), which callers must not
    read as gone.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return PROCESS_GONE
    except PermissionError:
        # Owned by another user, such as a sudo the agent runs under: lstart
        # below still tells it from a reused pid.
        pass
    except OSError:
        return PROCESS_UNKNOWN
    try:
        completed = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
            env={**os.environ, **PS_ENV_OVERRIDES},
        )
    except Exception:
        return PROCESS_UNKNOWN
    if completed.returncode != 0:
        return PROCESS_GONE
    current = completed.stdout.split()
    if not current:
        return PROCESS_UNKNOWN
    return PROCESS_ALIVE if current == started.split() else PROCESS_GONE


def agent_process(parent_pid: int, *, limit: int = 4) -> ProcessInfo | None:
    """The first ancestor that isn't a shell or interpreter wrapping the hook."""
    seen: set[int] = set()
    current = parent_pid
    while current > 1 and current not in seen and len(seen) < limit:
        seen.add(current)
        info = process_info(current)
        if info is None:
            return None
        if process_basename(info):
            return info
        if info.ppid is None:
            return None
        current = info.ppid
    return None


def process_basename(info: ProcessInfo) -> str:
    candidates = [info.comm]
    try:
        split = shlex.split(info.command)
    except ValueError:
        split = []
    if split:
        candidates.append(split[0])

    for value in candidates:
        name = Path(value).name.strip().lower()
        if name and name not in {"sh", "bash", "zsh", "python", "python3", "env"}:
            return name
    return ""


def terminal_environment(env: Mapping[str, str]) -> bool:
    term_program = str(env.get("TERM_PROGRAM") or "").strip()
    return bool(term_program and term_program.lower() != "vscode" or env.get('WT_SESSION'))


def annotate_payload_with_origin(
    provider: str,
    payload: dict[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if "agent_origin" in payload or "agentOrigin" in payload:
        return payload
    active_env = os.environ if env is None else env
    result = dict(payload)
    result.update(detect_agent_origin(provider, env=active_env).to_payload())
    thread_id = clean_label(active_env.get(T3CODE_THREAD_ENV))
    if provider.lower() in SERVER_HOSTED_PROVIDERS:
        # One server runs many threads, so a thread id in its environment is stale.
        thread_id = None
    if thread_id and T3CODE_THREAD_KEY not in result:
        result[T3CODE_THREAD_KEY] = thread_id
    if provider.lower() in AGENT_PID_PROVIDERS and AGENT_PID_KEY not in result:
        agent = agent_process(os.getppid())
        if agent is not None and agent.started:
            result[AGENT_PID_KEY] = agent.pid
            result[AGENT_PID_STARTED_KEY] = agent.started
    return result


def is_t3code_bundle_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    bundle_id = value.strip().lower()
    return bundle_id == T3CODE_BUNDLE_ID or bundle_id.startswith(f"{T3CODE_BUNDLE_ID}.")


def is_t3code_origin(origin: str | None) -> bool:
    """Whether an origin label names T3 Code as the session's host.

    A relayed session's label carries its computer after a separator, as in
    "Claude in T3 Code · KARLY-WSL", so only the part before it is checked.
    """
    host = str(origin or "").split(RELAY_ORIGIN_SEPARATOR, 1)[0]
    normalized = " ".join(host.strip().lower().replace("-", " ").split())
    return normalized.endswith(" in t3 code") or normalized in {"t3 code", "t3code"}


def is_relayed_origin(origin: str | None) -> bool:
    """Whether an origin label was stamped by the relay with a remote computer's name."""
    return RELAY_ORIGIN_SEPARATOR in str(origin or "")


def is_transcript_origin(origin: str | None) -> bool:
    """Whether an origin label only says SidePulse read the session's transcript."""
    return str(origin or "").strip().lower().endswith(" transcript")


def origin_label_from_payload(provider: str, raw: Mapping[str, Any]) -> str | None:
    for key in ("agent_origin", "agentOrigin", "agent_origin_label", "origin_label"):
        label = clean_label(raw.get(key))
        if label:
            return label

    structured = raw.get("sidepulse_origin") or raw.get("sidepulseOrigin")
    if isinstance(structured, Mapping):
        for key in ("label", "name", "origin"):
            label = clean_label(structured.get(key))
            if label:
                return label
    elif isinstance(structured, str):
        label = clean_label(structured)
        if label:
            return label

    source = clean_label(raw.get("source"))
    if source in {"codex-transcripts", "claude-transcripts"}:
        return surface_origin(provider, "transcript", f"source:{source}").label

    return None


def clean_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def normalize_kind(label: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return normalized or "custom"
