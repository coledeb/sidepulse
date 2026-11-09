// sidepulse-opencode-plugin
// Managed by SidePulse: `sidepulse install opencode` rewrites this file and
// `sidepulse uninstall opencode` removes it.
//
// OpenCode 2 runs agents inside a long-lived server (its background service,
// or a server T3 Code starts). This plugin follows that server's event stream
// and hands each lifecycle change to SidePulse's hook command, in order, as
// the same payload shape Claude-style hooks use. Monitoring never blocks or
// fails a session: hook runs are fire-and-forget and every error is swallowed.

import { spawn } from "node:child_process"

const HOOK_COMMAND = __SIDEPULSE_HOOK_COMMAND__
const HOOK_TIMEOUT_MS = 10000
const LOOKUP_TIMEOUT_MS = 2000
const MAX_SESSIONS = 256

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
    }
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

function runHook(payload) {
  return new Promise((resolve) => {
    let child
    try {
      child = spawn("/bin/sh", ["-c", HOOK_COMMAND], { stdio: ["pipe", "ignore", "ignore"] })
    } catch {
      resolve()
      return
    }
    const timer = setTimeout(() => {
      try {
        child.kill("SIGKILL")
      } catch {}
    }, HOOK_TIMEOUT_MS)
    const done = () => {
      clearTimeout(timer)
      resolve()
    }
    child.on("error", done)
    child.on("close", done)
    child.stdin.on("error", () => {})
    child.stdin.end(JSON.stringify(payload))
  })
}

function emit(state, eventName, sessionID, fields = {}) {
  const record = sessionRecord(state, sessionID)
  const payload = {
    hook_event_name: eventName,
    session_id: sessionID,
    cwd: firstString(record.directory, fallbackDirectory(state)),
    timestamp: new Date().toISOString(),
    ...fields,
  }
  if (record.title) payload.sidepulse_title = record.title
  for (const key of Object.keys(payload)) {
    if (payload[key] === undefined) delete payload[key]
  }
  state.queue = state.queue.then(() => runHook(payload)).catch(() => {})
}

function adoptSessionInfo(record, info) {
  record.known = true
  record.parentID = firstString(info?.parentID) ?? record.parentID
  record.directory = record.directory ?? firstString(info?.location?.directory, info?.directory)
  record.title = record.title ?? firstString(info?.title)
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
      emit(state, "Stop", sessionID, { last_assistant_message: record.lastText })
      record.lastText = undefined
      record.tools.clear()
      return
    case "session.execution.failed":
      emit(state, "StopFailure", sessionID, {
        error_details: firstString(data.error?.message, data.message) ?? "OpenCode run failed",
      })
      record.tools.clear()
      return
    case "session.execution.interrupted":
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
