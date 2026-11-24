// Providers: a name, the API shape it speaks at a base URL, and a key. A provider may speak more than one shape
// with the same key. The key is write-only: it is sent when typed, and kept when left empty.
import { confirm, dialog } from "../components/feedback.js";
import { field, mono, pill, top } from "../components/layout.js";
import { all, del, put } from "../lib/api.js";
import { add, clear, h, icon } from "../lib/dom.js";
import { again } from "../lib/route.js";

// The API shapes a harness may speak, each with where it is usually served.
const SHAPES = { "anthropic": "https://api.anthropic.com", "openai-responses": "https://api.openai.com/v1",
                 "openai-chat": "https://api.openai.com/v1", "gemini": "https://generativelanguage.googleapis.com" };

function edit(provider) {
  const apis = h("div", {});
  const row = (shape, url) => {
    const address = h("input", { "data-url": true, value: url, required: true, placeholder: "https://…" });
    const select = h("select", { "data-shape": true, onchange: () => { address.value = SHAPES[select.value]; } },   // the shape's usual address
      Object.keys(SHAPES).map(name => h("option", { value: name, selected: name === shape }, name)));
    const node = h("div", { class: "rows" }, h("div", { class: "row" }, select, address,
      h("button", { type: "button", class: "quiet square danger", title: "Remove", onclick: () => { if (apis.children.length > 1) node.remove(); } }, icon("close"))));
    return node;
  };
  add(apis, Object.entries(provider ? provider.apis : { "openai-responses": SHAPES["openai-responses"] }).map(([shape, url]) => row(shape, url)));
  const unused = () => Object.keys(SHAPES).find(name => ![...apis.querySelectorAll("[data-shape]")].some(select => select.value === name));
  dialog(provider ? `Provider ${provider.name}` : "New provider", [
    field("Name", h("input", { name: "name", value: provider ? provider.name : "", readOnly: Boolean(provider), required: true, placeholder: "anthropic",
                               pattern: "[a-z0-9][a-z0-9\\-]*", autocomplete: "off" }), "As a request names it: environment/harness/provider/model"),
    h("label", {}, "API shape and base URL"), apis,
    h("button", { type: "button", class: "link", onclick: () => { const shape = unused(); if (shape) apis.append(row(shape, SHAPES[shape])); } },
      "This provider speaks another API with the same key"),
    field("API key", h("input", { name: "key", type: "password", autocomplete: "off",
                                  placeholder: provider && provider.secrets_set.length ? "Set. Leave empty to keep it" : "sk-…" }),
      "Sealed at rest. It never enters a sandbox: the egress proxy puts it into requests to this provider."),
  ], provider ? "Save" : "Add provider", async form => {
    const sent = { apis: Object.fromEntries([...apis.children].map(node => [node.querySelector("[data-shape]").value, node.querySelector("[data-url]").value.trim()])) };
    if (form.elements.key.value) sent.key = form.elements.key.value;
    await put(`/v1/providers/${encodeURIComponent(form.elements.name.value.trim())}`, sent);
    again();
  });
}

export async function providers(main) {
  const found = await all("/v1/providers", "name");
  add(clear(main), top("Providers", "Where models are served. A request names one in its model line, and the harness speaks to it through the egress proxy.",
    h("button", { class: "primary", onclick: () => edit(null) }, icon("plus"), "New provider")),
    h("div", { class: "cards" }, found.map(provider => h("div", { class: "card" },
      h("div", { class: "name" }, provider.name, provider.secrets_set.length ? pill("key set", "ok") : pill("no key", "warn")),
      Object.entries(provider.apis).map(([shape, url]) => h("div", { class: "line", title: url }, pill(shape), " ", mono(url))),
      h("div", { class: "chips acts" }, h("button", { onclick: () => edit(provider) }, "Edit"),
        h("button", { class: "quiet danger", onclick: () => confirm(`Delete provider ${provider.name}? Requests that name it are refused from then on.`,
          async () => { await del(`/v1/providers/${encodeURIComponent(provider.name)}`); again(); }) }, "Delete")))),
      h("button", { class: "card add", onclick: () => edit(null) }, icon("plus"), "New provider")));
}
