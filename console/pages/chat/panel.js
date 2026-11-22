// The panel beside a chat: the session's files, and a preview of one of them, or of a page or a diagram from a
// reply. A preview is drawn once for each version of a file and each way of showing it, so a page that redraws
// the conversation does not restart a video or reload a running page.
import { viewer } from "../../components/rich.js";
import { add, clear, h, icon } from "../../lib/dom.js";
import { bytes } from "../../lib/format.js";
import { badgeOf, dirName, inWork } from "../../lib/kinds.js";
import { keep, kept } from "../../lib/storage.js";
import { hasSource, preview } from "./preview.js";
import { viewUrl } from "../../lib/work.js";

const OPEN = "hg.chat.panel", LIST_MAX = 2000;   // the rows drawn in the list of files; a filter finds the rest

export const badge = path => { const [letters, colour] = badgeOf(path); return h("span", { class: "badge", style: `background:${colour}` }, letters); };

/** `session()` is the chat's session, or null; `refresh()` reads its files again and gives them to `setFiles`. */
export function sidePanel({ session, refresh }) {
  const state = { open: kept(OPEN, true), tab: "files", view: null, mode: "preview", wide: false, filter: "", drawn: null, files: [], more: false };
  const title = h("span", { class: "title mono" }), body = h("div", { class: "pb" });
  const tool = (glyph, label, onclick, tag = "button") => h(tag, { class: "quiet square", title: label, onclick, ...(tag === "a" && { target: "_blank", rel: "noopener" }) }, icon(glyph));
  const filesTab = h("button", { onclick: async () => { await refresh(); show("files"); } }, "Files"), viewTab = h("button", { onclick: () => show("view") }, "Preview");
  const mode = tool("source", "", () => { state.mode = state.mode === "code" ? "preview" : "code"; draw(); });
  const inTab = tool("open", "Open in a new tab", null, "a"), download = tool("save", "Download", null, "a");
  const widen = tool("widen", "", () => { state.wide = !state.wide; draw(); });
  const node = h("section", { class: "side" }, h("div", { class: "ph" }, h("div", { class: "segment" }, filesTab, viewTab), title, mode, inTab, download,
    tool("refresh", "Refresh", async () => { await refresh(); state.drawn = null; draw(); }), widen, tool("close", "Close", () => toggle())), body);

  function show(tab, view) {
    Object.assign(state, { open: true, tab }, view && { view, mode: "preview" });
    keep(OPEN, true);
    draw();
  }
  function toggle() {
    state.open = !state.open;
    keep(OPEN, state.open);
    draw();
  }
  /** What the panel's own bar says and offers for what is shown. */
  function bar(text, url, switchable) {
    title.textContent = text;
    inTab.hidden = download.hidden = !url;
    if (url) { inTab.href = url; download.href = `${url}?download`; }
    mode.hidden = !switchable;
    add(clear(mode), icon(state.mode === "code" ? "eye" : "source"));
    mode.title = state.mode === "code" ? "Show the preview" : "Show the code";
  }
  function draw() {
    node.hidden = !state.open;
    node.classList.toggle("wide", state.wide);
    if (!state.open) { clear(body); state.drawn = null; return; }            // which stops what played there
    const listing = state.tab === "files" || !state.view;
    filesTab.classList.toggle("on", listing); viewTab.classList.toggle("on", !listing);
    viewTab.hidden = !state.view;
    add(clear(widen), icon(state.wide ? "narrow" : "widen"));
    widen.title = state.wide ? "Narrow" : "Widen";
    listing ? drawFiles() : drawView();
  }

  function drawFiles() {
    const count = state.files.length;
    bar(session() && count ? `${count.toLocaleString()}${state.more ? "+" : ""} file${count === 1 ? "" : "s"}` : "");
    const key = `files|${session()}|${state.files.map(file => file.path + file.bytes + file.at).join("|")}`;
    if (key === state.drawn) return;
    state.drawn = key;
    if (!session()) return add(clear(body), h("div", { class: "note" }, "What the agent writes in /work shows here."));
    if (!count) return add(clear(body), h("div", { class: "note" }, "No files yet. They show here when a turn ends."));
    const list = h("div", {});
    const drawList = () => {
      const wanted = state.filter.trim().toLowerCase(), found = wanted ? state.files.filter(file => file.path.toLowerCase().includes(wanted)) : state.files;
      add(clear(list), found.slice(0, LIST_MAX).map(file => {
        const folder = dirName(file.path);
        return h("a", { class: "row", href: "#", title: file.path, onclick: event => { event.preventDefault(); openFile(file.path); } }, badge(file.path),
          h("span", { class: "nm" }, folder && h("span", { class: "muted" }, `${folder}/`), file.path.slice(folder ? folder.length + 1 : 0)),
          h("span", { class: "sz" }, bytes(file.bytes)),
          h("a", { class: "dl", href: `${viewUrl(session(), file.path)}?download`, title: "Download", onclick: event => event.stopPropagation() }, icon("save")));
      }), found.length > LIST_MAX && h("div", { class: "note" }, `${(found.length - LIST_MAX).toLocaleString()} more: filter to find them.`),
      wanted && !found.length && h("div", { class: "note" }, "No file matches."),
      state.more && !wanted && h("div", { class: "note" }, "The session holds more files than are listed here."));
    };
    add(clear(body), h("div", { class: "files-list" }, count > 12 && h("input", { class: "filter", placeholder: "Filter files", spellcheck: "false", autocomplete: "off", value: state.filter,
      oninput: event => { state.filter = event.target.value; drawList(); } }), list));
    drawList();
  }

  function drawView() {
    const view = state.view;
    if (view.kind !== "file") {                    // a page or a diagram from a reply: drawn by the viewer
      bar(view.kind === "diagram" ? "Diagram" : `${view.language || "code"} preview`);
      if (state.drawn === view) return;
      state.drawn = view;
      return add(clear(body), viewer(view.kind === "diagram" ? { kind: "mermaid", data: view.source } : { kind: "page", data: view.code, language: view.language }));
    }
    const file = state.files.find(found => found.path === view.path), url = viewUrl(session(), view.path);
    bar(view.path, file && url, Boolean(file) && hasSource(view.path));
    const key = `${session()}|${view.path}|${state.mode}|${file ? `${file.bytes}|${file.at}` : "missing"}`;
    if (key === state.drawn) return;
    state.drawn = key;
    if (!file || !url) return add(clear(body), h("div", { class: "note" }, `${view.path} is not among the session's files.`));
    body.scrollTop = 0;
    preview(body, { ...file, url, session: session() }, state.mode, () => state.drawn === key, { open: openFile, show: shown => show("view", shown) });
  }
  const openFile = path => show("view", { kind: "file", path: inWork(path) });

  draw();
  return {
    node, toggle, openFile, draw,
    isOpen: () => state.open,
    files: () => state.files,
    /** A page or a diagram from a reply. */
    show: view => show("view", view),
    setFiles({ files, more }) { Object.assign(state, { files, more }); draw(); },
  };
}
