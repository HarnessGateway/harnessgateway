// The model line, as four choices: environment / harness / provider / model. Each offers only what can follow the
// one before it: the harnesses an environment carries, the providers that speak an API the harness speaks. A
// harness that calls no model has no provider and no model: none/none.
import { add, clear, h } from "../../lib/dom.js";
import { keep, kept } from "../../lib/storage.js";

const MODELS = "hg.models", REMEMBERED = 12;     // the models used with each provider lately, in this browser

export function modelLine({ environments, harnesses, providers }) {
  const environmentPick = h("select", { title: "Environment" }), harnessPick = h("select", { title: "Harness" }), providerPick = h("select", { title: "Provider" });
  const known = h("datalist", { id: "models-used" }), modelPick = h("input", { title: "Model", placeholder: "model", list: "models-used", spellcheck: "false", autocomplete: "off" });
  const calls = h("span", { style: "display:contents" }, "/", providerPick, "/", modelPick, known);
  const needs = h("a", { class: "needs", href: "#/providers" });       // in their place, when no provider can serve the harness
  const used = () => kept(MODELS, {}), lately = provider => [].concat(used()[provider] || []);

  const manifest = name => (harnesses.find(found => found.name === name) || { manifest: { apis: [] } }).manifest;
  const alone = () => !manifest(harnessPick.value).apis.length;
  const serving = name => providers.filter(found => manifest(name).apis.some(api => api in found.apis)).map(found => found.name);
  const offer = (select, names, wanted) => add(clear(select), names.map(name => h("option", { value: name, selected: name === wanted }, name)));
  const offerModels = model => {
    add(clear(known), lately(providerPick.value).map(name => h("option", { value: name })));
    modelPick.value = model || lately(providerPick.value)[0] || "";
  };
  const offerProviders = (wanted, model) => {
    const able = serving(harnessPick.value);
    offer(providerPick, able, wanted);
    calls.hidden = alone() || !able.length;
    needs.hidden = alone() || able.length > 0;
    needs.textContent = alone() ? "" : `Add a provider that speaks ${manifest(harnessPick.value).apis.join(" or ")}`;
    offerModels(model);
  };
  const offerHarnesses = (wanted, ...rest) => {
    const carried = (environments.find(found => found.name === environmentPick.value) || { harnesses: [] }).harnesses;
    const runs = name => !manifest(name).apis.length || serving(name).length > 0;
    offer(harnessPick, carried, carried.includes(wanted) ? wanted : carried.find(runs));       // unasked: one that can run now
    offerProviders(...rest);
  };
  environmentPick.onchange = () => offerHarnesses(harnessPick.value, providerPick.value);
  harnessPick.onchange = () => offerProviders(providerPick.value);
  providerPick.onchange = () => offerModels();

  return {
    node: h("div", { class: "line-bar" }, environmentPick, "/", harnessPick, calls, needs),
    get harness() { return harnessPick.value; },
    /** The line as a request names it. */
    value: () => [environmentPick.value, harnessPick.value, alone() ? "none" : providerPick.value, alone() ? "none" : modelPick.value.trim()].join("/"),
    /** Why a message cannot be sent on this line, or nothing. */
    wrong: () => !needs.hidden ? `${needs.textContent}, under Providers.` : !alone() && !modelPick.value.trim() ? "Name the model: any model the provider serves." : "",
    set(line) {
      const [environment, harness, provider, ...rest] = String(line || "").split("/"), model = rest.join("/");
      offer(environmentPick, environments.map(found => found.name), environment);
      offerHarnesses(harness, provider, model === "none" ? "" : model);
    },
    /** Keep the model that was just used with its provider, to be offered again. */
    remember() {
      if (alone()) return;
      const model = modelPick.value.trim();
      keep(MODELS, { ...used(), [providerPick.value]: [model, ...lately(providerPick.value).filter(name => name !== model)].slice(0, REMEMBERED) });
    },
  };
}
