// How the console tells a person something, and asks: a toast, and a dialog that does one thing.
import { field } from "./layout.js";
import { h } from "../lib/dom.js";

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
