// The side panel's preview of one workspace file, whatever it is and whatever its size. Pages run live and sandboxed;
// images, PDF, audio and video stream from the server (it serves byte ranges, so media seeks); markdown, notebooks,
// tables, JSON, code and text render in place, reading at most TEXT_MAX of a file and saying so when that is not all
// of it; Word documents, spreadsheets and diagrams render in the sandboxed viewer; anything else is offered as a
// download.

import { esc, size, kindOf, langOf, dirName, ic, I } from "./util.js";
import { markdown, workspaceLinks, sanitize } from "./markdown.js";
import { enhance, codeBlock, viewer } from "./rich.js";

const TEXT_MAX = 8 * 1048576;                        // the most of a text file the panel reads
const BINARY_MAX = 64 * 1048576;                     // the largest document or spreadsheet the viewer is given
const ROWS_PAGE = 1000;                              // table rows drawn at a time
const SOURCE = new Set(["html", "svg", "markdown", "mermaid", "notebook"]);

/** Whether a kind of file has both a rendering and a source to switch between. */
export const hasSource = path => SOURCE.has(kindOf(path));

/**
 * Show `file` ({path, bytes, url, sid}) in `body`. mode: "preview" or "code". ctx.current() says whether the panel
 * still shows this file (a read that finishes after the reader moved on is dropped).
 */
export async function preview(body, file, mode, ctx) {
  const kind = kindOf(file.path);
  try {
    if (mode === "code" && SOURCE.has(kind)) return await source(body, file, ctx, langOf(file.path) || "xml");
    switch (kind) {
      case "html": return frame(body, file.url, "allow-scripts allow-forms allow-popups allow-modals allow-downloads");
      case "pdf": body.innerHTML = `<iframe src="${esc(file.url)}" title="${esc(file.path)}"></iframe>`; return;
      case "image": case "svg": return image(body, file);
      case "audio": body.innerHTML = `<div class="media"><audio controls preload="metadata" src="${esc(file.url)}"></audio></div>`; return;
      case "video": body.innerHTML = `<div class="media"><video controls preload="metadata" playsinline src="${esc(file.url)}"></video></div>`; return;
      case "markdown": return await doc(body, file, ctx);
      case "table": return await table(body, file, ctx);
      case "json": return await json(body, file, ctx);
      case "notebook": return await notebook(body, file, ctx);
      case "mermaid": return await diagram(body, file, ctx);
      case "docx": case "sheet": return await office(body, file, ctx, kind);
      case "binary": return card(body, file, "No preview for this kind of file.");
      default: return await source(body, file, ctx, langOf(file.path));
    }
  } catch (e) {
    if (ctx.current()) body.innerHTML = `<div class="note error">${esc(e.message || e)}</div>`;
  }
}

function loading(body) { body.innerHTML = `<div class="note">Loading…</div>`; }

/** The start of a file as text: at most TEXT_MAX of it, or {binary} when it is not text. */
async function readText(file) {
  const r = await fetch(file.url);
  if (!r.ok) throw new Error((await r.text()).slice(0, 300) || r.statusText);
  const reader = r.body.getReader(), parts = [];
  let got = 0;
  while (got < TEXT_MAX) {
    const { value, done } = await reader.read();
    if (done) break;
    parts.push(value); got += value.length;
  }
  if (got >= TEXT_MAX) reader.cancel().catch(() => {});
  const bytes = new Uint8Array(Math.min(got, TEXT_MAX));
  let at = 0;
  for (const p of parts) { if (at >= bytes.length) break; const take = p.subarray(0, bytes.length - at); bytes.set(take, at); at += take.length; }
  if (bytes.subarray(0, 8192).includes(0)) return { binary: true };
  return { text: new TextDecoder().decode(bytes), truncated: file.bytes > TEXT_MAX };
}

async function readOr(body, file, ctx) {
  loading(body);
  const read = await readText(file);
  if (!ctx.current()) return null;
  if (read.binary) { card(body, file, "This file is not text, and there is no preview for its kind."); return null; }
  return read;
}

const truncatedNote = file => `<div class="trunc">Showing the first ${size(TEXT_MAX)} of ${size(file.bytes)}. ` +
  `<a href="${esc(file.url)}?download=1">Download</a> the whole file.</div>`;

// ── renderings ─────────────────────────────────────────────────────────────────────────────────────────────────
function frame(body, url, sandbox) {
  body.innerHTML = `<iframe sandbox="${sandbox}" src="${esc(url)}"></iframe>`;
}

function image(body, file) {
  body.innerHTML = `<div class="imagebox"><img class="fit" src="${esc(file.url)}" alt="${esc(file.path)}" title="Click to zoom"></div>`;
  const img = body.querySelector("img");
  img.addEventListener("click", () => img.classList.toggle("fit"));
  img.addEventListener("error", () => card(body, file, "This image could not be shown."));
}

function card(body, file, why) {
  body.innerHTML = `<div class="filecard"><div class="big">${esc((file.path.split(".").pop() || "file").slice(0, 5).toUpperCase())}</div>` +
    `<div class="nm">${esc(file.path.split("/").pop())}</div><div class="sub">${size(file.bytes)} · ${esc(why)}</div>` +
    `<a class="btn" href="${esc(file.url)}?download=1">${ic(I.down, 16)}<span>Download</span></a></div>`;
}

/** Source code or text, with line numbers; highlighted when it is short enough. */
export function codeView(text, lang) {
  const lines = text.split("\n");
  if (lines.length > 1 && lines.at(-1) === "") lines.pop();
  const gutter = Array.from({ length: lines.length }, (_, i) => i + 1).join("\n");
  return `<div class="codeview"><pre class="gutter" aria-hidden="true">${gutter}</pre>` +
    `<pre class="src"><code data-lang="${esc(lang || "")}">${esc(lines.join("\n"))}</code></pre></div>`;
}

async function source(body, file, ctx, lang) {
  const read = await readOr(body, file, ctx);
  if (!read) return;
  body.innerHTML = (read.truncated ? truncatedNote(file) : "") + codeView(read.text, lang);
  enhance(body);
}

/** Options for markdown that lives in the workspace: links and images resolve against its folder. */
function docOptions(file, extra = {}) {
  return { ...workspaceLinks(file.sid, dirName(file.path)), anchors: true, code: codeBlock, ...extra };
}

async function doc(body, file, ctx) {
  const read = await readOr(body, file, ctx);
  if (!read) return;
  body.innerHTML = (read.truncated ? truncatedNote(file) : "") + `<article class="doc">${markdown(read.text, docOptions(file))}</article>`;
  enhance(body);
}

async function json(body, file, ctx) {
  const read = await readOr(body, file, ctx);
  if (!read) return;
  let text = read.text;
  if (!read.truncated) { try { text = JSON.stringify(JSON.parse(text), null, 2); } catch { /* shown as it is */ } }
  body.innerHTML = (read.truncated ? truncatedNote(file) : "") + codeView(text, "json");
  enhance(body);
}

async function diagram(body, file, ctx) {
  const read = await readOr(body, file, ctx);
  if (!read) return;
  body.innerHTML = "";
  body.appendChild(viewer({ kind: "mermaid", data: read.text }, {
    onError: message => { if (ctx.current()) body.innerHTML = `<div class="trunc error">The diagram did not render: ${esc(message)}</div>` + codeView(read.text, ""); },
  }));
}

async function office(body, file, ctx, kind) {
  if (file.bytes > BINARY_MAX) return card(body, file, `Too large to preview here (over ${size(BINARY_MAX)}).`);
  loading(body);
  const r = await fetch(file.url);
  if (!r.ok) throw new Error((await r.text()).slice(0, 300) || r.statusText);
  const data = await r.arrayBuffer();
  if (!ctx.current()) return;
  body.innerHTML = "";
  body.appendChild(viewer({ kind, data }, { onError: message => { if (ctx.current()) card(body, file, `The preview failed: ${message}`); } }));
}

// ── tables ─────────────────────────────────────────────────────────────────────────────────────────────────────
/** Rows of CSV or TSV (RFC 4180: quoted fields may hold the separator, quotes and line breaks). */
export function parseDelimited(text, sep) {
  const rows = [];
  let row = [], cur = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"') { if (text[i + 1] === '"') { cur += '"'; i++; } else quoted = false; }
      else cur += ch;
    } else if (ch === '"' && cur === "") quoted = true;
    else if (ch === sep) { row.push(cur); cur = ""; }
    else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(cur); rows.push(row); row = []; cur = "";
    } else cur += ch;
  }
  if (cur !== "" || row.length) { row.push(cur); rows.push(row); }
  return rows;
}

async function table(body, file, ctx) {
  const read = await readOr(body, file, ctx);
  if (!read) return;
  const rows = parseDelimited(read.text, file.path.toLowerCase().endsWith(".tsv") ? "\t" : ",");
  if (read.truncated) rows.pop();                    // the last row read may be cut short
  if (!rows.length) { body.innerHTML = `<div class="note">Empty.</div>`; return; }
  const [head, ...data] = rows, width = Math.max(...rows.slice(0, 1000).map(r => r.length));
  const cells = (tag, r) => Array.from({ length: width }, (_, k) => `<${tag}>${esc(r[k] ?? "")}</${tag}>`).join("");
  const page = from => data.slice(from, from + ROWS_PAGE).map((r, k) => `<tr><td class="ln">${from + k + 1}</td>${cells("td", r)}</tr>`).join("");
  body.innerHTML = (read.truncated ? truncatedNote(file) : "") +
    `<div class="tableinfo">${data.length.toLocaleString()} row${data.length === 1 ? "" : "s"} · ${width} column${width === 1 ? "" : "s"}</div>` +
    `<table class="grid"><thead><tr><th class="ln"></th>${cells("th", head)}</tr></thead><tbody>${page(0)}</tbody></table>` +
    (data.length > ROWS_PAGE ? `<button class="more">Show more rows</button>` : "");
  let shown = ROWS_PAGE;
  body.querySelector(".more")?.addEventListener("click", e => {
    body.querySelector("tbody").insertAdjacentHTML("beforeend", page(shown));
    shown += ROWS_PAGE;
    if (shown >= data.length) e.currentTarget.remove();
  });
}

// ── Jupyter notebooks ──────────────────────────────────────────────────────────────────────────────────────────
const joined = v => Array.isArray(v) ? v.join("") : String(v ?? "");
const ansi = s => s.replace(/\x1b\[[0-9;]*[A-Za-z]/g, "");
const BASE64 = /^[A-Za-z0-9+/=\s]+$/;

async function notebook(body, file, ctx) {
  const read = await readOr(body, file, ctx);
  if (!read) return;
  if (read.truncated) return card(body, file, `Too large to preview here (over ${size(TEXT_MAX)}).`);
  const nb = JSON.parse(read.text);
  const lang = nb.metadata?.kernelspec?.language || nb.metadata?.language_info?.name || "python";
  const cells = nb.cells || nb.worksheets?.[0]?.cells || [];
  body.innerHTML = `<article class="doc notebook">${cells.map(cell => notebookCell(cell, lang, file)).join("")}</article>`;
  enhance(body);
}

function notebookCell(cell, lang, file) {
  const src = joined(cell.source ?? cell.input);
  if (cell.cell_type === "markdown") {
    const attached = cell.attachments || {};
    const image = url => {                           // ![](attachment:name.png) is held in the cell itself
      const name = url.startsWith("attachment:") ? url.slice(11) : null;
      const data = name && attached[name];
      const mime = data && Object.keys(data).find(m => m.startsWith("image/"));
      return mime && BASE64.test(joined(data[mime])) ? `data:${mime};base64,${joined(data[mime]).replace(/\s/g, "")}` : null;
    };
    const links = docOptions(file);
    return `<div class="nb-md">${markdown(src, { ...links, image: url => image(url) ?? links.image(url) })}</div>`;
  }
  if (cell.cell_type !== "code") return `<pre class="nb-out">${esc(src)}</pre>`;
  const n = cell.execution_count ?? cell.prompt_number;
  return `<div class="nb-cell"><div class="nb-in">In [${n ?? " "}]:</div>${codeBlock(src, lang, true)}` +
    (cell.outputs || []).map(o => notebookOutput(o, file)).join("") + `</div>`;
}

function notebookOutput(o, file) {
  if (o.output_type === "stream") return `<pre class="nb-out${o.name === "stderr" ? " err" : ""}">${esc(ansi(joined(o.text)))}</pre>`;
  if (o.output_type === "error") return `<pre class="nb-out err">${esc(ansi((o.traceback || [`${o.ename}: ${o.evalue}`]).join("\n")))}</pre>`;
  const data = o.data || {};
  for (const mime of ["image/png", "image/jpeg", "image/gif", "image/webp"]) {
    if (data[mime] && BASE64.test(joined(data[mime]))) return `<div class="nb-out"><img src="data:${mime};base64,${joined(data[mime]).replace(/\s/g, "")}" alt=""></div>`;
  }
  if (data["image/svg+xml"]) return `<div class="nb-out"><img src="data:image/svg+xml;charset=utf-8,${encodeURIComponent(joined(data["image/svg+xml"]))}" alt=""></div>`;
  if (data["text/html"]) return `<div class="nb-out nb-html">${sanitize(joined(data["text/html"]), docOptions(file))}</div>`;
  if (data["text/markdown"]) return `<div class="nb-out">${markdown(joined(data["text/markdown"]), docOptions(file))}</div>`;
  if (data["text/latex"]) {
    const tex = joined(data["text/latex"]).replace(/^\s*\$\$?|\$\$?\s*$/g, "");
    return `<div class="nb-out"><div class="math display" data-tex="${esc(tex)}">${esc(tex)}</div></div>`;
  }
  if (data["application/json"]) return `<pre class="nb-out">${esc(JSON.stringify(data["application/json"], null, 2))}</pre>`;
  if (data["text/plain"]) return `<pre class="nb-out">${esc(ansi(joined(data["text/plain"])))}</pre>`;
  return "";
}
