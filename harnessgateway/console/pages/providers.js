// Providers (spec §20): the model endpoints harnesses think with, and their keys.

import { del, get, patch, post } from "/api.js";
import { attempt, clear, field, h, pill, put } from "/ui.js";

const APIS = [["anthropic", "Anthropic Messages (base URL without /v1)"],
  ["openai-responses", "OpenAI Responses (base URL ending /v1)"], ["openai-chat", "OpenAI Chat Completions (base URL ending /v1)"]];

function form(provider, reload) {
  const isNew = !provider;
  const problem = h("div", {});
  const name = h("input", { value: provider?.name || "", placeholder: "deepseek", disabled: !isNew });
  const urls = Object.fromEntries(APIS.map(([api]) => [api, h("input", { value: provider?.apis[api] || "", placeholder: "not offered" })]));
  const model = h("input", { value: provider?.default_model || "", placeholder: "none" });
  const key = h("input", { type: "password", placeholder: provider?.has_key ? "set (leave empty to keep)" : "none" });
  const save = () => attempt(problem, async () => {
    const apis = Object.fromEntries(Object.entries(urls).filter(([, input]) => input.value.trim())
      .map(([api, input]) => [api, input.value.trim()]));
    const body = { apis, default_model: model.value.trim() || null };
    if (key.value) body.api_key = key.value;
    if (isNew) await post("/v1/providers", { name: name.value.trim(), ...body });
    else await patch(`/v1/providers/${provider.name}`, body);
    reload();
  });
  return h("div", { class: "card" },
    h("div", { class: "row" },
      h("h3", {}, isNew ? "New provider" : provider.name, " ",
        isNew ? null : pill(provider.has_key ? "key set" : "no key", provider.has_key ? "good" : "warn")),
      isNew ? null : h("button", { class: "fit danger", onclick: () => attempt(problem, async () => {
        if (!confirm(`Delete the provider ${provider.name}?`)) return;
        await del(`/v1/providers/${provider.name}`);
        reload();
      }) }, "Delete")),
    isNew ? field("Name", name) : null,
    APIS.map(([api, label]) => field(label, urls[api])),
    h("div", { class: "row" }, field("Default model", model), field("API key (never shown again)", key)),
    problem,
    h("div", { class: "row" }, h("span", {}), h("button", { class: "primary", onclick: save }, isNew ? "Add" : "Save")));
}

export async function render(root, context) {
  const reload = () => { clear(root); render(root, context); };
  const providers = await get("/v1/providers?limit=100");
  const admin = context.about.role === "admin";
  put(root, h("h1", {}, "Providers"),
    h("p", { class: "muted" }, "A provider is a model endpoint. One provider can offer several APIs, so different " +
      "harnesses can use the same key and model. The key is put into requests outside the sandbox; agents never hold it."),
    providers.data.length ? providers.data.map((provider) => admin ? form(provider, reload) :
      h("div", { class: "card" }, h("h3", {}, provider.name), h("pre", {}, JSON.stringify(provider.apis, null, 2)))) :
      h("div", { class: "notice" }, "No provider yet."),
    admin ? [h("h2", {}, "Add a provider"), form(null, reload)] :
      h("p", { class: "muted" }, "Adding and changing providers needs an admin key."));
}
