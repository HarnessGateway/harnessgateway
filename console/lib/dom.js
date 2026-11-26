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
  clip: "M21 11l-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5",
  side: "M3 5h18v14H3zM9 5v14", panel: "M3 5h18v14H3zM15 5v14", open: "M14 4h6v6M20 4L10 14M19 14v5H5V5h5",
  save: "M12 4v11M7 10l5 5 5-5M5 20h14", refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7", widen: "M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7",
  narrow: "M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7", chevron: "M9 6l6 6-6 6", terminal: "M5 8l4 4-4 4M12 17h7",
  file: "M14 3H6v18h12V7zM14 3v4h4", tool: "M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17l3 3 5.3-5.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z",
  search: "M11 5a6 6 0 1 0 0 12 6 6 0 0 0 0-12zM20 20l-4.5-4.5", copy: "M8 8h12v12H8zM16 8V4H4v12h4", rewind: "M3 12a9 9 0 1 0 3-6.7M3 4v5h5",
  branch: "M6 7.5v9M6 3a2.2 2.2 0 1 0 0 4.4A2.2 2.2 0 0 0 6 3zM6 16.6A2.2 2.2 0 1 0 6 21a2.2 2.2 0 0 0 0-4.4zM18 7a2.2 2.2 0 1 0 0 4.4A2.2 2.2 0 0 0 18 7zM18 11.4a6 6 0 0 1-6 6H8.2",
  play: "M7 5v14l12-7z", source: "M8 8l-4 4 4 4M16 8l4 4-4 4", eye: "M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
  rename: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z",
  keys: "M4 10a4 4 0 1 0 8 0a4 4 0 1 0-8 0M12 10h9M18 10v3M21 10v2",
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
