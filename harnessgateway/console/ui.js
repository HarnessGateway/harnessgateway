// Small helpers for building pages without a framework.

export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") node.className = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else if (name === "value") node.value = value;
    else node.setAttribute(name, value === true ? "" : value);
  }
  return put(node, children);
}

// Adds children to a node: nodes, text, and lists of either, however nested. Nothing is added for null,
// undefined or false, so a child can be written as `condition ? node : null`.
export function put(node, ...children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export function field(label, control) {
  return h("div", {}, h("label", {}, label), control);
}

export function table(columns, rows, onPick) {
  return h("table", {},
    h("thead", {}, h("tr", {}, columns.map((column) => h("th", {}, column.title)))),
    h("tbody", {}, rows.length ? rows.map((row) =>
      h("tr", onPick ? { class: "pick", onclick: () => onPick(row) } : {},
        columns.map((column) => h("td", {}, column.cell(row))))) :
      h("tr", {}, h("td", { colspan: columns.length, class: "muted" }, "Nothing here yet."))));
}

export function pill(text, kind = "") {
  return h("span", { class: `pill ${kind}` }, text);
}

const STATUS = { completed: "good", ready: "good", failed: "bad", cancelled: "warn", incomplete: "warn" };
export function status(value) {
  return pill(value, STATUS[value] || "");
}

export function when(text) {
  return text ? new Date(text).toLocaleString() : "";
}

export function bytes(count) {
  if (count < 1024) return `${count} B`;
  if (count < 1024 * 1024) return `${(count / 1024).toFixed(1)} KB`;
  return `${(count / 1024 / 1024).toFixed(1)} MB`;
}

// Runs an action started from a button or a form, showing what the API refused in `place`.
export async function attempt(place, action) {
  clear(place);
  try {
    return await action();
  } catch (error) {
    put(place, h("div", { class: "error" }, error.message));
    return undefined;
  }
}

export function parseJson(text, what) {
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new Error(`${what} is not valid JSON: ${error.message}`);
  }
}
