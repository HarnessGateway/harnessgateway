// What makes a reply or a document rich: highlighted code, typeset mathematics, drawn diagrams. The two libraries
// this page runs load only when something first needs them, each pinned to a version and checked against an
// integrity hash; without them (offline, a network that blocks them) everything still reads: code as plain text,
// mathematics as its TeX, a diagram as its source.
//
// Diagrams, Word documents, spreadsheets and pages from a reply are drawn by the viewer: a frame sandboxed into an
// origin of its own, so what parses or runs them never runs in this page.
import { add, h, icon } from "../lib/dom.js";
import { copyText } from "../lib/files.js";

const CDN = "https://cdn.jsdelivr.net/npm";
const HIGHLIGHT = { global: "hljs", src: `${CDN}/@highlightjs/cdn-assets@11.12.0/highlight.min.js`,
                    integrity: "sha384-wjfDDhOPPdjtva8vWBhWeVprSpmxisEu5aYT3q1JyACqXpdKpo3PWZTMVq24MBix" };
const KATEX = { global: "katex", src: `${CDN}/katex@0.19.1/dist/katex.min.js`,
                integrity: "sha384-a1WQdjRj162i159OYq041X6I4K8NenkrI09LlPmiHg0YOb7CR+wJOraXwK9abpnE",
                css: `${CDN}/katex@0.19.1/dist/katex.min.css`, cssIntegrity: "sha384-WwGtaC6ywCHxA5cstpj/3+xIZvTez8dq2IcBQaXRkenHZ7FVVq1ZROtURwfbtTlG" };
const HIGHLIGHT_MAX = 256 * 1024;                // longer code stays plain: highlighting it would stall the page

const loading = new Map();
/** Call `use` with a library: now if it is loaded, once it is if it can be, never if it cannot. */
function withLibrary(library, use) {
  if (window[library.global]) return use(window[library.global]);
  if (!loading.has(library.src)) loading.set(library.src, new Promise((loaded, failed) => {
    if (library.css) document.head.append(h("link", { rel: "stylesheet", href: library.css, integrity: library.cssIntegrity, crossorigin: "anonymous" }));
    document.head.append(h("script", { src: library.src, integrity: library.integrity, crossorigin: "anonymous", onload: loaded, onerror: failed }));
  }));
  loading.get(library.src).then(() => use(window[library.global]), () => {});
}

/** Highlight, typeset and draw what `root` holds. Diagrams only when asked: a reply still arriving is redrawn often. */
export function enhance(root, { diagrams = true } = {}) {
  const code = [...root.querySelectorAll("code[data-lang]:not([data-done])")];
  if (code.length) withLibrary(HIGHLIGHT, hljs => code.forEach(node => {
    node.dataset.done = "1";
    if (hljs.getLanguage(node.dataset.lang) && node.textContent.length <= HIGHLIGHT_MAX) {     // it escapes what it is given
      node.innerHTML = hljs.highlight(node.textContent, { language: node.dataset.lang, ignoreIllegals: true }).value;
    }
  }));
  const mathematics = [...root.querySelectorAll(".math:not([data-done])")];
  if (mathematics.length) withLibrary(KATEX, katex => mathematics.forEach(node => {
    node.dataset.done = "1";
    try {
      katex.render(node.dataset.tex, node, { displayMode: node.classList.contains("display"), throwOnError: false, trust: false,
                                             strict: "ignore", maxSize: 50, maxExpand: 1000, output: "htmlAndMathml" });
    } catch { /* it stays as its TeX */ }
  }));
  if (diagrams) for (const holder of root.querySelectorAll(".cb .diagram:not([data-done])")) {
    const block = holder.closest(".cb");
    holder.dataset.done = "1";
    block.classList.add("drawn");
    holder.append(viewer({ kind: "mermaid", data: holder.dataset.source }, { autosize: true, onError: () => block.classList.remove("drawn") }));
  }
}

/** A button that says it worked, for a moment. */
function action(glyph, label, onclick) {
  const words = h("span", {}, label);
  return h("button", { type: "button", onclick: async () => {
    const said = await onclick();
    if (said) { words.textContent = said; setTimeout(() => { words.textContent = label; }, 1200); }
  } }, icon(glyph), words);
}

/** A fenced block as replies and documents show one: its language and Copy; Preview for a page, when there is
 *  somewhere to show it (`show(view)`); a diagram drawn, with its source one click away. */
export function codeBlock(code, language, closed, show) {
  const diagram = language === "mermaid", page = ["html", "svg", "xml"].includes(language) || (!language && /^\s*<(!doctype|html|svg)/i.test(code));
  const block = h("div", { class: "cb" });
  return add(block, h("div", { class: "cbh" }, h("span", {}, language || "code"), h("span", {},
      diagram && action("source", "Code", () => { block.classList.toggle("drawn"); }),
      diagram && show && action("open", "Open", () => { show({ kind: "diagram", source: code }); }),
      page && show && action("play", "Preview", () => { show({ kind: "snippet", code, language }); }),
      action("copy", "Copy", async () => { await copyText(code); return "Copied"; }))),
    h("pre", {}, h("code", { "data-lang": diagram ? null : language || null }, code)),
    diagram && closed && h("div", { class: "diagram", "data-source": code }));
}

/** Source or text with line numbers, highlighted when it is short enough. */
export function codeView(text, language) {
  const lines = text.split("\n");
  if (lines.length > 1 && !lines[lines.length - 1]) lines.pop();
  return h("div", { class: "codeview" }, h("pre", { class: "gutter", "aria-hidden": "true" }, lines.map((_, index) => index + 1).join("\n")),
    h("pre", { class: "src" }, h("code", { "data-lang": language || null }, lines.join("\n"))));
}

// ── the viewer ─────────────────────────────────────────────────────────────────────────────────────────────────
const frames = new Map();                        // a viewer's frame → what it is to show
/** A sandboxed frame that draws `payload`: {kind: mermaid | docx | sheet | page, data}. `autosize`: it is as tall
 *  as what it drew. A page (HTML or SVG from a reply) runs there, apart from this page. */
export function viewer(payload, { autosize = false, onError = null } = {}) {
  const frame = h("iframe", { class: autosize ? "viewer auto" : "viewer", src: new URL("./viewer/viewer.html", import.meta.url).href,
    sandbox: payload.kind === "page" ? "allow-scripts allow-forms allow-popups allow-modals" : "allow-scripts allow-popups allow-downloads" });
  frames.set(frame, { payload, autosize, onError, sent: false });
  return frame;
}
window.addEventListener("message", event => {
  for (const [frame, held] of frames) {
    if (!frame.isConnected) { frames.delete(frame); continue; }
    if (event.source !== frame.contentWindow || !event.data || typeof event.data !== "object") continue;
    if (event.data.type === "ready" && !held.sent) {             // once: a frame that went elsewhere is sent nothing
      held.sent = true;
      const theme = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      frame.contentWindow.postMessage({ type: "render", ...held.payload, theme }, "*");
    } else if (event.data.type === "size" && held.autosize && Number.isFinite(event.data.height)) {
      frame.style.height = `${Math.min(Math.max(Math.ceil(event.data.height), 48), 4000)}px`;
    } else if (event.data.type === "error" && held.onError) held.onError(String(event.data.message || "").slice(0, 500));
  }
});
