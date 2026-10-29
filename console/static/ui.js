// Small view helpers: tables, cards, modals, toasts, icons. Markup is built from template strings; every value goes
// through esc() at the call site.
import { esc } from "./api.js";

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function table(cols, rows, empty = "Nothing yet.") {
  if (!rows.length) return `<div class="empty">${esc(empty)}</div>`;
  return `<table><thead><tr>${cols.map(c => `<th>${esc(c.h)}</th>`).join("")}</tr></thead><tbody>` +
    rows.map(r => `<tr>${cols.map(c => `<td${c.cls ? ` class="${c.cls}"` : ""}>${c.cell(r)}</td>`).join("")}</tr>`).join("") + `</tbody></table>`;
}
export const pill = (text, kind = "q") => `<span class="pill ${kind}">${esc(text)}</span>`;
export const statusPill = s => pill(s, { completed: "ok", active: "ok", idle: "q", running: "run", in_progress: "run", queued: "q",
                                         saving: "warn", failed: "bad", cancelled: "q", incomplete: "warn", draining: "warn", suspended: "bad" }[s] || "q");
export const card = (title, body, acts = "") => `<div class="card"><div class="ch"><h2>${esc(title)}</h2><div class="acts">${acts}</div></div>${body}</div>`;
export const stat = (k, v, small = "") => `<div class="card"><div class="k">${esc(k)}</div><div class="v">${v}${small ? `<small>${esc(small)}</small>` : ""}</div></div>`;
export const field = (label, control, help = "") => `<div class="field"><label>${esc(label)}</label>${control}${help ? `<div class="help">${esc(help)}</div>` : ""}</div>`;
export const input = (name, value = "", attrs = "") => `<input name="${name}" value="${esc(value)}" ${attrs}>`;
export const select = (name, options, value = "", attrs = "") =>
  `<select name="${name}" ${attrs}>${options.map(o => { const [v, l] = Array.isArray(o) ? o : [o, o]; return `<option value="${esc(v)}"${v === value ? " selected" : ""}>${esc(l)}</option>`; }).join("")}</select>`;
export const kv = pairs => `<div class="kv">${pairs.map(([k, v]) => `<div>${esc(k)}</div><div>${v}</div>`).join("")}</div>`;
export const pre = obj => `<pre>${esc(typeof obj === "string" ? obj : JSON.stringify(obj, null, 2))}</pre>`;

const toasts = () => $(".toasts") || document.body.appendChild(Object.assign(document.createElement("div"), { className: "toasts" }));
export function toast(message, kind = "") {
  const t = document.createElement("div");
  t.className = `toast ${kind}`; t.textContent = message;
  toasts().appendChild(t);
  setTimeout(() => t.remove(), kind === "bad" ? 7000 : 3500);
}
export const fail = e => toast(e && e.message ? e.message : String(e), "bad");

// A modal with a form. `onSubmit(data, modal)` gets the form's fields; it resolves to true to close, or throws.
export function modal({ title, body, submit = "Save", wide = false, onSubmit = null, danger = false }) {
  return new Promise(resolve => {
    const back = document.createElement("div");
    back.className = "backdrop";
    back.innerHTML = `<form class="modal${wide ? " wide" : ""}"><h2>${esc(title)}</h2>${body}<div class="mf">` +
      `<button type="button" class="btn2" data-cancel>Cancel</button>` +
      (onSubmit ? `<button type="submit" class="btn${danger ? " danger" : ""}">${esc(submit)}</button>` : "") + `</div></form>`;
    const form = back.firstElementChild;
    const close = v => { back.remove(); resolve(v); };
    back.addEventListener("click", e => { if (e.target === back) close(null); });
    form.querySelector("[data-cancel]").addEventListener("click", () => close(null));
    form.addEventListener("submit", async e => {
      e.preventDefault();
      if (!onSubmit) { close(true); return; }
      const btn = form.querySelector("[type=submit]"); btn.disabled = true;
      try {
        const data = Object.fromEntries(new FormData(form).entries());
        const ok = await onSubmit(data, form);
        if (ok !== false) close(data);
      } catch (err) { fail(err); } finally { btn.disabled = false; }
    });
    document.body.appendChild(back);
    const first = form.querySelector("input:not([type=hidden]),select,textarea");
    if (first) first.focus();
  });
}
export const confirm = (title, text, submit = "Delete") => modal({ title, body: `<p>${esc(text)}</p>`, submit, danger: true, onSubmit: () => true });
export function reveal(title, label, secret, note) {
  return modal({ title, body: `<p>${esc(note)}</p>${field(label, `<div class="secret">${esc(secret)}</div>`)}`,
                 submit: "Done", onSubmit: () => true });
}
// Renders the current page again (through the router, on a fresh element).
export const rerender = () => window.dispatchEvent(new Event("hashchange"));
// Runs after the current modal has closed: for a second dialog that follows a submit (a secret shown once).
export const then = fn => setTimeout(fn, 0);
export async function copy(text) { try { await navigator.clipboard.writeText(text); toast("Copied"); } catch { toast("Copy failed", "bad"); } }

export const I = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  nodes: '<rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01"/>',
  org: '<path d="M3 21h18M5 21V7l7-4 7 4v14M9 21v-6h6v6"/>', usage: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  box: '<path d="M21 16V8l-9-5-9 5v8l9 5 9-5Z"/><path d="M3.3 8.3 12 13l8.7-4.7M12 13v9"/>',
  cfg: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2"/>',
  log: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  chat: '<path d="M21 12a8 8 0 0 1-11.5 7.2L4 21l1.8-4.6A8 8 0 1 1 21 12Z"/>',
  sess: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 10h18M9 20V10"/>',
  code: '<path d="m8 8-4 4 4 4M16 8l4 4-4 4M14 4l-4 16"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z"/><path d="M14 3v5h5"/>',
  globe: '<path d="M4 12a8 8 0 1 0 16 0 8 8 0 0 0-16 0Z"/><path d="M4 12h16M12 4c3 3 3 13 0 16M12 4c-3 3-3 13 0 16"/>',
  shield: '<path d="M12 3 4 7v5c0 5 3.5 8 8 9 4.5-1 8-4 8-9V7l-8-4Z"/>',
  key: '<circle cx="8" cy="14" r="4"/><path d="m11 11 9-9M17 5l2 2M14 8l2 2"/>',
  search: '<circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2 20a7 7 0 0 1 14 0M17 11a3 3 0 1 0 0-6M22 20a6 6 0 0 0-5-5.9"/>',
  side: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M10 4v16"/>',
  tag: '<path d="M20 12 12 20 3 11V4h7l10 8Z"/><path d="M7 8h.01"/>',
};
export const ic = (d, s = 16) => `<svg width="${s}" height="${s}" viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;
