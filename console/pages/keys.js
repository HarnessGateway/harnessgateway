// API keys: the keys of the person signed in (everyone's, for whoever may read those). A key acts as whoever made
// it. The key itself is shown once, when it is made; after that a row shows how it ends.
import { keyRows, newKey, shownOnce } from "../components/keys.js";
import { top } from "../components/layout.js";
import { all } from "../lib/api.js";
import { add, clear, h, icon } from "../lib/dom.js";
import { names } from "../lib/names.js";
import { again } from "../lib/route.js";

let made = null;                                 // the key just made, until the page has shown it once

export async function keys(main) {
  const [every, known] = await Promise.all([all("/v1/keys"), names()]);
  const found = every.filter(key => !key.revoked_at).reverse(), shown = made;      // newest first
  made = null;
  add(clear(main), top("API keys", "What a program signs in with: send one as Authorization: Bearer hg_… to the API. A key acts as whoever made it.",
    h("button", { class: "primary", onclick: () => newKey("/v1/keys", "A key acts as you, and does only what you may do.", key => { made = key; again(); }) }, icon("plus"), "New key")),
    shown && shownOnce(shown),
    found.length ? keyRows(found, key => `/v1/keys/${key.id}`, known) : h("div", { class: "empty" }, "No key yet."));
}
