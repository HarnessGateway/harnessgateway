// Harnesses (spec §20): what is installed and its install states; add one; set its defaults and settings.

import { call, del, get, patch, post } from "/api.js";
import { attempt, clear, field, h, put, status, when } from "/ui.js";

function card(harness, context, reload) {
  const problem = h("div", {});
  const provider = h("input", { value: harness.provider || "", placeholder: "none" });
  const model = h("input", { value: harness.model || "", placeholder: "none" });
  const declared = harness.manifest.settings || {};
  const secrets = harness.manifest.secrets || {};
  const settings = Object.fromEntries(Object.keys(declared).map((name) =>
    [name, h("input", { value: harness.settings[name] ?? "", placeholder: declared[name].default ?? "not set" })]));
  const secretInputs = Object.fromEntries(Object.keys(secrets).map((name) =>
    [name, h("input", { type: "password", placeholder: harness.secrets_set.includes(name) ? "set (leave empty to keep)" : "not set" })]));

  const save = () => attempt(problem, async () => {
    const changes = { provider: provider.value.trim() || null, model: model.value.trim() || null, settings: {} };
    for (const [name, input] of Object.entries(settings)) changes.settings[name] = input.value === "" ? null : input.value;
    const given = Object.entries(secretInputs).filter(([, input]) => input.value !== "");
    if (given.length) changes.secrets = Object.fromEntries(given.map(([name, input]) => [name, input.value]));
    await patch(`/v1/harnesses/${harness.name}`, changes);
    reload();
  });

  return h("div", { class: "card", id: `harness-${harness.name}` },
    h("div", { class: "row" },
      h("h3", {}, harness.name, " ", h("span", { class: "muted" }, harness.version)),
      h("button", { class: "fit danger", onclick: () => attempt(problem, async () => {
        if (!confirm(`Remove the harness ${harness.name}? Sessions keep their turns.`)) return;
        await del(`/v1/harnesses/${harness.name}`);
        reload();
      }) }, "Remove")),
    h("div", { class: "muted" }, harness.description),
    h("div", { class: "small muted" }, "Speaks: ", harness.manifest.apis.join(", ") || "no provider needed",
      harness.manifest.mind ? ` · keeps its mind in ~/${harness.manifest.mind}` : ""),
    harness.missing.length ? h("div", { class: "error" }, `Needs a value for: ${harness.missing.join(", ")}`) : null,
    h("div", { style: "margin: 8px 0" }, harness.installs.length ?
      harness.installs.map((install) => h("span", {}, status(install.status),
        h("span", { class: "small muted" }, ` image ${install.image_id.slice(7, 19)} · ${when(install.updated_at)}  `))) :
      h("span", { class: "muted small" }, "Not installed yet for any image.")),
    h("div", { class: "row" }, field("Default provider", provider), field("Default model", model)),
    Object.keys(settings).length ? h("div", { class: "row" }, Object.entries(settings).map(([name, input]) =>
      field(`${name} — ${declared[name].description || "setting"}`, input))) : null,
    context.about.role === "admin" && Object.keys(secretInputs).length ?
      h("div", { class: "row" }, Object.entries(secretInputs).map(([name, input]) =>
        field(`${name} (secret, goes only to ${secrets[name].hosts.join(", ")})`, input))) : null,
    problem,
    h("div", { class: "row" }, h("span", {}),
      h("button", { onclick: async () => {
        const installs = await get(`/v1/harnesses/${harness.name}/installs`);
        put(clear(problem), installs.data.map((install) =>
          h("div", {}, status(install.status), h("pre", {}, install.log || "(no output)"))));
      } }, "Install log"),
      h("button", { class: "primary", onclick: save }, "Save")));
}

export async function render(root, context) {
  const reload = () => { clear(root); render(root, context); };
  const [harnesses, catalog, environments] = await Promise.all([
    get("/v1/harnesses?limit=100"), get("/v1/catalog"), get("/v1/environments?limit=100")]);
  const problem = h("div", {});
  const pick = h("select", { id: "catalog" }, catalog.data.map((entry) =>
    h("option", { value: entry.name }, `${entry.name} ${entry.version}${entry.installed_version ? " (added)" : ""}`)));
  const file = h("input", { type: "file", accept: ".gz,.tgz" });
  const session = h("input", { placeholder: "sess_…" });
  const folder = h("input", { placeholder: "folder in its work" });
  const which = h("select", {}, harnesses.data.map((harness) => h("option", { value: harness.name }, harness.name)));
  const where = h("select", {}, environments.data.map((environment) => h("option", { value: environment.name }, environment.name)));
  const add = (action) => attempt(problem, async () => { await action(); reload(); });

  put(root,
    h("h1", {}, "Harnesses"),
    harnesses.data.length ? harnesses.data.map((harness) => card(harness, context, reload)) :
      h("div", { class: "notice" }, "No harness yet. Add one from the catalog below."),
    h("h2", {}, "Add a harness"), problem,
    h("div", { class: "card" },
      h("div", { class: "row" }, field("From the catalog", pick),
        h("button", { class: "primary", onclick: () => add(() => post("/v1/harnesses", { catalog: pick.value })) }, "Add")),
      h("div", { class: "row" }, field("Upload a package archive (.tar.gz)", file),
        h("button", { onclick: () => add(() => {
          if (!file.files[0]) throw new Error("Choose a package archive first.");
          const form = new FormData();
          form.append("package", file.files[0]);
          return call("POST", "/v1/harnesses", form);
        }) }, "Upload")),
      h("div", { class: "row" }, field("From a session's work folder", session), field("Folder", folder),
        h("button", { onclick: () => add(() => post("/v1/harnesses", { session: session.value.trim(), path: folder.value.trim() })) },
          "Add"))),
    harnesses.data.length ? h("div", {},
      h("h2", {}, "Install for another environment"),
      h("div", { class: "card row" }, field("Harness", which), field("Environment", where),
        h("button", { onclick: () => add(() => post(`/v1/harnesses/${which.value}/install`, { environment: where.value })) },
          "Install"))) : null);
}
