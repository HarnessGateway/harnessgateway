// Markdown as DOM nodes, for an agent's replies and for the documents it writes: headings, paragraphs, emphasis,
// strikethrough, code (inline and fenced), links (inline, in angle brackets, bare), images, block quotes and GitHub's
// alerts, nested lists and task lists, tables with alignment, rules, mathematics ($…$, $$…$$, \(…\), \[…\]) and a
// short list of HTML tags.
//
// Nothing the text holds becomes markup: every node is made here, and HTML in the text is parsed into an inert
// document and rebuilt from a list of tags and attributes that are kept. Links and images go through the caller's
// resolvers, so a document can neither run code nor load anything from a host on the web.
import { h } from "../lib/dom.js";

/**
 * options
 *   breaks       a line break inside a paragraph is a break (a reply) rather than a space (a document)
 *   link(url)    → {href} (the web: a new tab) | {open: path} (one of the session's files) | {anchor} | null (text only)
 *   image(url)   → an address this page may load, or null
 *   open(path)   what opening one of the session's files does
 *   code(text, language, closed) → the node for a fenced block (closed: its fence has ended, while a reply streams)
 */
export const markdown = (source, options = {}) => blocks(String(source ?? "").replace(/\r\n?/g, "\n").split("\n"), options);

export const slug = text => String(text).toLowerCase().replace(/[^\p{L}\p{N}\s_-]/gu, "").trim().replace(/\s+/g, "-");

// ── blocks ─────────────────────────────────────────────────────────────────────────────────────────────────────
const FENCE = /^ {0,3}(`{3,}|~{3,})[ \t]*([^\s`]*)[^`]*$/, HEADING = /^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$/;
const RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/, QUOTE = /^ {0,3}> ?(.*)$/, ITEM = /^( *)([-*+]|\d{1,9}[.)])(?:[ \t]+(.*))?$/;
const DELIMITER = /^ {0,3}\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/, MATH = /^ {0,3}(\$\$|\\\[)(.*)$/;
const ALERT = /^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*$/i, HTML = /^ {0,3}<\/?[a-zA-Z][a-zA-Z0-9-]*(?=[\s/>]|$)/;
const starts = line => FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line) || ITEM.test(line) || MATH.test(line);

function blocks(lines, o) {
  const out = [];
  for (let at = 0; at < lines.length;) {
    const line = lines[at];
    let found;
    if (!line.trim()) { at++; continue; }
    if ((found = FENCE.exec(line))) {
      const end = lines.findIndex((other, index) => index > at && new RegExp(`^ {0,3}${found[1][0]}{${found[1].length},}[ \\t]*$`).test(other));
      const text = lines.slice(at + 1, end < 0 ? lines.length : end).join("\n");
      out.push(o.code ? o.code(text, found[2].toLowerCase(), end >= 0) : h("pre", {}, h("code", {}, text)));
      at = end < 0 ? lines.length : end + 1;
    } else if ((found = MATH.exec(line))) {
      const close = found[1] === "$$" ? "$$" : "\\]", body = [found[2]];
      while (!body[body.length - 1].includes(close) && ++at < lines.length) body.push(lines[at]);
      out.push(math(body.join("\n").split(close)[0].trim(), true));
      at++;
    } else if ((found = HEADING.exec(line))) {
      out.push(h(`h${found[1].length}`, { class: "heading", "data-anchor": slug(found[2]) }, inline(found[2], o)));
      at++;
    } else if (RULE.test(line)) { out.push(h("hr")); at++; }
    else if (QUOTE.test(line)) {
      const inner = [];
      for (; at < lines.length && QUOTE.test(lines[at]); at++) inner.push(QUOTE.exec(lines[at])[1]);
      const alert = ALERT.exec(inner[0]);
      out.push(h("blockquote", { class: alert ? `alert alert-${alert[1].toLowerCase()}` : null },
        alert && h("b", {}, alert[1][0] + alert[1].slice(1).toLowerCase()), blocks(alert ? inner.slice(1) : inner, o)));
    } else if (ITEM.test(line)) at = list(lines, at, o, out);
    else if (line.includes("|") && DELIMITER.test(lines[at + 1] || "")) at = table(lines, at, o, out);
    else if (HTML.test(line)) {
      const end = lines.findIndex((other, index) => index > at && !other.trim());
      out.push(...sanitize(lines.slice(at, end < 0 ? lines.length : end).join("\n"), o));
      at = end < 0 ? lines.length : end;
    } else {
      const text = [];
      for (; at < lines.length && lines[at].trim() && !(text.length && starts(lines[at])); at++) text.push(lines[at].trim());
      out.push(h("p", {}, inline(text.join("\n"), o)));
    }
  }
  return out;
}

/** A list from `at`: its items are the lines that begin as the first does, and each holds what is indented under it. */
function list(lines, at, o, out) {
  const [, lead, marker] = ITEM.exec(lines[at]), ordered = /\d/.test(marker), items = [];
  const same = line => { const found = ITEM.exec(line); return found && found[1].length === lead.length && /\d/.test(found[2]) === ordered; };
  while (at < lines.length && same(lines[at])) {
    const inner = [ITEM.exec(lines[at])[3] || ""], width = lead.length + 2;
    for (at++; at < lines.length; at++) {
      const line = lines[at], next = lines[at + 1] || "";
      if (!line.trim()) { if (next.search(/\S/) < width) break; inner.push(""); }      // a blank line stays when what follows is still the item's
      else if (line.search(/\S/) >= width) inner.push(line.slice(width));
      else break;
    }
    const task = /^\[([ xX])\][ \t]+/.exec(inner[0]);
    if (task) inner[0] = inner[0].slice(task[0].length);
    const body = blocks(inner, o), tight = body.length && body[0].tagName === "P";
    items.push(h("li", { class: task ? "task" : null }, task && h("input", { type: "checkbox", disabled: true, checked: task[1] !== " " }),
      tight ? [...body[0].childNodes] : null, tight ? body.slice(1) : body));
    while (at < lines.length && !lines[at].trim() && same(lines[at + 1] || "")) at++;
  }
  out.push(h(ordered ? "ol" : "ul", { start: ordered && parseInt(marker, 10) !== 1 ? parseInt(marker, 10) : null }, items));
  return at;
}

/** A table row's cells: split on the pipes that are neither escaped nor inside a code span. */
function cells(line) {
  const out = [];
  let cell = "", code = false;
  for (const [index, char] of [...line.trim().replace(/^\||\|$/g, "")].entries()) {
    if (char === "`") code = !code;
    if (char === "|" && !code && line.trim().replace(/^\|/, "")[index - 1] !== "\\") { out.push(cell); cell = ""; } else cell += char;
  }
  return [...out, cell].map(text => text.trim().replace(/\\\|/g, "|"));
}
function table(lines, at, o, out) {
  const align = cells(lines[at + 1]).map(mark => mark.startsWith(":") && mark.endsWith(":") ? "center" : mark.endsWith(":") ? "right" : null);
  const row = (tag, line) => h("tr", {}, cells(line).slice(0, align.length).map((cell, column) => h(tag, { style: align[column] && `text-align:${align[column]}` }, inline(cell, o))));
  const body = [];
  let end = at + 2;
  for (; end < lines.length && lines[end].trim() && lines[end].includes("|"); end++) body.push(row("td", lines[end]));
  out.push(h("div", { class: "table" }, h("table", {}, h("thead", {}, row("th", lines[at])), h("tbody", {}, body))));
  return end;
}

const math = (tex, display) => h(display ? "div" : "span", { class: display ? "math display" : "math", "data-tex": tex }, tex);

// ── inline ─────────────────────────────────────────────────────────────────────────────────────────────────────
// One alternative for each thing a run of text may hold, tried from the left; what none of them takes is text.
const INLINE = new RegExp([
  /(`+)([\s\S]*?[^`])\1(?!`)/,                                  // 1, 2: a code span
  /!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)/,       // 3, 4: an image
  /\[((?:[^[\]\\]|\\.|\[[^\]]*\])*)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)/,   // 5, 6: a link
  /<((?:https?:\/\/|mailto:)[^>\s]+)>/,                         // 7: an address in angle brackets
  /\b(https?:\/\/[^\s<>]*[^\s<>.,;:!?'")\]])/,                  // 8: a bare address
  /\*\*([^\s*](?:[\s\S]*?[^\s])?)\*\*|__([^\s_](?:[\s\S]*?[^\s])?)__/,   // 9, 10: strong
  /~~([^\s~](?:[\s\S]*?[^\s])?)~~/,                             // 11: struck through
  /\*([^\s*](?:[^*]*?[^\s*])?)\*|(?<![\p{L}\p{N}])_([^\s_](?:[^_]*?[^\s_])?)_(?![\p{L}\p{N}])/,   // 12, 13: emphasis
  /\$\$([^$]+)\$\$|\\\[([\s\S]+?)\\\]/,                         // 14, 15: mathematics, set apart
  /(?<![\\$\w])\$([^\s$](?:[^$\n]*?[^\s$])?)\$(?![\d\w])|\\\(([\s\S]+?)\\\)/,     // 16, 17: mathematics, in the line
  /<br\s*\/?>/,                                                 // a break
  /\\([\\`*_{}[\]()#+\-.!|~$>])/,                               // 18: a character that is itself
].map(part => part.source).join("|"), "gu");

function inline(text, o) {
  const out = [], plain = run => run && out.push(...(o.breaks ? run.split("\n").flatMap((part, index) => index ? [h("br"), part] : [part]) : [run.replace(/\n/g, " ")]));
  let from = 0;
  for (const found of text.matchAll(INLINE)) {
    plain(text.slice(from, found.index));
    from = found.index + found[0].length;
    const [, , code, alt, source, label, address, pointed, bare, strong, strong2, struck, emphasised, emphasised2, apart, apart2, inLine, inLine2, itself] = found;
    if (code !== undefined) out.push(h("code", {}, code.replace(/^ (.+) $/, "$1")));
    else if (source !== undefined) { const allowed = o.image && o.image(source); out.push(allowed ? h("img", { src: allowed, alt, loading: "lazy" }) : alt); }
    else if (address !== undefined) out.push(link(address, inline(label, o), o));
    else if (pointed !== undefined || bare !== undefined) out.push(link(pointed ?? bare, [pointed ?? bare], o));
    else if (strong !== undefined || strong2 !== undefined) out.push(h("b", {}, inline(strong ?? strong2, o)));
    else if (struck !== undefined) out.push(h("del", {}, inline(struck, o)));
    else if (emphasised !== undefined || emphasised2 !== undefined) out.push(h("em", {}, inline(emphasised ?? emphasised2, o)));
    else if (apart !== undefined || apart2 !== undefined) out.push(math((apart ?? apart2).trim(), true));
    else if (inLine !== undefined || inLine2 !== undefined) out.push(math((inLine ?? inLine2).trim(), false));
    else if (itself !== undefined) out.push(itself);
    else out.push(h("br"));
  }
  plain(text.slice(from));
  return out;
}

/** A link as its resolver allows it: to the web in a new tab, to one of the session's files, or only its words. */
function link(address, words, o) {
  const to = o.link ? o.link(address) : /^(https?:|mailto:)/i.test(address) ? { href: address } : null;
  if (to && to.href) return h("a", { href: to.href, target: "_blank", rel: "noopener noreferrer" }, words);
  if (to && to.open) return h("a", { href: "#", class: "work", title: to.open, onclick: event => { event.preventDefault(); o.open && o.open(to.open); } }, words);
  if (to && to.anchor) return h("a", { href: "#", onclick: event => {
    event.preventDefault();
    event.target.closest(".doc, .text")?.querySelector(`[data-anchor="${CSS.escape(to.anchor)}"]`)?.scrollIntoView({ behavior: "smooth" });
  } }, words);
  return h("span", {}, words);
}

// ── HTML in the text ───────────────────────────────────────────────────────────────────────────────────────────
const KEEP = new Set(("a abbr b blockquote br caption cite code col colgroup dd del details dfn div dl dt em figcaption figure h1 h2 h3 h4 h5 h6 hr i img " +
  "ins kbd li mark ol p pre q s samp small span strong sub summary sup table tbody td tfoot th thead tr u ul var center").split(" "));
const DROP = new Set("script style iframe frame frameset object embed applet template noscript form input button select textarea link meta base title head svg math".split(" "));
const ATTRIBUTES = ["align", "alt", "title", "width", "height", "colspan", "rowspan", "open", "start", "span"];

/** HTML from a document, rebuilt from a parsed, inert copy of it: the kept tags and attributes and nothing else. */
export function sanitize(html, o = {}) {
  const rebuilt = node => {
    if (node.nodeType === Node.TEXT_NODE) return [node.textContent];
    if (node.nodeType !== Node.ELEMENT_NODE || DROP.has(node.localName)) return [];
    const inside = [...node.childNodes].flatMap(rebuilt);
    if (!KEEP.has(node.localName)) return inside;                 // an unknown tag: its words stay
    if (node.localName === "a") return [link(node.getAttribute("href") || "", inside, o)];
    const allowed = Object.fromEntries(ATTRIBUTES.filter(name => node.hasAttribute(name)).map(name => [name, node.getAttribute(name) || true]));
    if (node.localName === "img") {
      const source = o.image && o.image(node.getAttribute("src") || "");
      return source ? [h("img", { ...allowed, src: source, loading: "lazy" })] : [allowed.alt || ""];
    }
    return [h(node.localName, allowed, inside)];
  };
  return [...new DOMParser().parseFromString(html, "text/html").body.childNodes].flatMap(rebuilt);
}
