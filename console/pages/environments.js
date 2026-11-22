// Environments: a card for each, and an editor with a tab for each part. Edits are a working copy across all the
// tabs, saved once. A new environment, or one read from a file, is a working copy too: every tab is there before
// it exists. Out of the console and back in, an environment is one JSON file.
import { all, del, get, put } from "./api.js";
import { NEW, again, code, drafts, frame, rows } from "./edit.js";
import { add, clear, confirm, download, fail, field, h, icon, lines, mono, pickFile, pill, toast, top } from "./ui.js";

const TABS = ["General", "Resources", "Network", "Variables", "Secrets", "Scripts", "Harnesses"];
const BLANK = { image: "", harnesses: [], cpus: 2, memory_mb: 4096, pids: 512, tmp_mb: 1024, disk_mb: 10240, timeout_s: 1800,
                network: { mode: "limited", allow: [], deny: [], package_managers: true }, variables: {}, secrets: {}, scripts: { startup: "" } };
const at = name => `/v1/environments/${encodeURIComponent(name)}`;
const page = (name, tab = "") => `#/environments/${encodeURIComponent(name)}${tab && `/${tab.toLowerCase()}`}`;
const copy = value => JSON.parse(JSON.stringify(value));
/** An environment's definition, and nothing else of what the API or a file holds. */
export const definition = shown => copy(Object.fromEntries(Object.keys(BLANK).map(name => [name, shown[name] ?? BLANK[name]])));
/** A definition as it is sent: each list of hosts only where the network's mode gives it a meaning. */
const sent = working => ({ ...working, network: { ...working.network, allow: working.network.mode === "limited" ? working.network.allow : [],
                                                  deny: working.network.mode === "closed" ? [] : working.network.deny } });

async function draftOf(name) {
  const key = `environments/${name}`;
  if (name !== NEW && !(drafts.get(key) || {}).dirty) drafts.delete(key);      // nothing unsaved: show what is there now
  if (!drafts.has(key)) {
    const shown = name === NEW ? { ...BLANK, secrets_set: [] } : await get(at(name));
    drafts.set(key, { name: name === NEW ? "" : name, original: definition(shown), working: definition(shown), kept: shown.secrets_set, dirty: false });
  }
  return drafts.get(key);
}

const PARTS = {
  general: (draft, { isNew, changed }) => h("div", { class: "cols" },
    field("Name", h("input", { value: draft.name, readOnly: !isNew, placeholder: "dev", autocomplete: "off",
                               oninput: event => { draft.name = event.target.value.trim(); changed(); } }), "The first part of a model line: environment/harness/provider/model"),
    field("Image", h("input", { value: draft.working.image, placeholder: "node:22", autocomplete: "off",
                                oninput: event => { draft.working.image = event.target.value.trim(); changed(); } }), "Any image this host can pull. Every sandbox of the environment starts from it")),

  resources: ({ working }, { changed }) => {
    const number = (name, step = 1) => h("input", { type: "number", step, value: working[name], oninput: event => { working[name] = Number(event.target.value); changed(); } });
    return [h("p", { class: "muted" }, "What one sandbox may use, and how long one turn may take."), h("div", { class: "cols" },
      field("CPUs", number("cpus", 0.25)), field("Memory (MB)", number("memory_mb")), field("Processes", number("pids")),
      field("Disk (MB)", number("disk_mb"), "The most a session's files may hold"), field("/tmp (MB)", number("tmp_mb"), "In memory: it counts against the memory"),
      field("Turn time limit (s)", number("timeout_s")))];
  },

  network: ({ working }, { changed }) => {
    const network = working.network, allowed = h("div", {}), denied = h("div", {});
    const show = () => { allowed.hidden = network.mode !== "limited"; denied.hidden = network.mode === "closed"; };
    const modes = [["closed", "Closed", "The turn's provider, and nothing else"], ["limited", "Limited", "The provider, and what is allowed below"],
                   ["open", "Open", "Any public host"]].map(([mode, label, says]) => {
      const option = h("label", { class: `option${network.mode === mode ? " on" : ""}` },
        h("input", { type: "radio", name: "mode", checked: network.mode === mode, onchange: () => {
          network.mode = mode;
          for (const other of option.parentNode.children) other.classList.toggle("on", other === option);
          show(); changed();
        } }), h("div", { class: "grow" }, h("b", {}, label), h("div", { class: "muted small" }, says)));
      return option;
    });
    add(allowed, field("Allowed hosts", h("textarea", { placeholder: "api.github.com\n*.example.com", oninput: event => { network.allow = lines(event.target.value); changed(); } },
      network.allow.join("\n")), "One a line: host, *.suffix, or host:port"),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: network.package_managers, onchange: event => { network.package_managers = event.target.checked; changed(); } }),
        "Allow package managers (npm, PyPI, GitHub, …)"));
    add(denied, field("Denied hosts", h("textarea", { placeholder: "bad.example.com", oninput: event => { network.deny = lines(event.target.value); changed(); } },
      network.deny.join("\n")), "Refused whatever else allows them"));
    show();
    return [h("p", { class: "muted" }, "What a sandbox may reach. Everything goes through the egress proxy, and private addresses are never reachable."),
      h("div", {}, modes), h("div", { class: "cols" }, allowed, denied)];
  },

  variables: ({ working }, { changed }) => [
    h("p", { class: "muted" }, "Given to every sandbox of the environment. They are visible inside it: a value that must not be belongs under Secrets."),
    rows([{ key: "name", title: "Name", placeholder: "TEAM" }, { key: "value", title: "Value", placeholder: "payments" }],
      Object.entries(working.variables).map(([name, value]) => ({ name, value })),
      list => { working.variables = Object.fromEntries(list.filter(row => (row.name || "").trim()).map(row => [row.name.trim(), row.value || ""])); changed(); }, "Add variable")],

  secrets: (draft, { changed }) => [
    h("p", { class: "muted" }, "A secret's value never enters a sandbox. The sandbox holds a placeholder, and the egress proxy puts the value into requests to the hosts named here, and nowhere else. With a header, it is set as that header on every request to those hosts."),
    rows([{ key: "name", title: "Name", placeholder: "GITHUB_TOKEN", fixed: true }, { key: "hosts", title: "Hosts it may go to", placeholder: "api.github.com" },
          { key: "header", title: "Header (optional)", placeholder: "Authorization" }, { key: "prefix", title: "Prefix", placeholder: "Bearer " },
          { key: "value", title: "Value", type: "password", placeholder: row => row.kept ? "Set. Leave empty to keep it" : "Write-only" }],
      Object.entries(draft.working.secrets).map(([name, secret]) => ({ name, hosts: secret.hosts.join(", "), header: secret.header || "", prefix: secret.prefix || "",
                                                                         value: secret.value || "", kept: draft.kept.includes(name) })),
      list => {
        draft.working.secrets = Object.fromEntries(list.filter(row => (row.name || "").trim()).map(row => [row.name.trim(), {
          hosts: lines(row.hosts), ...(row.header ? { header: row.header.trim(), prefix: row.prefix || "" } : {}), ...(row.value ? { value: row.value } : {}) }]));
        changed();
      }, "Add secret")],

  scripts: ({ working }, { changed }) => [
    h("p", { class: "muted" }, "The startup script runs in the sandbox before every turn, once the session's files are in place and before the harness starts, the way user data runs when a machine starts. It runs as the sandbox's user, in /work. What it prints goes to the turn's log, and if it fails the turn fails."),
    code({ value: working.scripts.startup, onInput: text => { working.scripts.startup = text; changed(); },
           placeholder: "#!/bin/sh\n# runs before every turn: write it so that running it again is safe\n[ -d repo ] || git clone https://github.com/acme/repo.git repo" }),
    h("div", { class: "hint" }, "Its first line may name its interpreter. The sandbox's root is read-only: software that needs root to install belongs in the image.")],

  harnesses: (draft, { changed, harnesses }) => [
    h("p", { class: "muted" }, "The harnesses this environment carries. A request may name only these with it; each is installed on the image the first time one does."),
    harnesses.length ? harnesses.map(harness => {
      const carried = draft.working.harnesses.includes(harness.name);
      const option = h("label", { class: `option${carried ? " on" : ""}` },
        h("input", { type: "checkbox", checked: carried, onchange: event => {
          draft.working.harnesses = event.target.checked ? [...draft.working.harnesses, harness.name].sort() : draft.working.harnesses.filter(name => name !== harness.name);
          option.classList.toggle("on", event.target.checked); changed();
        } }),
        h("div", { class: "grow" }, h("b", {}, harness.name), " ", mono(harness.manifest.version), h("div", { class: "muted small" }, harness.manifest.description.slice(0, 120))),
        mono(`${draft.name || "…"}/${harness.name}/${harness.manifest.apis.length ? "provider/model" : "none/none"}`));
      return option;
    }) : h("div", { class: "empty" }, "No harness yet. Add one under Harnesses, then tick it here.")],
};

async function environment(main, [name, tab = "general"]) {
  const isNew = name === NEW, key = `environments/${name}`;
  const [draft, harnesses] = await Promise.all([draftOf(name), all("/v1/harnesses", "name")]);
  const working = draft.working;
  const counts = { variables: Object.keys(working.variables).length, secrets: Object.keys(working.secrets).length,
                   harnesses: working.harnesses.length, scripts: working.scripts.startup.trim() ? 1 : 0 };
  const view = frame(main, {
    back: "#/environments", backLabel: "Environments", title: draft.name || "New environment", isNew,
    subtitle: isNew ? "Every part has a tab. Nothing exists until you create it." : `${working.image} · network ${working.network.mode}`,
    tabs: TABS.map(label => [label.toLowerCase(), label, counts[label.toLowerCase()]]), tab, href: id => page(name, id),
    onSave: async () => {
      try {
        if (!draft.name) throw new Error("Name the environment, on the General tab.");
        await put(at(draft.name), sent(working));
        drafts.delete(key);
        toast(isNew ? `Created ${draft.name}` : "Saved");
        isNew ? location.hash = page(draft.name) : again();
      } catch (error) { fail(error); }
    },
    onDiscard: () => { drafts.delete(key); isNew ? location.hash = "#/environments" : again(); },
    more: [["Export", () => download(`${draft.name || "environment"}.environment.json`, JSON.stringify({ name: draft.name, ...exported(working) }, null, 2) + "\n", "application/json")],
           !isNew && ["Delete", () => confirm(`Delete environment ${name}? Requests that name it are refused from then on.`,
             async () => { await del(at(name)); drafts.delete(key); location.hash = "#/environments"; }), true]].filter(Boolean),
  });
  const changed = () => { draft.dirty = isNew || JSON.stringify(working) !== JSON.stringify(draft.original); view.changed(draft.dirty); };
  view.body.append(h("div", { class: "panel" }, (PARTS[tab] || PARTS.general)(draft, { isNew, changed, harnesses })));
  view.changed(draft.dirty);
}

/** An environment as a file holds it: its definition, with no secret's value. */
const exported = working => ({ ...copy(working), secrets: Object.fromEntries(Object.entries(working.secrets).map(([name, { value, ...where }]) => [name, where])) });

async function importFile() {
  const file = await pickFile(".json,application/json");
  try {
    const read = JSON.parse(await file.text());
    if (!read || typeof read !== "object" || Array.isArray(read)) throw new Error("it is not an object");
    drafts.set(`environments/${NEW}`, { name: String(read.name || file.name.replace(/(\.environment)?\.json$/, "")), original: copy(BLANK),
                                         working: definition(read), kept: [], dirty: true });
    location.hash = page(NEW);
    toast("Imported. Look it over, give its secrets their values, and create it");
  } catch (error) { toast(`${file.name} is not an environment: ${error.message}`, true); }
}

export async function environments(main, params) {
  if (params[0]) return environment(main, params);
  const found = await all("/v1/environments", "name");
  const many = (count, what) => count > 0 && pill(`${count} ${what}${count === 1 ? "" : "s"}`);
  add(clear(main), top("Environments", "Where turns run: an image, limits, what a sandbox may reach and is given, a script that runs when each one starts, and the harnesses it carries.",
    h("button", { onclick: importFile }, "Import"), h("a", { href: page(NEW) }, h("button", { class: "primary" }, icon("plus"), "New environment"))),
    h("div", { class: "cards" }, found.map(e => h("a", { class: "card", href: page(e.name) },
      h("div", { class: "name" }, e.name, (drafts.get(`environments/${e.name}`) || {}).dirty && pill("Unsaved", "warn")),
      h("div", { class: "line mono" }, e.image),
      h("div", { class: "line" }, `${e.cpus} CPU · ${e.memory_mb} MB · ${e.timeout_s} s a turn · network ${e.network.mode}`),
      h("div", { class: "chips" }, e.harnesses.length ? e.harnesses.map(name => pill(name, "accent")) : pill("no harness yet", "warn"),
        e.scripts.startup.trim() && pill("startup script"), many(Object.keys(e.variables).length, "variable"), many(Object.keys(e.secrets).length, "secret")))),
      h("a", { class: "card add", href: page(NEW) }, icon("plus"), drafts.has(`environments/${NEW}`) ? "Continue the new environment" : "New environment")));
}
