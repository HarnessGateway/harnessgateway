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
  return h("div", {}, head("Dashboard", "The platform's usage: counts and sizes only, never an organization's prompts, output, packages or keys.", pick(7), pick(30), pick(90), pick(0)),
    h("div", { class: "stats" }, stat(num(u.totals.turns), "turns"), stat(hours(u.totals.seconds), "sandbox time"), stat(num(u.totals.calls), "provider calls"), stat(bytes(u.totals.bytes_in + u.totals.bytes_out), "relayed"), stat(fleet.workers, "workers alive"), stat(`${fleet.running}/${fleet.slots}`, "slots busy"), stat(fleet.queued, "queued")),
    h("div", { class: "grid-2", style: "margin-top:16px" }, h("div", { class: "card" }, columns(u.daily, "turns", num, "Turns per day")), h("div", { class: "card" }, columns(u.daily, "seconds", hours, "Sandbox time per day"))),
    h("div", { class: "grid-2" }, h("div", { class: "card" }, h("h2", {}, "By organization"), h("div", { class: "bars" }, ...u.orgs.map(r => h("div", { class: "r" }, h("span", { class: "n" }, r.org), h("div", { class: "b", style: `width:${100 * r.turns / Math.max(1, u.totals.turns)}%` }), h("span", { class: "v" }, num(r.turns)))))),
      h("div", { class: "card" }, h("h2", {}, "By harness"), h("div", { class: "bars" }, ...u.harnesses.map(r => h("div", { class: "r" }, h("span", { class: "n" }, r.harness), h("div", { class: "b", style: `width:${100 * r.turns / Math.max(1, u.totals.turns)}%` }), h("span", { class: "v" }, num(r.turns))))))));
};
VIEWS.Organizations = async function organizations() {
  const list = await api("GET", "/v1/orgs");
  const name = h("input", { placeholder: "acme" }), owner = h("input", { placeholder: "owner" }), out = h("div");
  const rows = list.data.map(t => h("tr", {}, h("td", {}, h("b", {}, t.name), t.title ? h("span", { class: "muted" }, " · ", t.title) : null), h("td", {}, statusTag(t.status)), h("td", {}, num(t.usage.turns)), h("td", {}, hours(t.usage.seconds)), h("td", {}, `${t.usage.running} running · ${t.usage.queued} queued`), h("td", {}, t.limits.max_runs, " runs · ", t.limits.cpus, " cpu · ", t.limits.memory),
    h("td", {}, h("div", { class: "row end" }, h("button", { class: "ghost small", on: { click: async () => { try { await api("PATCH", `/v1/orgs/${t.name}`, { status: t.status === "active" ? "suspended" : "active" }); shell("Organizations"); } catch (e) { out.replaceChildren(fail(e)); } } } }, t.status === "active" ? "suspend" : "reactivate"),
      h("button", { class: "ghost small", on: { click: async () => { const cur = JSON.stringify(t.limits); const box = h("textarea", { value: cur, style: "min-height:60px" }); const save = h("button", { class: "act small", on: { click: async () => { try { await api("PATCH", `/v1/orgs/${t.name}`, { limits: JSON.parse(box.value) }); shell("Organizations"); } catch (e) { out.replaceChildren(fail(e)); } } } }, "save limits"); out.replaceChildren(h("div", { class: "card flat" }, h("h3", {}, `limits of ${t.name}`), box, h("div", { class: "row end", style: "margin-top:8px" }, save))); } } }, "limits"),
      confirmBtn("delete", "ghost small", async () => { await api("DELETE", `/v1/orgs/${t.name}`); shell("Organizations"); })))));
  return h("div", {}, head("Organizations", "Every organization on the platform, its ceilings and its usage. An organization's content is its own."),
    h("div", { class: "card" }, h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Organization"), h("th", {}, "Status"), h("th", {}, "Turns"), h("th", {}, "Sandbox time"), h("th", {}, "Now"), h("th", {}, "Limits"), h("th"))), h("tbody", {}, ...rows)))),
    h("div", { class: "card" }, h("h2", {}, "Create an organization"), h("p", { class: "dim" }, "Its first owner gets a key, shown once below."), h("div", { class: "row" }, field("Name", name), field("Owner", owner), h("button", { class: "act", style: "align-self:flex-end", on: { click: async () => { try { const r = await api("POST", "/v1/orgs", { name: name.value.trim(), owner: owner.value.trim() }); out.replaceChildren(h("div", { class: "secret" }, r.key.secret, " ", copy(r.key.secret))); } catch (e) { out.replaceChildren(fail(e)); } } } }, "Create")), out));
};
VIEWS.Workers = async function workers() {
  const list = await api("GET", "/v1/workers");
  const rows = list.data.map(w => h("tr", {}, h("td", {}, h("b", {}, w.name)), h("td", { class: "mono" }, w.id), h("td", {}, w.alive ? tag("alive", "ok") : tag("lost", "bad")), h("td", {}, `${w.running}/${w.slots}`), h("td", {}, w.version), h("td", { class: "muted" }, ago(w.last_seen)), h("td", {}, h("div", { class: "row end" }, confirmBtn("forget", "ghost small", async () => { await api("DELETE", `/v1/workers/${w.id}`); shell("Workers"); })))));
  return h("div", {}, head("Workers", "The nodes that run turns. A worker joins with the join token and heartbeats; one that stops is lost and its turns fail, its sessions go on."),
    h("div", { class: "card" }, h("div", { class: "table-wrap" }, rows.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Id"), h("th", {}, "State"), h("th", {}, "Slots"), h("th", {}, "Version"), h("th", {}, "Seen"), h("th"))), h("tbody", {}, ...rows)) : empty("No workers have joined."))));
};
