// What makes rendered replies and documents rich: highlighted code, typeset math and drawn diagrams. The libraries
// load only when a page first needs them, pinned to a version and checked against an integrity hash. Without them
// (offline, a CDN outage) everything still reads: code as plain text, math as its TeX, a diagram as its source.
//
// Diagrams, Word documents and spreadsheets render in the viewer (/viewer, static/viewer.js): a sandboxed frame in an
// origin of its own, so the libraries that parse them never run in this page.

import { esc, ic, I, post, copyText, flash } from "./util.js";

const CDN = "https://cdn.jsdelivr.net/npm";
const HLJS = { src: `${CDN}/@highlightjs/cdn-assets@11.11.1/highlight.min.js`, integrity: "sha384-RH2xi4eIQ/gjtbs9fUXM68sLSi99C7ZWBRX1vDrVv6GQXRibxXLbwO2NGZB74MbU", global: "hljs" };
const KATEX = { src: `${CDN}/katex@0.16.22/dist/katex.min.js`, integrity: "sha384-cMkvdD8LoxVzGF/RPUKAcvmm49FQ0oxwDF3BGKtDXcEc+T1b2N+teh/OJfpU0jr6", global: "katex",
  css: `${CDN}/katex@0.16.22/dist/katex.min.css`, cssIntegrity: "sha384-5TcZemv2l/9On385z///+d7MSYlvIEw9FuZTIdZ14vJLqWphw7e7ZPuOiCHJcFCP" };
const HIGHLIGHT_MAX = 256 * 1024;                    // longer code shows plain: highlighting it would stall the page

const loading = new Map();
function load(lib) {
  if (!loading.has(lib.src)) {
    loading.set(lib.src, new Promise((ok, fail) => {
      if (lib.css) {
        const link = Object.assign(document.createElement("link"), { rel: "stylesheet", href: lib.css, integrity: lib.cssIntegrity, crossOrigin: "anonymous" });
        document.head.appendChild(link);
      }
      const script = Object.assign(document.createElement("script"), { src: lib.src, integrity: lib.integrity, crossOrigin: "anonymous" });
      script.onload = () => ok(window[lib.global]);
      script.onerror = () => fail(new Error(`could not load ${lib.src}`));
      document.head.appendChild(script);
    }));
  }
  return loading.get(lib.src);
}
/** Run `use` with the library now if it is loaded, else once it is; never if it cannot load. */
function withLib(lib, use) {
  if (window[lib.global]) use(window[lib.global]);
  else load(lib).then(use, () => {});
}

/** Highlight, typeset and draw what `root` holds. Diagrams only when asked: a reply still streaming redraws often. */
export function enhance(root, { diagrams = true } = {}) {
  const code = [...root.querySelectorAll("code[data-lang]:not([data-done])")].filter(el => el.dataset.lang);
  if (code.length) withLib(HLJS, hljs => code.forEach(el => paint(hljs, el)));
  const math = [...root.querySelectorAll(".math:not([data-done])")];
  if (math.length) withLib(KATEX, katex => math.forEach(el => typeset(katex, el)));
  if (diagrams) root.querySelectorAll(".cb .diagram:not([data-done])").forEach(mountDiagram);
}

function paint(hljs, el) {
  el.dataset.done = "1";
  const lang = el.dataset.lang, text = el.textContent;
  if (!hljs.getLanguage(lang) || text.length > HIGHLIGHT_MAX) return;
  el.innerHTML = hljs.highlight(text, { language: lang, ignoreIllegals: true }).value;   // escapes what it is given
}

function typeset(katex, el) {
  el.dataset.done = "1";
  try {
    katex.render(el.dataset.tex, el, { displayMode: el.classList.contains("display"), throwOnError: false, trust: false,
      strict: "ignore", maxSize: 50, maxExpand: 1000, output: "htmlAndMathml" });
  } catch { /* left as its TeX */ }
}

// ── code blocks ────────────────────────────────────────────────────────────────────────────────────────────────
const PREVIEWABLE = ["html", "svg", "xml"];
const button = (act, icon, label) => `<button data-act="${act}">${ic(icon, 13)}<span>${label}</span></button>`;

/** A fenced block as replies and documents show it: its language, Copy, and Preview for a page, or a drawn diagram. */
export function codeBlock(code, lang, closed) {
  const diagram = lang === "mermaid";
  const page = PREVIEWABLE.includes(lang) || (!lang && /^\s*<(!doctype|html|svg)/i.test(code));
  const actions = (diagram ? button("source", I.code, "Code") + button("diagram", I.ext, "Open") : page ? button("preview", I.play, "Preview") : "") +
    button("copy", I.copy, "Copy");
  return `<div class="cb" data-lang="${esc(lang)}"><div class="cbh"><span>${esc(lang || "code")}</span><span>${actions}</span></div>` +
    `<pre><code data-lang="${esc(diagram ? "" : lang)}">${esc(code)}</code></pre>` +
    (diagram && closed ? `<div class="diagram" data-src="${esc(code)}"></div>` : "") + `</div>`;
}

/** A click on a code block's button. `show` opens a view in the side panel. */
export function codeAction(btn, show) {
  const block = btn.closest(".cb"), code = block.querySelector("pre code").textContent, lang = block.dataset.lang;
  const act = btn.dataset.act;
  if (act === "copy") copyText(code).then(() => flash(btn));
  else if (act === "preview") post("api/snippet", { content: code, lang }).then(d => show({ kind: "snippet", url: d.url, lang }));
  else if (act === "diagram") show({ kind: "diagram", source: code });
  else if (act === "source") block.classList.toggle("drawn");
}

function mountDiagram(holder) {
  holder.dataset.done = "1";
  const block = holder.closest(".cb");
  block.classList.add("drawn");
  holder.appendChild(viewer({ kind: "mermaid", data: holder.dataset.src }, { autosize: true, onError: () => block.classList.remove("drawn") }));
}

// ── the viewer ─────────────────────────────────────────────────────────────────────────────────────────────────
const frames = new Map();                            // a viewer frame -> what it shows
const dark = () => matchMedia("(prefers-color-scheme: dark)").matches;

/** A sandboxed frame that renders `payload` ({kind: mermaid | docx | sheet, data}). autosize: as tall as its content. */
export function viewer(payload, { autosize = false, onError = null } = {}) {
  const frame = document.createElement("iframe");
  frame.className = autosize ? "viewer auto" : "viewer";
  frame.setAttribute("sandbox", "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads");
  frame.src = "viewer";
  frames.set(frame, { payload, autosize, onError, sent: false });
  return frame;
}

window.addEventListener("message", e => {
  for (const [frame, v] of frames) {
    if (!frame.isConnected) { frames.delete(frame); continue; }   // gone from the page (it is mounted as it is made)
    if (e.source !== frame.contentWindow || !e.data || typeof e.data !== "object") continue;
    if (e.data.type === "ready" && !v.sent) {          // once: a frame that navigated away gets nothing
      v.sent = true;
      frame.contentWindow.postMessage({ type: "render", ...v.payload, theme: dark() ? "dark" : "light" }, "*");
    } else if (e.data.type === "size" && v.autosize && Number.isFinite(e.data.height)) {
      frame.style.height = Math.min(Math.max(Math.ceil(e.data.height), 48), 4000) + "px";
    } else if (e.data.type === "error") {
      v.onError?.(String(e.data.message || "").slice(0, 500));
    }
  }
});
