// Building pages out of DOM nodes. Nothing here writes markup from a string that came from anywhere but this
// file, so nothing an agent or an API returned can become markup: every value is a text node.

const SVG = "http://www.w3.org/2000/svg";
const ICONS = {
  chat: "M4 5h16v11H9l-5 4z", providers: "M7 18a4 4 0 0 1-.5-7.97 5.5 5.5 0 0 1 10.6-1.2A4.6 4.6 0 0 1 17 18z", environments: "M4 6h16v12H4zM8 10l2 2-2 2M13 14h3",
  harnesses: "M12 3l8 4.5v9L12 21l-8-4.5v-9zM12 12l8-4.5M12 12v9M12 12L4 7.5", sessions: "M4 6h16M4 12h16M4 18h9",
  usage: "M5 20V10M12 20V4M19 20v-7", back: "M15 6l-6 6 6 6", plus: "M12 5v14M5 12h14", close: "M6 6l12 12M18 6L6 18",
  cpu: "M7 7h10v10H7zM10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4", memory: "M4 8h16v8H4zM8 8v8M12 8v8M16 8v8",
  network: "M12 3v18M3 12h18M5.5 6.5a9 9 0 0 0 13 0M5.5 17.5a9 9 0 0 1 13 0", storage: "M4 7c0-2 16-2 16 0v10c0 2-16 2-16 0zM4 7c0 2 16 2 16 0M4 12c0 2 16 2 16 0",
  send: "M12 19V5M6 11l6-6 6 6", stop: "M7 7h10v10H7z",
};

/** h("div", {class: "card", onclick: fn}, child, "text", [more]). A null, undefined or false child adds nothing. */
export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else if (["value", "checked", "disabled", "hidden", "open", "selected", "readOnly"].includes(name)) node[name] = value;
    else node.setAttribute(name, value === true ? "" : value);
  }
  return add(node, children);
}
export function add(node, ...children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
export const clear = node => { node.replaceChildren(); return node; };
/** An outline: one of the console's own by name, or the path given. */
export function icon(name) {
  const svg = document.createElementNS(SVG, "svg"), path = document.createElementNS(SVG, "path");
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("class", "i");
  path.setAttribute("d", ICONS[name] || name);
  svg.append(path);
  return svg;
}

export const pill = (text, tone = "") => h("span", { class: `pill ${tone}` }, text);
export const mono = text => h("span", { class: "mono" }, text);
export const field = (label, control, hint) => h("div", {}, h("label", {}, label), control, hint && h("div", { class: "hint" }, hint));
export const top = (title, lead, ...acts) => h("div", { class: "top" }, h("div", {}, h("h1", {}, title), h("p", {}, lead)), h("div", { class: "acts" }, acts));
export const STATUS = { completed: "ok", failed: "bad", cancelled: "warn", incomplete: "warn", in_progress: "accent", queued: "" };

export function toast(text, bad = false) {
  const node = h("div", { class: `toast${bad ? " bad" : ""}` }, text);
  document.getElementById("toasts").append(node);
  setTimeout(() => node.remove(), bad ? 7000 : 2600);
}
export const fail = error => toast(error && error.message ? error.message : String(error), true);

/** A dialog. `onAction(form)` does the work; when it throws, the dialog stays and says why. */
export function dialog(title, body, action, onAction, { danger = false } = {}) {
  const go = h("button", { class: `primary${danger ? " danger" : ""}` }, action);
  const form = h("form", { class: "dialog" }, h("h3", {}, title), body,
    h("div", { class: "acts" }, h("button", { type: "button", class: "quiet", onclick: () => veil.remove() }, "Cancel"), go));
  const veil = h("div", { class: "veil", onmousedown: event => { if (event.target === veil) veil.remove(); },
                          onkeydown: event => { if (event.key === "Escape") veil.remove(); } }, form);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    go.disabled = true;
    try { await onAction(form); veil.remove(); } catch (error) { fail(error); } finally { go.disabled = false; }
  });
  document.body.append(veil);
  (form.querySelector("input, select, textarea") || go).focus();
  return form;
}
/** Ask before something that cannot be taken back. */
export const confirm = (question, onYes, action = "Delete") => dialog("Are you sure?", h("p", { class: "muted" }, question), action, onYes, { danger: true });
/** Ask for one line of text. `onAnswer(text)` may throw to say why the answer will not do. */
export const ask = (title, label, value, onAnswer, action = "OK") => dialog(title,
  field(label, h("input", { name: "answer", value, required: true, autocomplete: "off", spellcheck: "false" })), action,
  form => onAnswer(form.elements.answer.value.trim()));

export const lines = text => String(text || "").split(/[\n,]+/).map(part => part.trim()).filter(Boolean);
export function bytes(count) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(count) || 0, unit = 0;
  while (value >= 1000 && unit < units.length - 1) { value /= 1000; unit++; }
  return `${unit && value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}
export function ago(time) {
  const seconds = Math.max(0, (Date.now() - Date.parse(time)) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}
/** A number worth reading: three figures of it, however small it is. */
export const figure = value => Number(value) ? Number(Number(value).toPrecision(3)).toLocaleString(undefined, { maximumFractionDigits: 6 }) : "0";

/** Give the person a file to keep. */
export function download(filename, content, type = "application/octet-stream") {
  const link = h("a", { href: URL.createObjectURL(content instanceof Blob ? content : new Blob([content], { type })), download: filename });
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
/** Ask the person for a file. Resolves to it, or never when they change their mind. */
export const pickFile = accept => new Promise(resolve => {
  const input = h("input", { type: "file", accept, hidden: true, onchange: () => { if (input.files[0]) resolve(input.files[0]); input.remove(); } });
  document.body.append(input);
  input.click();
});
