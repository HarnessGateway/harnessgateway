// A deployment's own page module, as the browser tests load it (HG_CONSOLE_PAGES): one page in each console, built
// from the console's own helpers.
import { api, esc } from "/static/api.js";
import { top } from "/static/ui.js";

export const pages = {
  notes: { title: "Notes", async render(el, params, me) {
    const mine = await api("GET", "/v1/auth/me");                        // the relay, as the signed-in person
    el.innerHTML = top("Notes", "A page of this deployment's own.") + `<div class="card" id="note">${esc(me.account.email)} · ${esc(mine.role)}</div>`;
  } },
};

export const nav = (me, app) => app === "ops"
  ? [{ mod: "Deployment", items: [{ id: "notes", label: "Notes", icon: "file" }] }]         // a group of its own
  : [{ mod: "Observability", items: [{ id: "notes", label: "Notes", icon: "file" }] }];     // joins the console's group
