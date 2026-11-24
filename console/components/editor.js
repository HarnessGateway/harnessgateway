// Editing one thing (an environment, a harness): a working copy that is kept while the person moves between
// tabs and pages, and one frame with the same actions in the same places.
import { pill } from "./layout.js";
import { add, clear, h, icon } from "../lib/dom.js";

/** Working copies, by "<kind>/<name>". They live as long as the page does: a typed secret is never written to the
 *  browser's storage. One is dropped when it is saved or discarded. */
export const drafts = new Map();
export const NEW = "+";                          // the name a thing has in its address before it exists

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
