// Small helpers the page's modules share: escaping, sizes, paths, file kinds, icons.

export const $ = id => document.getElementById(id);
export const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
export const size = n => n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB"
  : n < 1073741824 ? (n / 1048576).toFixed(1) + " MB" : (n / 1073741824).toFixed(2) + " GB";
export const sleep = ms => new Promise(ok => setTimeout(ok, ms));

// ── paths in a session's workspace ─────────────────────────────────────────────────────────────────────────────
export const ext = p => { const name = String(p).split("/").pop(); return name.includes(".") ? name.split(".").pop().toLowerCase() : ""; };
export const baseName = p => String(p).split("/").pop();
export const dirName = p => String(p).includes("/") ? String(p).slice(0, String(p).lastIndexOf("/")) : "";
export const wsUrl = (sid, path) => `ws/${sid}/` + path.split("/").map(encodeURIComponent).join("/");
/** A path as the workspace lists it: relative to /workspace, without ./ or a trailing slash. */
export const rel = p => String(p || "").replace(/^file:\/\//, "").replace(/^\/workspace\//, "").replace(/^(\.\/)+/, "").replace(/\/$/, "");
/** A link target in a document, resolved against the document's folder; null when it leaves the workspace. */
export function joinPath(base, target) {
  const clean = String(target).split(/[?#]/)[0];
  let decoded;
  try { decoded = decodeURIComponent(clean); } catch { decoded = clean; }
  if (/^(file:\/\/)?\/workspace\//.test(decoded)) return rel(decoded);
  if (decoded.startsWith("/")) return null;
  const parts = base ? base.split("/") : [];
  for (const part of decoded.split("/")) {
    if (part === "..") { if (!parts.length) return null; parts.pop(); }
    else if (part && part !== ".") parts.push(part);
  }
  return parts.join("/") || null;
}

// ── what a file is, and how the side panel shows it ────────────────────────────────────────────────────────────
const KINDS = {
  html: ["html", "htm", "xhtml"],
  image: ["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif", "apng"],
  svg: ["svg"],
  pdf: ["pdf"],
  audio: ["mp3", "wav", "ogg", "oga", "m4a", "aac", "flac", "opus", "weba"],
  video: ["mp4", "webm", "mov", "m4v", "ogv"],
  markdown: ["md", "markdown", "mdx", "mkd"],
  table: ["csv", "tsv"],
  json: ["json", "geojson", "webmanifest"],
  notebook: ["ipynb"],
  mermaid: ["mmd", "mermaid"],
  docx: ["docx"],
  sheet: ["xlsx", "xlsm", "xls", "ods"],
  binary: ["zip", "tar", "gz", "tgz", "bz2", "xz", "zst", "7z", "rar", "jar", "whl", "exe", "dll", "so", "dylib", "bin",
    "o", "a", "class", "pyc", "wasm", "sqlite", "sqlite3", "db", "parquet", "feather", "arrow", "npy", "npz", "pkl",
    "pt", "onnx", "pptx", "ppt", "doc", "key", "pages", "numbers", "psd", "ai", "ttf", "otf", "woff", "woff2", "eot",
    "tif", "tiff", "heic", "dmg", "iso", "deb", "rpm"],
};
const KIND_OF = Object.fromEntries(Object.entries(KINDS).flatMap(([kind, list]) => list.map(e => [e, kind])));
/** html | image | svg | pdf | audio | video | markdown | table | json | notebook | mermaid | docx | sheet | binary | text */
export const kindOf = p => KIND_OF[ext(p)] || "text";
/** Kinds the panel opens by itself when a turn makes one, best first. */
export const SHOWCASE = ["html", "svg", "markdown", "image", "notebook", "pdf", "video", "audio", "mermaid", "docx", "sheet", "table"];

// highlight.js language names, by file extension or name
const LANGS = {
  py: "python", pyi: "python", js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript",
  ts: "typescript", tsx: "typescript", mts: "typescript", sh: "bash", bash: "bash", zsh: "bash", yml: "yaml",
  yaml: "yaml", toml: "ini", ini: "ini", cfg: "ini", conf: "ini", env: "ini", rs: "rust", go: "go", java: "java",
  kt: "kotlin", kts: "kotlin", c: "c", h: "c", cc: "cpp", cpp: "cpp", cxx: "cpp", hpp: "cpp", cs: "csharp", rb: "ruby",
  php: "php", swift: "swift", sql: "sql", html: "xml", htm: "xml", xml: "xml", svg: "xml", vue: "xml", css: "css",
  scss: "scss", less: "less", json: "json", jsonl: "json", geojson: "json", ipynb: "json", md: "markdown",
  markdown: "markdown", diff: "diff", patch: "diff", lua: "lua", r: "r", pl: "perl", m: "objectivec",
  graphql: "graphql", gql: "graphql", mk: "makefile", vb: "vbnet", wat: "wasm", txt: "plaintext", log: "plaintext",
};
const NAMED = { makefile: "makefile", gnumakefile: "makefile", "cmakelists.txt": "cmake", gemfile: "ruby", rakefile: "ruby" };
export const langOf = p => NAMED[baseName(p).toLowerCase()] || LANGS[ext(p)] || "";

const COLORS = [[KINDS.html, "#e8590c"], [["css", "scss", "less"], "#1c7ed6"], [["js", "mjs", "ts", "tsx", "jsx"], "#e0a800"],
  [["py", "ipynb"], "#3572a5"], [[...KINDS.image, ...KINDS.svg], "#7048e8"], [KINDS.pdf, "#e03131"],
  [[...KINDS.markdown, "txt"], "#868e96"], [[...KINDS.table, ...KINDS.json, ...KINDS.sheet], "#2b8a3e"],
  [KINDS.docx, "#1864ab"], [[...KINDS.audio, ...KINDS.video], "#c2255c"], [["sh", "bash", "zsh"], "#495057"]];
export function badge(p) {
  const e = ext(p), kind = COLORS.find(([list]) => list.includes(e));
  return `<span class="badge" style="background:${kind ? kind[1] : "#6c757d"}">${esc((e || "file").slice(0, 4).toUpperCase())}</span>`;
}

// ── icons ──────────────────────────────────────────────────────────────────────────────────────────────────────
export const ic = (d, s = 18) => `<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
export const I = {
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  clip: '<path d="m21.4 11.1-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5"/>',
  up: '<path d="M12 19V5"/><path d="m5 12 7-7 7 7"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" stroke="none"/>',
  panel: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M14 4v16"/>',
  side: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M10 4v16"/>',
  ext: '<path d="M14 4h6v6"/><path d="M20 4 10 14"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
  down: '<path d="M12 4v11"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  chev: '<path d="m9 6 6 6-6 6"/>',
  down2: '<path d="m6 9 6 6 6-6"/>',
  term: '<path d="m5 8 4 4-4 4"/><path d="M12 17h7"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z"/><path d="M14 3v5h5"/>',
  tool: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17l3 3 5.3-5.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4Z"/>',
  search: '<circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  check: '<path d="m5 12 5 5 9-10"/>',
  play: '<path d="M7 5v14l12-7Z"/>',
  code: '<path d="m8 8-4 4 4 4"/><path d="m16 8 4 4-4 4"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  expand: '<path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="m21 3-7 7"/><path d="m3 21 7-7"/>',
  shrink: '<path d="M4 14h6v6"/><path d="M20 10h-6V4"/><path d="m14 10 7-7"/><path d="m3 21 7-7"/>',
};

// ── the page's own requests ────────────────────────────────────────────────────────────────────────────────────
export async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || r.statusText);
  return d;
}
export function copyText(text) {                     // works on plain http too, where navigator.clipboard is absent
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const t = document.createElement("textarea");
  t.value = text; t.style.position = "fixed"; t.style.opacity = "0";
  document.body.appendChild(t); t.select();
  try { document.execCommand("copy"); } finally { t.remove(); }
  return Promise.resolve();
}
/** A button's label (its <span>) says "Copied" for a moment. */
export function flash(button, text = "Copied") {
  const label = button.querySelector("span");
  if (!label) return;
  const was = label.textContent;
  label.textContent = text;
  setTimeout(() => { label.textContent = was; }, 1200);
}
