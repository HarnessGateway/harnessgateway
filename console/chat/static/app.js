// The chat: conversations in the sidebar, replies streamed as they come, the composer with attachments, and the
// harness / provider / model picker. Each conversation is one session on the platform; its turns run in the
// background there, and this page follows their events while it is open.

import { $, esc, size, rel, badge, ic, I, post, copyText, flash, sleep, kindOf, SHOWCASE } from "./util.js";
import { markdown, workspaceLinks } from "./markdown.js";
import { enhance, codeBlock, codeAction } from "./rich.js";
import { initPanel, openPanel, openFile, renderPanel, resetPanel, togglePanel, isOpen } from "./panel.js";

const STORE = "hg-chatgui", MODELS = "hg-chatgui-models", SIDE = "hg-chatgui-side";
const SETTLE_MS = 120000;                            // how long a finished turn's files are waited for
let chats = [], current = null, pending = [], files = [], sideOpen = true;
let catalog = { harnesses: [], providers: [], default_model: "" }, shownChat;
const saving = new Set();                            // sessions whose last turn's state is being saved

// ── storage ────────────────────────────────────────────────────────────────────────────────────────────────────
function load() { try { chats = JSON.parse(localStorage.getItem(STORE)) || []; } catch { chats = []; } }
function save() { try { localStorage.setItem(STORE, JSON.stringify(chats)); localStorage.setItem(STORE + "-open", current || ""); } catch { /* full or blocked */ } }
function chat() { return chats.find(c => c.id === current); }
const newId = () => Date.now().toString(36) + Math.random().toString(36).slice(2);

// ── the conversation ───────────────────────────────────────────────────────────────────────────────────────────
const STEP_ICON = { terminal: I.term, file: I.file, tool: I.tool, search: I.search };
const replyOptions = c => ({ ...workspaceLinks(c && c.session), breaks: true, code: codeBlock });
const replyText = turn => turn.segments.filter(s => s.t === "text").map(s => s.text).join("\n\n").trim();

function tiles(paths) {
  const known = Object.fromEntries(files.map(f => [f.path, f]));
  return `<div class="tiles">` + paths.map(p => `<button class="tile" data-open="${esc(p)}">${badge(p)}<span style="min-width:0">` +
    `<div class="nm">${esc(p.split("/").pop())}</div><div class="sub">${esc(p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "workspace")}` +
    `${known[p] ? " · " + size(known[p].bytes) : ""}</div></span></button>`).join("") + `</div>`;
}
function stepSummary(steps) {
  const n = k => steps.filter(s => s.icon === k).length, parts = [];
  if (n("terminal")) parts.push(`ran ${n("terminal")} command${n("terminal") > 1 ? "s" : ""}`);
  const edits = steps.filter(s => s.icon === "file");
  if (edits.length) parts.push(`${edits.every(s => s.verb === "Created") ? "created" : "edited"} ${edits.length} file${edits.length > 1 ? "s" : ""}`);
  if (n("tool")) parts.push(`used ${n("tool")} tool${n("tool") > 1 ? "s" : ""}`);
  if (n("search")) parts.push("searched the web");
  const s = parts.join(", ") || `${steps.length} steps`;
  return s[0].toUpperCase() + s.slice(1);
}
function renderAssistant(c, turn, live, ti) {
  let html = "";
  turn.segments.forEach((seg, si) => {
    const at = `data-t="${ti}" data-s="${si}"`, last = seg === turn.segments.at(-1);
    if (seg.t === "thinking") {
      const thinking = live && last && !seg.ended;
      const secs = seg.started && seg.ended ? Math.max(1, Math.round((seg.ended - seg.started) / 1000)) : 0;
      const label = thinking ? `<span class="shimmer">Thinking</span>` : secs ? `Thought for ${secs}s` : "Thought";
      html += `<details ${at}${seg.open ? " open" : ""}><summary>${label}<span class="chev">${ic(I.chev, 14)}</span></summary><div class="body">${esc(seg.text)}</div></details>`;
    } else if (seg.t === "steps") {
      const label = esc(stepSummary(seg.steps));
      html += `<details ${at}${seg.open ? " open" : ""}><summary>${live && last ? `<span class="shimmer">${label}</span>` : label}<span class="chev">${ic(I.chev, 14)}</span></summary><div class="body">` +
        seg.steps.map(s => {
          const text = s.path ? `${esc(s.verb)} <span class="link" data-open="${esc(rel(s.path))}">${esc(s.path)}</span>` : esc(s.label);
          return `<div class="step"><div class="label">${ic(STEP_ICON[s.icon] || I.tool, 14)}<span>${text}</span></div>` +
            (s.output || s.detail ? `<pre>${esc((s.output || s.detail).slice(0, 20000))}</pre>` : "") +
            (s.exit_code ? `<div class="error">exit ${s.exit_code}</div>` : "") + `</div>`;
        }).join("") + `</div></details>`;
    } else if (seg.t === "text") {
      html += `<div class="reply">${markdown(seg.text, replyOptions(c))}</div>`;
    }
  });
  if (turn.files && turn.files.length) html += tiles(turn.files);
  if (turn.error) html += `<div class="error">${esc(turn.error)}</div>`;
  else if (turn.status && turn.status !== "completed") html += `<div class="status">${esc(turn.status === "cancelled" ? (turn.interrupted ? "Interrupted" : "Stopped") : turn.status)}</div>`;
  if (live && (!turn.segments.length || turn.segments.at(-1).t === "text")) html += `<span class="dot"></span>`;
  if (!live && replyText(turn)) html += `<div class="actions"><button data-reply="copy" title="Copy">${ic(I.copy, 16)}<span></span></button></div>`;
  return html;
}
function renderChats() {
  $("chats").innerHTML = chats.map(x => `<div class="chat${x.id === current ? " active" : ""}" data-id="${x.id}"><span title="${esc(x.title)}">${esc(x.title)}</span>` +
    (liveTurn(x) ? `<i class="working" title="Working"></i>` : "") +
    `<button data-ren="${x.id}" title="Rename">${ic(I.edit, 14)}</button><button data-del="${x.id}" title="Delete">${ic(I.close, 14)}</button></div>`).join("");
  updateSend();
}
function renderSide() {
  document.body.classList.toggle("noside", !sideOpen);
  $("sideopen").hidden = $("hnew").hidden = sideOpen;
}
function toggleSide() {
  sideOpen = !sideOpen;
  try { localStorage.setItem(SIDE, sideOpen ? "open" : "closed"); } catch { /* full or blocked */ }
  renderSide();
}
function rename(row) {                                // the chat's name, edited in place: Enter keeps, Escape drops
  const c = chats.find(x => x.id === row.dataset.id), span = row.querySelector("span");
  if (!c || !span) return;
  const input = document.createElement("input");
  input.className = "rename"; input.value = c.title; input.maxLength = 120;
  span.replaceWith(input); input.focus(); input.select();
  let done = false;
  const finish = keep => {
    if (done) return;
    done = true;
    const title = input.value.trim();
    if (keep && title && title !== c.title) { c.title = title; save(); }
    renderChats();
  };
  input.addEventListener("keydown", e => { if (e.key === "Enter") finish(true); else if (e.key === "Escape") finish(false); });
  input.addEventListener("blur", () => finish(true));
}
function render() {
  const c = chat(), col = $("col"), blank = !c || !c.turns.length;
  $("main").classList.toggle("blank", blank);
  renderChats();
  $("session").textContent = c && c.session ? c.session : "";
  if (shownChat !== current) { shownChat = current; if (c) setPick(c.model); }
  const locked = !blank;                              // a session keeps its harness; provider and model may change
  const hv = $("harness").value, all = catalog.harnesses.map(h => h.name);
  if (locked) options($("harness"), [], hv);          // only its own harness, so the label is as wide as its name
  else if ($("harness").options.length !== all.length) options($("harness"), all, hv);
  $("harness").disabled = locked; $("harness").parentElement.classList.toggle("locked", locked);
  $("harness").parentElement.title = locked ? "A chat keeps its harness: start a new chat for another" : "Harness";
  if (blank) col.innerHTML = `<div class="hello"><h1>What should we build?</h1><p>The agent works in its own sandbox. What it makes opens on the right.</p></div>`;
  else {
    col.innerHTML = c.turns.map((t, i) => t.role === "user"
      ? `<div class="msg user"><div class="bubble">${esc(t.text)}</div>` + (t.attachments && t.attachments.length ? `<div class="attached">` + t.attachments.map(a => `<span class="tile" style="cursor:default">${badge(a.name)}<span class="nm">${esc(a.name)}</span></span>`).join("") + `</div>` : "") + `</div>`
      : `<div class="msg assistant" id="t${i}">${renderAssistant(c, t, isLive(t), i)}</div>`).join("");
    c.turns.forEach((t, i) => { if (t.role === "assistant") enhance($("t" + i), { diagrams: !isLive(t) }); });
  }
  $("log").scrollTop = $("log").scrollHeight;
  renderPanel();
}
let frame = 0;
function renderLive() {                               // at most once a frame while streaming
  if (frame) return;
  frame = requestAnimationFrame(() => {
    frame = 0;
    const c = chat(), log = $("log");
    if (!c) return;
    const stick = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    for (const turn of dirty) {
      const i = c.turns.indexOf(turn), el = $("t" + i);
      if (i < 0) continue;
      if (!el) { render(); break; }
      el.innerHTML = renderAssistant(c, turn, isLive(turn), i);
      enhance(el, { diagrams: !isLive(turn) });
    }
    dirty.clear();
    if (stick) log.scrollTop = log.scrollHeight;
  });
}

// ── workspace files ────────────────────────────────────────────────────────────────────────────────────────────
async function filesOf(sid) {
  try { return (await (await fetch(`api/files?session=${encodeURIComponent(sid)}`)).json()).files || []; } catch { return []; }
}
async function refreshFiles() {
  const c = chat();
  const list = c && c.session ? await filesOf(c.session) : [];
  if (chat() === c) files = list;                     // the reader may have moved to another chat meanwhile
}
async function sessionState(sid) {
  try { return (await (await fetch(`api/session?session=${encodeURIComponent(sid)}`)).json()).state || "idle"; } catch { return "idle"; }
}
/** Until the session's state is saved: a turn ends before its process exits and its workspace is saved. */
async function settle(c, turn) {
  const sid = c.session, until = Date.now() + SETTLE_MS;
  saving.add(sid);
  if (chat() === c) renderPanel();
  try {
    for (let wait = 400; Date.now() < until && lastAssistant(c) === turn; wait = Math.min(wait * 1.5, 3000)) {
      const state = await sessionState(sid);
      if (state !== "running" && state !== "saving") return;
      await sleep(wait);
    }
  } finally { saving.delete(sid); }
}
async function afterTurn(c, turn, before) {
  // The workspace as the turn left it: tiles for what it made or changed, and what it built opens in the panel.
  await settle(c, turn);
  const list = await filesOf(c.session);
  if (chat() === c) files = list;
  const edited = new Set();
  for (const seg of turn.segments) if (seg.t === "steps") for (const s of seg.steps) if (s.path && s.verb !== "Deleted") edited.add(rel(s.path));
  if (before) for (const f of list) { const b = before[f.path]; if (!b || b.bytes !== f.bytes || b.at !== f.at) edited.add(f.path); }
  // best kind first; then what the turn named (its commands, edits and reply), then the newest
  const listed = new Map(list.map(f => [f.path, f]));
  const said = turn.segments.map(s => s.t === "steps" ? s.steps.map(x => `${x.label} ${x.detail}`).join(" ") : s.text).join(" ");
  const named = p => said.includes(p.split("/").pop()) ? 0 : 1;
  turn.files = [...edited].filter(p => listed.has(p))
    .sort((a, b) => rank(a) - rank(b) || named(a) - named(b) || listed.get(b).at - listed.get(a).at).slice(0, 12);
  save();
  if (chat() !== c) return;
  const show = turn.files.find(p => SHOWCASE.includes(kindOf(p)));
  if (show && turn.status === "completed" && turn === lastAssistant(c)) openFile(show);
  render();
}
const rank = p => { const i = SHOWCASE.indexOf(kindOf(p)); return i < 0 ? SHOWCASE.length : i; };

// ── a turn ─────────────────────────────────────────────────────────────────────────────────────────────────────
// Turns run in the background on the platform; the page follows each one's events while it is open. A turn without a
// status has not ended: it shows as working, and after a reload the page follows it again from its first event.
const streams = new Map();                            // turn -> AbortController, for the turns this page is following
const dirty = new Set();                              // turns with events not yet drawn
const isLive = t => t.role === "assistant" && !t.status;
const liveTurn = c => c ? [...c.turns].reverse().find(isLive) : undefined;
const lastAssistant = c => [...c.turns].reverse().find(t => t.role === "assistant");
function apply(turn, ev) {
  const last = turn.segments.at(-1);
  if (last && last.t === "thinking" && !last.ended && ev.type !== "thinking") last.ended = Date.now();
  if (ev.type === "text") { if (last && last.t === "text") last.text += ev.delta; else turn.segments.push({ t: "text", text: ev.delta }); }
  else if (ev.type === "thinking") { if (last && last.t === "thinking") last.text += ev.delta; else turn.segments.push({ t: "thinking", text: ev.delta, started: Date.now() }); }
  else if (ev.type === "tool") {
    const step = { id: ev.id, icon: ev.icon, label: ev.label, verb: ev.verb, path: ev.path, detail: ev.detail || "", output: ev.output || "" };
    if (last && last.t === "steps") last.steps.push(step); else turn.segments.push({ t: "steps", steps: [step] });
  } else if (ev.type === "tool_output") {
    for (const seg of turn.segments) if (seg.t === "steps") for (const s of seg.steps) if (s.id === ev.id) { s.output = ev.text; s.exit_code = ev.exit_code; }
  } else if (ev.type === "end") { turn.status = ev.status || "failed"; turn.error = ev.error; }
}
async function follow(c, turn, url, init, before) {
  // Read one turn's events into the conversation, whichever chat is on screen.
  const controller = new AbortController();
  streams.set(turn, controller);
  try {
    const res = await fetch(url, { ...init, signal: controller.signal });
    const reader = res.body.getReader(), decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const text = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        if (!text.startsWith("data: ")) continue;
        const ev = JSON.parse(text.slice(6));
        if (ev.type === "start") {
          turn.rid = ev.id; c.session = ev.session || c.session;
          if (turn === lastAssistant(c)) c.previous = ev.id;
          if (turn.stopping) post("api/cancel", { id: ev.id }).catch(() => {});
          save(); renderChats();
          if (chat() === c) $("session").textContent = c.session || "";
        } else apply(turn, ev);
        if (chat() === c) { dirty.add(turn); renderLive(); }
      }
    }
    if (!turn.status && !turn.rid) { turn.status = "failed"; turn.error = "the turn did not start"; }
  } catch (e) {
    if (e.name === "AbortError") return;             // its chat was deleted
    if (!turn.rid) { turn.status = "failed"; turn.error = String(e); }
  } finally {
    streams.delete(turn);
    for (const seg of turn.segments) if (seg.t === "thinking" && !seg.ended) seg.ended = Date.now();
    save(); renderChats();
    if (chat() === c) render();
  }
  if (!turn.status && turn.rid && chats.includes(c)) setTimeout(() => reattach(c, turn), 2000);   // the relay broke: follow again
  else if (turn.status && c.session && chats.includes(c)) await afterTurn(c, turn, before);
}
function reattach(c, turn) {
  // Follow a turn this page lost (a reload, a dropped connection) from its first event.
  if (streams.has(turn) || turn.status || !chats.includes(c)) return;
  turn.segments = []; turn.error = null;
  follow(c, turn, `api/follow?id=${encodeURIComponent(turn.rid)}`, {}, null);
}
async function send() {
  const text = $("input").value.trim();
  if (!text || pending.some(p => p.uploading)) return;
  let c = chat();
  const model = pickedModel(), picked = { provider: $("provider").value, model: $("model").value.trim() };
  if (!c) {
    c = { id: newId(), title: text.slice(0, 60), model, previous: null, session: null, turns: [] };
    chats.unshift(c); current = c.id; files = []; resetPanel();
  }
  const running = liveTurn(c);
  if (running && !running.rid) return;               // still starting: a moment later it can be interrupted
  if (running) {                                      // interrupt: stop it; this message continues from where it stopped
    running.interrupted = true;
    post("api/cancel", { id: running.rid }).catch(() => {});
  }
  c.model = model;
  const attachments = pending.filter(p => p.id).map(p => ({ id: p.id, name: p.name }));
  pending = []; renderPending();
  c.turns.push({ role: "user", text, attachments });
  const turn = { role: "assistant", segments: [], status: null, error: null, rid: null };
  c.turns.push(turn);
  const before = Object.fromEntries(files.map(f => [f.path, f]));
  $("input").value = ""; grow();
  save(); render();
  await follow(c, turn, "api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model, input: text, previous: running ? running.rid : c.previous, attachments: attachments.map(a => a.id) }) }, before);
  if (turn.status === "completed") remember(picked.provider, picked.model);
}
function stop() {
  const t = liveTurn(chat());
  if (!t) return;
  t.stopping = true;                                  // before it starts: cancelled as soon as it has an id
  if (t.rid) post("api/cancel", { id: t.rid }).catch(() => {});
}
function updateSend() {
  const busy = !!liveTurn(chat());
  $("send").innerHTML = ic(busy ? I.stop : I.up, 18); $("send").title = busy ? "Stop" : "Send";
  $("input").placeholder = busy ? "Send a message to interrupt and steer" : "Ask the agent to build something";
}

// ── attachments ────────────────────────────────────────────────────────────────────────────────────────────────
function renderPending() {
  $("pending").innerHTML = pending.map((p, i) => `<span class="tile">${badge(p.name)}<span style="min-width:0"><div class="nm">${esc(p.name)}</div>` +
    `<div class="sub">${p.uploading ? "Uploading…" : p.error ? esc(p.error) : size(p.bytes)}</div></span><button data-unpend="${i}" title="Remove">${ic(I.close, 13)}</button></span>`).join("");
}
function attach(list) {
  for (const file of list) {
    const item = { name: file.name, bytes: file.size, uploading: true };
    pending.push(item); renderPending();
    if (file.size > 20 * 1048576) { item.uploading = false; item.error = "Over 20 MB"; renderPending(); continue; }
    const reader = new FileReader();
    reader.onload = () => {
      post("api/attach", { name: file.name, data: String(reader.result).split(",")[1] || "" })
        .then(d => { item.id = d.id; item.uploading = false; })
        .catch(e => { item.uploading = false; item.error = e.message; })
        .finally(renderPending);
    };
    reader.readAsDataURL(file);
  }
}

// ── the picker: harness / provider / model, from what the organization has ────────────────────────────────────
function used() { try { return JSON.parse(localStorage.getItem(MODELS)) || {}; } catch { return {}; } }
function remember(provider, model) {
  if (!provider || !model) return;
  const u = used();
  u[provider] = [model, ...(u[provider] || []).filter(m => m !== model)].slice(0, 12);
  try { localStorage.setItem(MODELS, JSON.stringify(u)); } catch { /* full or blocked */ }
}
function harnessOf(name) { return catalog.harnesses.find(h => h.name === name); }
function options(sel, names, value) {
  if (value && !names.includes(value)) names = [value, ...names];   // a chat's model the catalog no longer has
  sel.innerHTML = names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  sel.value = value || names[0] || "";
}
function fillProviders(want, keep) {        // keep: a chat's own provider stays even if the catalog lacks it
  const h = harnessOf($("harness").value), apis = h ? h.provider_apis || [] : null;
  const fits = catalog.providers.filter(p => !apis || apis.includes(p.api)).map(p => p.name);
  $("route").classList.toggle("solo", !!h && !apis.length);      // a harness that calls no model (shell)
  const pick = [want, h && h.default_provider].find(n => n && fits.includes(n)) || (keep && want) || fits[0];
  options($("provider"), fits, pick);
  fillModels();
}
function fillModels() {
  const h = harnessOf($("harness").value), def = h ? h.default_model || "" : "";
  $("model").placeholder = def ? `${def} (default)` : "model";
  const list = [...new Set([def, ...(used()[$("provider").value] || [])].filter(Boolean))];
  $("models").innerHTML = list.map(m => `<option value="${esc(m)}">`).join("");
}
function setPick(id) {
  const [harness = "", provider = "", ...rest] = (id || "").split("/"), model = rest.join("/");   // the model may hold a /
  options($("harness"), catalog.harnesses.map(h => h.name), harness);
  fillProviders(provider, true);
  $("model").value = model;
}
function pickedModel() {
  const h = harnessOf($("harness").value), harness = $("harness").value;
  if (h && !(h.provider_apis || []).length) return harness;
  const provider = $("provider").value, model = $("model").value.trim();
  return [harness, provider, model].filter(Boolean).join("/") || catalog.default_model;
}

// ── wiring ─────────────────────────────────────────────────────────────────────────────────────────────────────
function grow() { const t = $("input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 220) + "px"; }
function wire() {
  $("new").innerHTML = $("hnew").innerHTML = ic(I.edit); $("attach").innerHTML = ic(I.clip); $("filesbtn").innerHTML = ic(I.panel);
  $("sideclose").innerHTML = $("sideopen").innerHTML = ic(I.side);
  $("sideclose").addEventListener("click", toggleSide); $("sideopen").addEventListener("click", toggleSide);
  document.querySelectorAll(".dd").forEach(e => { e.innerHTML = ic(I.down2, 16); });
  $("input").addEventListener("input", grow);
  $("input").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
  $("input").addEventListener("paste", e => { if (e.clipboardData && e.clipboardData.files.length) { e.preventDefault(); attach(e.clipboardData.files); } });
  $("send").addEventListener("click", () => liveTurn(chat()) ? stop() : send());
  $("attach").addEventListener("click", () => $("picker").click());
  $("picker").addEventListener("change", e => { attach(e.target.files); e.target.value = ""; });
  $("pending").addEventListener("click", e => { const b = e.target.closest("[data-unpend]"); if (b) { pending.splice(+b.dataset.unpend, 1); renderPending(); } });
  document.addEventListener("dragover", e => e.preventDefault());
  document.addEventListener("drop", e => { e.preventDefault(); if (e.dataTransfer && e.dataTransfer.files.length) attach(e.dataTransfer.files); });
  $("col").addEventListener("toggle", e => {          // a section the reader opens or closes stays that way
    const d = e.target, c = chat();
    if (!c || d.tagName !== "DETAILS" || d.dataset.t === undefined) return;
    const seg = c.turns[+d.dataset.t]?.segments[+d.dataset.s];
    if (seg && seg.open !== d.open) { seg.open = d.open; save(); }
  }, true);
  $("col").addEventListener("click", e => {
    const open = e.target.closest("[data-open]");
    if (open) { e.preventDefault(); openFile(open.dataset.open); return; }
    const reply = e.target.closest("[data-reply]");
    if (reply) {
      const turn = chat()?.turns[+reply.closest(".msg").id.slice(1)];
      if (turn) copyText(replyText(turn)).then(() => flash(reply));
      return;
    }
    const act = e.target.closest(".cb [data-act]");
    if (act) codeAction(act, view => openPanel("view", view));
  });
  $("filesbtn").addEventListener("click", async () => { if (isOpen()) { togglePanel(); return; } await refreshFiles(); openPanel("files"); });
  const fresh = () => { current = null; files = []; resetPanel(); save(); render(); $("input").focus(); };
  $("new").addEventListener("click", fresh); $("hnew").addEventListener("click", fresh);
  $("chats").addEventListener("dblclick", e => { const row = e.target.closest(".chat"); if (row && !e.target.closest("input")) rename(row); });
  $("chats").addEventListener("click", async e => {
    if (e.target.closest("input")) return;
    const ren = e.target.closest("[data-ren]");
    if (ren) { rename(ren.closest(".chat")); return; }
    const del = e.target.closest("[data-del]");
    if (del) {
      const gone = chats.find(c => c.id === del.dataset.del);
      for (const t of gone.turns) if (isLive(t)) {   // a deleted chat's work stops too
        if (t.rid) post("api/cancel", { id: t.rid }).catch(() => {});
        streams.get(t)?.abort();
      }
      chats = chats.filter(c => c !== gone);
      if (current === gone.id) { current = null; files = []; resetPanel(); }
      save(); render();
      return;
    }
    const row = e.target.closest(".chat");
    if (!row || row.dataset.id === current) return;
    current = row.dataset.id; files = []; resetPanel(); save(); render();
    await refreshFiles(); renderPanel();
  });
  $("harness").addEventListener("change", () => { fillProviders(""); $("model").value = ""; });   // the harness's own defaults
  $("provider").addEventListener("change", fillModels);
}

async function openFromHash() {                      // #open=cntr_…: a platform session (the console's, or the SDK's)
  const sid = new URLSearchParams(location.hash.slice(1)).get("open");
  if (!sid) return;
  history.replaceState(null, "", location.pathname);
  let c = chats.find(x => x.session === sid);
  if (!c) {
    const r = await fetch(`api/container?session=${encodeURIComponent(sid)}`);
    if (!r.ok) return;
    const m = (await r.json()).metadata || {};
    c = { id: newId(), title: `${m.harness || "session"} · ${sid.slice(5, 13)}`, model: m.harness || "", previous: m.last_response_id || null,
          session: sid, turns: [] };
    chats.unshift(c);
  }
  current = c.id; files = []; resetPanel(); save(); render();
  await refreshFiles(); renderPanel();
}

function start() {
  const embedded = window.self !== window.top;                              // inside the console: its theme, no second brand,
  document.body.classList.toggle("embedded", embedded);                      // the chat list at any width
  document.documentElement.classList.toggle("embedded", embedded);
  initPanel({ session: () => chat()?.session || null, files: () => files, saving: () => saving.has(chat()?.session), refresh: refreshFiles });
  wire();
  load();
  try { const open = localStorage.getItem(STORE + "-open"); current = chats.some(c => c.id === open) ? open : null; } catch { current = null; }
  try { sideOpen = localStorage.getItem(SIDE) !== "closed"; } catch { sideOpen = true; }
  renderSide();
  for (const c of chats) for (const t of c.turns) if (isLive(t)) {     // turns still running when the page last closed
    if (t.rid) reattach(c, t); else { t.status = "failed"; t.error = "the page closed before the turn started"; }
  }
  save(); render(); grow(); $("input").focus();
  refreshFiles().then(renderPanel);
  openFromHash();
  fetch("api/catalog").then(r => r.json()).then(d => {
    if (d.harnesses) catalog = d;
    const c = chat();
    setPick(c ? c.model : catalog.default_model);
    render();
  }).catch(() => {});
}
start();
