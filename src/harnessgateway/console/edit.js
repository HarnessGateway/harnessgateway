// What editing an environment and editing a harness have in common: a working copy that is kept while the person
// moves between tabs and pages, one frame with the same actions in the same places, a code editor, and a list of
// rows of inputs.
import { add, clear, h, icon, pill } from "./ui.js";

/** Working copies, by "<kind>/<name>". They live as long as the page does: a typed secret is never written to the
 *  browser's storage. One is dropped when it is saved or discarded. */
export const drafts = new Map();
export const NEW = "+";                          // the name a thing has in its address before it exists
export const again = () => window.dispatchEvent(new Event("hashchange"));

window.addEventListener("beforeunload", event => {
  if ([...drafts.values()].some(draft => draft.dirty)) event.preventDefault();
});

/** The frame around one thing being edited. `tabs` are [id, label, count]; `href(id)` is a tab's address. Returns
 *  where the tab's content goes, and `changed(dirty)` to say whether there is something to save. */
export function frame(main, { back, backLabel, title, subtitle, tabs, tab, href, isNew, onSave, onDiscard, more = [] }) {
  const state = pill(isNew ? "Not created yet" : "Unsaved", "warn");
  const save = h("button", { class: "primary", onclick: onSave }, isNew ? "Create" : "Save");
  const discard = h("button", { class: "quiet", onclick: onDiscard }, isNew ? "Cancel" : "Discard");
  const body = h("div", {});
  add(clear(main),
    h("a", { class: "crumb", href: back }, icon("back"), backLabel),
    h("div", { class: "head" },
      h("div", { style: "min-width:0" }, h("h1", {}, title, state), h("div", { class: "sub" }, subtitle)),
      h("div", { class: "acts" }, more.map(([label, onclick, danger]) => h("button", { class: `quiet${danger ? " danger" : ""}`, onclick }, label)), discard, save)),
    h("div", { class: "tabs" }, tabs.map(([id, label, count]) =>
      h("a", { href: href(id), class: id === tab ? "on" : "" }, label, count ? h("span", { class: "n" }, count) : null))),
    body);
  const changed = dirty => { state.hidden = discard.hidden = !(dirty || isNew); save.disabled = !(dirty || isNew); };
  changed(false);
  main.addEventListener("keydown", event => {       // Ctrl or Cmd with S saves, wherever the cursor is
    if ((event.metaKey || event.ctrlKey) && event.key === "s") { event.preventDefault(); if (!save.disabled) onSave(); }
  });
  return { body, changed };
}

/** A code editor: line numbers, a Tab key that indents, and nothing else. `onInput(text)` hears every change. */
export function code({ value = "", placeholder = "", onInput }) {
  const gutter = h("div", { class: "gutter" });
  const area = h("textarea", { spellcheck: "false", wrap: "off", placeholder, autocapitalize: "off", autocomplete: "off" });
  area.value = value;
  const number = () => { gutter.textContent = area.value.split("\n").map((_, line) => line + 1).join("\n"); };
  area.addEventListener("input", () => { number(); onInput(area.value); });
  area.addEventListener("scroll", () => { gutter.scrollTop = area.scrollTop; });
  area.addEventListener("keydown", event => {
    if (event.key !== "Tab" || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    event.preventDefault();
    area.setRangeText("    ", area.selectionStart, area.selectionEnd, "end");
    area.dispatchEvent(new Event("input"));
  });
  number();
  return h("div", { class: "code" }, gutter, area);
}

/** A list of rows of inputs, with a button to add one and one to remove each. `columns` are {key, title,
 *  placeholder, type, fixed}; `rows` are objects by those keys; `onChange(rows)` hears every change. */
export function rows(columns, given, onChange, addLabel) {
  const list = h("div", {}), state = given.map(row => ({ ...row }));
  const line = row => {
    const node = h("div", { class: "row" }, columns.map(column => h("input", {
      value: row[column.key] ?? "", type: column.type || "text", autocomplete: "off", readOnly: Boolean(column.fixed && row.kept),
      placeholder: typeof column.placeholder === "function" ? column.placeholder(row) : column.placeholder,
      oninput: event => { row[column.key] = event.target.value; onChange(state); } })),
      h("button", { type: "button", class: "quiet square danger", title: "Remove", onclick: () => {
        state.splice(state.indexOf(row), 1); node.remove(); onChange(state);
      } }, icon("close")));
    return node;
  };
  add(list, state.map(line));
  return h("div", { class: "rows" },
    h("div", { class: "heads" }, columns.map(column => h("span", {}, column.title)), h("i", {})), list,
    h("button", { type: "button", onclick: () => { const row = {}; state.push(row); list.append(line(row)); list.lastChild.querySelector("input").focus(); } },
      icon("plus"), addLabel));
}
