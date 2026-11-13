// Environments (spec §20): create and edit where turns run.

import { del, get, patch, post } from "/api.js";
import { attempt, clear, h, parseJson, put } from "/ui.js";

const STARTER = {
  name: "", cpus: 2, memory_mb: 4096, pids: 512, tmp_mb: 1024, disk_mb: 10240, turn_timeout_s: 1800,
  network: { mode: "limited", allow: [], deny: [], package_managers: true }, variables: {}, secrets: {},
};

function editor(environment, reload, isNew) {
  const problem = h("div", {});
  const text = h("textarea", { style: "min-height: 260px", value: JSON.stringify(environment, null, 2) });
  const save = () => attempt(problem, async () => {
    const body = parseJson(text.value, "The definition");
    if (isNew) await post("/v1/environments", body);
    else await patch(`/v1/environments/${environment.name}`, body);
    reload();
  });
  return h("div", { class: "card" },
    h("div", { class: "row" },
      h("h3", {}, isNew ? "New environment" : environment.name),
      !isNew && environment.name !== "default" ? h("button", { class: "fit danger", onclick: () => attempt(problem, async () => {
        if (!confirm(`Delete the environment ${environment.name}?`)) return;
        await del(`/v1/environments/${environment.name}`);
        reload();
      }) }, "Delete") : null),
    isNew ? null : h("div", { class: "small muted" },
      `${environment.cpus} CPU · ${environment.memory_mb} MB memory · ${environment.disk_mb} MB disk · ` +
      `${environment.turn_timeout_s} s per turn · network ${environment.network.mode} · image ${environment.image}`),
    text, problem,
    h("div", { class: "row" },
      h("span", { class: "small muted" }, "A secret's value is written as \"value\" and is never shown again. " +
        "Leave it out to keep the one it has."),
      h("button", { class: "fit primary", onclick: save }, isNew ? "Create" : "Save")));
}

export async function render(root) {
  const reload = () => { clear(root); render(root); };
  const environments = await get("/v1/environments?limit=100");
  put(root, h("h1", {}, "Environments"),
    h("p", { class: "muted" }, "An environment is where turns run: the image, the limits, what the sandbox may reach, " +
      "and the variables and secrets it is given. Every turn gets a new sandbox built from it."),
    environments.data.map((environment) => editor(environment, reload, false)),
    h("h2", {}, "Add an environment"), editor(STARTER, reload, true));
}
