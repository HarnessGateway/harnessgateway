// Sessions (spec §20): every session; its turns; each turn's trace; its files.

import { del, download, get, text } from "/api.js";
import { attempt, bytes, clear, h, put, status, table, when } from "/ui.js";

async function list(root) {
  const sessions = await get("/v1/sessions?limit=100");
  put(root, h("h1", {}, "Sessions"), table([
    { title: "Session", cell: (session) => h("span", { class: "mono" }, session.id) },
    { title: "Turns", cell: (session) => session.turns },
    { title: "Size", cell: (session) => bytes(session.bytes) },
    { title: "Last active", cell: (session) => when(session.last_active_at) },
    { title: "Forked from", cell: (session) => h("span", { class: "mono" }, session.forked_from_response_id || "") },
  ], sessions.data, (session) => { location.hash = `#/sessions/${session.id}`; }));
}

async function trace(place, turnId) {
  const found = await get(`/v1/responses/${turnId}/trace`);
  put(clear(place),
    h("h2", {}, `Trace of ${turnId}`),
    h("div", {}, status(found.status), found.reason ? ` reason: ${found.reason}` : "", found.code ? ` code: ${found.code}` : "",
      found.message ? ` — ${found.message}` : ""),
    h("div", { class: "small muted" },
      `harness ${found.harness} ${found.harness_version} · environment ${found.environment} · ` +
      `provider ${found.provider || "none"} · model ${found.model || "none"} · waited ${found.queued_for_s ?? "?"} s`),
    found.handover ? h("div", { class: "notice" }, found.handover) : null,
    table([
      { title: "Step", cell: (step) => step.name },
      { title: "Started", cell: (step) => when(step.started_at) },
      { title: "Ended", cell: (step) => when(step.ended_at) },
    ], found.steps),
    h("h2", {}, "Sandbox command"), h("pre", {}, found.command || "(the sandbox was not started)"),
    h("h2", {}, "Network"),
    table([
      { title: "Host", cell: (c) => `${c.host}:${c.port}` },
      { title: "", cell: (c) => status(c.allowed ? "allowed" : "refused") },
      { title: "Sent", cell: (c) => bytes(c.bytes_up) },
      { title: "Received", cell: (c) => bytes(c.bytes_down) },
      { title: "Why", cell: (c) => c.reason },
    ], found.connections),
    h("h2", {}, "Log"), h("pre", {}, found.log || "(empty)"));
}

async function one(root, id) {
  const [session, turns] = await Promise.all([get(`/v1/sessions/${id}`), get(`/v1/sessions/${id}/turns`)]);
  const detail = h("div", {});
  const problem = h("div", {});
  let files = { data: [] };
  try {
    files = await get(`/v1/sessions/${id}/files`);
  } catch { /* no saved files yet */ }

  put(root,
    h("div", { class: "row" },
      h("h1", {}, h("a", { href: "#/sessions" }, "Sessions"), " / ", h("span", { class: "mono" }, id)),
      session.head_response_id ?
        h("button", { class: "fit", onclick: () => { location.hash = `#/chat/${session.head_response_id}`; } }, "Continue in chat") : null,
      h("button", { class: "fit danger", onclick: () => attempt(problem, async () => {
        if (!confirm("Delete this session, its turns and its files?")) return;
        await del(`/v1/sessions/${id}`);
        location.hash = "#/sessions";
      }) }, "Delete")),
    problem,
    h("h2", {}, "Turns"),
    table([
      { title: "#", cell: (turn) => turn.number },
      { title: "Response", cell: (turn) => h("span", { class: "mono" }, turn.id) },
      { title: "", cell: (turn) => [status(turn.status), turn.reason ? ` ${turn.reason}` : ""] },
      { title: "Ran with", cell: (turn) => `${turn.environment}:${turn.harness}${turn.provider ? "/" + turn.provider : ""}${turn.model ? "/" + turn.model : ""}` },
      { title: "When", cell: (turn) => when(turn.created_at) },
    ], turns.data, (turn) => trace(detail, turn.id).catch((error) => put(clear(detail), h("div", { class: "error" }, error.message)))),
    h("h2", {}, "Files (as the latest turn left them)"),
    table([
      { title: "Path", cell: (file) => h("a", { href: "#", onclick: async (event) => {
        event.preventDefault();
        event.stopPropagation();
        const path = `/v1/sessions/${id}/files/content?path=${encodeURIComponent(file.path)}`;
        if (file.bytes > 200000) return download(path, file.path.split("/").pop());
        put(clear(detail), h("h2", {}, file.path), h("pre", {}, await text(path)));
      } }, file.path) },
      { title: "Size", cell: (file) => bytes(file.bytes) },
    ], files.data),
    detail);
}

export async function render(root, context) {
  return context.args[0] ? one(root, context.args[0]) : list(root);
}
