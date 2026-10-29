// The deployer's console: the platform as infrastructure.
import { api, date, esc, when } from "./api.js";
import { $, card, confirm, copy, fail, field, input, kv, modal, pill, pre, select, stat, statusPill, table, rerender, then, toast } from "./ui.js";
import { usagePage, usersPage } from "./pages_user.js";

export function nav() {
  const mods = [{ mod: "Operations", items: [
    { id: "health", label: "Health", icon: "grid" }, { id: "nodes", label: "Nodes & pools", icon: "nodes" },
    { id: "orgs", label: "Organizations", icon: "org" }, { id: "usage", label: "Platform usage", icon: "usage" },
    { id: "versions", label: "Versions", icon: "tag" }, { id: "config", label: "Configuration", icon: "cfg" },
    { id: "audit", label: "Audit log", icon: "log" }] },
    { mod: "Account", items: [{ id: "users", label: "Deployer users", icon: "user" }] }];
  for (const m of mods) for (const it of m.items) it.mod = m.mod;
  return mods;
}
const top = (title, lead, acts = "") => `<div class="top"><div><h1>${esc(title)}</h1><p class="lead">${esc(lead)}</p></div><div class="acts">${acts}</div></div>`;
const ok = (b, text) => `<span class="dot" style="background:var(--${b ? "ok" : "bad"})"></span>${esc(text)}`;

// ── Health ────────────────────────────────────────────────────────────────────────────────────────────────────────
const health = { title: "Health", async render(el) {
  const h = await api("GET", "/internal/health");
  const t = h.turns_24h || {}, f = Object.entries(h.failures_24h || {});
  el.innerHTML = top("Health", "The platform as infrastructure: processes, stores, nodes, the queue.", `<button class="btn2" id="re">Refresh</button>`) +
    `<div class="grid">${stat("API", ok(true, "ready"), `v${h.version}${h.extensions.length ? " · " + h.extensions.join(", ") : ""}`)}` +
    `${stat("Database", ok(h.database.ok, h.database.ok ? "ok" : h.database.error))}${stat("Objects", ok(h.objects?.ok, h.objects?.ok ? "ok" : h.objects?.error || "?"), h.objects?.url || "")}` +
    `${stat("Queue", h.queue.queued, h.queue.queued ? `oldest ${h.queue.oldest_s} s` : "empty")}</div>` +
    `<div class="grid">${stat("Nodes", `${h.nodes.live}<small>of ${h.nodes.count} live</small>`)}${stat("Slots", h.nodes.slots, `${h.nodes.running} running`)}${stat("Warm sandboxes", h.nodes.warm)}` +
    `${stat("Turns (24 h)", (t.completed || 0) + (t.failed || 0) + (t.cancelled || 0) + (t.incomplete || 0), `${t.failed || 0} failed`)}</div>` +
    `<div class="grid two">${card("Turns in the last 24 hours", table([{ h: "Status", cell: ([s]) => statusPill(s) }, { h: "Count", cell: ([, n]) => n }], Object.entries(t), "No turns yet."))}` +
    `${card("Failures by reason (24 h)", table([{ h: "Reason", cell: ([r]) => `<span class="mono">${esc(r)}</span>` }, { h: "Count", cell: ([, n]) => n }], f, "No failures."))}</div>`;
  $("#re", el).addEventListener("click", () => rerender());
} };

// ── Nodes & pools ─────────────────────────────────────────────────────────────────────────────────────────────────
const nodes = { title: "Nodes & pools", async render(el, params, me) {
  const rows = (await api("GET", "/internal/nodes")).data;
  const pools = [...new Set(rows.map(n => n.pool))];
  el.innerHTML = top("Nodes & pools", "Every node that joined: what it holds and whether it is alive. A node joins a pool with a join token.",
    `<button class="btn" id="token">New join token</button>`) +
    `<div class="card">${table([{ h: "Node", cell: n => `<b>${esc(n.name)}</b><br><span class="mono">${esc(n.id)}</span>` }, { h: "Pool", cell: n => pill(n.pool || "default", "b") },
      { h: "Slots", cell: n => `${n.running} / ${n.slots}` }, { h: "Warm", cell: n => n.warm }, { h: "Version", cell: n => esc(n.version || "—") },
      { h: "Heartbeat", cell: n => `${n.live ? ok(true, "") : ok(false, "")}${when(n.last_seen_at)}` }, { h: "Status", cell: n => statusPill(n.status) },
      { h: "", cls: "acts", cell: n => n.status === "active" ? `<button class="btn2" data-drain="${n.id}">Drain</button>` : "" }], rows, "No node has joined yet.")}</div>` +
    (pools.length > 1 ? `<p class="muted small" style="margin-top:10px">Pools: ${pools.map(p => esc(p || "default")).join(", ")}. An organization pinned to a pool runs only there.</p>` : "");
  $("#token", el).addEventListener("click", () => modal({ title: "Join token", submit: "Create", body:
    field("Pool", input("pool", "", 'placeholder="(default)"'), "the pool the node joins; empty for the default pool") + field("Label", input("label", "", 'placeholder="rack-2"')) +
    `<div class="row">${field("Uses", input("uses", "1", 'type="number" min="1"'))}${field("Valid for (days)", input("ttl_days", "7", 'type="number" min="1"'))}</div>`,
    onSubmit: async d => {
      const r = await api("POST", "/internal/node-tokens", { pool: d.pool, label: d.label, uses: +d.uses, ttl_days: +d.ttl_days });
      const line = `docker run -d --name harnessgateway-node --restart unless-stopped --user 0:0 \\\n  -e HG_API_URL=${me.api} -e HG_GATEWAY_URL=${me.api}/gateway \\\n  -e HG_NODE_JOIN_TOKEN=${r.token}${d.pool ? ` -e HG_NODE_POOL=${d.pool}` : ""} -e HG_UPLINK_NETWORK=bridge \\\n  -v /var/run/docker.sock:/var/run/docker.sock -v /var/lib/harnessgateway:/var/lib/harnessgateway \\\n  ghcr.io/harnessgateway/harnessgateway:${me.platform.version || "latest"} node`;
      then(() => modal({ title: "Run this on the new host", wide: true, body: `<p>Docker and gVisor must be installed (deploy/install.sh does both). The token is shown once.</p>${pre(line)}`, submit: "Copy", onSubmit: () => copy(line) }));
    } }));
  el.addEventListener("click", async e => {
    const d = e.target.closest("[data-drain]");
    if (d && await confirm("Drain node", "The node finishes its running turns and takes no new ones. Its warm sandboxes are removed when it stops.", "Drain")) {
      try { await api("POST", `/internal/nodes/${d.dataset.drain}/drain`); toast("Draining", "ok"); rerender(); } catch (err) { fail(err); }
    }
  });
} };

// ── Organizations ─────────────────────────────────────────────────────────────────────────────────────────────────
const CAP = [["vcpus", "vCPUs per sandbox", "number", "0.5"], ["memory_mb", "Memory (MB)", "number", "256"], ["pids", "Processes", "number", "64"], ["tmp_mb", "/tmp (MB)", "number", "64"],
  ["max_sandboxes", "Max sandboxes (0: none)", "number", "1"], ["max_running_turns", "Max running turns (0: none)", "number", "1"], ["idle_ttl_s", "Warm idle TTL (s)", "number", "1"],
  ["turn_timeout_s", "Turn timeout (s, 0: none)", "number", "1"], ["retention_days", "Retention (days, 0: keep)", "number", "1"], ["node_pool", "Node pool", "text", ""]];
const orgs = { title: "Organizations", async render(el, params, me) {
  const rows = (await api("GET", "/internal/orgs")).data;
  const saas = (me.platform.extensions || []).length > 0;
  el.innerHTML = top("Organizations", saas ? "Every tenant: what it may use, what it is using, and a way in." : "Self-hosted runs one organization. Its capacity is what each of its sandboxes gets; the SaaS extension adds organizations here.") +
    `<div class="card">${table([{ h: "Organization", cell: o => `<b>${esc(o.name)}</b><br><span class="mono">${esc(o.id)}</span>` },
      { h: "Capacity", cell: o => `${o.capacity.vcpus} vCPU · ${o.capacity.memory_mb} MB · ${o.capacity.max_sandboxes || "∞"} sandboxes · ${o.capacity.max_running_turns || "∞"} turns${o.capacity.node_pool ? ` · pool ${esc(o.capacity.node_pool)}` : ""}` },
      { h: "Sessions", cell: o => o.sessions }, { h: "Running", cell: o => o.running }, { h: "Warm", cell: o => o.boxes }, { h: "Observability", cell: o => pill(o.capacity.observability, "b") },
      { h: "Status", cell: o => statusPill(o.status) },
      { h: "", cls: "acts", cell: o => `<a class="btn2" href="/">Open as admin</a><button class="btn2" data-edit="${o.id}">Edit</button>` +
        (o.status === "active" ? `<button class="btn2 danger" data-status="suspended" data-id="${o.id}">Suspend</button>` : `<button class="btn2" data-status="active" data-id="${o.id}">Unsuspend</button>`) }], rows)}</div>`;
  el.addEventListener("click", async e => {
    const ed = e.target.closest("[data-edit]");
    if (ed) { editOrg(rows.find(o => o.id === ed.dataset.edit), () => rerender()); return; }
    const st = e.target.closest("[data-status]");
    if (st && (st.dataset.status === "active" || await confirm("Suspend organization", "New turns are refused until it is unsuspended; running turns finish.", "Suspend"))) {
      try { await api("PATCH", `/internal/orgs/${st.dataset.id}`, { status: st.dataset.status }); rerender(); } catch (err) { fail(err); }
    }
  });
} };
function editOrg(o, done) {
  modal({ title: `Organization ${o.name}`, wide: true, submit: "Save", body:
    `<div class="grid two" style="margin-bottom:0">${CAP.map(([k, l, t, step]) => field(l, input(k, o.capacity[k], `type="${t}"${step ? ` step="${step}"` : ""}`), k in o.capacity_set ? "set for this organization" : "the platform default")).join("")}` +
    `${field("Observability", select("observability", [["metadata", "metadata — timings, sizes, outcomes"], ["full", "full — request and response bodies too"]], o.capacity.observability))}</div>` +
    field("Egress entries (one per line)", `<textarea name="egress">${esc(o.egress.join("\n"))}</textarea>`, "host · *.domain · host:port · !host to deny · * for everything"),
    onSubmit: async d => {
      const capacity = {};
      for (const [k, , t] of CAP) capacity[k] = t === "number" ? Number(d[k]) : d[k];
      capacity.observability = d.observability;
      await api("PATCH", `/internal/orgs/${o.id}`, { capacity, egress: d.egress.split("\n").map(s => s.trim()).filter(Boolean) });
      toast("Saved", "ok"); done();
    } });
}

// ── Platform usage ────────────────────────────────────────────────────────────────────────────────────────────────
const usage = usagePage("/internal/usage", [["", "total"], ["org", "organization"], ["day", "day"]],
  "What every organization's sandboxes used, together: the figures a bill is made from.");

// ── Versions, configuration ───────────────────────────────────────────────────────────────────────────────────────
const versions = { title: "Versions", async render(el) {
  const v = await api("GET", "/internal/versions");
  el.innerHTML = top("Versions", "What is running, and what the nodes run sandboxes with.") +
    `<div class="grid two">${card("Platform", kv([["Platform", esc(v.platform)], ["Platform image", `<span class="mono">${esc(v.platform_image)}</span>`], ["Sandbox image", `<span class="mono">${esc(v.sandbox_image)}</span>`],
      ...Object.entries(v.migrations).map(([p, n]) => [`Schema ${p}`, `migration ${n}`]), ["Built-in packages from", `<span class="mono">${esc(v.packages_dir)}</span>`]]))}` +
    `${card("Built-in packages", table([{ h: "Package", cell: b => esc(b.name) }, { h: "Version", cell: b => `<span class="mono">${esc(b.version)}</span>` }], v.builtins))}</div>`;
} };
const config = { title: "Configuration", async render(el) {
  const c = await api("GET", "/internal/config");
  el.innerHTML = top("Configuration", "The settings this API process runs with (secrets are never shown), and the capacity every organization starts from.") +
    `<div class="grid two">${card("Settings (HG_*)", kv(Object.entries(c.settings).map(([k, v]) => [`HG_${k.toUpperCase()}`, `<span class="mono">${esc(v)}</span>`])))}` +
    `${card("Capacity defaults (HG_DEFAULT_*)", kv(Object.entries(c.defaults).map(([k, v]) => [k, esc(String(v))])))}</div>`;
} };

// ── Audit log ─────────────────────────────────────────────────────────────────────────────────────────────────────
const audit = { title: "Audit log", async render(el) {
  const rows = (await api("GET", "/audit")).data;
  el.innerHTML = top("Audit log", "What the consoles did, newest first: sign-ins, user changes, every write to the platform.") +
    `<div class="card">${table([{ h: "When", cell: r => date(r.at) }, { h: "User", cell: r => esc(r.user) }, { h: "Action", cell: r => `<span class="mono">${esc(r.action)}</span>` },
      { h: "Detail", cell: r => `<span class="mono">${esc(Object.entries(r).filter(([k]) => !["at", "user", "action"].includes(k)).map(([k, v]) => `${k}=${v}`).join(" "))}</span>` }], rows, "Nothing yet.")}</div>`;
} };

const users = usersPage("Deployer users", "Who can sign in to the deployer console.", [["deployer", "deployer"]]);

export const pages = { health, nodes, orgs, usage, versions, config, audit, users };
