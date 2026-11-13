// Keys (spec §20): create and revoke API keys.

import { del, get, post } from "/api.js";
import { attempt, clear, field, h, pill, put, table, when } from "/ui.js";

export async function render(root) {
  const reload = () => { clear(root); render(root); };
  const keys = await get("/v1/keys");
  const problem = h("div", {});
  const made = h("div", {});
  const name = h("input", { placeholder: "what it is for" });
  const role = h("select", {}, ["runner", "developer", "admin"].map((value) => h("option", { value }, value)));
  put(root, h("h1", {}, "Keys"),
    h("p", { class: "muted" }, "runner: run and read turns, for keys embedded in applications. developer: also manage " +
      "harnesses and read sessions and usage. admin: everything."),
    table([
      { title: "Name", cell: (key) => key.name },
      { title: "Role", cell: (key) => pill(key.role) },
      { title: "Created", cell: (key) => when(key.created_at) },
      { title: "Last used", cell: (key) => when(key.last_used_at) },
      { title: "", cell: (key) => key.revoked_at ? pill("revoked", "bad") :
        h("button", { class: "danger", onclick: () => attempt(problem, async () => {
          if (!confirm(`Revoke the key "${key.name}"? Whatever uses it stops working.`)) return;
          await del(`/v1/keys/${key.id}`);
          reload();
        }) }, "Revoke") },
    ], keys.data),
    problem,
    h("h2", {}, "Create a key"),
    h("div", { class: "card" },
      h("div", { class: "row" }, field("Name", name), field("Role", role),
        h("button", { class: "primary", onclick: () => attempt(made, async () => {
          const key = await post("/v1/keys", { name: name.value.trim(), role: role.value });
          put(made, h("div", { class: "notice" }, "Copy this key now. It is not shown again: ",
            h("code", { id: "new-key" }, key.key)));
        }) }, "Create")),
      made));
}
