// The small pieces every page is laid out with: a page's heading, a labelled field, a pill, a status's tone.
import { h } from "../lib/dom.js";

export const pill = (text, tone = "") => h("span", { class: `pill ${tone}` }, text);
export const mono = text => h("span", { class: "mono" }, text);
export const field = (label, control, hint) => h("div", {}, h("label", {}, label), control, hint && h("div", { class: "hint" }, hint));
export const top = (title, lead, ...acts) => h("div", { class: "top" }, h("div", {}, h("h1", {}, title), h("p", {}, lead)), h("div", { class: "acts" }, acts));
export const STATUS = { completed: "ok", failed: "bad", cancelled: "warn", incomplete: "warn", in_progress: "accent", queued: "" };
