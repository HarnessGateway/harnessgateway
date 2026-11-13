// Activity (spec §20): usage, and the audit record.

import { get } from "/api.js";
import { bytes, clear, field, h, put, table, when } from "/ui.js";

export async function render(root, context) {
  const usage = h("div", {});
  const group = h("select", {}, ["harness", "provider", "model", "environment", "day"].map((value) =>
    h("option", { value }, `by ${value}`)));
  const show = async () => {
    const found = await get(`/v1/usage?group_by=${group.value}`);
    put(clear(usage), table([
      { title: group.value, cell: (row) => row.group ?? "(none)" },
      { title: "Turns", cell: (row) => row.turns },
      { title: "Tokens in", cell: (row) => row.input_tokens },
      { title: "Tokens out", cell: (row) => row.output_tokens },
      { title: "Sandbox time", cell: (row) => `${row.sandbox_seconds.toFixed(1)} s` },
      { title: "Network", cell: (row) => `${bytes(row.bytes_up)} up, ${bytes(row.bytes_down)} down` },
      { title: "Saved", cell: (row) => bytes(row.snapshot_bytes) },
    ], found.data));
  };
  group.onchange = show;
  put(root, h("h1", {}, "Activity"), h("h2", {}, "Usage, last 30 days"),
    h("div", { class: "row" }, field("Summed", group), h("span", {})), usage);
  await show();
  if (context.about.role === "admin") {
    const audit = await get("/v1/audit?limit=100");
    put(root, h("h2", {}, "Audit record"), table([
      { title: "When", cell: (record) => when(record.at) },
      { title: "Key", cell: (record) => h("span", { class: "mono" }, record.key_id || "") },
      { title: "What", cell: (record) => record.action },
      { title: "To", cell: (record) => h("span", { class: "mono" }, record.target) },
      { title: "Detail", cell: (record) => h("span", { class: "small mono" }, JSON.stringify(record.detail)) },
    ], audit.data));
  }
}
