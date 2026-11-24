// What a file is, from its name: how it is previewed, the language its source is highlighted as, and its badge.

const KINDS = {
  html: ["html", "htm", "xhtml"], image: ["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif", "apng"], svg: ["svg"],
  pdf: ["pdf"], audio: ["mp3", "wav", "ogg", "oga", "m4a", "aac", "flac", "opus", "weba"], video: ["mp4", "webm", "mov", "m4v", "ogv"],
  markdown: ["md", "markdown", "mdx", "mkd"], table: ["csv", "tsv"], json: ["json", "geojson", "webmanifest"], notebook: ["ipynb"],
  mermaid: ["mmd", "mermaid"], docx: ["docx"], sheet: ["xlsx", "xlsm", "xls", "ods"],
  binary: ["zip", "tar", "gz", "tgz", "bz2", "xz", "zst", "7z", "rar", "jar", "whl", "exe", "dll", "so", "dylib", "bin", "o", "a", "class",
           "pyc", "wasm", "sqlite", "sqlite3", "db", "parquet", "feather", "arrow", "npy", "npz", "pkl", "pt", "onnx", "pptx", "ppt", "doc",
           "key", "pages", "numbers", "psd", "ai", "ttf", "otf", "woff", "woff2", "eot", "tif", "tiff", "heic", "dmg", "iso", "deb", "rpm"],
};
const KIND_OF = Object.fromEntries(Object.entries(KINDS).flatMap(([kind, extensions]) => extensions.map(extension => [extension, kind])));
// highlight.js's name for a language, by a file's extension or its whole name
const LANGUAGES = {
  py: "python", pyi: "python", js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript",
  mts: "typescript", sh: "bash", bash: "bash", zsh: "bash", yml: "yaml", yaml: "yaml", toml: "ini", ini: "ini", cfg: "ini", conf: "ini",
  env: "ini", rs: "rust", go: "go", java: "java", kt: "kotlin", kts: "kotlin", c: "c", h: "c", cc: "cpp", cpp: "cpp", cxx: "cpp", hpp: "cpp",
  cs: "csharp", rb: "ruby", php: "php", swift: "swift", sql: "sql", html: "xml", htm: "xml", xml: "xml", svg: "xml", vue: "xml", css: "css",
  scss: "scss", less: "less", json: "json", jsonl: "json", geojson: "json", ipynb: "json", md: "markdown", markdown: "markdown", diff: "diff",
  patch: "diff", lua: "lua", r: "r", pl: "perl", graphql: "graphql", gql: "graphql", mk: "makefile", txt: "plaintext", log: "plaintext",
};
const NAMED = { makefile: "makefile", gnumakefile: "makefile", dockerfile: "dockerfile", "cmakelists.txt": "cmake", gemfile: "ruby", rakefile: "ruby" };
const COLOURS = [[KINDS.html, "#e8590c"], [["css", "scss", "less"], "#1c7ed6"], [["js", "mjs", "ts", "tsx", "jsx"], "#b98900"], [["py", "ipynb"], "#3572a5"],
  [[...KINDS.image, ...KINDS.svg], "#7048e8"], [KINDS.pdf, "#e03131"], [[...KINDS.table, ...KINDS.json, ...KINDS.sheet], "#2b8a3e"],
  [KINDS.docx, "#1864ab"], [[...KINDS.audio, ...KINDS.video], "#c2255c"]];

export const baseName = path => String(path).split("/").pop();
export const dirName = path => String(path).includes("/") ? String(path).slice(0, String(path).lastIndexOf("/")) : "";
export const extension = path => baseName(path).includes(".") ? baseName(path).split(".").pop().toLowerCase() : "";
/** html | image | svg | pdf | audio | video | markdown | table | json | notebook | mermaid | docx | sheet | binary | text */
export const kindOf = path => KIND_OF[extension(path)] || "text";
export const languageOf = path => NAMED[baseName(path).toLowerCase()] || LANGUAGES[extension(path)] || "";
/** A badge's letters and colour. */
export const badgeOf = path => [(extension(path) || "file").slice(0, 4).toUpperCase(), (COLOURS.find(([list]) => list.includes(extension(path))) || [0, "#6c757d"])[1]];
/** The kinds worth opening unasked when a turn makes one, best first. */
export const SHOWCASE = ["html", "svg", "markdown", "image", "notebook", "pdf", "video", "audio", "mermaid", "docx", "sheet", "table"];

/** A path as a session's files are listed: under /work, without ./ or a trailing slash. */
export const inWork = path => String(path || "").replace(/^file:\/\//, "").replace(/^\/work\//, "").replace(/^(\.\/)+/, "").replace(/\/$/, "");
/** Where a link in a document in the folder `base` leads, as a path under /work; null when it leaves the work. */
export function resolve(base, target) {
  let wanted = String(target).split(/[?#]/)[0];
  try { wanted = decodeURIComponent(wanted); } catch { /* as it is written */ }
  if (/^(file:\/\/)?\/work\//.test(wanted)) return inWork(wanted);
  if (wanted.startsWith("/")) return null;
  const parts = base ? base.split("/") : [];
  for (const part of wanted.split("/")) {
    if (part === "..") { if (!parts.length) return null; parts.pop(); } else if (part && part !== ".") parts.push(part);
  }
  return parts.join("/") || null;
}
