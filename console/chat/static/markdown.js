// Markdown to HTML, GitHub-flavoured, for replies and for files: headings, paragraphs, emphasis, strikethrough, code
// (inline, fenced, indented), links (inline, reference, autolinks, bare URLs), images, block quotes and GitHub alerts,
// nested lists and task lists, tables with alignment, rules, math ($…$, $$…$$, \(…\), \[…\]) and a safe subset of
// HTML.
//
// Everything the text holds is escaped: markup comes only from this renderer, and HTML in the text passes a filter
// that keeps a short list of tags and attributes and drops the rest (see sanitize). Links and images go through the
// caller's resolvers (see workspaceLinks): a workspace path opens in the side panel, a web link opens in a new tab,
// and anything else stays text. Images load only from the workspace or a data: URI, never from a host on the web, so
// showing a document can neither run code nor send anything anywhere.

import { esc, joinPath, wsUrl } from "./util.js";

/**
 * options
 *   breaks   a line break inside a paragraph is a break (replies) rather than a space (documents)
 *   anchors  headings carry data-anchor, so #links within a document can scroll to them
 *   link(url)  → {href} (the web, a new tab) | {open: path} (the side panel) | {anchor} | null (text only)
 *   image(url) → a URL this page may load, or null
 *   code(text, lang, closed) → the HTML for a fenced block (closed: its fence has ended, while a reply streams)
 */
export function markdown(src, options = {}) {
  return new Renderer(options).document(String(src ?? ""));
}

/** Resolvers for documents in a session's workspace, relative to the folder `dir`. */
export function workspaceLinks(sid, dir = "") {
  const scheme = url => /^[a-z][a-z0-9+.-]*:/i.test(url) && !/^file:/i.test(url);
  return {
    link(url) {
      url = url.trim();
      if (/^(https?:|mailto:)/i.test(url)) return { href: url };
      if (url.startsWith("#")) return { anchor: slug(decode(url.slice(1))) };
      if (scheme(url) || url.startsWith("//")) return null;          // javascript:, data:, vbscript:, …
      const path = sid && joinPath(dir, url);
      return path ? { open: path } : null;
    },
    image(url) {
      url = url.trim();
      if (/^data:image\/(png|jpe?g|gif|webp|avif|bmp|svg\+xml)[;,]/i.test(url)) return url;
      if (scheme(url) || url.startsWith("//")) return null;          // the web: not loaded, shown as a link
      const path = sid && joinPath(dir, url);
      return path ? wsUrl(sid, path) : null;
    },
  };
}

const decode = s => { try { return decodeURIComponent(s); } catch { return s; } };
export const slug = text => String(text).toLowerCase().replace(/<[^>]*>/g, "").replace(/[^\p{L}\p{N}\s_-]/gu, "").trim().replace(/\s/g, "-");

// ── blocks ─────────────────────────────────────────────────────────────────────────────────────────────────────
const FENCE = /^( {0,3})(`{3,}|~{3,})[ \t]*([^\s`]*)[^`]*$/;
const FENCE_END = /^ {0,3}(`{3,}|~{3,})[ \t]*$/;
const HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/;
const RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const SETEXT = /^ {0,3}(=+|-+)[ \t]*$/;
const QUOTE = /^ {0,3}> ?(.*)$/;
const ITEM = /^( {0,3}|\t)([-*+]|\d{1,9}[.)])(?:([ \t]+)(.*))?$/;
const DELIMITER = /^ {0,3}\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/;
const MATH = /^ {0,3}(\$\$|\\\[)/;
const REF = /^ {0,3}\[([^\]^][^\]]*)\]:[ \t]*<?([^\s>]+)>?(?:[ \t]+["'(](.*)["')])?[ \t]*$/;
const ALERT = /^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*$/i;
const HTML_TAGS = new Set(("a abbr address article aside b blockquote br caption center cite code col colgroup dd del details " +
  "dfn div dl dt em figcaption figure footer h1 h2 h3 h4 h5 h6 header hr i img ins kbd li main mark nav ol p picture pre q s " +
  "samp section small span strong sub summary sup table tbody td tfoot th thead tr tt u ul var video audio source script style " +
  "iframe object embed form input button select textarea noscript template svg math link meta base").split(" "));
const WRAPPER_OPEN = /^<(details|div|center)\b[^>]*>(?:\s*<summary\b[^>]*>[\s\S]*<\/summary>)?$/i;
const WRAPPER_CLOSE = /^<\/(details|div|center)\s*>$/i;
const HTML_BLOCK = /^ {0,3}<(?:!--|\/?([a-zA-Z][a-zA-Z0-9-]*)(?=[\s/>]|$))/;

/** Columns of leading white space, a tab to the next multiple of four. */
function indent(line) {
  let n = 0;
  for (const ch of line) { if (ch === " ") n++; else if (ch === "\t") n += 4 - (n % 4); else break; }
  return n;
}
/** The line with up to `n` columns of leading white space removed. */
function dedent(line, n) {
  let col = 0, i = 0;
  while (i < line.length && col < n && (line[i] === " " || line[i] === "\t")) { col += line[i] === "\t" ? 4 - (col % 4) : 1; i++; }
  return (col > n ? " ".repeat(col - n) : "") + line.slice(i);
}
const blank = line => !line.trim();

class Renderer {
  constructor(o) { this.o = o; this.refs = new Map(); this.slugs = new Map(); this.inLink = 0; this.open = []; }

  document(src) {
    const lines = [];
    let fence = null;                                     // reference definitions are collected outside code
    for (const line of src.replace(/\r\n?/g, "\n").split("\n")) {
      lines.push(line);
      if (fence) {
        const end = FENCE_END.exec(line);
        if (end && end[1][0] === fence[0] && end[1].length >= fence.length) fence = null;
        continue;
      }
      const open = FENCE.exec(line), ref = !open && REF.exec(line);
      if (open) fence = open[2];
      else if (ref) {
        lines.pop();
        const key = ref[1].trim().toLowerCase().replace(/\s+/g, " ");
        if (!this.refs.has(key)) this.refs.set(key, { url: ref[2], title: ref[3] || "" });
      }
    }
    return this.blocks(lines, false);
  }

  blocks(lines, tight) {
    let html = "", i = 0;
    const depth = this.open.length;
    while (i < lines.length) {
      if (blank(lines[i])) { i++; continue; }
      const [out, next] = this.block(lines, i, tight);
      html += out; i = next;
    }
    while (this.open.length > depth) html += `</${this.open.pop()}>`;
    return html;
  }

  block(lines, i, tight) {
    const line = lines[i];
    let m;
    if ((m = FENCE.exec(line))) return this.fence(lines, i, m);
    if ((m = HEADING.exec(line))) return [this.heading(+m[1].length, m[2] || ""), i + 1];
    if (RULE.test(line)) return ["<hr>", i + 1];
    if (QUOTE.test(line)) return this.quote(lines, i);
    if ((m = ITEM.exec(line))) return this.list(lines, i);
    if (MATH.test(line)) { const math = this.mathBlock(lines, i); if (math) return math; }
    if (this.tableStarts(lines, i)) return this.table(lines, i);
    if (HTML_BLOCK.test(line) && this.htmlTag(line)) return this.htmlBlock(lines, i);
    if (indent(line) >= 4) return this.indented(lines, i);
    return this.paragraph(lines, i, tight);
  }

  /** Whether a line ends the paragraph above it. */
  interrupts(line) {
    if (FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line) || MATH.test(line)) return true;
    if (HTML_BLOCK.test(line) && this.htmlTag(line)) return true;
    const m = ITEM.exec(line);
    return !!m && !!(m[4] || "").trim() && (!/\d/.test(m[2]) || parseInt(m[2], 10) === 1);
  }

  htmlTag(line) {
    const m = HTML_BLOCK.exec(line);
    return !!m && (!m[1] || HTML_TAGS.has(m[1].toLowerCase()));
  }

  fence(lines, i, m) {
    const pad = m[1].length, mark = m[2], body = [];
    let j = i + 1, closed = false;
    for (; j < lines.length; j++) {
      const end = FENCE_END.exec(lines[j]);
      if (end && end[1][0] === mark[0] && end[1].length >= mark.length) { closed = true; j++; break; }
      body.push(dedent(lines[j], pad));
    }
    return [this.code(body.join("\n"), m[3].toLowerCase(), closed), j];
  }

  code(text, lang, closed) {
    return this.o.code ? this.o.code(text, lang, closed)
      : `<pre><code data-lang="${esc(lang)}">${esc(text)}</code></pre>`;
  }

  indented(lines, i) {
    const body = [];
    let j = i;
    while (j < lines.length && (blank(lines[j]) || indent(lines[j]) >= 4)) body.push(dedent(lines[j++], 4));
    while (body.length && blank(body.at(-1))) body.pop();
    return [this.code(body.join("\n"), "", true), j];
  }

  heading(level, text) {
    let anchor = "";
    if (this.o.anchors) {
      const base = slug(text) || "section", seen = this.slugs.get(base) || 0;
      this.slugs.set(base, seen + 1);
      anchor = ` data-anchor="${esc(seen ? `${base}-${seen}` : base)}"`;
    }
    return `<h${level}${anchor}>${this.inline(text.trim())}</h${level}>`;
  }

  quote(lines, i) {
    const body = [];
    let j = i;
    while (j < lines.length) {
      const m = QUOTE.exec(lines[j]);
      if (m) body.push(m[1]);
      else if (!blank(lines[j]) && body.length && !blank(body.at(-1)) && !this.interrupts(lines[j])) body.push(lines[j]);
      else break;
      j++;
    }
    const alert = ALERT.exec(body[0] || "");
    if (alert) {
      const kind = alert[1].toLowerCase();
      return [`<div class="callout ${kind}"><div class="callout-title">${kind[0].toUpperCase() + kind.slice(1)}</div>` +
        `${this.blocks(body.slice(1), false)}</div>`, j];
    }
    return [`<blockquote>${this.blocks(body, false)}</blockquote>`, j];
  }

  list(lines, i) {
    const first = ITEM.exec(lines[i]), ordered = /\d/.test(first[2]), delim = first[2].slice(-1);
    const same = m => m && /\d/.test(m[2]) === ordered && (!ordered || m[2].slice(-1) === delim);
    const items = [];
    let j = i, loose = false;
    while (j < lines.length) {
      const m = ITEM.exec(lines[j]);
      if (!same(m) || (items.length && indent(lines[j]) > indent(lines[i]) + 3)) break;
      const at = indent(lines[j]), space = m[3] ? m[3].replace(/\t/g, "    ").length : 0;
      const offset = at + m[2].length + (!m[4] || space > 4 ? 1 : space);
      const body = [m[4] === undefined ? "" : space > 4 ? " ".repeat(space - 1) + m[4] : m[4]];
      j++;
      while (j < lines.length) {
        const line = lines[j];
        if (blank(line)) {                                // a blank line continues the item if more of it follows
          let k = j;
          while (k < lines.length && blank(lines[k])) k++;
          if (k < lines.length && indent(lines[k]) >= offset) { while (j < k) { body.push(""); j++; } continue; }
          break;
        }
        const depth = indent(line);
        if (depth >= offset) { body.push(dedent(line, offset)); j++; continue; }
        if (ITEM.test(line) && depth > at) { body.push(dedent(line, depth)); j++; continue; }  // nested, under-indented
        if (!ITEM.test(line) && !blank(body.at(-1)) && !this.interrupts(line)) { body.push(line.trim()); j++; continue; }
        break;
      }
      while (body.length > 1 && blank(body.at(-1))) body.pop();
      if (body.some(blank)) loose = true;
      items.push(body);
      if (j < lines.length && blank(lines[j])) {        // a blank line between two items makes the list loose
        let k = j;
        while (k < lines.length && blank(lines[k])) k++;
        if (k < lines.length && same(ITEM.exec(lines[k])) && indent(lines[k]) <= indent(lines[i]) + 3) { loose = true; j = k; continue; }
        break;
      }
    }
    const tag = ordered ? "ol" : "ul", start = ordered ? parseInt(first[2], 10) : 1;
    let html = `<${tag}${ordered && start !== 1 ? ` start="${start}"` : ""}>`;
    for (const body of items) {
      const task = /^\[([ xX])\][ \t]+/.exec(body[0]);
      if (task) body[0] = body[0].slice(task[0].length);
      const inner = this.blocks(body, !loose);
      html += task ? `<li class="task"><input type="checkbox" disabled${task[1] === " " ? "" : " checked"}> ${inner}</li>` : `<li>${inner}</li>`;
    }
    return [html + `</${tag}>`, j];
  }

  mathBlock(lines, i) {
    const open = lines[i].trim(), close = open.startsWith("$$") ? "$$" : "\\]", rest = open.slice(2);
    if (rest.trimEnd().endsWith(close) && rest.trim().length > close.length) return [this.math(rest.trimEnd().slice(0, -close.length), true, "div"), i + 1];
    const body = [rest];
    for (let j = i + 1; j < lines.length; j++) {
      const line = lines[j].trimEnd();
      if (line.endsWith(close)) { body.push(line.slice(0, -close.length)); return [this.math(body.join("\n"), true, "div"), j + 1]; }
      body.push(lines[j]);
    }
    return null;                                          // never closed: plain text (a reply still streaming)
  }

  math(tex, display, tag = "span") {
    return `<${tag} class="math${display ? " display" : ""}" data-tex="${esc(tex.trim())}">${esc(tex.trim())}</${tag}>`;
  }

  tableStarts(lines, i) {
    return lines[i].includes("|") && i + 1 < lines.length && DELIMITER.test(lines[i + 1]) && lines[i + 1].includes("-") &&
      cells(lines[i]).length === cells(lines[i + 1]).length;
  }

  table(lines, i) {
    const head = cells(lines[i]);
    const align = cells(lines[i + 1]).map(c => { c = c.trim(); return c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : c.startsWith(":") ? "left" : ""; });
    const cell = (tag, text, k) => `<${tag}${align[k] ? ` style="text-align:${align[k]}"` : ""}>${this.inline(text.trim())}</${tag}>`;
    let html = `<div class="table"><table><thead><tr>${head.map((c, k) => cell("th", c, k)).join("")}</tr></thead><tbody>`;
    let j = i + 2;
    for (; j < lines.length && !blank(lines[j]) && !this.interrupts(lines[j]); j++) {
      const row = cells(lines[j]);
      html += `<tr>${head.map((_, k) => cell("td", row[k] || "", k)).join("")}</tr>`;
    }
    return [html + "</tbody></table></div>", j];
  }

  /** HTML on lines of its own. A wrapper opened in one block and closed in a later one (<details>, <div
   *  align="center">) holds the markdown between; wrappers left open are closed where their container ends. */
  htmlBlock(lines, i) {
    const body = [];
    let j = i;
    while (j < lines.length && !blank(lines[j])) body.push(lines[j++]);
    const text = body.join("\n").trim(), html = sanitize(text, this.o);
    const close = WRAPPER_CLOSE.exec(text);
    if (close) {
      const tag = close[1].toLowerCase() === "center" ? "div" : close[1].toLowerCase();
      if (this.open.at(-1) !== tag) return ["", j];
      this.open.pop();
      return [`</${tag}>`, j];
    }
    const open = WRAPPER_OPEN.exec(text);
    if (open) {
      const tag = open[1].toLowerCase() === "center" ? "div" : open[1].toLowerCase(), end = `</${tag}>`;
      if (html.endsWith(end)) { this.open.push(tag); return [html.slice(0, -end.length), j]; }
    }
    return [html, j];
  }

  paragraph(lines, i, tight) {
    const body = [lines[i].replace(/^[ \t]+/, "")];
    let j = i + 1;
    for (; j < lines.length; j++) {
      const line = lines[j];
      if (blank(line)) break;
      const setext = SETEXT.exec(line);
      if (setext) return [this.heading(setext[1][0] === "=" ? 1 : 2, body.join(" ")), j + 1];
      if (this.interrupts(line) || this.tableStarts(lines, j)) break;
      body.push(line.replace(/^[ \t]+/, ""));
    }
    const text = this.inline(body.join("\n").replace(/[ \t]+$/, ""));
    return [tight ? text : `<p>${text}</p>`, j];
  }

  // ── inline ─────────────────────────────────────────────────────────────────────────────────────────────────
  inline(s) {
    const special = /[\\`$!\[<*_~\n]|https?:\/\/|www\./gi;
    let out = "", i = 0;
    while (i < s.length) {
      special.lastIndex = i;
      const hit = special.exec(s);
      if (!hit) { out += esc(s.slice(i)); break; }
      out += esc(s.slice(i, hit.index));
      i = hit.index;
      const done = this.token(s, i);
      if (done) { out += done.html; i = done.end; }
      else { out += esc(s[i]); i++; }
    }
    return out;
  }

  /** The construct at s[i], or null when the character there is only itself. */
  token(s, i) {
    const c = s[i], next = s[i + 1] || "";
    if (c === "\\") {
      if (next === "\n") return { html: "<br>", end: i + 2 };
      if (next === "(" || next === "[") {
        const close = s.indexOf(next === "(" ? "\\)" : "\\]", i + 2);
        if (close > i + 2) return { html: this.math(s.slice(i + 2, close), next === "["), end: close + 2 };
      }
      if (/[!-/:-@[-`{-~]/.test(next)) return { html: esc(next), end: i + 2 };
      return null;
    }
    if (c === "`") {
      const run = runOf(s, i, "`"), close = findRun(s, i + run, "`", run);
      if (close < 0) return { html: esc("`".repeat(run)), end: i + run };
      let code = s.slice(i + run, close).replace(/\n/g, " ");
      if (code.length > 2 && code[0] === " " && code.at(-1) === " " && code.trim()) code = code.slice(1, -1);
      return { html: `<code>${esc(code)}</code>`, end: close + run };
    }
    if (c === "$") return this.inlineMath(s, i);
    if (c === "!" && next === "[") {
      const l = this.linkAt(s, i + 1);
      return l && { html: this.image(l), end: l.end };
    }
    if (c === "[" && !this.inLink) {
      const l = this.linkAt(s, i);
      if (!l) return null;
      this.inLink++;                                     // a link holds no other link
      const text = this.inline(l.text);
      this.inLink--;
      return { html: this.anchor(this.o.link?.(l.url), text, l.title), end: l.end };
    }
    if (c === "<") return this.angle(s, i);
    if (c === "*" || c === "_" || c === "~") return this.emphasis(s, i);
    if (c === "\n") {
      const hard = this.o.breaks || / {2,}$/.test(s.slice(Math.max(0, i - 2), i));
      return { html: hard ? "<br>" : "\n", end: i + 1 };
    }
    return this.bareUrl(s, i);
  }

  inlineMath(s, i) {
    if (s[i + 1] === "$") {                                // $$…$$ within a line
      const close = s.indexOf("$$", i + 2);
      return close > i + 2 ? { html: this.math(s.slice(i + 2, close), true), end: close + 2 } : null;
    }
    if (!s[i + 1] || /\s/.test(s[i + 1])) return null;
    for (let j = s.indexOf("$", i + 1); j > 0; j = s.indexOf("$", j + 1)) {    // $…$, unless it reads as money
      if (s[j - 1] === "\\") continue;
      if (/\s/.test(s[j - 1]) || /\d/.test(s[j + 1] || "")) continue;
      const tex = s.slice(i + 1, j);
      if (tex.includes("\n\n")) return null;
      return { html: this.math(tex, false), end: j + 1 };
    }
    return null;
  }

  linkAt(s, i) {                                         // s[i] is "["
    let depth = 0, j = i;
    for (; j < s.length; j++) {
      if (s[j] === "\\") { j++; continue; }
      if (s[j] === "`") { const run = runOf(s, j, "`"), close = findRun(s, j + run, "`", run); j = close < 0 ? j + run - 1 : close + run - 1; continue; }
      if (s[j] === "[") depth++;
      else if (s[j] === "]" && --depth === 0) break;
    }
    if (j >= s.length) return null;
    const text = s.slice(i + 1, j);
    if (s[j + 1] === "(") {
      let k = j + 2, url;
      while (s[k] === " ") k++;
      if (s[k] === "<") {
        const close = s.indexOf(">", k);
        if (close < 0) return null;
        url = s.slice(k + 1, close); k = close + 1;
      } else {
        const from = k;
        for (let d = 0; k < s.length; k++) {
          if (s[k] === "\\") { k++; continue; }
          if (s[k] === "(") d++;
          else if (s[k] === ")") { if (!d) break; d--; }
          else if (/\s/.test(s[k])) break;
        }
        url = s.slice(from, k);
      }
      while (s[k] === " " || s[k] === "\n") k++;
      let title = "";
      if (s[k] === '"' || s[k] === "'" || (s[k] === "(" && url)) {
        const close = s.indexOf(s[k] === "(" ? ")" : s[k], k + 1);
        if (close < 0) return null;
        title = s.slice(k + 1, close); k = close + 1;
        while (s[k] === " ") k++;
      }
      return s[k] === ")" ? { text, url: url.replace(/\\(.)/g, "$1"), title, end: k + 1 } : null;
    }
    const ref = /^\[([^\]]*)\]/.exec(s.slice(j + 1));
    const key = (ref && ref[1] ? ref[1] : text).trim().toLowerCase().replace(/\s+/g, " ");
    const found = this.refs.get(key);
    return found ? { text, url: found.url, title: found.title, end: j + 1 + (ref ? ref[0].length : 0) } : null;
  }

  anchor(target, html, title = "") {
    if (!target) return html;
    const t = title ? ` title="${esc(title)}"` : "";
    if (target.open) return `<a href="#" data-open="${esc(target.open)}"${t}>${html}</a>`;
    if (target.anchor !== undefined) return `<a href="#" data-anchor-link="${esc(target.anchor)}"${t}>${html}</a>`;
    return `<a href="${esc(target.href)}" target="_blank" rel="noopener noreferrer"${t}>${html}</a>`;
  }

  image(l) {
    const src = this.o.image?.(l.url), alt = l.text.replace(/[*_`[\]]/g, "");
    if (src) return `<img src="${esc(src)}" alt="${esc(alt)}"${l.title ? ` title="${esc(l.title)}"` : ""} loading="lazy">`;
    const shown = `<span class="img-link">${esc(alt || l.url)}</span>`;
    return this.inLink ? shown : this.anchor(this.o.link?.(l.url), shown, l.title);
  }

  angle(s, i) {
    const auto = /^<((?:https?|mailto):[^\s<>]+)>/i.exec(s.slice(i, i + 2048));
    if (auto && !this.inLink) return { html: this.anchor(this.o.link?.(auto[1]), esc(auto[1])), end: i + auto[0].length };
    if (s.startsWith("<!--", i)) { const close = s.indexOf("-->", i + 4); return { html: "", end: close < 0 ? s.length : close + 3 }; }
    const tag = /^<(\/?)(br|wbr|kbd|sub|sup|b|i|u|s|mark|small|ins|del|em|strong|code|q|abbr|span|cite|var|samp)\s*\/?>/i.exec(s.slice(i, i + 16));
    if (!tag) return null;
    const name = tag[2].toLowerCase();
    return { html: name === "br" || name === "wbr" ? `<${name}>` : `<${tag[1]}${name}>`, end: i + tag[0].length };
  }

  emphasis(s, i) {
    const c = s[i], run = runOf(s, i, c), after = s[i + run] || " ", before = s[i - 1] || " ";
    if (/\s/.test(after)) return null;                                  // not where emphasis opens
    if (c === "_" && /[\p{L}\p{N}]/u.test(before)) return null;           // snake_case is not emphasis
    if (c === "~" && run !== 2) return null;                              // ~ alone means about, not struck
    const n = c === "~" ? 2 : Math.min(run, 3), close = findCloser(s, i + n, c, n);
    if (close < 0) return null;
    const inner = s.slice(i + n, close);
    if (inner.includes("\n\n")) return null;
    const [open, shut] = c === "~" ? ["<del>", "</del>"] : n === 1 ? ["<em>", "</em>"] : n === 2 ? ["<strong>", "</strong>"] : ["<strong><em>", "</em></strong>"];
    return { html: (run > n ? esc(c.repeat(run - n)) : "") + open + this.inline(inner) + shut, end: close + n };
  }

  bareUrl(s, i) {
    if (this.inLink || (i > 0 && !/[\s(*_~"'>]/.test(s[i - 1]))) return null;
    const m = /^(?:https?:\/\/|www\.)[^\s<]+/i.exec(s.slice(i, i + 4096));
    if (!m) return null;
    let url = m[0];
    for (;;) {                                                          // trailing punctuation is the sentence's
      const last = url.at(-1);
      if ("?!.,:;*_~'\"]".includes(last)) url = url.slice(0, -1);
      else if (last === ")" && (url.match(/\)/g) || []).length > (url.match(/\(/g) || []).length) url = url.slice(0, -1);
      else break;
    }
    if (url.length < 5) return null;
    const href = /^www\./i.test(url) ? "https://" + url : url;
    return { html: this.anchor(this.o.link?.(href), esc(url)), end: i + url.length };
  }
}

function runOf(s, i, c) { let j = i; while (s[j] === c) j++; return j - i; }
/** Where a run of exactly `n` of `c` starts, from `from`; -1 if none. */
function findRun(s, from, c, n) {
  for (let j = s.indexOf(c, from); j >= 0; j = s.indexOf(c, j)) {
    const run = runOf(s, j, c);
    if (run === n) return j;
    j += run;
  }
  return -1;
}
/** Where the delimiter run that closes emphasis opened with `n` of `c` ends its first `n` characters; -1 if none. */
function findCloser(s, from, c, n) {
  for (let j = from; j < s.length;) {
    const ch = s[j];
    if (ch === "\\") { j += 2; continue; }
    if (ch === "`") { const run = runOf(s, j, "`"), close = findRun(s, j + run, "`", run); j = close < 0 ? j + run : close + run; continue; }
    if (ch !== c) { j++; continue; }
    const run = runOf(s, j, c);
    const closes = j > from && !/\s/.test(s[j - 1]) && (c !== "_" || !/[\p{L}\p{N}]/u.test(s[j + run] || ""));
    if (closes && (run === n || (run === 3 && n < 3))) return j + run - n;
    j += run;
  }
  return -1;
}
/** A table row's cells: split on pipes outside code spans and escapes. */
function cells(line) {
  let row = line.trim();
  if (row.startsWith("|")) row = row.slice(1);
  if (row.endsWith("|") && !row.endsWith("\\|")) row = row.slice(0, -1);
  const out = [];
  let cur = "", code = false;
  for (let i = 0; i < row.length; i++) {
    const ch = row[i];
    if (ch === "\\" && row[i + 1] === "|") { cur += "|"; i++; }
    else if (ch === "`") { code = !code; cur += ch; }
    else if (ch === "|" && !code) { out.push(cur); cur = ""; }
    else cur += ch;
  }
  out.push(cur);
  return out;
}

// ── HTML in markdown ───────────────────────────────────────────────────────────────────────────────────────────
const KEEP = new Set(("a abbr b blockquote br caption cite code col colgroup dd del details dfn div dl dt em figcaption figure h1 " +
  "h2 h3 h4 h5 h6 hr i img ins kbd li mark ol p picture pre q s samp small span strong sub summary sup table tbody td tfoot " +
  "th thead tr tt u ul var center").split(" "));
const DROP = new Set(("script style iframe frame frameset object embed applet template noscript form input button select " +
  "textarea option link meta base title head svg math audio video source track canvas dialog portal").split(" "));
const ATTRS = new Set(["align", "alt", "title", "width", "height", "colspan", "rowspan", "open", "start", "span"]);
const VOID = new Set(["br", "hr", "img", "col"]);

/** HTML from a document, rebuilt from a parsed (inert) copy: kept tags and attributes only, links and images through
 *  the resolvers, and everything else reduced to its text. */
export function sanitize(html, o = {}) {
  const doc = new DOMParser().parseFromString(`<!doctype html><body>${html}`, "text/html");
  return children(doc.body, o);
}
function children(node, o) {
  let out = "";
  for (const child of node.childNodes) out += clean(child, o);
  return out;
}
function clean(node, o) {
  if (node.nodeType === Node.TEXT_NODE) return esc(node.nodeValue);
  if (node.nodeType !== Node.ELEMENT_NODE) return "";
  const tag = node.localName;
  if (DROP.has(tag)) return "";
  const inner = VOID.has(tag) ? "" : children(node, o);
  if (!KEEP.has(tag)) return inner;
  let attrs = "";
  for (const a of node.attributes) if (ATTRS.has(a.name)) attrs += ` ${a.name}="${esc(a.value)}"`;
  if (tag === "a") return Renderer.prototype.anchor(o.link?.(node.getAttribute("href") || ""), inner, node.getAttribute("title") || "");
  if (tag === "img") {
    const src = o.image?.(node.getAttribute("src") || "");
    return src ? `<img src="${esc(src)}"${attrs} loading="lazy">` : esc(node.getAttribute("alt") || "");
  }
  if (tag === "center") return `<div align="center">${inner}</div>`;
  return VOID.has(tag) ? `<${tag}${attrs}>` : `<${tag}${attrs}>${inner}</${tag}>`;
}
