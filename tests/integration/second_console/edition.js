// What the second extension adds to the console, beside the sample edition's: a page of its own, and one whose
// name is taken, which the console refuses.
import { top } from "../../components/layout.js";
import { get } from "../../lib/api.js";
import { add, clear, h } from "../../lib/dom.js";

async function notes(main) {
  add(clear(main), top("Notes", "What the second extension heard."),
    h("div", { class: "panel" }, (await get("/v1/notes")).data.map(said => h("div", { class: "mono" }, said))));
}

export const pages = [
  { group: "Activity", id: "notes", label: "Notes", icon: "M5 4h14v16H5z", page: notes },
  { group: "Activity", id: "audit", label: "Taken", icon: "M5 4h14v16H5z", page: notes },
];
