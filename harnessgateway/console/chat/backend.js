// What the chat asks of the platform, on its public API as the signed-in person: turns and their events, the
// session's files, attachments, and links a browser can open by itself to show what an agent made.

import { get, post, stream } from "/api.js";

const sleep = ms => new Promise(ok => setTimeout(ok, ms));

// ── a session's files ──────────────────────────────────────────────────────────────────────────────────────────
/** The id a session's work is read under in the Containers API. */
export const containerOf = sid => `cntr_${String(sid).slice(5)}`;

// An image, a video or a page an agent built is shown by giving the browser an address, and the browser sends no
// key with it. The platform gives a link that is itself the permission, for an hour (spec §13.11).
const views = new Map();                             // session -> { base, until }
const RENEW_S = 900;
/** Make sure there is a link for this session's files that lasts a while yet. */
export async function ensureView(sid) {
  const held = views.get(sid);
  if (held && held.until - Date.now() / 1000 > RENEW_S) return held.base;
  const link = await post(`/v1/containers/${containerOf(sid)}/view`);
  views.set(sid, { base: link.url, until: link.expires_at });
  return link.url;
}
/** Where a browser opens one of the session's files. Call ensureView(sid) first. */
export const wsUrl = (sid, path) => (views.get(sid)?.base || "/view/-/") + String(path).split("/").map(encodeURIComponent).join("/");

/** The session's files as its latest turn left them: [{id, path, bytes, at}], by path. */
export async function filesOf(sid) {
  const out = [];
  let after = null;
  for (let page = 0; page < 200; page++) {          // 200 pages of 100
    const doc = await get(`/v1/containers/${containerOf(sid)}/files?limit=100${after ? `&after=${after}` : ""}`);
    for (const f of doc.data) out.push({ id: f.id, path: f.path.replace(/^\/work\//, ""), bytes: f.bytes || 0, at: f.created_at || 0 });
    if (!doc.has_more) break;
    after = doc.last_id;
  }
  await ensureView(sid);
  return out.sort((a, b) => a.path.localeCompare(b.path));
}

/** A file for the next message: stored as an uploaded file; the harness finds it in the sandbox. */
export async function attach(file) {
  const form = new FormData();
  form.append("file", file, file.name);
  form.append("purpose", "user_data");
  const stored = await post("/v1/files", form);
  return { id: stored.id, name: stored.filename || file.name, bytes: stored.bytes };
}

// ── turns ──────────────────────────────────────────────────────────────────────────────────────────────────────
export const cancel = rid => post(`/v1/responses/${encodeURIComponent(rid)}/cancel`);

/** One output item as the page shows it: a tool step (with its call id, to pair its output), or nothing. */
function describe(item) {
  const kind = item.type, id = item.call_id || item.id;
  if (kind === "shell_call") return { type: "tool", id, icon: "terminal", label: ((item.action || {}).commands || []).join(" && ") };
  if (kind === "shell_call_output") {
    const results = item.output || [];
    return { type: "tool_output", id, text: results.map(r => (r.stdout || "") + (r.stderr || "")).join(""),
             exit_code: results.map(r => (r.outcome || {}).exit_code).find(code => code) || 0 };
  }
  if (kind === "apply_patch_call") {
    const op = item.operation || {};
    const verb = { create_file: "Created", update_file: "Edited", delete_file: "Deleted" }[op.type] || "Changed";
    return { type: "tool", id, icon: "file", verb, path: op.path || "", label: `${verb} ${op.path || ""}`, detail: op.diff || "" };
  }
  if (kind === "function_call" || kind === "custom_tool_call") {
    return { type: "tool", id, icon: "tool", label: item.name || "tool", detail: item.arguments || item.input || "" };
  }
  if (kind === "function_call_output" || kind === "custom_tool_call_output") {
    return { type: "tool_output", id, text: typeof item.output === "string" ? item.output : JSON.stringify(item.output ?? ""), exit_code: 0 };
  }
  if (kind === "mcp_call") {
    return { type: "tool", id, icon: "tool", label: `${item.server_label}.${item.name}`, detail: item.arguments || "", output: item.output || "" };
  }
  if (kind === "web_search_call") return { type: "tool", id, icon: "search", label: "Searched the web" };
  return null;
}

/** The platform's stream events as the page's: start, text, thinking, tool, tool_output, end. */
function relay(onEvent) {
  return event => {
    const kind = event.type;
    if (kind === "response.created") {
      onEvent({ type: "start", id: event.response.id, session: (event.response.metadata || {}).hg_session });
    } else if (kind === "response.output_text.delta") {
      onEvent({ type: "text", delta: event.delta || "" });
    } else if (kind === "response.reasoning_summary_text.delta" || kind === "response.reasoning_text.delta") {
      onEvent({ type: "thinking", delta: event.delta || "" });
    } else if (kind === "response.output_item.done") {
      const shown = describe(event.item || {});
      if (shown) onEvent(shown);
    } else if (kind === "response.completed" || kind === "response.failed" || kind === "response.incomplete") {
      const r = event.response, meta = r.metadata || {};
      const error = r.status === "failed" && r.error ? `${meta.hg_code || meta.hg_reason || r.error.code}: ${r.error.message || ""}` : null;
      onEvent({ type: "end", status: r.status, error });
    }
  };
}

const BUSY_TRIES = 60, BUSY_WAIT_MS = 1000;          // a message sent while the turn before it is still ending
/** Start a turn and read its events. It runs on the platform whether or not this page keeps watching. */
export async function runTurn({ model, input, previous, attachments = [] }, onEvent, signal) {
  const body = { model, stream: true, background: true,
                 input: attachments.length ? [{ role: "user", content: [{ type: "input_text", text: input },
                                                                         ...attachments.map(id => ({ type: "input_file", file_id: id }))] }] : input };
  if (previous) body.previous_response_id = previous;
  for (let attempt = 0; ; attempt++) {
    try {
      return await stream("/v1/responses", body, relay(onEvent), signal);
    } catch (error) {
      if (error.status !== 409 || !/in progress/.test(error.message) || attempt >= BUSY_TRIES) throw error;
      await sleep(BUSY_WAIT_MS);
    }
  }
}
/** Read a turn that is running or over from its first event: after a reload, or a dropped connection. */
export const followTurn = (rid, onEvent, signal) =>
  stream(`/v1/responses/${encodeURIComponent(rid)}?stream=true&starting_after=-1`, null, relay(onEvent), signal);

/** A session as the chat opens one it did not start: what its latest turn ran with, and where to continue. */
export const sessionOf = sid => get(`/v1/sessions/${encodeURIComponent(sid)}`);
