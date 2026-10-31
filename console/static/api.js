// The console's calls: its own API (/api/*) and the platform through the relay (/api/v1/*, /api/internal/*).
export async function api(method, path, body = undefined, { raw = false } = {}) {
  const init = { method, headers: {} };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  const r = await fetch("/api" + path, init);
  if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname + location.hash); throw new Error("sign in"); }
  if (raw) return r;
  const text = await r.text();
  let doc = null;
  try { doc = text ? JSON.parse(text) : {}; } catch { doc = { error: text.slice(0, 300) }; }
  if (!r.ok) {
    if (doc && doc.must_change) { location.href = "/login?change=1"; throw new Error("change the password first"); }
    const e = doc && doc.error;
    throw new Error(typeof e === "string" ? e : e && e.message ? e.message : `${r.status} ${r.statusText}`);
  }
  return doc;
}

// A streamed Responses call (POST with a body, GET without): every SSE event to onEvent({type, data}); resolves when
// the stream ends.
export async function stream(path, body, onEvent) {
  const init = body == null ? { method: "GET", headers: { Accept: "text/event-stream" } }
    : { method: "POST", headers: { "Content-Type": "application/json", Accept: "text/event-stream" }, body: JSON.stringify(body) };
  const r = await fetch("/api" + path, init);
  if (!r.ok || !r.body) {
    const text = await r.text();
    let msg = text; try { const d = JSON.parse(text); msg = d.error?.message || d.error || text; } catch { /* as is */ }
    throw new Error(msg || `${r.status}`);
  }
  const reader = r.body.getReader(), dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, i); buf = buf.slice(i + 2);
      let type = "", data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) type = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) { try { onEvent({ type, data: JSON.parse(data) }); } catch { onEvent({ type, data }); } }
    }
  }
}

export const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export function bytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return `${n} B`;
  const u = ["KB", "MB", "GB", "TB"]; let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return `${n < 10 ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}
export function when(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 7 * 86400) return `${Math.floor(s / 86400)} d ago`;
  return new Date(ts * 1000).toLocaleDateString();
}
export const date = ts => ts ? new Date(ts * 1000).toLocaleString() : "—";
export const num = n => (Number(n) || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
export const hours = s => `${((Number(s) || 0) / 3600).toLocaleString(undefined, { maximumFractionDigits: 2 })} h`;
export const short = id => (id || "").length > 14 ? id.slice(0, 13) + "…" : id || "";
// A meter's quantity, shown in its natural unit.
export function meter(name, q) {
  if (name === "vcpu_seconds") return hours(q).replace(" h", " vCPU·h");
  if (name === "memory_gib_seconds") return hours(q).replace(" h", " GiB·h");
  if (name === "storage_gib_hours") return `${num(q)} GiB·h`;
  return bytes(q);
}
export const METERS = { vcpu_seconds: "CPU", memory_gib_seconds: "Memory", egress_bytes: "Egress", ingress_bytes: "Ingress", storage_gib_hours: "Storage" };
