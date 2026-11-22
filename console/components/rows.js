// A list of rows of inputs: variables, secrets.
import { add, h, icon } from "../lib/dom.js";

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
