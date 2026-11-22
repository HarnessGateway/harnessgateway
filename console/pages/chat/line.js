// The model line, as four choices: environment / harness / provider / model. An environment offers the harnesses it
// carries; a provider is any the organization has, or none, for a harness that calls no model. Whether a harness
// can use a provider is the harness's own to say when it runs, so each harness is offered what it last ran with.
import { add, clear, h } from "../../lib/dom.js";
import { keep, kept } from "../../lib/storage.js";

const MODELS = "hg.models", REMEMBERED = 12;     // the models used with each provider lately, in this browser
const WITH = "hg.providers";                     // and the provider each harness was last used with
const NONE = "none";

export function modelLine({ environments, providers }) {
  const environmentPick = h("select", { title: "Environment" }), harnessPick = h("select", { title: "Harness" }), providerPick = h("select", { title: "Provider" });
  const known = h("datalist", { id: "models-used" }), modelPick = h("input", { title: "Model", placeholder: "model", list: "models-used", spellcheck: "false", autocomplete: "off" });
  const model = h("span", { style: "display:contents" }, "/", modelPick, known);
  const used = () => kept(MODELS, {}), lately = provider => [].concat(used()[provider] || []);
  const alone = () => providerPick.value === NONE;

  const offer = (select, names, wanted) => add(clear(select), names.map(name => h("option", { value: name, selected: name === wanted }, name)));
  const offerModels = wanted => {
    model.hidden = alone();
    add(clear(known), lately(providerPick.value).map(name => h("option", { value: name })));
    modelPick.value = wanted || lately(providerPick.value)[0] || "";
  };
  const offerProviders = (wanted, ...rest) => {
    const names = [...providers.map(found => found.name), NONE];
    offer(providerPick, names, names.includes(wanted) ? wanted : kept(WITH, {})[harnessPick.value]);
    offerModels(...rest);
  };
  const offerHarnesses = (wanted, ...rest) => {
    offer(harnessPick, (environments.find(found => found.name === environmentPick.value) || { harnesses: [] }).harnesses, wanted);
    offerProviders(...rest);
  };
  environmentPick.onchange = () => offerHarnesses(harnessPick.value, providerPick.value);
  harnessPick.onchange = () => offerProviders();
  providerPick.onchange = () => offerModels();

  return {
    node: h("div", { class: "line-bar" }, environmentPick, "/", harnessPick, "/", providerPick, model),
    get harness() { return harnessPick.value; },
    /** The line as a request names it. */
    value: () => [environmentPick.value, harnessPick.value, providerPick.value, alone() ? NONE : modelPick.value.trim()].join("/"),
    /** Why a message cannot be sent on this line, or nothing. */
    wrong: () => !alone() && !modelPick.value.trim() ? "Name the model: any model the provider serves." : "",
    set(line) {
      const [environment, harness, provider, ...rest] = String(line || "").split("/"), wanted = rest.join("/");
      offer(environmentPick, environments.map(found => found.name), environment);
      offerHarnesses(harness, provider, wanted === NONE ? "" : wanted);
    },
    /** Keep what was just used, to be offered again: the provider with this harness, and the model with it. */
    remember() {
      keep(WITH, { ...kept(WITH, {}), [harnessPick.value]: providerPick.value });
      if (alone()) return;
      const name = modelPick.value.trim();
      keep(MODELS, { ...used(), [providerPick.value]: [name, ...lately(providerPick.value).filter(other => other !== name)].slice(0, REMEMBERED) });
    },
  };
}
