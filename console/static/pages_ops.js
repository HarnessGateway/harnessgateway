// The platform console (SPEC §14.3): the platform as infrastructure, for platform admins.
import { api, esc, when } from "./api.js";
import { $, card, confirm, fail, field, input, kv, modal, pill, rerender, reveal, select, stat, statusPill, table, then, toast } from "./ui.js";
import { auditPage, usagePage } from "./pages_user.js";

export function nav() {
  const mods = [{ mod: "Platform", items: [
    { id: "health", label: "Health", icon: "grid" }, { id: "boxes", label: "Boxes", icon: "nodes" },
    { id: "orgs", label: "Organizations", icon: "org" }, { id: "usage", label: "Platform usage", icon: "usage" },
    { id: "versions", label: "Versions", icon: "tag" }, { id: "config", label: "Configuration", icon: "cfg" },
    { id: "audit", label: "Audit log", icon: "log" }] }];
  for (const m of mods) for (const it of m.items) it.mod = m.mod;
  return mods;
}
const top = (title, lead, acts = "") => `<div class="top"><div><h1>${esc(title)}</h1><p class="lead">${esc(lead)}</p></div><div class="acts">${acts}</div></div>`;
const ok = (b, text) => `<span class="dot" style="background:var(--${b ? "ok" : "bad"})"></span>${esc(text)}`;
// The API as another host reaches it. The console knows only its own upstream (inside compose, a name no other host
// resolves), so the guess is this page's host on the API's default port; the operator corrects it where it is used.
const apiGuess = () => `${location.protocol}//${location.hostname}:8000`;

// ── Health ────────────────────────────────────────────────────────────────────────────────────────────────────────
const health = { title: "Health", async render(el) {
  const h = await api("GET", "/internal/health");
  const t = h.turns_24h || {}, f = Object.entries(h.failures_24h || {}), b = h.boxes || {}, q = h.turns || {};
  const count = prefix => Object.entries(b).filter(([k]) => k.startsWith(prefix)).reduce((n, [, v]) => n + v, 0);
  el.innerHTML = top("Health", "The platform as infrastructure: the stores, the queue, the boxes.", `<button class="btn2" id="re">Refresh</button>`) +
    `<div class="grid">${stat("API", ok(true, "ready"), `v${h.version}${h.extensions.length ? " · " + h.extensions.join(", ") : ""}`)}` +
    `${stat("Database", ok(h.database.ok, h.database.ok ? "ok" : h.database.error))}${stat("Redis", ok(h.redis?.ok, h.redis?.ok ? "ok" : h.redis?.error || "?"), h.redis?.ok ? `${h.redis.jobs} jobs queued` : "")}` +
    `${stat("Objects", ok(h.objects?.ok, h.objects?.ok ? "ok" : h.objects?.error || "?"), h.objects?.url || "")}</div>` +
    `<div class="grid">${stat("Turns running", q.running || 0)}${stat("Turns queued", q.queued || 0, q.queued ? `oldest ${q.oldest_queued_s} s` : "none waiting")}` +
    `${stat("Session boxes", count("session."), `${b["session.running"] || 0} running · ${b["session.standby"] || 0} standing by · ${b["session.starting"] || 0} starting`)}${stat("Install boxes", count("install."))}` +
    `${stat("Turns (24 h)", (t.completed || 0) + (t.failed || 0) + (t.cancelled || 0) + (t.incomplete || 0), `${t.failed || 0} failed`)}</div>` +
    `<div class="grid two">${card("Turns in the last 24 hours", table([{ h: "Status", cell: ([s]) => statusPill(s) }, { h: "Count", cell: ([, n]) => n }], Object.entries(t), "No turns yet."))}` +
    `${card("Failures by reason (24 h)", table([{ h: "Reason", cell: ([r]) => `<span class="mono">${esc(r)}</span>` }, { h: "Count", cell: ([, n]) => n }], f, "No failures."))}</div>`;
  $("#re", el).addEventListener("click", () => rerender());
} };

// ── Boxes ─────────────────────────────────────────────────────────────────────────────────────────────────────────
const boxes = { title: "Boxes", async render(el) {
  const rows = (await api("GET", "/internal/boxes")).data;
  el.innerHTML = top("Boxes", "Every live sandbox: one per session while it runs or stands by for its next turn, and one per install in progress. A box that stops answering is lost after a minute; stopping one fails its turn.",
    `<button class="btn2" id="re">Refresh</button>`) +
    `<div class="card">${table([{ h: "Box", cell: b => `<span class="mono">${esc(b.id)}</span><br><span class="muted small">${esc(b.launcher_ref || "not launched yet")}</span>` },
      { h: "Kind", cell: b => pill(b.kind, "b") }, { h: "Organization", cell: b => `<span class="mono">${esc(b.org_id)}</span>` },
      { h: "Harness", cell: b => esc(b.harness || "—") }, { h: "Session", cell: b => b.session_id ? `<span class="mono">${esc(b.session_id)}</span>` : "—" },
      { h: "Turn", cell: b => b.turn_id ? `<span class="mono">${esc(b.turn_id)}</span>` : "—" }, { h: "Status", cell: b => statusPill(b.status) },
      { h: "Seen", cell: b => when(b.last_seen_at) }, { h: "Started", cell: b => when(b.created_at) },
      { h: "", cls: "acts", cell: b => `<button class="btn2 danger" data-stop="${b.id}">Stop</button>` }], rows, "No box is live.")}</div>`;
  $("#re", el).addEventListener("click", () => rerender());
  el.addEventListener("click", async e => {
    const s = e.target.closest("[data-stop]");
    if (s && await confirm("Stop box", "The box ends now. A turn running in it fails as cancelled; the session keeps its last saved state and gets a new box on its next turn.", "Stop")) {
      try { await api("POST", `/internal/boxes/${s.dataset.stop}/stop`); toast("Stopped", "ok"); rerender(); } catch (err) { fail(err); }
    }
  });
} };

// ── Organizations ─────────────────────────────────────────────────────────────────────────────────────────────────
const LIMITS = [["vcpus", "vCPUs per box", "number", "0.5"], ["memory_mb", "Memory (MB)", "number", "256"], ["tmp_mb", "/tmp (MB)", "number", "64"], ["pids", "Processes", "number", "64"],
  ["max_concurrent_turns", "Concurrent turns (0: no ceiling)", "number", "1"], ["standby_s", "Standby after a turn (s)", "number", "1"],
  ["turn_timeout_s", "Turn timeout (s, 0: none)", "number", "1"], ["retention_days", "Retention (days, 0: keep)", "number", "1"]];
const orgs = { title: "Organizations", async render(el) {
  const rows = (await api("GET", "/internal/orgs")).data;
  el.innerHTML = top("Organizations", "Every organization on this platform: what each of its boxes gets, how many turns may run at once, and what it is using. Self-hosted runs one.") +
    `<div class="card">${table([{ h: "Organization", cell: o => `<b>${esc(o.name)}</b><br><span class="mono">${esc(o.id)}</span>` },
      { h: "Limits", cell: o => `${o.limits.vcpus} vCPU · ${o.limits.memory_mb} MB · ${o.limits.max_concurrent_turns || "∞"} turns · standby ${o.limits.standby_s} s` },
      { h: "Sessions", cell: o => o.sessions }, { h: "Running", cell: o => o.running }, { h: "Boxes", cell: o => o.boxes }, { h: "Observability", cell: o => pill(o.limits.observability, "b") },
      { h: "Status", cell: o => statusPill(o.status) },
      { h: "", cls: "acts", cell: o => `<button class="btn2" data-invite="${o.id}" data-name="${esc(o.name)}">Invite admin</button><button class="btn2" data-edit="${o.id}">Edit</button>` +
        (o.status === "active" ? `<button class="btn2 danger" data-status="suspended" data-id="${o.id}">Suspend</button>` : `<button class="btn2" data-status="active" data-id="${o.id}">Unsuspend</button>`) }], rows)}</div>` +
    `<div class="card" style="margin-top:14px"><h2>Metrics</h2><p class="muted small">Prometheus scrapes <span class="mono">GET ${esc(apiGuess())}/internal/metrics</span> (the API as the scraper reaches it) with the operator key (<span class="mono">HG_OPERATOR_KEY</span>) as the bearer.</p></div>`;
  el.addEventListener("click", async e => {
    const inv = e.target.closest("[data-invite]");
    if (inv) {
      modal({ title: `Invite an admin of ${inv.dataset.name}`, submit: "Invite", body:
        field("Email", input("email", "", 'type="email" required')) + field("Name", input("name", "")),
        onSubmit: async d => {
          const a = await api("POST", `/internal/orgs/${inv.dataset.invite}/accounts`, { email: d.email, name: d.name });
          then(() => reveal(`Invitation for ${a.email}`, "Invitation link", a.invitation, (a.sent ? `Sent to ${a.email}. ` : "") +
            "The link is shown once, works once and expires in seven days; the person chooses their password with it."));
        } });
      return;
    }
    const ed = e.target.closest("[data-edit]");
    if (ed) { editOrg(rows.find(o => o.id === ed.dataset.edit), () => rerender()); return; }
    const st = e.target.closest("[data-status]");
    if (st && (st.dataset.status === "active" || await confirm("Suspend organization", "New turns are refused and queued ones fail until it is unsuspended; running turns finish.", "Suspend"))) {
      try { await api("PATCH", `/internal/orgs/${st.dataset.id}`, { status: st.dataset.status }); rerender(); } catch (err) { fail(err); }
    }
  });
} };
function editOrg(o, done) {
  modal({ title: `Organization ${o.name}`, wide: true, submit: "Save", body:
    `<div class="grid two" style="margin-bottom:0">${LIMITS.map(([k, l, t, step]) => field(l, input(k, o.limits[k], `type="${t}"${step ? ` step="${step}"` : ""}`), k in o.limits_set ? "set for this organization" : "the platform default")).join("")}` +
    `${field("Observability", select("observability", [["metadata", "metadata — timings, sizes, outcomes"], ["full", "full — each turn's whole stdout and stderr too"]], o.limits.observability))}</div>` +
    field("Egress entries (one per line)", `<textarea name="egress">${esc(o.egress.join("\n"))}</textarea>`, "host · *.domain · host:port · !host to deny · * for everything"),
    onSubmit: async d => {
      const limits = {};
      for (const [k] of LIMITS) limits[k] = Number(d[k]);
      limits.observability = d.observability;
      await api("PATCH", `/internal/orgs/${o.id}`, { limits, egress: d.egress.split("\n").map(s => s.trim()).filter(Boolean) });
      toast("Saved", "ok"); done();
    } });
}

// ── Platform usage ────────────────────────────────────────────────────────────────────────────────────────────────
const usage = usagePage("/internal/usage", [["", "total"], ["org", "organization"], ["day", "day"]],
  "What every organization's boxes used, together: agent time, the resources behind it, bytes through the door, storage held.");

// ── Versions, configuration ───────────────────────────────────────────────────────────────────────────────────────
const versions = { title: "Versions", async render(el) {
  const v = await api("GET", "/internal/versions");
  el.innerHTML = top("Versions", "What is running, and the image every box runs.") +
    `<div class="grid two">${card("Platform", kv([["Platform", esc(v.platform)], ["Platform image", `<span class="mono">${esc(v.platform_image)}</span>`], ["Sandbox image", `<span class="mono">${esc(v.sandbox_image)}</span>`],
      ...Object.entries(v.migrations).map(([p, n]) => [`Schema ${p}`, `migration ${n}`])]))}</div>`;
} };
const config = { title: "Configuration", async render(el) {
  const c = await api("GET", "/internal/config");
  el.innerHTML = top("Configuration", "The settings this API process runs with (secrets are never shown), and the limits every organization starts from.") +
    `<div class="grid two">${card("Settings (HG_*)", kv(Object.entries(c.settings).map(([k, v]) => [`HG_${k.toUpperCase()}`, `<span class="mono">${esc(v)}</span>`])))}` +
    `${card("Limit defaults (HG_DEFAULT_*)", kv(Object.entries(c.defaults).map(([k, v]) => [k, esc(String(v))])))}</div>`;
} };

// ── Audit log ─────────────────────────────────────────────────────────────────────────────────────────────────────
const audit = auditPage("/internal/audit", "Every change of standing across the platform, newest first: the organizations' and the platform's own (limits, stopped boxes).");

export const pages = { health, boxes, orgs, usage, versions, config, audit };
