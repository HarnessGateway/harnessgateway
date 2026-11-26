// API keys, wherever they are listed: the dialog that makes one, the one time it is shown, and the rows of a list.
// A key acts as someone, and never does more than they may; it may be limited to some actions, and given a last day.
// A row says whose a key is, who made it, and how the key itself ends: by that, someone who finds one knows which.
import { del, post } from "../lib/api.js";
import { h, icon } from "../lib/dom.js";
import { copyText } from "../lib/files.js";
import { ago, lines } from "../lib/format.js";
import { named } from "../lib/names.js";
import { again } from "../lib/route.js";
import { confirm, dialog, toast } from "./feedback.js";
import { field, mono, pill } from "./layout.js";

const ROW = "display: grid; grid-template-columns: 1.3fr 1fr 1.2fr 100px 1.2fr 90px 80px; gap: 14px; align-items: center; padding: 12px 18px; border-top: 1px solid var(--line);";
const day = time => new Date(time).toLocaleDateString(undefined, { dateStyle: "medium", timeZone: "UTC" });

/** Ask for a new key and make it at `path`. `whose` says who it acts as; `made` is given the answer, which holds the key itself, once. */
export function newKey(path, whose, made) {
  dialog("New API key", [
    h("p", { class: "muted" }, `${whose} It is shown once: copy it then.`),
    field("Name", h("input", { name: "name", required: true, maxlength: 200, placeholder: "my app", autocomplete: "off" }), "What the key is for"),
    field("Last day", h("input", { name: "ends", type: "date" }), "It is refused from the end of that day (UTC). Empty: it does not end"),
    field("Limited to", h("textarea", { name: "scopes", rows: 3, placeholder: "responses.*\nsessions.read" }),
      "One pattern a line, noun.verb, with * for either part. Empty: everything whoever it acts as may do. A key that is limited, or ends, makes no key"),
  ], "Create key", async form => {
    const ends = form.elements.ends.value, scopes = lines(form.elements.scopes.value);
    made(await post(path, { name: form.elements.name.value.trim(), ...(ends && { expires_at: `${ends}T23:59:59Z` }), ...(scopes.length && { scopes }) }));
  });
}

/** A key that was just made: the one time the key itself is shown. */
export const shownOnce = made => h("div", { id: "new-key", class: "panel", style: "margin-bottom: 14px;" },
  h("div", { class: "muted small" }, `${made.name}: copy it now. It is not shown again.`),
  h("div", { class: "acts", style: "margin-top: 8px;" }, mono(made.key), h("button", { onclick: () => copyText(made.key).then(() => toast("Copied")) }, icon("copy"), "Copy")));

/** Keys as rows, each revoked at `path(key)`. `known` is who is here, by id (`names()`): whose each key is, and who made it. */
export function keyRows(keys, path, known) {
  const ended = key => key.expires_at && Date.parse(key.expires_at) <= Date.now();
  return h("div", { class: "panel", style: "padding: 0; overflow: hidden;" },
    h("div", { class: "muted small", style: `${ROW} border-top: 0; font-weight: 600;` }, ["Key", "Acts as", "Limited to", "Last day", "Created", "Last used", ""].map(title => h("span", {}, title))),
    keys.map(key => h("div", { style: ROW },
      h("div", { style: "min-width: 0;" }, h("div", {}, key.name, " ", ended(key) && pill("ended", "warn")),
        h("div", { class: "muted small mono", title: key.id }, key.hint ? `hg_…${key.hint}` : key.id)),
      h("span", { class: "small", title: key.subject }, named(known, key.subject)),
      h("span", { class: "muted small" }, key.scopes.length ? key.scopes.join(", ") : "Not limited"),
      h("span", { class: "muted small" }, key.expires_at ? day(key.expires_at) : "Does not end"),
      h("span", { class: "muted small", title: key.created_by || "" }, ago(key.created_at), key.created_by && ` by ${named(known, key.created_by)}`),
      h("span", { class: "muted small" }, key.last_used_at ? ago(key.last_used_at) : "Never"),
      h("div", { class: "acts" }, h("button", { class: "quiet danger", onclick: () => confirm(`Revoke ${key.name}? Whatever signs in with it is refused from then on.`,
        async () => { await del(path(key)); again(); }, "Revoke") }, "Revoke")))));
}
