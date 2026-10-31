// One session: its responses as a conversation trace (live while a turn runs), what the platform measured about each
// turn, and the workspace beside it (the chat's file panel, embedded). Everything here is read from the public API:
// the session's responses, each one's input items and event stream, and the session's trace records.
import { api, bytes, clientOf, esc, short, stream, when } from "./api.js";
import { $, confirm, fail, pre, statusPill, toast } from "./ui.js";
import { markdown } from "/chat/static/markdown.js";

const POLL_MS = 4000;
const TERMINAL = new Set(["response.completed", "response.failed", "response.incomplete"]);
const VERB = { create_file: "Created", update_file: "Edited", delete_file: "Deleted" };
const ICON = {
  term: '<path d="m5 8 4 4-4 4"/><path d="M12 17h7"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z"/><path d="M14 3v5h5"/>',
  tool: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17l3 3 5.3-5.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4Z"/>',
  search: '<circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/>',
  chev: '<path d="m9 6 6 6-6 6"/>',
};
const ic = (d, s = 14) => `<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;

// ── the output items of a response, as a conversation ─────────────────────────────────────────────────────────────
/** Output items → segments: {t: "thinking", text} | {t: "steps", steps: [...]} | {t: "text", text}. */
function segments(items) {
  const out = [], byCall = {};
  const push = seg => out.push(seg);
  const last = () => out[out.length - 1];
  for (const item of items) {
    const kind = item.type, cid = item.call_id || item.id;
    if (kind === "message" && item.role === "assistant") {
      const text = (item.content || []).filter(c => c.type === "output_text" || c.type === "text").map(c => c.text || "").join("");
      if (text) push({ t: "text", text });
    } else if (kind === "reasoning") {
      const text = (item.summary || []).map(s => s.text || "").join("\n").trim() || (item.content || []).map(c => c.text || "").join("\n").trim();
      if (text) { if (last() && last().t === "thinking") last().text += "\n" + text; else push({ t: "thinking", text }); }
    } else if (kind.endsWith("_output")) {
      const step = byCall[cid];
      if (!step) continue;
      if (kind === "shell_call_output") {
        const results = item.output || [];
        step.output = results.map(r => (r.stdout || "") + (r.stderr || "")).join("");
        step.exit = results.map(r => (r.outcome || {}).type === "timeout" ? "timeout" : (r.outcome || {}).exit_code).find(c => c) || 0;
      } else {
        step.output = String(item.output || "");
        step.failed = item.status === "failed" || item.status === "incomplete";
      }
    } else {
      let step = null;
      if (kind === "shell_call") step = { icon: "term", label: ((item.action || {}).commands || []).join(" && ") };
      else if (kind === "apply_patch_call") {
        const op = item.operation || {};
        step = { icon: "file", verb: VERB[op.type] || "Changed", path: op.path || "", detail: op.diff || "" };
      } else if (kind === "function_call") step = { icon: "tool", label: item.name || "tool", detail: item.arguments || "" };
      else if (kind === "mcp_call") step = { icon: "tool", label: `${item.server_label}.${item.name}`, detail: item.arguments || "", output: item.output || "",
                                             failed: item.status === "failed", error: item.error ? JSON.stringify(item.error) : "" };
      else if (kind === "web_search_call") step = { icon: "search", label: "Searched the web", detail: JSON.stringify(item.action || {}) };
      else if (kind.endsWith("_call")) step = { icon: "tool", label: kind.replace(/_call$/, ""), detail: JSON.stringify(item) };
      if (!step) continue;
      step.status = item.status;
      byCall[cid] = step;
      if (last() && last().t === "steps") last().steps.push(step); else push({ t: "steps", steps: [step] });
    }
  }
  return out;
}

function stepSummary(steps) {
  const n = k => steps.filter(s => s.icon === k).length, parts = [];
  if (n("term")) parts.push(`ran ${n("term")} command${n("term") > 1 ? "s" : ""}`);
  const edits = steps.filter(s => s.icon === "file");
  if (edits.length) parts.push(`${edits.every(s => s.verb === "Created") ? "created" : "edited"} ${edits.length} file${edits.length > 1 ? "s" : ""}`);
  if (n("tool")) parts.push(`used ${n("tool")} tool${n("tool") > 1 ? "s" : ""}`);
  if (n("search")) parts.push("searched the web");
  const s = parts.join(", ") || `${steps.length} step${steps.length > 1 ? "s" : ""}`;
  return s[0].toUpperCase() + s.slice(1);
}

function mdOptions(sid) {
  const scheme = url => /^[a-z][a-z0-9+.-]*:/i.test(url);
  return { breaks: true,
    link(url) { url = url.trim(); if (/^(https?:|mailto:)/i.test(url)) return { href: url }; if (scheme(url) || url.startsWith("#")) return null;
                return { open: url.replace(/^\/workspace\//, "").replace(/^\.\//, "") }; },
    image(url) { url = url.trim(); if (/^data:image\//i.test(url)) return url; if (scheme(url) || url.startsWith("//")) return null;
                 return `/chat/ws/${sid}/${url.replace(/^\/workspace\//, "").replace(/^\.\//, "").split("/").map(encodeURIComponent).join("/")}`; } };
}

function renderBody(turn, sid, live) {
  const segs = segments(turn.items);
  let html = "";
  segs.forEach((seg, i) => {
    const lastSeg = i === segs.length - 1;
    if (seg.t === "thinking") {
      html += `<details class="fold"><summary>${live && lastSeg ? '<span class="shimmer">Thinking</span>' : "Thought"}${ic(ICON.chev)}</summary><div class="fbody">${esc(seg.text)}</div></details>`;
    } else if (seg.t === "steps") {
      const label = esc(stepSummary(seg.steps));
      html += `<details class="fold" open><summary>${live && lastSeg ? `<span class="shimmer">${label}</span>` : label}${ic(ICON.chev)}</summary><div class="steps">` +
        seg.steps.map(s => {
          const title = s.path ? `${esc(s.verb)} <a href="#" data-open="${esc(s.path)}">${esc(s.path)}</a>` : esc(s.label);
          const running = live && s.status === "in_progress";
          return `<div class="step"><div class="lbl">${ic(ICON[s.icon] || ICON.tool)}<span>${title}</span>${running ? '<span class="live"></span>' : ""}</div>` +
            (s.detail ? `<pre class="diff">${esc(s.detail.slice(0, 20000))}</pre>` : "") +
            (s.output ? `<pre>${esc(s.output.slice(0, 20000))}</pre>` : "") +
            (s.exit ? `<div class="bad small">exit ${esc(s.exit)}</div>` : s.failed ? `<div class="bad small">failed${s.error ? ": " + esc(s.error.slice(0, 400)) : ""}</div>` : "") + `</div>`;
        }).join("") + `</div></details>`;
    } else {
      html += `<div class="reply">${markdown(seg.text, mdOptions(sid))}</div>`;
    }
  });
  if (live && (!segs.length || segs[segs.length - 1].t === "text")) html += `<span class="live"></span>`;
  const r = turn.response, err = r.error;
  if (err) html += `<div class="bad small" style="margin-top:6px">${esc(err.code || "")}${err.code ? ": " : ""}${esc(err.message || "")}</div>`;
  else if (r.incomplete_details) html += `<div class="muted small" style="margin-top:6px">incomplete: ${esc(r.incomplete_details.reason || "")}</div>`;
  return html;
}

function renderInput(turn) {
  const items = turn.input || [], r = turn.response;
  const texts = [], files = [];
  for (const it of items) {
    if (it.role && it.role !== "user") continue;
    const content = typeof it.content === "string" ? [{ type: "input_text", text: it.content }] : it.content || [];
    for (const c of content) {
      if (c.type === "input_text") texts.push(c.text || "");
      else if (c.type === "input_file" || c.type === "input_image") files.push(c.filename || c.file_id || "image");
    }
  }
  const instructions = typeof r.instructions === "string" ? r.instructions : "";
  return `<div class="ask"><span class="av">U</span><div style="min-width:0;flex:1"><div class="bubble">${esc(texts.join("\n\n")) || '<span class="muted">(no text)</span>'}</div>` +
    (files.length ? `<div class="muted small" style="margin-top:4px">attached: ${files.map(esc).join(", ")}</div>` : "") +
    (instructions ? `<details class="fold small"><summary>Instructions${ic(ICON.chev)}</summary><div class="fbody">${esc(instructions)}</div></details>` : "") + `</div></div>`;
}

function duration(r) {
  const start = r.created_at, end = r.completed_at || (r.status === "in_progress" || r.status === "queued" ? Date.now() / 1000 : null);
  if (!start || !end) return "";
  const s = Math.max(0, Math.round(end - start));
  return s < 90 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}

function renderFooter(turn, records) {
  const r = turn.response, mine = records.filter(x => x.response_id === r.id);
  const harness = mine.find(x => x.kind === "harness"), gw = mine.filter(x => x.kind === "gateway"), door = mine.find(x => x.kind === "door");
  const u = r.usage, cells = [];
  const cell = (k, v) => cells.push(`<span>${esc(k)} <b>${v}</b></span>`);
  cell("status", statusPill(r.status) + (r.metadata && r.metadata.hg_reason ? ` <span class="mono">${esc(r.metadata.hg_reason)}</span>` : ""));
  const d = duration(r); if (d) cell(r.completed_at ? "duration" : "elapsed", esc(d));
  if (u) cell("tokens", `${esc(Number(u.input_tokens || 0).toLocaleString())} in · ${esc(Number(u.output_tokens || 0).toLocaleString())} out`);
  if (gw.length) cell("provider calls", `${gw.length} · ${bytes(gw.reduce((n, x) => n + (x.data.request_bytes || 0) + (x.data.response_bytes || 0), 0))}` +
    (gw.some(x => x.data.status >= 400) ? ` · <span class="bad">${gw.filter(x => x.data.status >= 400).length} failed</span>` : ""));
  if (door) {
    const c = door.data.connections || [], denied = c.filter(x => x.verdict === "denied").length;
    cell("door", `${c.length - denied} allowed${denied ? ` · <span class="bad">${denied} denied</span>` : ""}`);
  }
  if (harness && harness.data.exit_code !== null && harness.data.exit_code !== undefined) cell("exit", esc(harness.data.exit_code));
  if (r.metadata && r.metadata.hg_ignored_fields) cell("ignored", `<span class="mono">${esc(r.metadata.hg_ignored_fields)}</span>`);
  return cells.join("") + `<a href="#" data-raw="${esc(r.id)}" style="margin-left:auto">Raw</a>`;
}

function renderTurn(turn, i, sid, records) {
  const r = turn.response, live = r.status === "in_progress" || r.status === "queued";
  return `<article class="turn${live ? " on" : ""}" id="turn-${esc(r.id)}">` +
    `<div class="th"><b>Turn ${i + 1}</b><span class="mono">${esc(short(r.id))}</span>${statusPill(r.status)}<span style="margin-left:auto">${live ? "started " : ""}${esc(when(r.created_at))}</span></div>` +
    renderInput(turn) + `<div class="tbody">${renderBody(turn, sid, live)}</div>` +
    `<div class="tf">${renderFooter(turn, records)}</div>` +
    `<div class="raw" hidden>${pre({ response: r, trace: records.filter(x => x.response_id === r.id) })}</div></article>`;
}

// ── the page ──────────────────────────────────────────────────────────────────────────────────────────────────────
export const session = { title: "Session", async render(el, params, me) {
  const sid = params[0];
  const admin = me.role !== "member";
  let container = await api("GET", `/v1/containers/${encodeURIComponent(sid)}`);
  const client = clientOf(admin ? (await api("GET", "/v1/keys")).data || [] : []);
  const turns = [];                                   // {response, items, input, streaming}
  let records = [], frame = 0;
  const m = () => container.metadata || {};

  el.innerHTML = `<p><a href="#/sessions">← Sessions</a></p>` +
    `<div class="card shead" id="shead"></div>` +
    `<div class="sess"><section class="scol"><div class="row" style="margin-bottom:10px"><h2 style="margin:0">Transcript</h2><span class="muted small">every response of this session, live while a turn runs</span></div><div id="turns"><div class="muted">Loading…</div></div></section>` +
    `<aside class="card spanel"><iframe src="/chat/?panel=${encodeURIComponent(sid)}" title="Workspace"></iframe></aside></div>`;

  const header = () => {
    const md = m(), running = md.state === "running";
    $("#shead", el).innerHTML =
      `<div class="kv"><span class="k">Harness</span><span class="v">${esc(md.harness || container.name)}</span></div>` +
      `<div class="kv"><span class="k">Model</span><span class="v">${esc(md.model || "—")}</span></div>` +
      `<div class="kv"><span class="k">Client</span><span class="v">${esc(client(md.created_by))}</span></div>` +
      `<div class="kv"><span class="k">State</span><span class="v">${statusPill(md.state || "idle")}</span></div>` +
      `<div class="kv"><span class="k">Turns</span><span class="v">${esc(md.turns || turns.length)}</span></div>` +
      `<div class="kv"><span class="k">Workspace</span><span class="v">${bytes(md.bytes)}</span></div>` +
      `<div class="kv"><span class="k">Last active</span><span class="v">${esc(when(container.last_active_at))}</span></div>` +
      `<div class="kv"><span class="k">Session</span><span class="v mono">${esc(sid)}</span></div>` +
      `<div class="acts" style="margin-left:auto">` +
      (running ? `<button class="btn2 danger" id="cancel">Cancel turn</button>` : "") +
      (admin ? `<button class="btn2 danger" id="del">Delete</button>` : "") + `</div>`;
  };

  const draw = () => {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      if (!el.isConnected) return;
      const box = $("#turns", el), stick = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
      box.innerHTML = turns.length ? turns.map((t, i) => renderTurn(t, i, sid, records)).join("") : `<div class="empty">No responses yet.</div>`;
      if (stick) box.scrollTop = box.scrollHeight;
    });
  };

  const loadTraces = async () => {
    if (!admin) return;
    try { records = (await api("GET", `/v1/containers/${encodeURIComponent(sid)}/traces`)).data; draw(); } catch { /* shown without */ }
  };

  const follow = async turn => {
    // Replay the turn's events from the first and keep up live: items by output index, deltas into the open ones.
    if (turn.streaming) return;
    turn.streaming = true;
    const items = turn.items;
    try {
      await stream(`/v1/responses/${encodeURIComponent(turn.response.id)}?stream=true&starting_after=-1`, null, ev => {
        const e = ev.data, t = e.type || ev.type;
        if (t === "response.output_item.added" || t === "response.output_item.done") items[e.output_index] = e.item;
        else if (t === "response.output_text.delta") {
          const it = items[e.output_index]; if (!it) return;
          it.content = it.content || []; const part = it.content[e.content_index] || (it.content[e.content_index] = { type: "output_text", text: "" });
          part.text = (part.text || "") + (e.delta || "");
        } else if (t === "response.reasoning_summary_text.delta") {
          const it = items[e.output_index]; if (!it) return;
          it.summary = it.summary || []; const part = it.summary[e.summary_index] || (it.summary[e.summary_index] = { type: "summary_text", text: "" });
          part.text = (part.text || "") + (e.delta || "");
        } else if (TERMINAL.has(t)) { turn.response = e.response; turn.items = e.response.output || items.filter(Boolean); }
        else if (t === "response.in_progress" || t === "response.created") turn.response = { ...e.response, output: turn.response.output };
        draw();
      });
    } catch (e) { if (el.isConnected) fail(e); }
    turn.streaming = false;
    if (TERMINAL.has(`response.${turn.response.status}`) || turn.response.status === "cancelled") {
      setTimeout(loadTraces, 1500);                     // the node's finish report lands after the terminal event
      refresh().catch(() => {});
    } else if (el.isConnected) setTimeout(() => follow(turn), 2000);
  };

  const add = async r => {
    const turn = { response: r, items: (r.output || []).slice(), input: [], streaming: false };
    turns.push(turn);
    api("GET", `/v1/responses/${encodeURIComponent(r.id)}/input_items?order=asc`).then(d => { turn.input = d.data || []; draw(); }).catch(() => {});
    if (r.status === "in_progress" || r.status === "queued") { turn.items = []; follow(turn); }
  };

  const load = async () => {
    let after = turns.length ? turns[turns.length - 1].response.id : null;
    for (let i = 0; i < 50; i++) {
      const pg = await api("GET", `/v1/containers/${encodeURIComponent(sid)}/responses?limit=100${after ? `&after=${after}` : ""}`);
      for (const r of pg.data) await add(r);
      if (!pg.has_more) break;
      after = pg.last_id;
    }
    draw();
  };

  const refresh = async () => {
    if (!el.isConnected) return;
    container = await api("GET", `/v1/containers/${encodeURIComponent(sid)}`);
    header();
    if (Number(m().turns) > turns.length) await load();
  };

  header();
  await load();
  await loadTraces();
  const timer = setInterval(() => { if (!el.isConnected) { clearInterval(timer); return; } refresh().catch(() => {}); }, POLL_MS);

  el.addEventListener("click", async e => {
    const raw = e.target.closest("[data-raw]");
    if (raw) { e.preventDefault(); const box = raw.closest(".turn").querySelector(".raw"); box.hidden = !box.hidden; return; }
    const open = e.target.closest("[data-open]");
    if (open) { e.preventDefault(); $(".spanel iframe", el).contentWindow.postMessage({ type: "open", path: open.dataset.open }, location.origin); return; }
    if (e.target.id === "cancel") {
      const live = [...turns].reverse().find(t => t.response.status === "in_progress" || t.response.status === "queued");
      if (live) { try { await api("POST", `/v1/responses/${encodeURIComponent(live.response.id)}/cancel`); toast("Cancelling", "ok"); } catch (err) { fail(err); } }
    } else if (e.target.id === "del" && await confirm("Delete session", `Delete ${sid}, its snapshots and files? A running session is refused.`)) {
      try { await api("DELETE", `/v1/containers/${encodeURIComponent(sid)}`); location.hash = "#/sessions"; } catch (err) { fail(err); }
    }
  });
} };

