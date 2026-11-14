// sidepulse-opencode-plugin
// Managed by SidePulse: `sidepulse install opencode` rewrites this file and
// `sidepulse uninstall opencode` removes it.
//
// OpenCode 2 runs agents inside a long-lived server (its background service,
// or a server T3 Code starts). This plugin follows that server's event stream
// and hands each lifecycle change to SidePulse's hook command, in order, as
// the same payload shape Claude-style hooks use. Monitoring never blocks or
// fails a session: hook runs are fire-and-forget and every error is swallowed.
//
// A one-shot `opencode run` exits as soon as its answer is out and sends
// SIGTERM to its child processes on the way, so hooks run in their own process
// group, and whatever is still queued at exit is handed to one detached shell
// that delivers it in order after the hook in flight finishes.

import { spawn } from "node:child_process"
import { mkdtempSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

const HOOK_COMMAND = __SIDEPULSE_HOOK_COMMAND__
const HOOK_TIMEOUT_MS = 10000
const LOOKUP_TIMEOUT_MS = 2000
const MAX_SESSIONS = 256
// How long the exit flush waits for the hook in flight before sending the rest.
const EXIT_WAIT_TICKS = 100 // x 0.05s

const EVENT_TYPES = [
  "session.created",
  "session.inbox.enqueued",
  "session.execution.started",
  "session.execution.succeeded",
  "session.execution.failed",
  "session.execution.interrupted",
  "session.tool.input.started",
  "session.tool.called",
  "session.tool.success",
  "session.tool.failed",
  "session.text.ended",
  "session.renamed",
  "permission.asked",
  "permission.replied",
  "form.created",
  "form.replied",
  "form.cancelled",
]

// OpenCode calls setup once per project directory, yet every instance's
// subscription sees the whole server's events. One shared record per server
// process keeps a single subscription and hands it to another live instance
// when its owner unloads.
const STATE_KEY = Symbol.for("sidepulse.opencode.monitor")

function sharedState() {
  if (!globalThis[STATE_KEY]) {
    globalThis[STATE_KEY] = {
      instances: new Map(),
      owner: null,
      controller: null,
      sessions: new Map(),
      queue: Promise.resolve(),
      // Payloads emitted but not yet handed to a hook, oldest first.
      pending: [],
      // The hook process currently delivering a payload.
      inflight: null,
      // Top-level sessions between execution.started and its outcome.
      running: new Set(),
    }
    const state = globalThis[STATE_KEY]
    process.on("exit", () => {
      try {
        flushOnExit(state)
      } catch {}
    })
  }
  return globalThis[STATE_KEY]
}

function firstString(...values) {
  return values.find((value) => typeof value === "string" && value.trim().length > 0)
}

function sessionRecord(state, sessionID) {
  let record = state.sessions.get(sessionID)
  if (!record) {
    record = { tools: new Map(), permissions: new Map(), known: false }
    state.sessions.set(sessionID, record)
    if (state.sessions.size > MAX_SESSIONS) {
      state.sessions.delete(state.sessions.keys().next().value)
    }
  }
  return record
}

function fallbackDirectory(state) {
  if (state.instances.size !== 1) return undefined
  const [ctx] = state.instances.values()
  return firstString(ctx?.location?.directory)
}

function runHook(state, payload) {
  return new Promise((resolve) => {
    let child
    try {
      // Its own process group: OpenCode's shutdown SIGTERM must not reach it.
      child = spawn("/bin/sh", ["-c", HOOK_COMMAND], { stdio: ["pipe", "ignore", "ignore"], detached: true })
    } catch {
      resolve()
      return
    }
    state.inflight = child.pid ?? null
    const timer = setTimeout(() => {
      try {
        child.kill("SIGKILL")
      } catch {}
    }, HOOK_TIMEOUT_MS)
    const done = () => {
      clearTimeout(timer)
      if (state.inflight === child.pid) state.inflight = null
      resolve()
    }
    child.on("error", done)
    child.on("close", done)
    child.stdin.on("error", () => {})
    child.stdin.end(JSON.stringify(payload))
  })
}

function buildPayload(state, eventName, sessionID, fields = {}) {
  const record = sessionRecord(state, sessionID)
  const payload = {
    hook_event_name: eventName,
    session_id: sessionID,
    cwd: firstString(record.directory, fallbackDirectory(state)),
    timestamp: new Date().toISOString(),
    ...fields,
  }
  if (record.title) payload.sidepulse_title = record.title
  if (record.t3ThreadID) payload.t3code_thread_id = record.t3ThreadID
  for (const key of Object.keys(payload)) {
    if (payload[key] === undefined) delete payload[key]
  }
  return payload
}

function emit(state, eventName, sessionID, fields = {}) {
  state.pending.push(buildPayload(state, eventName, sessionID, fields))
  state.queue = state.queue
    .then(() => {
      const payload = state.pending.shift()
      return payload ? runHook(state, payload) : undefined
    })
    .catch(() => {})
}

// Runs inside process.on("exit"), so everything here is synchronous: a run cut
// short ends as interrupted, and the queued payloads go to one detached shell.
function flushOnExit(state) {
  for (const sessionID of state.running) {
    state.pending.push(buildPayload(state, "Interrupt", sessionID))
  }
  state.running.clear()
  const payloads = state.pending.splice(0)
  if (payloads.length === 0) return
  const dir = mkdtempSync(join(tmpdir(), "sidepulse-opencode-"))
  payloads.forEach((payload, index) => {
    writeFileSync(join(dir, `${String(index).padStart(4, "0")}.json`), JSON.stringify(payload))
  })
  const script = [
    'n=0; while [ -n "$1" ] && kill -0 "$1" 2>/dev/null && [ "$n" -lt "$3" ]; do sleep 0.05; n=$((n+1)); done',
    'for f in "$2"/*.json; do /bin/sh -c "$SIDEPULSE_HOOK_COMMAND" < "$f"; done',
    'rm -rf "$2"',
  ].join("; ")
  const child = spawn(
    "/bin/sh",
    ["-c", script, "sidepulse-flush", String(state.inflight ?? ""), dir, String(EXIT_WAIT_TICKS)],
    { stdio: "ignore", detached: true, env: { ...process.env, SIDEPULSE_HOOK_COMMAND: HOOK_COMMAND } },
  )
  child.unref()
}

// T3 Code runs every OpenCode thread on one shared server, so no per-thread
// environment reaches the plugin. Each session T3 creates does carry a
// permission rule allowing that thread's own T3 tools ("t3-code-<thread id>_*"),
// which names the thread.
const T3_THREAD_RULE = /^t3-code-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})_\*$/i

function permissionRules(info) {
  // Session info names them "permissions"; the stored session row, "permission".
  for (const value of [info?.permissions, info?.permission]) {
    if (Array.isArray(value)) return value
    if (Array.isArray(value?.rules)) return value.rules
  }
  return undefined
}

function t3ThreadFromPermissions(rules) {
  for (const rule of rules ?? []) {
    const match = typeof rule?.action === "string" ? T3_THREAD_RULE.exec(rule.action) : null
    if (match && rule.effect !== "deny") return match[1].toLowerCase()
  }
  return undefined
}

function adoptSessionInfo(record, info) {
  record.known = true
  record.parentID = firstString(info?.parentID) ?? record.parentID
  record.directory = record.directory ?? firstString(info?.location?.directory, info?.directory)
  record.title = record.title ?? firstString(info?.title)
  record.t3ThreadID = record.t3ThreadID ?? t3ThreadFromPermissions(permissionRules(info))
}

// A session first seen mid-run (the plugin loaded after it was created) is
// looked up once, so a subagent started before then still folds into its parent.
async function learnSession(state, ctx, sessionID, record) {
  record.known = true
  try {
    const info = await Promise.race([
      ctx.session.get({ sessionID }),
      new Promise((resolve) => setTimeout(resolve, LOOKUP_TIMEOUT_MS)),
    ])
    if (info) adoptSessionInfo(record, info)
  } catch {}
}

// The top-level session a subagent works for; OpenCode nests subagents freely.
function rootSession(state, sessionID) {
  let current = sessionID
  for (let depth = 0; depth < 16; depth += 1) {
    const parentID = state.sessions.get(current)?.parentID
    if (!parentID) return current
    current = parentID
  }
  return current
}

async function handleEvent(state, ctx, event) {
  const type = event?.type
  const data = event?.data ?? {}
  const sessionID = firstString(data.sessionID)
  if (!type || !sessionID) return
  const record = sessionRecord(state, sessionID)
  const directory = firstString(event.location?.directory, data.location?.directory)
  if (directory) record.directory = directory

  if (type === "session.created") {
    adoptSessionInfo(record, data)
    // Without its permission rules the T3 thread is unknown; look it up once.
    if (!permissionRules(data)) record.known = false
    return
  }
  if (!record.known) await learnSession(state, ctx, sessionID, record)

  if (record.parentID) {
    handleSubagentEvent(state, type, data, record, rootSession(state, sessionID))
    return
  }

  switch (type) {
    case "session.renamed":
      record.title = firstString(data.title) ?? record.title
      return
    case "session.inbox.enqueued":
      if (data.item?.type === "user") record.prompt = firstString(data.item?.payload?.text)
      return
    case "session.execution.started":
      state.running.add(sessionID)
      record.lastText = undefined
      emit(state, "UserPromptSubmit", sessionID, { prompt: record.prompt })
      record.prompt = undefined
      return
    case "session.tool.input.started":
      if (firstString(data.id) && firstString(data.name)) record.tools.set(data.id, data.name)
      return
    case "session.tool.called":
      emit(state, "PreToolUse", sessionID, {
        tool_name: record.tools.get(data.id),
        tool_input: data.input && typeof data.input === "object" ? data.input : undefined,
      })
      return
    case "session.tool.success": {
      const toolName = record.tools.get(data.id)
      record.tools.delete(data.id)
      emit(state, "PostToolUse", sessionID, { tool_name: toolName })
      return
    }
    case "session.tool.failed": {
      const toolName = record.tools.get(data.id)
      record.tools.delete(data.id)
      // A denied permission already reported its answer; the agent carries on.
      if (data.error?.type === "permission.rejected") {
        emit(state, "PostToolUse", sessionID, { tool_name: toolName })
      } else {
        emit(state, "PostToolUseFailure", sessionID, {
          tool_name: toolName,
          error_details: firstString(data.error?.message),
        })
      }
      return
    }
    case "session.text.ended":
      record.lastText = firstString(data.text) ?? record.lastText
      return
    case "permission.asked": {
      const action = firstString(data.action)
      if (firstString(data.id)) record.permissions.set(data.id, action)
      const resources = Array.isArray(data.resources) ? data.resources.filter((r) => typeof r === "string") : []
      emit(state, "PermissionRequest", sessionID, {
        tool_name: action,
        message: [action, resources.join(" ")].filter(Boolean).join(": ") || undefined,
      })
      return
    }
    case "permission.replied": {
      const toolName = record.permissions.get(data.requestID)
      record.permissions.delete(data.requestID)
      emit(state, data.reply === "reject" ? "PostToolUse" : "PreToolUse", sessionID, { tool_name: toolName })
      return
    }
    case "form.created":
      emit(state, "PermissionRequest", sessionID, { tool_name: "question", sidepulse_status: "waiting_for_input" })
      return
    case "form.replied":
    case "form.cancelled":
      emit(state, "PostToolUse", sessionID, { tool_name: "question" })
      return
    case "session.execution.succeeded":
      state.running.delete(sessionID)
      emit(state, "Stop", sessionID, { last_assistant_message: record.lastText })
      record.lastText = undefined
      record.tools.clear()
      return
    case "session.execution.failed":
      state.running.delete(sessionID)
      emit(state, "StopFailure", sessionID, {
        error_details: firstString(data.error?.message, data.message) ?? "OpenCode run failed",
      })
      record.tools.clear()
      return
    case "session.execution.interrupted":
      state.running.delete(sessionID)
      emit(state, "Interrupt", sessionID)
      record.tools.clear()
      return
  }
}

// A subagent's tools and prompts show on the session it works for; its own
// start, finish, and text do not, since the parent's subagent tool call is
// still running until the result comes back.
function handleSubagentEvent(state, type, data, record, rootID) {
  switch (type) {
    case "session.tool.input.started":
      if (firstString(data.id) && firstString(data.name)) record.tools.set(data.id, data.name)
      return
    case "session.tool.called":
      emit(state, "PreToolUse", rootID, {
        tool_name: record.tools.get(data.id),
        tool_input: data.input && typeof data.input === "object" ? data.input : undefined,
      })
      return
    case "session.tool.success":
    case "session.tool.failed": {
      // A subagent's failed tool is the subagent's to recover from.
      const toolName = record.tools.get(data.id)
      record.tools.delete(data.id)
      emit(state, "PostToolUse", rootID, { tool_name: toolName })
      return
    }
    case "permission.asked": {
      const action = firstString(data.action)
      if (firstString(data.id)) record.permissions.set(data.id, action)
      const resources = Array.isArray(data.resources) ? data.resources.filter((r) => typeof r === "string") : []
      emit(state, "PermissionRequest", rootID, {
        tool_name: action,
        message: [action, resources.join(" ")].filter(Boolean).join(": ") || undefined,
      })
      return
    }
    case "permission.replied": {
      const toolName = record.permissions.get(data.requestID)
      record.permissions.delete(data.requestID)
      emit(state, data.reply === "reject" ? "PostToolUse" : "PreToolUse", rootID, { tool_name: toolName })
      return
    }
    case "form.created":
      emit(state, "PermissionRequest", rootID, { tool_name: "question", sidepulse_status: "waiting_for_input" })
      return
    case "form.replied":
    case "form.cancelled":
      emit(state, "PostToolUse", rootID, { tool_name: "question" })
      return
  }
}

function ensureSubscribed(state) {
  if (state.owner || state.instances.size === 0) return
  const [token, ctx] = state.instances.entries().next().value
  const controller = new AbortController()
  state.owner = token
  state.controller = controller
  ;(async () => {
    try {
      for await (const event of ctx.event.subscribe({ types: EVENT_TYPES, signal: controller.signal })) {
        try {
          await handleEvent(state, ctx, event)
        } catch {}
      }
    } catch {}
    if (state.owner === token) {
      state.owner = null
      state.controller = null
      // The stream ends on overflow or when the server shuts the location down;
      // pick it up again from whichever instance is still loaded.
      if (!controller.signal.aborted) setTimeout(() => ensureSubscribed(state), 1000)
    }
  })()
}

export default {
  id: "sidepulse",
  setup(ctx) {
    const state = sharedState()
    const token = {}
    state.instances.set(token, ctx)
    ensureSubscribed(state)
    return () => {
      state.instances.delete(token)
      if (state.owner === token) {
        state.controller?.abort()
        state.owner = null
        state.controller = null
        ensureSubscribed(state)
      }
    }
  },
}
