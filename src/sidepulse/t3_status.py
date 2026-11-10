"""Follow T3 Code threads on linked environments through T3's MCP endpoint.

T3 Code runs agents on other machines ("linked environments") and manages them
from this Mac. Those agents' hooks fire on the other machine, so SidePulse never
hears them. The Mac's T3 server can list every linked environment's threads,
so SidePulse asks it instead:

* ``/mcp`` on the local T3 server (origin from ``server-runtime.json``) speaks
  MCP over streamable HTTP and only accepts MCP client sessions. A read-only
  one comes from T3's OAuth flow (``connect_t3``): the user approves
  "SidePulse" once in the browser and the access token lasts 30 days. There are
  no refresh tokens, so an expired or revoked token asks for a reconnect.
* ``t3_environment_links`` names the linked environments, and
  ``t3_thread_list`` with ``settled: false`` returns, per environment, every
  thread that still needs attention: running, waiting, or finished and unseen.
  A thread the user has looked at in T3 drops out of that list.
* MCP pushes nothing here, so ``T3StatusPoller`` asks: often while something
  is active, rarely while everything is quiet.

This Mac's own T3 threads are left to the agents' hooks, which are instant and
richer; only linked environments are polled, so no session shows twice. The one
exception is settling: a hook row waiting on a closing question has no event
that says the user read it, so while one is shown the poller checks whether its
T3 thread was settled (``check_local_settled``). Claude and OpenCode hooks name
their T3 thread; a Codex row is matched to the thread whose latest run finished
when its Stop hook fired, since T3 runs every Codex thread in one shared server.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .models import HookEvent, provider_label
from .origin import RELAY_ORIGIN_SEPARATOR
from .settings import default_config_dir

T3_EVENT_NAME = "T3ThreadStatus"
T3_SCOPE = "orchestration:read"
T3_CLIENT_NAME = "SidePulse"
T3_MCP_PROTOCOL_VERSION = "2025-06-18"
T3_REQUEST_TIMEOUT_SECONDS = 10.0
T3_CONNECT_TIMEOUT_SECONDS = 15 * 60.0

T3_ACTIVE_POLL_SECONDS = 3.0
T3_IDLE_POLL_SECONDS = 15.0
T3_LINKS_REFRESH_SECONDS = 60.0
# A thread in a project SidePulse hasn't seen refetches that machine's project
# list, but no more often than this.
T3_PROJECTS_REFRESH_SECONDS = 60.0
# An unchanged active thread is re-reported this often, so it never ages into
# the monitor's stale window while it keeps running.
T3_ACTIVE_HEARTBEAT_SECONDS = 60.0
# A finished thread older than this is history, not news, when first seen.
T3_FINISHED_MAX_AGE_SECONDS = 60 * 60.0
# A hook row whose T3 thread is unknown matches the thread whose latest run
# finished within this long of the row's Stop. Hooks log whole seconds, and
# T3 marks a run done a few milliseconds after the agent's Stop.
T3_RUN_MATCH_SECONDS = 3.0
T3_SETTLED_SCAN_LIMIT = 50

ACTIVE_STATUSES = frozenset({"preparing", "queued", "starting", "running", "waiting"})

STATE_DISCONNECTED = "disconnected"
STATE_CONNECTED = "connected"
STATE_NEEDS_RECONNECT = "needs_reconnect"
STATE_UNAVAILABLE = "unavailable"


class T3Error(Exception):
    """T3 Code could not be reached or answered with something unusable."""


class T3AuthError(T3Error):
    """The stored credential is missing, expired, or was revoked."""


@dataclass(frozen=True)
class T3Credentials:
    token: str
    expires_at: float | None = None

    def expired(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (time.time() if now is None else now) >= self.expires_at


# --- locations -------------------------------------------------------------


def t3_home() -> Path:
    return Path(os.environ.get("T3CODE_HOME") or Path.home() / ".t3").expanduser()


def t3_server_origin(home: Path | None = None) -> str | None:
    """The running local T3 server's origin, or None when T3 Code is not running."""
    path = (home or t3_home()) / "userdata" / "server-runtime.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    origin = data.get("origin") if isinstance(data, dict) else None
    if not isinstance(origin, str) or not origin.startswith(("http://", "https://")):
        return None
    return origin.rstrip("/")


def t3_installed(home: Path | None = None) -> bool:
    return ((home or t3_home()) / "userdata").is_dir()


def default_t3_credentials_path() -> Path:
    return default_config_dir() / "t3-code.json"


def load_t3_credentials(path: Path | None = None) -> T3Credentials | None:
    try:
        data = json.loads((path or default_t3_credentials_path()).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
        return None
    expires_at = data.get("expires_at")
    return T3Credentials(
        token=data["token"],
        expires_at=float(expires_at) if isinstance(expires_at, (int, float)) else None,
    )


def save_t3_credentials(credentials: T3Credentials, path: Path | None = None) -> Path:
    target = path or default_t3_credentials_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"token": credentials.token, "expires_at": credentials.expires_at}
    temp = target.with_suffix(".json.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(payload).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(temp, target)
    return target


def delete_t3_credentials(path: Path | None = None) -> bool:
    try:
        (path or default_t3_credentials_path()).unlink()
        return True
    except FileNotFoundError:
        return False


# --- sign-in ---------------------------------------------------------------


def _post_json(url: str, data: dict[str, Any], *, form: bool = False) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
    content_type = "application/x-www-form-urlencoded" if form else "application/json"
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=T3_REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise T3Error(f"T3 Code refused {urllib.parse.urlparse(url).path}: HTTP {exc.code} {detail}") from exc
    except (OSError, ValueError) as exc:
        raise T3Error(f"Could not reach T3 Code: {exc}") from exc


def connect_t3(
    *,
    origin: str | None = None,
    open_url: Callable[[str], object] | None = None,
    timeout_seconds: float = T3_CONNECT_TIMEOUT_SECONDS,
    credentials_path: Path | None = None,
) -> T3Credentials:
    """Run T3's OAuth sign-in for a read-only SidePulse session and store it.

    Opens T3's approval page; the user picks "Read only", pastes a pairing code
    and approves. The browser returns to a one-shot listener on 127.0.0.1.
    """
    origin = origin or t3_server_origin()
    if origin is None:
        raise T3Error("T3 Code is not running. Open it and try again.")
    resource = f"{origin}/mcp"
    result: dict[str, str] = {}

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not query:
                self.send_response(404)
                self.end_headers()
                return
            result.update({key: values[0] for key, values in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            message = (
                "SidePulse is connected to T3 Code. You can close this tab."
                if "code" in result
                else "SidePulse was not connected to T3 Code. You can close this tab."
            )
            self.wfile.write(f"<!doctype html><title>SidePulse</title><p>{message}</p>".encode())
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), CallbackHandler)
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/callback"
        client = _post_json(
            f"{origin}/oauth/mcp/register",
            {
                "client_name": T3_CLIENT_NAME,
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = client.get("client_id")
        if not isinstance(client_id, str):
            raise T3Error("T3 Code did not register SidePulse.")
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        state = secrets.token_urlsafe(16)
        authorize_url = f"{origin}/oauth/mcp/authorize?" + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": T3_SCOPE,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": resource,
            }
        )
        (open_url or _open_in_browser)(authorize_url)
        timer = threading.Timer(timeout_seconds, server.shutdown)
        timer.daemon = True
        timer.start()
        try:
            server.serve_forever()
        finally:
            timer.cancel()
    finally:
        server.server_close()

    if result.get("state") != state:
        raise T3Error("The T3 Code sign-in timed out or came from another attempt.")
    if "code" not in result:
        raise T3Error(f"T3 Code did not approve SidePulse ({result.get('error', 'no answer')}).")

    token = _post_json(
        f"{origin}/oauth/mcp/token",
        {
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": resource,
        },
        form=True,
    )
    access_token = token.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise T3Error("T3 Code approved SidePulse but sent no token.")
    expires_in = token.get("expires_in")
    credentials = T3Credentials(
        token=access_token,
        expires_at=time.time() + float(expires_in) if isinstance(expires_in, (int, float)) else None,
    )
    save_t3_credentials(credentials, credentials_path)
    return credentials


def _open_in_browser(url: str) -> None:
    import webbrowser

    webbrowser.open(url)


# --- MCP client ------------------------------------------------------------


class T3McpClient:
    """A minimal MCP client for T3's streamable HTTP endpoint (tools only)."""

    def __init__(self, origin: str, token: str, *, timeout: float = T3_REQUEST_TIMEOUT_SECONDS) -> None:
        self.endpoint = f"{origin.rstrip('/')}/mcp"
        self.token = token
        self.timeout = timeout
        self.session_id: str | None = None
        self._next_id = 0

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.session_id is None:
            self._initialize()
        try:
            return self._call_tool(name, arguments)
        except _SessionExpired:
            # The T3 server restarted and forgot the MCP session.
            self.session_id = None
            self._initialize()
            return self._call_tool(name, arguments)

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self._request("tools/call", {"name": name, "arguments": arguments})
        result = response.get("result")
        if not isinstance(result, dict):
            error = response.get("error") or {}
            raise T3Error(f"{name} failed: {error.get('message', 'no result')}")
        payload = result.get("structuredContent")
        if not isinstance(payload, dict):
            text = "".join(
                part.get("text", "")
                for part in result.get("content", [])
                if isinstance(part, dict)
            )
            try:
                payload = json.loads(text) if text else {}
            except ValueError:
                payload = {"message": text}
        if result.get("isError"):
            raise T3Error(f"{name} failed: {payload.get('message') or payload.get('code') or 'error'}")
        return payload

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": T3_MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": T3_CLIENT_NAME, "version": "1"},
            },
        )
        self._request("notifications/initialized", None, notify=True)

    def _request(self, method: str, params: dict[str, Any] | None, *, notify: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self._next_id += 1
            body["id"] = self._next_id
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                self.session_id = response.headers.get("Mcp-Session-Id") or self.session_id
                raw = response.read().decode("utf-8", errors="replace")
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise T3AuthError("T3 Code no longer accepts SidePulse's credential.") from exc
            if exc.code == 404 and self.session_id is not None and method != "initialize":
                raise _SessionExpired() from exc
            raise T3Error(f"T3 Code answered HTTP {exc.code} to {method}.") from exc
        except (OSError, ValueError) as exc:
            raise T3Error(f"Could not reach T3 Code: {exc}") from exc
        if notify or not raw.strip():
            return {}
        if "event-stream" in content_type:
            messages = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
            raw = messages[-1] if messages else "{}"
        try:
            decoded = json.loads(raw)
        except ValueError as exc:
            raise T3Error(f"T3 Code sent an unreadable answer to {method}.") from exc
        return decoded if isinstance(decoded, dict) else {}


class _SessionExpired(Exception):
    pass


# --- mapping ---------------------------------------------------------------


def provider_for_t3_instance(instance: object) -> str:
    """SidePulse's provider name for a T3 provider instance id ("claudeAgent" -> "claude")."""
    text = str(instance or "").strip()
    lowered = text.lower()
    if lowered.startswith("claude"):
        return "claude"
    if lowered.startswith("codex"):
        return "codex"
    if lowered.endswith("agent") and len(lowered) > len("agent"):
        lowered = lowered[: -len("agent")]
    return lowered or "t3code"


def sidepulse_status_for_thread(thread: dict[str, Any]) -> str:
    """The SidePulse status a T3 thread shows: working, waiting, completed, error or idle."""
    status = str(thread.get("status") or "").strip().lower()
    unseen = not thread.get("settled") and not thread.get("snoozed")
    if status == "waiting":
        return "waiting"
    if status in ACTIVE_STATUSES:
        return "working"
    if status == "completed" and unseen:
        return "completed"
    if status == "failed" and unseen:
        return "error"
    return "idle"


def parse_t3_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def t3_thread_origin(provider: str, environment_label: str) -> str:
    return f"{provider_label(provider)} in T3 Code{RELAY_ORIGIN_SEPARATOR}{environment_label}"


def t3_thread_record(
    thread: dict[str, Any],
    *,
    environment_id: str,
    environment_label: str,
    status: str,
    observed_at: datetime,
    cwd: str | None = None,
) -> HookEvent | None:
    thread_id = thread.get("threadId")
    if not isinstance(thread_id, str) or not thread_id:
        return None
    provider = provider_for_t3_instance(thread.get("providerInstanceId"))
    origin = t3_thread_origin(provider, environment_label)
    updated_at = parse_t3_time(thread.get("updatedAt")) or observed_at
    # An active thread is known to be active now; a finished one ended when T3
    # last touched it, which is what the Done timeout should count from.
    logged_at = observed_at if status in {"working", "waiting", "idle"} else updated_at
    raw = {
        "hook_event_name": T3_EVENT_NAME,
        "sidepulse_status": status,
        "sidepulse_title": thread.get("title") if isinstance(thread.get("title"), str) else None,
        "agent_origin": origin,
        "t3code_thread_id": thread_id,
        "t3code_environment_id": environment_id,
        "t3code_status": thread.get("status"),
    }
    if cwd:
        # The project's folder on that machine, so the row names its project.
        raw["cwd"] = cwd
    return HookEvent(
        provider=provider,
        logged_at=logged_at,
        event_name=T3_EVENT_NAME,
        raw=raw,
        session_id=thread_id,
        cwd=cwd,
        origin=origin,
    )


# --- polling ---------------------------------------------------------------


@dataclass(frozen=True)
class LocalWaitingRow:
    """A hook row on this Mac, hosted by T3, that waits on a closing question."""

    agent_id: str
    provider: str
    updated_at: datetime
    t3_thread_id: str | None = None


def settled_run_matches(
    detail: dict[str, Any] | None,
    row: LocalWaitingRow,
    *,
    exact: bool,
) -> datetime | None:
    """When a T3 thread's detail shows ``row``'s turn was settled, the settle time.

    The thread must be settled after its latest run finished, with no prompt
    still pending and nothing running. A row matched by timing alone also needs
    that run to have finished alongside the row's Stop.
    """
    thread = detail.get("thread") if isinstance(detail, dict) else None
    if not isinstance(thread, dict) or not thread.get("settled"):
        return None
    if thread.get("pendingRequestCount") or str(thread.get("status") or "").lower() in ACTIVE_STATUSES:
        return None
    if thread.get("activeRunId"):
        return None
    settled_at = parse_t3_time(thread.get("settledAt"))
    runs = [run for run in detail.get("recentRuns") or () if isinstance(run, dict)]
    if settled_at is None or not runs:
        return None
    latest = max(runs, key=lambda run: run.get("ordinal") if isinstance(run.get("ordinal"), int) else -1)
    completed_at = parse_t3_time(latest.get("completedAt"))
    if completed_at is None or settled_at < completed_at:
        return None
    if not exact and abs((completed_at - row.updated_at).total_seconds()) > T3_RUN_MATCH_SECONDS:
        return None
    return settled_at


@dataclass
class _Tracked:
    status: str
    provider: str
    reported_monotonic: float


class T3StatusPoller:
    """Polls the local T3 server for linked environments' threads.

    ``ingest`` receives a ``HookEvent`` for every change; it is called from the
    poller's thread.
    """

    def __init__(
        self,
        ingest: Callable[[HookEvent], None],
        *,
        client_factory: Callable[[], T3McpClient | None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        on_state_change: Callable[[str], None] | None = None,
        credentials_signature: Callable[[], object] | None = None,
        local_waiting: Callable[[], Sequence[LocalWaitingRow]] | None = None,
        on_local_settled: Callable[[LocalWaitingRow, datetime], None] | None = None,
    ) -> None:
        self.ingest = ingest
        self.local_waiting = local_waiting
        self.on_local_settled = on_local_settled
        # (row, thread, settle time) checks that did not match, so a thread is
        # read once per settle rather than on every poll.
        self.settle_misses: set[tuple[str, str, str, str]] = set()
        self.client_factory = client_factory or default_t3_client
        self.credentials_signature = credentials_signature or (
            (lambda: None) if client_factory is not None else default_t3_credentials_signature
        )
        self.client_signature: object = None
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.on_state_change = on_state_change
        self.state = STATE_DISCONNECTED
        self.last_error: str | None = None
        self.client: T3McpClient | None = None
        self.environments: tuple[tuple[str, str], ...] = ()
        self.links_checked_at: float | None = None
        self.tracked: dict[str, _Tracked] = {}
        # environment id -> (monotonic time fetched, project id -> workspace root)
        self.project_roots: dict[str, tuple[float, dict[str, str]]] = {}
        self.any_active = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # lifecycle

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sidepulse-t3", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def reconnect(self) -> None:
        """Pick up new credentials on the next poll."""
        self.client = None
        self.links_checked_at = None
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._wake.clear()
            self._wake.wait(self.next_interval())

    def next_interval(self) -> float:
        if self.state != STATE_CONNECTED:
            return T3_IDLE_POLL_SECONDS
        return T3_ACTIVE_POLL_SECONDS if self.any_active else T3_IDLE_POLL_SECONDS

    # one round

    def poll_once(self) -> None:
        try:
            signature = self.credentials_signature()
            if self.client is not None and signature != self.client_signature:
                self.client = None  # signed in again, or disconnected
            if self.client is None:
                self.client_signature = signature
                self.client = self.client_factory()
                if self.client is None:
                    self._clear_tracked()
                    self._set_state(STATE_DISCONNECTED)
                    return
                self.links_checked_at = None
            now = self.monotonic()
            if self.links_checked_at is None or now - self.links_checked_at >= T3_LINKS_REFRESH_SECONDS:
                self.environments = self._linked_environments()
                self.links_checked_at = now
                linked = {environment_id for environment_id, _ in self.environments}
                self._clear_tracked(lambda environment_id: environment_id not in linked)
            any_active = False
            for environment_id, label in self.environments:
                try:
                    threads = self._unsettled_threads(environment_id)
                except T3AuthError:
                    raise
                except T3Error as exc:
                    # One unreachable machine leaves its threads as they were.
                    self.last_error = f"{label}: {exc}"
                    any_active = any_active or self._environment_active(environment_id)
                    continue
                any_active = self._apply(environment_id, label, threads) or any_active
            # A waiting row is settled from T3 within a poll or two.
            any_active = self.check_local_settled() or any_active
            self.any_active = any_active
            self._set_state(STATE_CONNECTED)
        except T3AuthError as exc:
            self.client = None
            self.last_error = str(exc)
            self._clear_tracked()
            self._set_state(STATE_NEEDS_RECONNECT)
        except T3Error as exc:
            # T3 Code quit or restarted: rows keep their last state until it is
            # back or they age out, since nothing says the agents stopped.
            self.client = None
            self.last_error = str(exc)
            self._set_state(STATE_UNAVAILABLE)
        except Exception as exc:  # never let a surprise end the polling thread
            self.client = None
            self.last_error = f"unexpected: {exc!r}"
            self._set_state(STATE_UNAVAILABLE)

    def _clear_tracked(self, which: Callable[[str], bool] = lambda _environment_id: True) -> None:
        """Clear the rows of threads SidePulse can no longer follow."""
        labels = dict(self.environments)
        for key in list(self.tracked):
            environment_id, thread_id = key.split("/", 1)
            if not which(environment_id):
                continue
            tracked = self.tracked.pop(key)
            if tracked.status != "idle":
                self._report_idle(
                    environment_id,
                    labels.get(environment_id, environment_id[:8]),
                    thread_id,
                    tracked.provider,
                    self.wall_clock(),
                )

    def _report_idle(
        self,
        environment_id: str,
        label: str,
        thread_id: str,
        provider: str,
        observed_at: datetime,
    ) -> None:
        origin = t3_thread_origin(provider, label)
        self.ingest(
            HookEvent(
                provider=provider,
                logged_at=observed_at,
                event_name=T3_EVENT_NAME,
                raw={
                    "hook_event_name": T3_EVENT_NAME,
                    "sidepulse_status": "idle",
                    "agent_origin": origin,
                    "t3code_thread_id": thread_id,
                    "t3code_environment_id": environment_id,
                },
                session_id=thread_id,
                origin=origin,
            )
        )

    def _linked_environments(self) -> tuple[tuple[str, str], ...]:
        result = self.client.call_tool("t3_environment_links", {})
        environments = []
        for link in result.get("links") or ():
            if not isinstance(link, dict):
                continue
            environment_id = link.get("environmentId")
            if not isinstance(environment_id, str) or link.get("status") not in (None, "reachable"):
                continue
            label = str(link.get("label") or environment_id[:8]).strip() or environment_id[:8]
            environments.append((environment_id, label))
        return tuple(environments)

    def _project_roots(self, environment_id: str, threads: list[dict[str, Any]]) -> dict[str, str]:
        """Each project's folder on that machine, refetched when a new project shows up."""
        fetched_at, roots = self.project_roots.get(environment_id, (None, {}))
        wanted = {
            project_id
            for thread in threads
            if isinstance(project_id := thread.get("projectId"), str) and project_id
        }
        now = self.monotonic()
        if wanted <= roots.keys() or (
            fetched_at is not None and now - fetched_at < T3_PROJECTS_REFRESH_SECONDS
        ):
            return roots
        fresh: dict[str, str] = {}
        cursor: int | None = 0
        try:
            while cursor is not None and len(fresh) < 1000:
                page = self.client.call_tool(
                    "t3_project_list",
                    {"environmentId": environment_id, "limit": 100, "cursor": cursor},
                )
                for project in page.get("projects") or ():
                    if not isinstance(project, dict):
                        continue
                    project_id, root = project.get("id"), project.get("workspaceRoot")
                    if isinstance(project_id, str) and isinstance(root, str) and root:
                        fresh[project_id] = root
                next_cursor = page.get("nextCursor")
                cursor = next_cursor if isinstance(next_cursor, int) and next_cursor > cursor else None
        except T3AuthError:
            raise
        except T3Error:
            # Rows still show without a project; try again after the wait.
            self.project_roots[environment_id] = (now, roots)
            return roots
        self.project_roots[environment_id] = (now, fresh)
        return fresh

    def _unsettled_threads(self, environment_id: str) -> list[dict[str, Any]]:
        threads: list[dict[str, Any]] = []
        cursor: int | None = 0
        while cursor is not None and len(threads) < 500:
            page = self.client.call_tool(
                "t3_thread_list",
                {
                    "environmentId": environment_id,
                    "allProjects": True,
                    "settled": False,
                    "includeSubagents": False,
                    "limit": 100,
                    "cursor": cursor,
                },
            )
            threads.extend(thread for thread in page.get("threads") or () if isinstance(thread, dict))
            next_cursor = page.get("nextCursor")
            cursor = next_cursor if isinstance(next_cursor, int) and next_cursor > cursor else None
        return threads

    def check_local_settled(self) -> bool:
        """Clear waiting hook rows whose T3 thread was settled; True while any wait."""
        if self.local_waiting is None or self.on_local_settled is None:
            return False
        rows = list(self.local_waiting())
        if not rows:
            self.settle_misses.clear()
            return False
        try:
            page = self.client.call_tool(
                "t3_thread_list",
                {
                    "allProjects": True,
                    "settled": True,
                    "includeSubagents": False,
                    "limit": T3_SETTLED_SCAN_LIMIT,
                },
            )
        except T3AuthError:
            raise
        except T3Error as exc:
            self.last_error = f"this Mac: {exc}"
            return True
        settled = {
            thread["threadId"]: thread
            for thread in page.get("threads") or ()
            if isinstance(thread, dict) and isinstance(thread.get("threadId"), str)
        }
        details: dict[str, dict[str, Any] | None] = {}
        for row in rows:
            exact = bool(row.t3_thread_id)
            if exact:
                candidates = [settled[row.t3_thread_id]] if row.t3_thread_id in settled else []
            else:
                candidates = [
                    thread for thread in settled.values()
                    if provider_for_t3_instance(thread.get("providerInstanceId")) == row.provider
                ]
            matches: list[datetime] = []
            for thread in candidates:
                thread_id = thread["threadId"]
                settled_at = parse_t3_time(thread.get("settledAt"))
                # Settled before this turn ended: that was an earlier turn.
                if settled_at is None or settled_at < row.updated_at:
                    continue
                miss = (row.agent_id, row.updated_at.isoformat(), thread_id, str(thread.get("settledAt")))
                if miss in self.settle_misses:
                    continue
                if thread_id not in details:
                    details[thread_id] = self._thread_detail(thread_id)
                matched = settled_run_matches(details[thread_id], row, exact=exact)
                if matched is None:
                    if details[thread_id] is not None:
                        self.settle_misses.add(miss)
                    continue
                matches.append(matched)
            # Two threads that finished together cannot be told apart.
            if len(matches) == 1:
                self.on_local_settled(row, matches[0])
        if len(self.settle_misses) > 1000:
            self.settle_misses.clear()
        return True

    def _thread_detail(self, thread_id: str) -> dict[str, Any] | None:
        try:
            return self.client.call_tool(
                "t3_thread_read",
                {"threadId": thread_id, "limit": 1, "runLimit": 3, "maxCharsPerItem": 1},
            )
        except T3AuthError:
            raise
        except T3Error:
            return None

    def _environment_active(self, environment_id: str) -> bool:
        prefix = f"{environment_id}/"
        return any(
            key.startswith(prefix) and tracked.status in {"working", "waiting"}
            for key, tracked in self.tracked.items()
        )

    def _apply(self, environment_id: str, label: str, threads: Iterable[dict[str, Any]]) -> bool:
        """Report what changed in one environment; True when a thread there is active."""
        now = self.monotonic()
        observed_at = self.wall_clock()
        prefix = f"{environment_id}/"
        seen: set[str] = set()
        any_active = False
        threads = list(threads)
        roots = self._project_roots(environment_id, threads)
        for thread in threads:
            thread_id = thread.get("threadId")
            if not isinstance(thread_id, str) or not thread_id:
                continue
            key = prefix + thread_id
            status = sidepulse_status_for_thread(thread)
            active = status in {"working", "waiting"}
            previous = self.tracked.get(key)
            if previous is None and not active:
                if status == "idle":
                    continue  # nothing to show for a thread we never saw running
                updated = parse_t3_time(thread.get("updatedAt"))
                if updated is None or (observed_at - updated).total_seconds() > T3_FINISHED_MAX_AGE_SECONDS:
                    continue  # unseen for days: history, not news
            seen.add(key)
            any_active = any_active or active
            if previous is not None and previous.status == status:
                if not active or now - previous.reported_monotonic < T3_ACTIVE_HEARTBEAT_SECONDS:
                    continue
            record = t3_thread_record(
                thread,
                environment_id=environment_id,
                environment_label=label,
                status=status,
                observed_at=observed_at,
                cwd=roots.get(str(thread.get("projectId") or "")),
            )
            if record is None:
                continue
            self.ingest(record)
            self.tracked[key] = _Tracked(status, record.provider, now)

        # A thread that left the unsettled list was looked at (or archived) in T3.
        for key in [key for key in self.tracked if key.startswith(prefix) and key not in seen]:
            tracked = self.tracked.pop(key)
            if tracked.status != "idle":
                self._report_idle(environment_id, label, key[len(prefix):], tracked.provider, observed_at)
        return any_active

    def _set_state(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        if self.on_state_change is not None:
            try:
                self.on_state_change(state)
            except Exception:
                pass


def default_t3_credentials_signature() -> object:
    try:
        stat = default_t3_credentials_path().stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def default_t3_client() -> T3McpClient | None:
    credentials = load_t3_credentials()
    if credentials is None:
        return None
    if credentials.expired():
        raise T3AuthError("SidePulse's T3 Code sign-in has expired.")
    origin = t3_server_origin()
    if origin is None:
        raise T3Error("T3 Code is not running.")
    return T3McpClient(origin, credentials.token)
