// Team: teams, members and invitations, keys, the account. And the deployer's views.

VIEWS.Teams = async function teams() {
  const [list, members] = await Promise.all([api("GET", "/v1/teams"), api("GET", "/v1/members")]);
  const name = h("input", { placeholder: "platform", pattern: "[a-z0-9][a-z0-9._-]*" }), title = h("input", { placeholder: "Platform engineering" });
  const picks = h("div", { class: "picks" }, ...members.data.map(m => h("label", {}, h("input", { type: "checkbox", value: m.name }), m.name)));
  const out = h("div");
  const save = async () => { out.replaceChildren(); try { await api("PUT", `/v1/teams/${name.value.trim()}`, { members: [...picks.querySelectorAll("input:checked")].map(i => i.value), title: title.value.trim() }); shell("Teams"); } catch (e) { out.replaceChildren(fail(e)); } };
  const rows = list.data.map(t => h("tr", {}, h("td", {}, h("b", {}, t.name), t.title ? h("span", { class: "muted" }, " · ", t.title) : null), h("td", {}, t.members.length ? t.members.map(m => tag(m)) : h("span", { class: "muted" }, "nobody yet")),
    h("td", {}, isOwner() ? h("div", { class: "row end" }, h("button", { class: "ghost small", on: { click: () => { name.value = t.name; title.value = t.title; for (const i of picks.querySelectorAll("input")) i.checked = t.members.includes(i.value); name.scrollIntoView(); } } }, "edit"), confirmBtn("delete", "ghost small", async () => { await api("DELETE", `/v1/teams/${t.name}`); shell("Teams"); })) : null)));
  return h("div", {}, head("Teams", "A team groups members and owns harnesses only its members can run and edit. Harnesses outside any team belong to the whole organization and are an owner's to change."),
    h("div", { class: "card" }, h("div", { class: "table-wrap" }, rows.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Team"), h("th", {}, "Members"), h("th"))), h("tbody", {}, ...rows)) : empty("No teams yet."))),
    isOwner() ? h("div", { class: "card" }, h("h2", {}, "Create or change a team"), h("div", { class: "grid-2" }, field("Name", name), field("Title", title)), h("div", { style: "margin-top:10px" }, h("label", {}, "Members"), picks), h("div", { class: "row end", style: "margin-top:12px" }, out, h("button", { class: "act", on: { click: save } }, "Save team"))) : null);
};

VIEWS.Members = async function members() {
  const list = await api("GET", "/v1/members");
  const invites = isOwner() ? await api("GET", "/v1/invites") : { data: [] };
  const email = h("input", { type: "email", placeholder: "colleague@company.com" }), role = h("select", {}, option("member"), option("owner"));
  const mname = h("input", { placeholder: "a member name, for a key-only member", pattern: "[a-z0-9][a-z0-9._-]*" }), mrole = h("select", {}, option("member"), option("owner"));
  const out = h("div");
  const rows = list.data.map(m => h("tr", {}, h("td", {}, h("b", {}, m.name)), h("td", {}, isOwner() && m.name !== state.me.name ? h("select", { on: { change: async (e) => { try { await api("PATCH", `/v1/members/${m.name}`, { role: e.target.value }); } catch (err) { out.replaceChildren(fail(err)); } } } }, option("member", "member", m.role === "member"), option("owner", "owner", m.role === "owner")) : tag(m.role, m.role === "owner" ? "orange" : "")), h("td", { class: "muted" }, when(m.created)),
    h("td", {}, isOwner() && m.name !== state.me.name ? h("div", { class: "row end" }, confirmBtn("remove", "ghost small", async () => { await api("DELETE", `/v1/members/${m.name}`); shell("Members"); })) : null)));
  return h("div", {}, head("Members", "Owners register providers, manage teams and every harness; members use the harnesses their teams own and the organization's, and make requests. An invitation turns an email into a member when its account signs in."),
    h("div", { class: "card" }, h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Member"), h("th", {}, "Role"), h("th", {}, "Since"), h("th"))), h("tbody", {}, ...rows)))),
    isOwner() ? h("div", { class: "grid-2" },
      h("div", { class: "card" }, h("h2", {}, "Invite by email"), h("div", { class: "row" }, field("Email", email), field("Role", role), h("button", { class: "act", style: "align-self:flex-end", on: { click: async () => { out.replaceChildren(); try { await api("POST", "/v1/invites", { email: email.value.trim(), role: role.value }); shell("Members"); } catch (e) { out.replaceChildren(fail(e)); } } } }, "Invite")),
        invites.data.length ? h("div", { style: "margin-top:12px" }, h("h3", {}, "Open invitations"), ...invites.data.map(i => h("div", { class: "row between" }, h("span", {}, i.email, " ", tag(i.role)), confirmBtn("revoke", "ghost small", async () => { await api("DELETE", `/v1/invites/${encodeURIComponent(i.email)}`); shell("Members"); })))) : null),
      h("div", { class: "card" }, h("h2", {}, "Add a member for a key"), h("p", { class: "dim" }, "A member without an account: a service or a script that holds an API key."), h("div", { class: "row" }, field("Name", mname), field("Role", mrole), h("button", { class: "quiet", style: "align-self:flex-end", on: { click: async () => { out.replaceChildren(); try { await api("POST", "/v1/members", { name: mname.value.trim(), role: mrole.value }); shell("Members"); } catch (e) { out.replaceChildren(fail(e)); } } } }, "Add")), out)) : out);
};

VIEWS.Keys = async function keys() {
  const list = await api("GET", "/v1/keys");
  const members = isOwner() ? (await api("GET", "/v1/members")).data : [{ name: state.me.name }];
  const label = h("input", { placeholder: "laptop, CI, the deploy bot" }), who = h("select", {}, ...members.map(m => option(m.name, m.name, m.name === state.me.name)));
  const shown = h("div");
  const rows = list.data.map(k => h("tr", {}, h("td", {}, h("code", {}, k.id)), h("td", {}, k.member), h("td", {}, k.label || h("span", { class: "muted" }, "—")), h("td", { class: "muted" }, when(k.created)), h("td", {}, k.revoked ? tag("revoked", "bad") : tag("live", "ok")),
    h("td", {}, !k.revoked ? h("div", { class: "row end" }, confirmBtn("revoke", "ghost small", async () => { await api("DELETE", `/v1/keys/${k.id}`); shell("Keys"); })) : null)));
  return h("div", {}, head("API keys", "A key opens the organization as its member from any OpenAI SDK: base_url is this platform's /v1. Mint one per place it is used and revoke the one that leaks. A key is shown once."),
    h("div", { class: "card" }, h("div", { class: "row" }, field("Label", label), isOwner() ? field("For member", who) : null, h("button", { class: "act", style: "align-self:flex-end", on: { click: async () => { shown.replaceChildren(); try { const k = await api("POST", `/v1/members/${who.value}/keys`, { label: label.value.trim() }); shown.replaceChildren(h("div", { class: "secret" }, k.secret, " ", copy(k.secret)), h("p", { class: "muted" }, "Copy it now; it is not shown again.")); } catch (e) { shown.replaceChildren(fail(e)); } } } }, "Mint a key")), shown,
      h("pre", { class: "light", style: "margin-top:12px" }, `from openai import OpenAI\nclient = OpenAI(api_key="hg_…", base_url="${location.origin}/v1")\nr = client.responses.create(model="claude-code", input="Add a test for the parser and run it.")\nprint(r.status, r.output_text)`)),
    h("div", { class: "card" }, h("div", { class: "table-wrap" }, rows.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Id"), h("th", {}, "Member"), h("th", {}, "Label"), h("th", {}, "Created"), h("th", {}, "State"), h("th"))), h("tbody", {}, ...rows)) : empty("No keys yet."))));
};

VIEWS.Account = async function account() {
  const a = state.account;
  const name = h("input", { value: a.name }), current = h("input", { type: "password", placeholder: "current password" }), pw = h("input", { type: "password", placeholder: "new password" });
  const orgName = h("input", { placeholder: "acme", pattern: "[a-z0-9][a-z0-9._-]*" });
  const out = h("div");
  return h("div", {}, head("Account", a.email),
    h("div", { class: "grid-2" },
      h("div", { class: "card" }, h("h2", {}, "Your name"), h("div", { class: "row" }, name, h("button", { class: "quiet", on: { click: async () => { try { state.account = await api("PATCH", "/auth/account", { name: name.value }); out.replaceChildren(h("div", { class: "notice ok" }, "saved")); } catch (e) { out.replaceChildren(fail(e)); } } } }, "Save")),
        h("h2", { style: "margin-top:18px" }, a.has_password ? "Change your password" : "Set a password"), h("div", { class: "stack" }, a.has_password ? current : null, pw, h("button", { class: "quiet", style: "align-self:flex-start", on: { click: async () => { try { await api("POST", "/auth/password", { password: pw.value, current: current.value }); out.replaceChildren(h("div", { class: "notice ok" }, "password set")); } catch (e) { out.replaceChildren(fail(e)); } } } }, "Save password")), out),
      h("div", { class: "card" }, h("h2", {}, "Your organizations"), h("ul", {}, ...state.orgs.map(t => h("li", {}, h("b", {}, t.org), ` · ${t.role}`, t.status !== "active" ? ` · ${t.status}` : ""))),
        h("h3", {}, "Create another"), h("div", { class: "row" }, orgName, h("button", { class: "act", on: { click: async () => { try { const me = await api("POST", "/auth/orgs", { name: orgName.value.trim() }); state.orgs = me.orgs; state.me = { ...me.current, name: me.current.member, teams: [] }; state.kind = me.current.role; shell("Providers"); } catch (e) { out.replaceChildren(fail(e)); } } } }, "Create")),
        h("p", { class: "muted", style: "margin-top:10px" }, "Signed in with: ", (a.providers || []).join(", ") || "email and password"))));
};

// the deployer
function columns(rows, key, fmt, title) {
  const W = 640, H = 200, pad = 28, max = Math.max(1, ...rows.map(r => r[key]));
  const bw = (W - pad) / Math.max(1, rows.length);
  const s = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}` });
  for (let i = 0; i <= 4; i++) { const y = pad / 2 + (H - pad) * (1 - i / 4); s.append(svg("line", { class: "grid", x1: pad, x2: W, y1: y, y2: y }), svg("text", { class: "axis", x: 0, y: y + 3 }, fmt(max * i / 4))); }
  rows.forEach((r, i) => { const hgt = (H - pad) * r[key] / max, x = pad + i * bw + 1; const bar = svg("rect", { class: "bar", x, y: pad / 2 + (H - pad) - hgt, width: Math.max(1, bw - 2), height: hgt }); s.append(bar); bar.append(svg("title", {}, `${r.day}: ${fmt(r[key])}`)); if (rows.length <= 10 || i % Math.ceil(rows.length / 10) === 0) s.append(svg("text", { class: "axis", x: x + bw / 2, y: H - 2, "text-anchor": "middle" }, r.day.slice(5))); });
  return h("div", {}, h("h3", {}, title), s);
}
VIEWS.Dashboard = async function dashboard() {
  const [u, fleet] = await Promise.all([api("GET", `/v1/platform/usage?days=${state.window}`), api("GET", "/v1/fleet")]);
  const pick = (d) => h("button", { class: "quiet small" + (state.window === d ? " on" : ""), on: { click: () => { state.window = d; shell("Dashboard"); } } }, d ? `${d} days` : "all time");
  const stat = (b, s) => h("div", { class: "stat" }, h("b", {}, b), h("span", {}, s));
  const ds = await api("GET", "/v1/dataset/summary");
  return h("div", {}, head("Dashboard", "The platform's usage: counts and sizes only, never an organization's prompts, output, packages or keys.", pick(7), pick(30), pick(90), pick(0)),
    h("div", { class: "stats" }, stat(num(u.totals.turns), "turns"), stat(hours(u.totals.cpu_seconds), "cpu time"), stat(hours(u.totals.memory_gb_seconds), "GB·time"), stat(bytes(u.totals.egress_bytes), "egress"), stat(bytes(u.storage.bytes), "stored"), stat(num(u.totals.input_tokens + u.totals.output_tokens), "tokens (shown)"), stat(fleet.workers, "nodes alive"), stat(`${fleet.running}/${fleet.slots}`, "slots busy"), stat(fleet.queued, "queued")),
    h("div", { class: "grid-2", style: "margin-top:16px" }, h("div", { class: "card" }, columns(u.daily, "turns", num, "Turns per day")), h("div", { class: "card" }, columns(u.daily, "cpu_seconds", hours, "CPU time per day"))),
    h("div", { class: "card" }, h("h2", {}, "The dataset"), h("p", { class: "dim" }, `${ds.orgs} organization${ds.orgs === 1 ? "" : "s"} share traces: ${ds.sessions} sessions, ${ds.turns} turns, pseudonymized. Export them as JSON lines:`),
      h("pre", { class: "light" }, `curl -H "Authorization: Bearer hgd_…" "${location.origin}/v1/dataset?since=0" > dataset.jsonl`), h("a", { href: "/docs/data/" }, "What is in it, and what is not →")),
    h("div", { class: "grid-2" }, h("div", { class: "card" }, h("h2", {}, "By organization"), h("div", { class: "bars" }, ...u.orgs.map(r => h("div", { class: "r" }, h("span", { class: "n" }, r.org), h("div", { class: "b", style: `width:${100 * r.turns / Math.max(1, u.totals.turns)}%` }), h("span", { class: "v" }, num(r.turns)))))),
      h("div", { class: "card" }, h("h2", {}, "By harness"), h("div", { class: "bars" }, ...u.harnesses.map(r => h("div", { class: "r" }, h("span", { class: "n" }, r.harness), h("div", { class: "b", style: `width:${100 * r.turns / Math.max(1, u.totals.turns)}%` }), h("span", { class: "v" }, num(r.turns))))))));
};
VIEWS.Organizations = async function organizations() {
  const list = await api("GET", "/v1/orgs");
  const name = h("input", { placeholder: "acme" }), owner = h("input", { placeholder: "owner" }), out = h("div"), plan = h("select", {}, option("free"), option("team"), option("enterprise"));
  const patch = async (name, body) => { try { await api("PATCH", `/v1/orgs/${name}`, body); shell("Organizations"); } catch (e) { out.replaceChildren(fail(e)); } };
  const rows = list.data.map(t => h("tr", {}, h("td", {}, h("b", {}, t.name), t.title ? h("span", { class: "muted" }, " · ", t.title) : null), h("td", {}, statusTag(t.status)),
    h("td", {}, h("select", { on: { change: (e) => patch(t.name, { plan: e.target.value }) } }, ...["free", "team", "enterprise"].map(p => option(p, p, t.plan === p)))),
    h("td", {}, t.plan === "enterprise" ? tag("never", "") : h("label", { class: "row", style: "gap:6px" }, h("input", { type: "checkbox", checked: t.trace_sharing || false, on: { change: (e) => patch(t.name, { trace_sharing: e.target.checked }) } }), t.trace_sharing ? "shares" : "no")),
    h("td", {}, num(t.usage.turns), " · ", hours(t.usage.cpu_seconds || 0)), h("td", {}, `${t.usage.running} running · ${t.usage.queued} queued`), h("td", {}, t.limits.max_runs, " runs · ", t.limits.cpus, " cpu · ", t.limits.memory, " · ", t.limits.requests_per_minute, "/min"),
    h("td", {}, h("div", { class: "row end" }, h("button", { class: "ghost small", on: { click: () => patch(t.name, { status: t.status === "active" ? "suspended" : "active" }) } }, t.status === "active" ? "suspend" : "reactivate"),
      h("button", { class: "ghost small", on: { click: async () => { const u = await api("GET", `/v1/orgs/${t.name}/usage?days=30`); out.replaceChildren(h("div", { class: "card flat" }, h("h3", {}, `${t.name}, the last 30 days`), h("div", { class: "stats" }, h("div", { class: "stat" }, h("b", {}, num(u.totals.turns)), h("span", {}, "turns")), h("div", { class: "stat" }, h("b", {}, hours(u.totals.cpu_seconds)), h("span", {}, "cpu time")), h("div", { class: "stat" }, h("b", {}, bytes(u.totals.egress_bytes)), h("span", {}, "egress")), h("div", { class: "stat" }, h("b", {}, bytes(u.storage?.bytes || 0)), h("span", {}, "stored"))))); } } }, "usage"),
      h("button", { class: "ghost small", on: { click: () => { const box = h("input", { value: t.billing_id || "", placeholder: "cus_…" }); out.replaceChildren(h("div", { class: "card flat" }, h("h3", {}, `billing id of ${t.name}`), h("div", { class: "row" }, box, h("button", { class: "act small", on: { click: () => patch(t.name, { billing_id: box.value.trim() }) } }, "save")))); } } }, "billing"),
      h("button", { class: "ghost small", on: { click: async () => { const cur = JSON.stringify(t.limits); const box = h("textarea", { value: cur, style: "min-height:60px" }); const save = h("button", { class: "act small", on: { click: async () => { try { await api("PATCH", `/v1/orgs/${t.name}`, { limits: JSON.parse(box.value) }); shell("Organizations"); } catch (e) { out.replaceChildren(fail(e)); } } } }, "save limits"); out.replaceChildren(h("div", { class: "card flat" }, h("h3", {}, `limits of ${t.name}`), box, h("div", { class: "row end", style: "margin-top:8px" }, save))); } } }, "limits"),
      confirmBtn("delete", "ghost small", async () => { await api("DELETE", `/v1/orgs/${t.name}`); shell("Organizations"); })))));
  return h("div", {}, head("Organizations", "Every organization on the platform, its ceilings and its usage. An organization's content is its own."),
    h("div", { class: "card" }, h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Organization"), h("th", {}, "Status"), h("th", {}, "Plan"), h("th", {}, "Traces"), h("th", {}, "Turns · cpu"), h("th", {}, "Now"), h("th", {}, "Limits"), h("th"))), h("tbody", {}, ...rows)))),
    h("div", { class: "card" }, h("h2", {}, "Create an organization"), h("p", { class: "dim" }, "Its first owner gets a key, shown once below. The plan sets the ceilings it starts with; change them per organization after."), h("div", { class: "row" }, field("Name", name), field("Owner", owner), field("Plan", plan), h("button", { class: "act", style: "align-self:flex-end", on: { click: async () => { try { const r = await api("POST", "/v1/orgs", { name: name.value.trim(), owner: owner.value.trim(), plan: plan.value }); out.replaceChildren(h("div", { class: "secret" }, r.key.secret, " ", copy(r.key.secret))); } catch (e) { out.replaceChildren(fail(e)); } } } }, "Create")), out));
};
VIEWS.Nodes = async function nodes() {
  const list = await api("GET", "/v1/workers");
  const rows = list.data.map(w => h("tr", {}, h("td", {}, h("b", {}, w.name)), h("td", { class: "mono" }, w.id), h("td", {}, w.alive ? tag("alive", "ok") : tag("lost", "bad")), h("td", {}, `${w.running}/${w.slots}`), h("td", {}, w.version), h("td", { class: "muted" }, ago(w.last_seen)), h("td", {}, h("div", { class: "row end" }, confirmBtn("forget", "ghost small", async () => { await api("DELETE", `/v1/workers/${w.id}`); shell("Nodes"); })))));
  return h("div", {}, head("Nodes", "The machines that run turns in sandboxes. A node joins with the join token and heartbeats; one that stops is lost and its turns fail, its sessions go on from their last snapshot on another node."),
    h("div", { class: "card" }, h("div", { class: "table-wrap" }, rows.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Id"), h("th", {}, "State"), h("th", {}, "Slots"), h("th", {}, "Version"), h("th", {}, "Seen"), h("th"))), h("tbody", {}, ...rows)) : empty("No nodes have joined."))));
};

// the organization's usage: what the platform meters, what it only shows
VIEWS.Usage = async function usage() {
  const teams = state.me.teams || [];
  const teamSel = h("select", {}, option("", isOwner() ? "whole organization" : "my own turns"), ...teams.map(t => option(t, `team ${t}`)));
  const pick = (d) => h("button", { class: "quiet small" + (state.window === d ? " on" : ""), on: { click: () => { state.window = d; shell("Usage"); } } }, d ? `${d} days` : "all time");
  const draw = async () => {
    const u = await api("GET", `/v1/usage?days=${state.window}${teamSel.value ? "&team=" + teamSel.value : ""}`);
    const stat = (b, s, note) => h("div", { class: "stat", title: note || "" }, h("b", {}, b), h("span", {}, s));
    const t = u.totals;
    const group = (title, rows, key) => h("div", { class: "card" }, h("h2", {}, title), rows.length ? h("div", { class: "bars" }, ...rows.slice(0, 12).map(r => h("div", { class: "r" }, h("span", { class: "n" }, r[key] || "(organization)"), h("div", { class: "b", style: `width:${100 * r.cpu_seconds / Math.max(1, t.cpu_seconds)}%` }), h("span", { class: "v" }, hours(r.cpu_seconds))))) : empty("nothing yet"));
    body.replaceChildren(
      h("div", { class: "stats" }, stat(num(t.turns), "turns"), stat(hours(t.cpu_seconds), "cpu time", u.meters.cpu_seconds), stat(hours(t.memory_gb_seconds), "GB·time", u.meters.memory_gb_seconds), stat(bytes(t.egress_bytes), "egress", u.meters.egress_bytes), stat(bytes(u.storage?.bytes || 0), "stored", u.meters.storage_gb_days), stat(num(t.input_tokens + t.output_tokens), "tokens, shown", u.shown.input_tokens)),
      h("div", { class: "grid-2", style: "margin-top:16px" }, h("div", { class: "card" }, columns(u.daily, "turns", num, "Turns per day")), h("div", { class: "card" }, columns(u.daily, "cpu_seconds", hours, "CPU time per day"))),
      h("div", { class: "grid-2" }, group("By harness", u.harnesses, "harness"), isOwner() && !teamSel.value ? group("By team", u.teams, "team") : group("By session", u.sessions, "session")),
      isOwner() ? h("div", { class: "grid-2" }, group("By member", u.members, "member"), group("By session", u.sessions, "session")) : null,
      h("div", { class: "card flat" }, h("h3", {}, "What is metered"), h("table", {}, h("tbody", {}, ...Object.entries(u.meters).map(([k, v]) => h("tr", {}, h("td", {}, h("code", {}, k)), h("td", { class: "dim" }, v), h("td", {}, tag("billed", "orange")))), ...Object.entries(u.shown).map(([k, v]) => h("tr", {}, h("td", {}, h("code", {}, k)), h("td", { class: "dim" }, v), h("td", {}, tag("shown")))))), h("a", { href: "/docs/data/" }, "Data, metering and trace sharing →")));
  };
  const body = h("div", {}, h("p", { class: "muted" }, "loading…"));
  teamSel.addEventListener("change", draw);
  await draw();
  return h("div", {}, head("Usage", "What the organization used: the sandbox compute reserved for its turns, the bytes they sent out, the storage kept. Tokens are between you and your provider.", teamSel, pick(7), pick(30), pick(90), pick(0)), body);
};

// the organization: title, plan, trace sharing, webhooks (owners)
VIEWS.Organization = async function organization() {
  const [org, hooks] = await Promise.all([api("GET", "/v1/org"), api("GET", "/v1/webhooks")]);
  const out = h("div");
  const title = h("input", { value: org.title || "", placeholder: "ACME Robotics" });
  const sharing = h("input", { type: "checkbox", checked: org.trace_sharing || false, disabled: org.plan === "enterprise" });
  const url = h("input", { placeholder: "https://ci.example.com/hooks/hg" }), label = h("input", { placeholder: "ci" });
  const hookRows = hooks.data.map(w => h("tr", {}, h("td", {}, h("b", {}, w.label || "—")), h("td", { class: "mono" }, w.url), h("td", {}, w.last_status ? tag(String(w.last_status), w.last_status < 300 ? "ok" : "bad") : tag("never")), h("td", { class: "muted" }, w.failures ? `${w.failures} failed in a row` : w.last_at ? ago(w.last_at) : ""),
    h("td", {}, h("div", { class: "row end" }, h("button", { class: "ghost small", on: { click: async () => { try { const r = await api("POST", `/v1/webhooks/${w.id}/ping`); out.replaceChildren(h("div", { class: "notice " + (r.status < 300 ? "ok" : "warn") }, `ping: HTTP ${r.status || "no answer"}`)); shell("Organization"); } catch (e) { out.replaceChildren(fail(e)); } } } }, "ping"), confirmBtn("delete", "ghost small", async () => { await api("DELETE", `/v1/webhooks/${w.id}`); shell("Organization"); })))));
  return h("div", {}, head(org.title || org.name, `${org.name} · ${org.plan} plan · since ${when(org.created)}`),
    h("div", { class: "grid-2" },
      h("div", { class: "card" }, h("h2", {}, "Settings"), field("Title", title),
        h("div", { style: "margin-top:12px" }, h("label", { class: "row", style: "gap:8px;align-items:flex-start" }, sharing, h("span", {}, h("b", {}, "Share session traces"), h("div", { class: "muted" }, org.plan === "enterprise" ? "An enterprise organization never shares; this is fixed." : "The requests, responses, steers and provider exchanges of sessions from now on, pseudonymized, exported by the deployer to train coding agents. Turning it off ends it at once, past sessions included."), org.trace_sharing && org.trace_sharing_since ? h("div", { class: "muted" }, "sharing since ", when(org.trace_sharing_since)) : null, h("a", { href: "/docs/data/", target: "_blank" }, "What is shared, exactly")))),
        h("div", { class: "row end", style: "margin-top:12px" }, out, h("button", { class: "act", on: { click: async () => { try { await api("PATCH", "/v1/org", { title: title.value, trace_sharing: sharing.checked }); shell("Organization"); } catch (e) { out.replaceChildren(fail(e)); } } } }, "Save"))),
      h("div", { class: "card" }, h("h2", {}, "Plan and ceilings"), h("p", { class: "dim" }, `The ${org.plan} plan. The deployer sets the plan and the ceilings; ask them to change either.`),
        h("table", {}, h("tbody", {}, ...Object.entries(org.limits).map(([k, v]) => h("tr", {}, h("td", {}, h("code", {}, k)), h("td", {}, String(v)))))))),
    h("div", { class: "card" }, h("h2", {}, "Webhooks"), h("p", { class: "dim" }, "Every final response of the organization is posted to each URL, signed with its secret (X-HG-Signature: HMAC-SHA256 of timestamp.body), retried after 1, 5 and 25 seconds. For agents that should not hold a connection open."),
      h("div", { class: "table-wrap" }, hookRows.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Label"), h("th", {}, "URL"), h("th", {}, "Last"), h("th", {}, "State"), h("th"))), h("tbody", {}, ...hookRows)) : empty("No webhooks.")),
      h("div", { class: "row", style: "margin-top:12px" }, field("URL", url), field("Label", label), h("button", { class: "act", style: "align-self:flex-end", on: { click: async () => { try { const w = await api("POST", "/v1/webhooks", { url: url.value.trim(), label: label.value.trim() }); out.replaceChildren(h("div", { class: "secret" }, w.secret, " ", copy(w.secret)), h("p", { class: "muted" }, "The signing secret, shown once. Verify X-HG-Signature with it.")); url.value = ""; } catch (e) { out.replaceChildren(fail(e)); } } } }, "Add webhook")),
      h("a", { href: "/docs/requests/#for-agents-retries-and-webhooks" }, "The event shape and the signature →")));
};
