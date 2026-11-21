// The sample edition's page in the console: the audit log its own route serves.
import { get } from "./api.js";
import { add, clear, h, top } from "./ui.js";

async function audit(main) {
  const rows = (await get("/v1/audit")).data;
  add(clear(main), top("Audit", "Every change, as the sample edition recorded it."),
    h("div", { class: "panel" }, rows.map(row => h("div", { class: "mono" }, `${row.name} ${row.target || ""}`))));
}

export const pages = [{ group: "Activity", id: "audit", label: "Audit", icon: "M5 4h14v16H5zM9 9h6M9 13h6", page: audit }];
