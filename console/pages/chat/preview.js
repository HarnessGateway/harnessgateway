// The preview of one of a session's files, whatever it is and whatever its size. A page runs live, sandboxed;
// images, PDF, audio and video are the browser's own to show, from the session's link (it serves byte ranges, so
// media seeks); markdown, notebooks, tables, JSON, code and text are drawn here, reading at most TEXT_MAX of a file
// and saying so when that is not all of it; Word documents, spreadsheets and diagrams are drawn by the sandboxed
// viewer; anything else is offered as a download.
import { markdown, sanitize } from "../../components/markdown.js";
import { codeBlock, codeView, enhance, viewer } from "../../components/rich.js";
import { add, clear, h } from "../../lib/dom.js";
import { bytes } from "../../lib/format.js";
import { baseName, dirName, extension, kindOf, languageOf } from "../../lib/kinds.js";
import { workLinks } from "./workspace.js";

const TEXT_MAX = 8 * 1048576;                    // the most of a text file that is read
const BINARY_MAX = 64 * 1048576;                 // the largest document or spreadsheet the viewer is given
const ROWS_PAGE = 1000;                          // a table's rows, drawn this many at a time
const SOURCE = new Set(["html", "svg", "markdown", "mermaid", "notebook"]);
const PAGE = "allow-scripts allow-forms allow-popups allow-modals allow-downloads";      // what a page an agent built may do

/** Whether a file has both a drawing and a source to switch between. */
export const hasSource = path => SOURCE.has(kindOf(path));

/** Show `file` ({path, bytes, url, session}) in `body`, as its preview or as its code. `still()` says whether the
 *  panel still shows this file: a read that ends after the reader moved on is dropped. `o` is {open(path), show(view)}. */
export async function preview(body, file, mode, still, o) {
  const kind = kindOf(file.path), note = text => add(clear(body), h("div", { class: "note" }, text));
  const download = () => h("a", { href: `${file.url}?download` }, "Download");
  const card = why => add(clear(body), h("div", { class: "filecard" }, h("div", { class: "big" }, (extension(file.path) || "file").slice(0, 5).toUpperCase()),
    h("b", {}, baseName(file.path)), h("div", { class: "muted small" }, `${bytes(file.bytes)} · ${why}`), h("div", {}, download())));
  const doc = { ...workLinks(file.session, dirName(file.path)), open: o.open, code: (code, language, closed) => codeBlock(code, language, closed, o.show) };

  /** The start of the file as text: {text, cut}, or nothing when it is not text or the reader has moved on. */
  async function read() {
    note("Loading…");
    const response = await fetch(file.url);
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    const reader = response.body.getReader(), parts = [];
    for (let got = 0; got < TEXT_MAX;) {
      const { value, done } = await reader.read();
      if (done) break;
      parts.push(value);
      got += value.length;
    }
    reader.cancel().catch(() => {});
    const content = new Uint8Array(await new Blob(parts).slice(0, TEXT_MAX).arrayBuffer());
    if (!still()) return null;
    if (content.subarray(0, 8192).includes(0)) { card("This file is not text, and there is no preview for its kind."); return null; }
    return { text: new TextDecoder().decode(content), cut: file.bytes > TEXT_MAX };
  }
  const drawn = (read, ...nodes) => {
    add(clear(body), read.cut && h("div", { class: "cut" }, `Showing the first ${bytes(TEXT_MAX)} of ${bytes(file.bytes)}. `, download(), " the whole file."), nodes);
    enhance(body);
  };
  const source = async language => { const got = await read(); if (got) drawn(got, codeView(got.text, language)); };

  try {
    if (mode === "code" && SOURCE.has(kind)) return await source(languageOf(file.path) || "xml");
    if (kind === "html") return add(clear(body), h("iframe", { sandbox: PAGE, src: file.url, title: file.path }));
    if (kind === "pdf") return add(clear(body), h("iframe", { src: file.url, title: file.path }));
    if (kind === "image" || kind === "svg") {
      const image = h("img", { class: "fit", src: file.url, alt: file.path, title: "Click to zoom", onclick: () => image.classList.toggle("fit"),
                               onerror: () => card("This image could not be shown.") });
      return add(clear(body), h("div", { class: "imagebox" }, image));
    }
    if (kind === "audio" || kind === "video") return add(clear(body), h("div", { class: "media" }, h(kind, { controls: true, preload: "metadata", playsinline: true, src: file.url })));
    if (kind === "binary") return card("No preview for this kind of file.");
    if (kind === "docx" || kind === "sheet") {
      if (file.bytes > BINARY_MAX) return card(`Too large to preview here (over ${bytes(BINARY_MAX)}).`);
      note("Loading…");
      const data = await (await fetch(file.url)).arrayBuffer();
      return still() && add(clear(body), viewer({ kind, data }, { onError: message => still() && card(`The preview failed: ${message}`) }));
    }
    const got = await read();
    if (!got) return;
    if (kind === "markdown") return drawn(got, h("article", { class: "doc text" }, markdown(got.text, doc)));
    if (kind === "mermaid") return add(clear(body), viewer({ kind: "mermaid", data: got.text }, {
      onError: message => still() && add(clear(body), h("div", { class: "cut bad" }, `The diagram did not draw: ${message}`), codeView(got.text, "")) }));
    if (kind === "json") {
      let text = got.text;
      if (!got.cut) try { text = JSON.stringify(JSON.parse(text), null, 2); } catch { /* shown as it is */ }
      return drawn(got, codeView(text, "json"));
    }
    if (kind === "table") return table(got);
    if (kind === "notebook") return got.cut ? card(`Too large to preview here (over ${bytes(TEXT_MAX)}).`) : drawn(got, notebook(JSON.parse(got.text)));
    drawn(got, codeView(got.text, languageOf(file.path)));
  } catch (error) {
    if (still()) add(clear(body), h("div", { class: "note bad" }, error.message || String(error)));
  }

  // ── tables ───────────────────────────────────────────────────────────────────────────────────────────────────
  function table(got) {
    const rows = delimited(got.text, file.path.toLowerCase().endsWith(".tsv") ? "\t" : ",");
    if (got.cut) rows.pop();                       // the last row read may be cut short
    if (!rows.length) return note("Empty.");
    const [head, ...data] = rows, width = Math.max(...rows.slice(0, 1000).map(row => row.length));
    const cells = (tag, row) => Array.from({ length: width }, (_, column) => h(tag, {}, row[column] ?? ""));
    const page = from => data.slice(from, from + ROWS_PAGE).map((row, index) => h("tr", {}, h("td", { class: "ln" }, from + index + 1), cells("td", row)));
    const tbody = h("tbody", {}, page(0));
    let shown = ROWS_PAGE;
    const more = data.length > ROWS_PAGE && h("button", { onclick: () => { add(tbody, page(shown)); shown += ROWS_PAGE; if (shown >= data.length) more.remove(); } }, "Show more rows");
    drawn(got, h("div", { class: "muted small tableinfo" }, `${data.length.toLocaleString()} row${data.length === 1 ? "" : "s"} · ${width} column${width === 1 ? "" : "s"}`),
      h("table", { class: "grid" }, h("thead", {}, h("tr", {}, h("th", { class: "ln" }), cells("th", head))), tbody), more);
  }

  // ── Jupyter notebooks ────────────────────────────────────────────────────────────────────────────────────────
  function notebook(book) {
    const language = book.metadata?.kernelspec?.language || book.metadata?.language_info?.name || "python";
    const joined = value => Array.isArray(value) ? value.join("") : String(value ?? ""), plain = text => text.replace(/\x1b\[[0-9;]*[A-Za-z]/g, "");
    const encoded = value => /^[A-Za-z0-9+/=\s]+$/.test(joined(value)) && joined(value).replace(/\s/g, "");
    const output = out => {
      if (out.output_type === "stream") return h("pre", { class: out.name === "stderr" ? "nb-out bad" : "nb-out" }, plain(joined(out.text)));
      if (out.output_type === "error") return h("pre", { class: "nb-out bad" }, plain((out.traceback || [`${out.ename}: ${out.evalue}`]).join("\n")));
      const data = out.data || {}, image = ["image/png", "image/jpeg", "image/gif", "image/webp"].find(type => data[type] && encoded(data[type]));
      if (image) return h("div", { class: "nb-out" }, h("img", { src: `data:${image};base64,${encoded(data[image])}`, alt: "" }));
      if (data["image/svg+xml"]) return h("div", { class: "nb-out" }, h("img", { src: `data:image/svg+xml;charset=utf-8,${encodeURIComponent(joined(data["image/svg+xml"]))}`, alt: "" }));
      if (data["text/html"]) return h("div", { class: "nb-out" }, sanitize(joined(data["text/html"]), doc));
      if (data["text/markdown"]) return h("div", { class: "nb-out" }, markdown(joined(data["text/markdown"]), doc));
      if (data["text/latex"]) { const tex = joined(data["text/latex"]).replace(/^\s*\$\$?|\$\$?\s*$/g, ""); return h("div", { class: "nb-out" }, h("div", { class: "math display", "data-tex": tex }, tex)); }
      if (data["application/json"]) return h("pre", { class: "nb-out" }, JSON.stringify(data["application/json"], null, 2));
      return data["text/plain"] ? h("pre", { class: "nb-out" }, plain(joined(data["text/plain"]))) : null;
    };
    return h("article", { class: "doc text notebook" }, (book.cells || []).map(cell => {
      const text = joined(cell.source);
      if (cell.cell_type === "markdown") return h("div", {}, markdown(text, doc));
      if (cell.cell_type !== "code") return h("pre", { class: "nb-out" }, text);
      return h("div", { class: "nb-cell" }, h("div", { class: "nb-in" }, `In [${cell.execution_count ?? " "}]:`), codeBlock(text, language, true, o.show), (cell.outputs || []).map(output));
    }));
  }
}

/** Rows of CSV or TSV: a quoted field may hold the separator, quotes and line breaks. */
export function delimited(text, separator) {
  const rows = [];
  let row = [], field = "", quoted = false;
  for (let at = 0; at < text.length; at++) {
    const char = text[at];
    if (quoted) {
      if (char !== '"') field += char; else if (text[at + 1] === '"') { field += '"'; at++; } else quoted = false;
    } else if (char === '"' && field === "") quoted = true;
    else if (char === separator) { row.push(field); field = ""; }
    else if (char === "\n" || char === "\r") {
      if (char === "\r" && text[at + 1] === "\n") at++;
      rows.push([...row, field]);
      row = []; field = "";
    } else field += char;
  }
  if (field !== "" || row.length) rows.push([...row, field]);
  return rows;
}
