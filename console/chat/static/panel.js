// The side panel: the session's workspace files, and a preview of one of them (or of a snippet or a diagram from a
// reply). A preview is drawn once per file version and mode, so a page that re-renders the conversation does not
// restart a video or reload a running app.

import { $, esc, size, rel, wsUrl, badge, ic, I, baseName, kindOf } from "./util.js";
import { preview, hasSource } from "./preview.js";
import { viewer, codeAction } from "./rich.js";

const LIST_MAX = 2000;                               // rows drawn in the file list; a filter finds the rest
const state = { open: false, tab: "files", view: null, mode: "preview", wide: false, filter: "", drawn: "", token: null };
let host = null;                                     // {session(), files(), saving(), refresh()}

export function initPanel(h) {
  host = h;
  $("pnew").innerHTML = ic(I.ext, 16); $("pdl").innerHTML = ic(I.down, 16); $("prefresh").innerHTML = ic(I.refresh, 16);
  $("pclose").innerHTML = ic(I.close, 16);
  $("tabfiles").addEventListener("click", async () => { await host.refresh(); openPanel("files"); });
  $("tabview").addEventListener("click", () => openPanel("view"));
  $("prefresh").addEventListener("click", async () => { await host.refresh(); state.drawn = ""; renderPanel(); });
  $("pclose").addEventListener("click", () => { state.open = false; renderPanel(); });
  $("pmode").addEventListener("click", () => { state.mode = state.mode === "code" ? "preview" : "code"; renderPanel(); });
  $("pwide").addEventListener("click", () => { state.wide = !state.wide; renderPanel(); });
  $("pbody").addEventListener("input", e => { if (e.target.id === "pfilter") { state.filter = e.target.value; drawList(); } });
  $("pbody").addEventListener("click", e => {
    if (e.target.closest("[data-stop]")) return;
    const open = e.target.closest("[data-open]");
    if (open) { e.preventDefault(); openFile(open.dataset.open); return; }
    const anchor = e.target.closest("[data-anchor-link]");
    if (anchor) {
      e.preventDefault();
      $("pbody").querySelector(`[data-anchor="${CSS.escape(anchor.dataset.anchorLink)}"]`)?.scrollIntoView({ behavior: "smooth" });
      return;
    }
    const act = e.target.closest(".cb [data-act]");
    if (act) codeAction(act, view => openPanel("view", view));
  });
}

export const isOpen = () => state.open;
export function togglePanel() { state.open = !state.open; renderPanel(); }
export function openPanel(tab, view) {
  state.open = true; state.tab = tab;
  if (view) { state.view = view; state.mode = "preview"; }
  renderPanel();
}
export function openFile(path) { openPanel("view", { kind: "file", path: rel(path) }); }
/** Another chat is on screen: its files, and nothing previewed yet. */
export function resetPanel() { state.view = null; state.tab = "files"; state.filter = ""; state.drawn = ""; }

export function renderPanel() {
  $("panel").hidden = !state.open;
  $("panel").classList.toggle("wide", state.wide);
  $("filesbtn").classList.toggle("on", state.open);
  if (!state.open) { $("pbody").innerHTML = ""; state.drawn = ""; return; }     // stops what played there
  const showFiles = state.tab === "files" || !state.view;
  $("tabfiles").classList.toggle("on", showFiles); $("tabview").classList.toggle("on", !showFiles);
  $("tabview").hidden = !state.view;
  $("pwide").innerHTML = ic(state.wide ? I.shrink : I.expand, 16);
  $("pwide").title = state.wide ? "Narrow" : "Widen";
  if (showFiles) drawFiles(); else drawView();
}

function chrome({ title = "", url = null, mode = false }) {
  $("ptitle").textContent = title;
  $("pnew").hidden = $("pdl").hidden = !url;
  if (url) { $("pnew").href = url; $("pdl").href = url + "?download=1"; }
  $("pmode").hidden = !mode;
  $("pmode").innerHTML = ic(state.mode === "code" ? I.eye : I.code, 16);
  $("pmode").title = state.mode === "code" ? "Show the preview" : "Show the code";
}

// ── files ──────────────────────────────────────────────────────────────────────────────────────────────────────
function drawFiles() {
  const sid = host.session(), files = host.files(), saving = host.saving();
  chrome({ title: sid && files.length ? `${files.length.toLocaleString()} file${files.length === 1 ? "" : "s"}` : "" });
  const key = `files|${sid}|${saving}|${files.length}|${files.map(f => f.path + f.bytes + f.at).join("|")}`;
  if (key === state.drawn) return;
  state.drawn = key;
  const body = $("pbody");
  if (!sid) { body.innerHTML = `<div class="note">Start a chat. Its workspace files show here.</div>`; return; }
  const savingNote = saving ? `<div class="saving">Saving the workspace…</div>` : "";
  if (!files.length) { body.innerHTML = savingNote || `<div class="note">No files yet. They appear here once a turn's work is saved.</div>`; return; }
  body.innerHTML = `<div class="files">${savingNote}` +
    (files.length > 12 ? `<input id="pfilter" class="filter" placeholder="Filter files" spellcheck="false" autocomplete="off" value="${esc(state.filter)}">` : "") +
    `<div id="plist"></div></div>`;
  drawList();
}

function drawList() {
  const sid = host.session(), q = state.filter.trim().toLowerCase();
  const list = q ? host.files().filter(f => f.path.toLowerCase().includes(q)) : host.files();
  $("plist").innerHTML = list.slice(0, LIST_MAX).map(f => {
    const dir = f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/") + 1) : "";
    return `<div class="row" data-open="${esc(f.path)}">${badge(f.path)}<span class="nm"><span class="dir">${esc(dir)}</span>${esc(f.path.slice(dir.length))}</span>` +
      `<span class="sz">${size(f.bytes)}</span><a href="${wsUrl(sid, f.path)}?download=1" title="Download" data-stop="1">${ic(I.down, 15)}</a></div>`;
  }).join("") + (list.length > LIST_MAX ? `<div class="note">${(list.length - LIST_MAX).toLocaleString()} more: filter to find them.</div>` : "") +
    (q && !list.length ? `<div class="note">No file matches.</div>` : "");
}

// ── the preview ────────────────────────────────────────────────────────────────────────────────────────────────
function drawView() {
  const v = state.view, sid = host.session(), body = $("pbody");
  if (v.kind === "snippet") {
    chrome({ title: `${v.lang || "code"} preview`, url: v.url });
    if (state.drawn === v.url) return;
    state.drawn = v.url;
    body.innerHTML = `<iframe sandbox="allow-scripts allow-forms allow-popups allow-modals" src="${esc(v.url)}"></iframe>`;
    return;
  }
  if (v.kind === "diagram") {
    chrome({ title: "Diagram" });
    if (state.drawn === v) return;
    state.drawn = v;
    body.replaceChildren(viewer({ kind: "mermaid", data: v.source }));
    return;
  }
  if (!sid) return;
  const f = host.files().find(x => x.path === v.path), url = wsUrl(sid, v.path), saving = host.saving();
  chrome({ title: v.path, url: f ? url : null, mode: !!f && hasSource(v.path) });
  const key = `${sid}|${v.path}|${state.mode}|${f ? `${f.bytes}|${f.at}` : `missing|${saving}`}`;
  if (key === state.drawn) return;
  state.drawn = key;
  if (!f) {
    body.innerHTML = `<div class="note">${saving ? `Saving the workspace… ${esc(baseName(v.path))} opens when the save lands.`
      : `${esc(v.path)} is not in the workspace.`}</div>`;
    return;
  }
  const token = state.token = {};
  body.scrollTop = 0;
  preview(body, { path: f.path, bytes: f.bytes, url, sid, kind: kindOf(f.path) }, state.mode, { current: () => state.token === token });
}
