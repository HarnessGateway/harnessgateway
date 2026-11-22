// Usage: what the organization used, as the infrastructure it is: processor time, memory, network and storage.
import { get } from "./api.js";
import { add, bytes, clear, figure, h, icon, top } from "./ui.js";

const day = date => date.toISOString().slice(0, 10);
const PERIODS = {
  "All time": () => "",
  "This month": () => `?start=${day(new Date()).slice(0, 8)}01`,
  "Today": () => `?start=${day(new Date())}`,
};
const address = period => `#/usage/${period.toLowerCase().replace(/ /g, "-")}`;
/** An amount held over seconds, in the unit of time that makes it a number worth reading: [figure, unit]. */
const over = (amount, what) => amount >= 3600 ? [figure(amount / 3600), `${what}-hours`] : amount >= 60 ? [figure(amount / 60), `${what}-minutes`] : [figure(amount), `${what}-seconds`];

export async function usage(main, params) {
  const period = Object.keys(PERIODS).find(name => address(name) === `#/usage/${params[0]}`) || "All time";
  const used = await get(`/v1/usage${PERIODS[period]()}`);
  const stat = (name, glyph, [value, unit], more) => h("div", { class: "stat" }, h("div", { class: "label" }, icon(glyph), name),
    h("div", { class: "value" }, value, unit && h("span", {}, unit)), h("div", { class: "muted small" }, more));
  add(clear(main), top("Usage", "What this organization's sandboxes used, and what it stores.",
    h("div", { class: "segment" }, Object.keys(PERIODS).map(name => h("button", { class: name === period ? "on" : "", onclick: () => { location.hash = address(name); } }, name)))),
    h("div", { class: "stats" },
      stat("CPU", "cpu", over(used.cpu_seconds, "CPU"), "Processor time, all processors added up"),
      stat("Memory", "memory", over(used.memory_byte_seconds / 1e9, "GB"), "Memory held, times how long it was held"),
      stat("Network", "network", [bytes(used.network_in_bytes + used.network_out_bytes)], `${bytes(used.network_in_bytes)} in · ${bytes(used.network_out_bytes)} out, through the egress proxy`),
      stat("Storage", "storage", over(used.storage_byte_seconds / 1e9, "GB"), `${bytes(used.storage_bytes)} stored now`)));
}
