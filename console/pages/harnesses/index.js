// Harnesses: a card for each, and an editor like an environment's. A harness is a package of files and nothing
// else, so its first tab is the files themselves, edited here; the other is which environments carry it, since
// everything about where it runs is an environment's to say. Edits are a working copy, packed and saved once. Out
// of the console and back in, a harness is its package archive.
import { code } from "../../components/code.js";
import { drafts, frame, NEW } from "../../components/editor.js";
import { ask, confirm, dialog, fail, toast } from "../../components/feedback.js";
import { field, mono, pill, top } from "../../components/layout.js";
import { all, call, del, get, put } from "../../lib/api.js";
import { pack, unpack } from "../../lib/archive.js";
import { add, clear, h, icon } from "../../lib/dom.js";
import { download, pickFile } from "../../lib/files.js";
import { bytes } from "../../lib/format.js";
import { again } from "../../lib/route.js";
import { definition } from "../environments.js";
import { template } from "./template.js";

const TABS = ["Files", "Environments"];
const NAME = "[a-z0-9][a-z0-9\\-]*";
const at = name => `/v1/harnesses/${encodeURIComponent(name)}`;
const page = (name, tab = "") => `#/harnesses/${encodeURIComponent(name)}${tab && `/${tab.toLowerCase()}`}`;
const carrying = (environments, name) => environments.filter(e => e.harnesses.includes(name)).map(e => e.name);
const begun = (files, more = {}) => ({ files, filesChanged: true, carried: [], carriedBefore: [], open: "run", dirty: true, ...more });

async function draftOf(name, environments) {
  const key = `harnesses/${name}`, held = drafts.get(key);
  if (name !== NEW && held && !held.dirty) drafts.delete(key);                 // nothing unsaved: show what is there now
  if (!drafts.has(key)) {
    if (name === NEW) throw new Error("Start a harness from the Harnesses page: New harness, or Import.");
    const archive = await call("GET", `${at(name)}/package`, undefined, { raw: true });
    drafts.set(key, begun(await unpack(await archive.arrayBuffer()), { filesChanged: false, dirty: false, name,
      carried: carrying(environments, name), carriedBefore: carrying(environments, name), open: (held || {}).open || "run" }));
  }
  return drafts.get(key);
}

function filesTab(draft, changed) {
  const tree = h("div", { class: "tree" }), pane = h("div", { class: "pane" });
  const touched = () => { draft.filesChanged = true; changed(); };
  const drawTree = () => {
    const paths = [...draft.files.keys()].sort((a, b) => (a.includes("/") - b.includes("/")) || a.localeCompare(b));
    let folder = "";
    add(clear(tree), paths.map(path => {
      const cut = path.lastIndexOf("/"), dir = cut < 0 ? "" : path.slice(0, cut), heading = dir !== folder && h("div", { class: "dir" }, `${dir}/`);
      folder = dir;
      return [heading, h("a", { title: path, class: path === draft.open ? "on" : "", style: dir ? "padding-left:18px" : null, onclick: () => { draft.open = path; draw(); } },
        path.slice(cut + 1), draft.files.get(path).executable && h("i", {}, "exec"))];
    }), h("div", { class: "acts" },
      h("button", { class: "quiet", onclick: () => ask("New file", "Its path in the package", "", answer => {
        const path = answer.replace(/^\.?\//, "");
        if (!path || draft.files.has(path)) throw new Error(path ? `${path} is already in the package` : "Give it a path.");
        draft.files.set(path, { text: "", executable: false }); draft.open = path; touched(); draw();
      }, "Add") }, icon("plus"), "File"),
      h("button", { class: "quiet", onclick: async () => {
        const file = await pickFile(""), content = new Uint8Array(await file.arrayBuffer());
        const into = draft.open.includes("/") ? draft.open.slice(0, draft.open.lastIndexOf("/") + 1) : "";
        let held; try { held = { text: new TextDecoder("utf-8", { fatal: true }).decode(content) }; } catch { held = { bytes: content }; }
        draft.files.set(into + file.name, { ...held, executable: false }); draft.open = into + file.name; touched(); draw();
      } }, "Upload")));
  };
  const drawPane = () => {
    const path = draft.open, file = draft.files.get(path);
    if (!file) return add(clear(pane), h("div", { class: "empty" }, "Pick a file."));
    add(clear(pane),
      h("div", { class: "bar" }, h("b", {}, path), h("span", { class: "muted small" }, file.text === undefined ? `binary · ${bytes(file.bytes.length)}` : `${file.text.split("\n").length} lines`),
        h("div", { class: "acts" },
          h("label", { class: "check", style: "margin:0 6px 0 0" }, h("input", { type: "checkbox", checked: file.executable, onchange: event => { file.executable = event.target.checked; touched(); drawTree(); } }), "Executable"),
          h("button", { class: "quiet", onclick: () => ask(`Rename ${path}`, "Its new path in the package", path, answer => {
            const to = answer.replace(/^\.?\//, "");
            if (to === path) return;
            if (!to || draft.files.has(to)) throw new Error(to ? `${to} is already in the package` : "Give it a path.");
            draft.files.delete(path); draft.files.set(to, file); draft.open = to; touched(); draw();
          }, "Rename") }, "Rename"),
          h("button", { class: "quiet danger", onclick: () => confirm(`Delete ${path} from the package? It is gone once the harness is saved.`, () => {
            draft.files.delete(path); draft.open = [...draft.files.keys()].sort()[0]; touched(); draw();
          }) }, "Delete"))),
      file.text === undefined ? h("div", { class: "empty" }, "A binary file: it is kept as it is.")
        : code({ value: file.text, onInput: text => { file.text = text; touched(); } }));
  };
  const draw = () => { drawTree(); drawPane(); };
  draw();
  return h("div", { class: "files" }, tree, pane);
}

function environmentsTab(draft, changed, environments) {
  return h("div", { class: "panel" }, h("p", { class: "muted" }, "The environments that carry this harness. An environment is where everything about a turn is decided: the image, the limits, the network, and the variables and secrets the package reads. The package is installed on an environment's image the first time a request names it there."),
    environments.length ? environments.map(environment => {
      const carried = draft.carried.includes(environment.name);
      const option = h("label", { class: `option${carried ? " on" : ""}` },
        h("input", { type: "checkbox", checked: carried, onchange: event => {
          draft.carried = event.target.checked ? [...draft.carried, environment.name].sort() : draft.carried.filter(name => name !== environment.name);
          option.classList.toggle("on", event.target.checked); changed();
        } }),
        h("div", { class: "grow" }, h("b", {}, environment.name), h("div", { class: "muted small mono" }, environment.image)),
        mono(`${environment.name}/${draft.name}/provider/model`));
      return option;
    }) : h("div", { class: "empty" }, "No environment yet. Add one under Environments."));
}

async function harness(main, [name, tab = "files"]) {
  const isNew = name === NEW, key = `harnesses/${name}`;
  const environments = await all("/v1/environments", "name");
  const draft = await draftOf(name, environments), target = draft.name;
  const save = async () => {
    try {
      for (const program of ["install", "run"]) {
        if (!(draft.files.get(program) || {}).executable) throw new Error(`A package needs an executable ${program}.`);
      }
      if (isNew || draft.filesChanged) await call("PUT", at(target), await pack(draft.files));
      for (const environment of environments) {            // carried by an environment, or no longer
        const carries = environment.harnesses.includes(target), wanted = draft.carried.includes(environment.name);
        if (carries !== wanted) await put(`/v1/environments/${encodeURIComponent(environment.name)}`, { ...definition(environment),
          harnesses: wanted ? [...environment.harnesses, target] : environment.harnesses.filter(carried => carried !== target) });
      }
      drafts.delete(key);
      toast(isNew ? `Created ${target}` : "Saved");
      isNew ? location.hash = page(target) : again();
    } catch (error) { fail(error); }
  };
  const view = frame(main, {
    back: "#/harnesses", backLabel: "Harnesses", title: target, isNew,
    subtitle: "A package: install runs once for each environment image, and run once a turn.",
    tabs: [["files", "Files", draft.files.size], ["environments", "Environments", draft.carried.length]], tab, href: id => page(name, id), onSave: save,
    onDiscard: () => { drafts.delete(key); isNew ? location.hash = "#/harnesses" : again(); },
    more: [["Export", async () => download(`${target}.tar.gz`, await pack(draft.files))],
           !isNew && ["Delete", () => confirm(`Delete harness ${name}? It is taken off every environment that carries it.`,
             async () => { await del(at(name)); drafts.delete(key); location.hash = "#/harnesses"; }), true]].filter(Boolean),
  });
  const changed = () => {
    draft.dirty = isNew || draft.filesChanged || draft.carried.join() !== draft.carriedBefore.join();
    view.changed(draft.dirty);
  };
  view.body.append(tab === "environments" ? environmentsTab(draft, changed, environments) : filesTab(draft, changed));
  view.changed(draft.dirty);
}

function create() {
  dialog("New harness", [
    h("p", { class: "muted" }, "It starts as the smallest package that works: install and run, each saying what a real agent CLI's package puts there. Change them in the editor, then create it."),
    field("Name", h("input", { name: "name", pattern: NAME, required: true, placeholder: "my-agent", autocomplete: "off" }), "As a request names it: environment/harness/provider/model"),
  ], "Open the editor", form => {
    drafts.set(`harnesses/${NEW}`, begun(template(), { name: form.elements.name.value.trim() }));
    location.hash = page(NEW);
  });
}

/** Bring in a package archive. A package says nothing of its name, so the person gives it: the file's, unless
 *  they say otherwise. Under a name that is taken it replaces that harness, once it is saved. */
async function importArchive(existing) {
  const file = await pickFile(".gz,.tgz,application/gzip");
  let files;
  try { files = await unpack(await file.arrayBuffer()); } catch (error) { return toast(`${file.name} is not a harness package: ${error.message}`, true); }
  ask("Import a harness", "Its name", file.name.replace(/\.(tar\.gz|tgz|gz)$/, "").toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+/, ""), async name => {
    if (!new RegExp(`^${NAME}$`).test(name)) throw new Error("A name is lower-case letters, digits and '-'.");
    if (existing.includes(name)) {
      Object.assign(await draftOf(name, await all("/v1/environments", "name")), { files, filesChanged: true, dirty: true, open: "run" });
      location.hash = page(name);
      toast(`Imported. Saving replaces ${name} with it`);
    } else {
      drafts.set(`harnesses/${NEW}`, begun(files, { name }));
      location.hash = page(NEW);
      toast("Imported. Look it over, and create it");
    }
  }, "Import");
}

/** Take a folder of a session's work as a harness. `given` is {session, path} when the way here named them (a link
 *  an agent gave, or the chat's own offer): then it is one click, and leads to a chat with the new harness. */
async function fromSession(given = {}) {
  const [environments, recent, started] = await Promise.all([all("/v1/environments", "name"), get("/v1/sessions?limit=50"),
    given.session ? get(`/v1/sessions/${encodeURIComponent(given.session)}`).catch(() => null) : null]);
  const ranIn = started && String(started.model || "").split("/")[0];
  dialog(given.path ? `Add ${given.path} as a harness` : "Harness from a session", [
    h("p", { class: "muted" }, "Takes a folder of a session's work, as it was last saved, and makes it a harness: how a package an agent wrote is brought in. Under a name that is taken, it replaces that harness."),
    field("Name", h("input", { name: "name", value: given.path || "", pattern: NAME, required: true, autocomplete: "off" })),
    field("Session", [h("input", { name: "session", value: given.session || "", placeholder: "sess_…", required: true, list: "recent", autocomplete: "off" }),
      h("datalist", { id: "recent" }, recent.data.map(found => h("option", { value: found.id }, `${found.model || ""} · ${found.turns} turns`)))]),
    field("Folder in /work", h("input", { name: "path", value: given.path || "", placeholder: "The same as the name, usually", autocomplete: "off" })),
    field("Environment that carries it", h("select", { name: "environment" }, h("option", { value: "" }, "None yet"),
      environments.map(each => h("option", { value: each.name, selected: each.name === ranIn }, each.name))),
      "A request names a harness with an environment that carries it. Pick one, and you go straight to a chat with it."),
  ], "Add harness", async form => {
    const read = name => form.elements[name].value.trim(), name = read("name"), environment = environments.find(each => each.name === read("environment"));
    await put(at(name), { session: read("session"), path: read("path") || name });
    drafts.delete(`harnesses/${name}`);
    if (environment && !environment.harnesses.includes(name)) {
      await put(`/v1/environments/${encodeURIComponent(environment.name)}`, { ...definition(environment), harnesses: [...environment.harnesses, name] });
    }
    toast(`Added ${name}`);
    location.hash = environment ? `#/chat/with/${encodeURIComponent(environment.name)}/${encodeURIComponent(name)}` : page(name);
  });
}

export async function harnesses(main, params) {
  const linked = params[0] === "from" && /^sess_/.test(params[1] || "") && params[2];       // #/harnesses/from/<session>/<folder>
  if (params[0] && !linked) return harness(main, params);
  const [found, environments] = await Promise.all([all("/v1/harnesses", "name"), all("/v1/environments", "name")]);
  add(clear(main), top("Harnesses", "The agents you have. Each is a package you own: write one here, import one, or bring in one an agent wrote in a session.",
    h("button", { class: "quiet", onclick: () => fromSession().catch(fail) }, "From a session"), h("button", { onclick: () => importArchive(found.map(each => each.name)) }, "Import"),
    h("button", { class: "primary", onclick: create }, icon("plus"), "New harness")),
    h("div", { class: "cards" }, found.map(each => h("a", { class: "card", href: page(each.name) },
      h("div", { class: "name" }, each.name, (drafts.get(`harnesses/${each.name}`) || {}).dirty && pill("Unsaved", "warn")),
      h("div", { class: "two muted small mono" }, `package ${each.digest.slice(0, 12)}`),
      h("div", { class: "chips" },
        carrying(environments, each.name).length ? carrying(environments, each.name).map(name => pill(name, "accent")) : pill("in no environment", "warn")))),
      drafts.has(`harnesses/${NEW}`) ? h("a", { class: "card add", href: page(NEW) }, "Continue the new harness")
        : h("button", { class: "card add", onclick: create }, icon("plus"), "New harness")));
  if (linked) {
    history.replaceState(null, "", "#/harnesses");               // the link is used once: the page is the list from here on
    await fromSession({ session: params[1], path: params.slice(2).join("/") });
  }
}
