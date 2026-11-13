// What the console's own pages share: calls to the platform as the signed-in person, and how values are shown.
import { call, stream as events } from "/api.js";

/** One call to the platform. A call refused for want of a sign-in sends the person back to the sign-in page. */
export async function api(method, path, body = undefined, { raw = false } = {}) {
  try {
    return await call(method, path, body, { raw });
  } catch (error) {
    if (error.status === 401) window.dispatchEvent(new Event("hg-signed-out"));
    throw error;
  }
}

/** A streamed Responses call (posted with a body, read without one): every event to onEvent({type, data}). */
export const stream = (path, body, onEvent, signal) => events(path, body, data => onEvent({ type: data.type, data }), signal);

export const list = async path => (await api("GET", path)).data || [];
export const quiet = path => list(path).catch(() => []);

/** What the pickers offer: the organization's environments, harnesses and providers. A key that may only run
 *  turns cannot list those, and gets the harnesses from /v1/models. */
export async function loadCatalog() {
  try {
    const [harnesses, providers, environments] = await Promise.all(
      [list("/v1/harnesses?limit=100"), list("/v1/providers?limit=100"), list("/v1/environments?limit=100")]);
    return {
      harnesses: harnesses.map(h => ({ name: h.name, description: h.description, apis: h.manifest.apis || [], provider: h.provider || "", model: h.model || "" })),
      providers: providers.map(p => ({ name: p.name, apis: Object.keys(p.apis || {}), model: p.default_model || "" })),
      environments: environments.map(e => e.name),
    };
  } catch (error) {
    if (error.status !== 403) throw error;
    const models = await list("/v1/models");
    return { harnesses: models.map(m => ({ name: m.id, description: "", apis: null, provider: "", model: "" })), providers: [], environments: [] };
  }
}
/** The providers a harness can use: those that speak one of its APIs. `apis: null` is a harness we know only by name. */
export const fitting = (catalog, harness) => !harness || !harness.apis ? []
  : catalog.providers.filter(p => p.apis.some(a => harness.apis.includes(a)));
/** [environment:]harness[/provider[/model]], with what is left out left to the platform's defaults. */
export function modelString({ environment = "", harness, provider = "", model = "" }) {
  const head = environment && environment !== "default" ? `${environment}:${harness}` : harness;
  return [head, provider, provider ? model : ""].filter(Boolean).join("/");
}
export function parseModel(text) {
  const [head = "", provider = "", ...rest] = String(text || "").split("/");
  const at = head.lastIndexOf(":");
  return { environment: at < 0 ? "" : head.slice(0, at), harness: at < 0 ? head : head.slice(at + 1), provider, model: rest.join("/") };
}

export const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export function bytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return `${n} B`;
  const u = ["KB", "MB", "GB", "TB"]; let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return `${n < 10 ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}
/** A time as the API gives it: seconds since 1970 (the Responses API) or an ISO string (the management API). */
export const seconds = t => !t ? 0 : typeof t === "number" ? t : Date.parse(t) / 1000;
export function when(t) {
  const ts = seconds(t);
  if (!ts) return "—";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 7 * 86400) return `${Math.floor(s / 86400)} d ago`;
  return new Date(ts * 1000).toLocaleDateString();
}
export const date = t => seconds(t) ? new Date(seconds(t) * 1000).toLocaleString() : "—";
export const num = n => (Number(n) || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
export function duration(s) {
  s = Number(s) || 0;
  if (s < 90) return `${num(s)} s`;
  if (s < 5400) return `${num(s / 60)} min`;
  return `${num(s / 3600)} h`;
}
export const short = id => (id || "").length > 14 ? id.slice(0, 13) + "…" : id || "";
/** Who made a change: a key by its name, or whoever an edition's sign-in says it was. */
export function actorOf(keys = []) {
  const byKey = Object.fromEntries(keys.map(k => [k.id, k.name]));
  return id => !id ? "—" : byKey[id] ? `key · ${byKey[id]}` : id;
}
