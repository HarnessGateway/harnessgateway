// Setup: providers, the organization's harnesses (git packages with an editor), the catalog.

VIEWS.Providers = async function providers() {
  const list = await api("GET", "/v1/providers");
  const name = h("input", { placeholder: "deepseek", pattern: "[a-z0-9][a-z0-9._-]*" }), url = h("input", { placeholder: "https://api.deepseek.com/anthropic" }), key = h("input", { type: "password", placeholder: "the provider's API key (sealed on arrival)" });
  const apiSel = h("select", {}, option("anthropic-messages", "Anthropic Messages (Claude Code, Pi, Hermes, Kilo, DeepSeek Harness…)"), option("openai-chat", "OpenAI chat completions (Cline, OpenAI-compatible CLIs)"), option("openai-responses", "OpenAI Responses (Codex)"), option("none", "no key: an open endpoint"));
  const out = h("div");
  const save = async () => { out.replaceChildren(); try { await api("PUT", `/v1/providers/${name.value.trim()}`, { url: url.value.trim(), api: apiSel.value, key: key.value }); shell("Providers"); } catch (e) { out.replaceChildren(fail(e)); } };
  const rows = list.data.map(p => h("tr", {}, h("td", {}, h("b", {}, p.name)), h("td", {}, tag(p.api, "blue")), h("td", { class: "mono" }, p.url),
    h("td", {}, isOwner() ? h("div", { class: "row end" }, h("button", { class: "ghost small", on: { click: () => { name.value = p.name; url.value = p.url; apiSel.value = p.api; key.focus(); } } }, "edit"), confirmBtn("delete", "ghost small", async () => { await api("DELETE", `/v1/providers/${p.name}`); shell("Providers"); })) : null)));
  return h("div", {}, head("Providers", "A provider is a model endpoint and its key. The key is sealed the moment it arrives and opened only inside the gateway, per request; a harness in a sandbox sees a session token and the gateway's address, never the key. The request's model path names the provider: harness/provider/model."),
    h("div", { class: "card" }, h("div", { class: "table-wrap" }, rows.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "API shape"), h("th", {}, "URL"), h("th"))), h("tbody", {}, ...rows)) : empty("No providers yet. Add the endpoint a harness should call."))),
    isOwner() ? h("div", { class: "card" }, h("h2", {}, "Add or replace a provider"), h("p", { class: "dim" }, "DeepSeek serves the Anthropic shape at https://api.deepseek.com/anthropic and the OpenAI chat shape at https://api.deepseek.com/v1; OpenAI serves chat and Responses at https://api.openai.com/v1."),
      h("div", { class: "grid-2" }, field("Name", name), field("API shape", apiSel), field("URL (https)", url), field("Key", key)), h("div", { class: "row end", style: "margin-top:12px" }, out, h("button", { class: "act", on: { click: save } }, "Save provider"))) : h("p", { class: "muted" }, "An owner adds providers."));
};

VIEWS.Catalog = async function catalog(main, args) {
  const cat = await api("GET", "/v1/catalog");
  if (args.show) return catalogDetail(args.show);
  const teams = state.me.teams || [];
  const cards = cat.data.map(p => {
    const v = p.verified || {}, checks = v.checks || {};
    return h("div", { class: "pkg" }, h("b", {}, p.title || p.name), h("span", { class: "muted" }, p.intro),
      h("div", { class: "row", style: "gap:6px" }, tag(`${p.cli.name} ${p.cli.version}`.trim()), p.defaults.provider ? tag(p.defaults.provider, "blue") : null),
      Object.keys(checks).length ? h("div", { class: "verified" }, ...Object.entries(checks).map(([k, ok]) => h("span", { class: ok ? "" : "no", title: v.how || "" }, k))) : h("span", { class: "muted" }, "not verified"),
      h("div", { class: "row", style: "margin-top:4px" }, h("button", { class: "quiet small", on: { click: () => shell("Catalog", { show: p.name }) } }, "Files"), forkButton(p.name, p.name === "template" ? "my-harness" : p.name, teams)));
  });
  return h("div", {}, head("Catalog", "The packages the platform ships, built from each CLI's own help and documentation. Fork one into your organization (or a team) and it is yours: a git repository you edit in place, with the catalog's commit as its first."),
    h("div", { class: "cat" }, ...cards));
};
function forkButton(source, suggested, teams, fromHarness = false) {
  const wrap = h("div", { class: "row" });
  const name = h("input", { value: suggested, style: "width:150px", pattern: "[a-z0-9][a-z0-9._-]*" });
  const team = h("select", {}, isOwner() ? option("", "whole organization") : null, ...teams.map(t => option(t, `team ${t}`)));
  const out = h("div");
  const go = async () => { out.replaceChildren(); try { await api("POST", `/v1/harnesses/${name.value.trim()}`, fromHarness ? { fork: source, team: team.value } : { catalog: source, team: team.value }); shell("Harnesses", { open: name.value.trim() }); } catch (e) { out.replaceChildren(fail(e)); } };
  const btn = h("button", { class: "act small", on: { click: () => { if (wrap.contains(name)) return go(); wrap.replaceChildren(name, team, h("button", { class: "act small", on: { click: go } }, "Create"), out); } } }, "Fork");
  wrap.append(btn);
  return wrap;
}
async function catalogDetail(name) {
  const p = await api("GET", `/v1/catalog/${name}`);
  const viewer = h("pre", { class: "light" }, "pick a file");
  const tree = h("div", { class: "tree" }, ...p.files.map(f => h("button", { on: { click: async (e) => { for (const b of tree.querySelectorAll("button")) b.classList.remove("on"); e.currentTarget.classList.add("on"); viewer.textContent = await (await api("GET", `/v1/catalog/${name}/files/${f.path}`, null, true)).text(); } } }, f.path, h("span", {}, bytes(f.size)))));
  return h("div", {}, head(p.title || name, p.intro, h("button", { class: "quiet", on: { click: () => shell("Catalog") } }, "← Catalog"), forkButton(name, name === "template" ? "my-harness" : name, state.me.teams || [])),
    h("div", { class: "card" }, h("div", { class: "row" }, tag(`${p.cli.name} ${p.cli.version}`.trim()), tag(`accepts: ${(p.accepts || []).join(", ")}`), p.docs ? h("a", { href: p.docs, target: "_blank", rel: "noopener" }, "the CLI's documentation") : null),
      p.verified?.how ? h("p", { class: "muted", style: "margin-top:8px" }, "Verified: ", p.verified.how) : null,
      h("ul", { class: "dim", style: "margin:8px 0 0" }, ...(p.notes || []).map(n => h("li", {}, n)))),
    h("div", { class: "card" }, h("div", { class: "files" }, tree, h("div", { class: "viewer" }, viewer))));
}

VIEWS.Harnesses = async function harnesses(main, args) {
  if (args.open) return harnessEditor(args.open);
  const list = await api("GET", "/v1/harnesses");
  const rows = list.data.map(hn => h("tr", {}, h("td", {}, h("a", { href: "#", on: { click: (e) => { e.preventDefault(); shell("Harnesses", { open: hn.name }); } } }, h("b", {}, hn.name))), h("td", {}, hn.team ? tag(hn.team, "orange") : tag("organization")),
    h("td", {}, `${hn.cli?.name || ""} ${hn.cli?.version || ""}`), h("td", { class: "mono" }, hn.head.slice(0, 8)), h("td", {}, hn.description), h("td", {}, hn.editable ? tag("editable", "ok") : tag("read"))));
  return h("div", {}, head("Harnesses", "Your organization's harness packages: each a git repository, forked from the catalog or imported, edited here and committed. A request's model names one of them.",
      h("button", { class: "act", on: { click: () => shell("Catalog") } }, "New from the catalog")),
    h("div", { class: "card" }, h("div", { class: "table-wrap" }, rows.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Scope"), h("th", {}, "CLI"), h("th", {}, "Head"), h("th", {}, "Description"), h("th"))), h("tbody", {}, ...rows))
      : empty("No harnesses yet. Fork one from the catalog to start."))),
    h("div", { class: "card flat" }, h("h3", {}, "From the command line"), h("pre", { class: "light" }, `hg harness pull ${list.data[0]?.name || "my-harness"} ./pkg      # the files\nhg harness run ./pkg --input "hello"                   # run it here while you work on it\nhg harness push ${list.data[0]?.name || "my-harness"} ./pkg -m "tune the mapper"   # the next commit`)));
};

async function harnessEditor(name) {
  let hn = await api("GET", `/v1/harnesses/${name}`);
  const files = {}; let current = ""; let dirty = false;
  const viewer = h("textarea", { class: "code", spellcheck: "false", disabled: !hn.editable });
  const pathIn = h("input", { placeholder: "path", disabled: !hn.editable });
  const status = h("span", { class: "muted" });
  const tree = h("div", { class: "tree" });
  const out = h("div");
  const load = async (path) => {
    if (dirty && current) files[current] = viewer.value;
    current = path; pathIn.value = path;
    if (!(path in files)) files[path] = await (await api("GET", `/v1/harnesses/${name}/files/${path}`, null, true)).text();
    viewer.value = files[path];
    for (const b of tree.querySelectorAll("button")) b.classList.toggle("on", b.dataset.path === path);
  };
  const drawTree = () => tree.replaceChildren(...hn.files.map(f => h("button", { "data-path": f.path, on: { click: () => load(f.path) } }, f.path, h("span", {}, bytes(f.size)))));
  drawTree();
  viewer.addEventListener("input", () => { dirty = true; status.textContent = "unsaved changes"; });
  const message = h("input", { placeholder: "what changed", style: "width:240px" });
  const changes = {};
  const commit = async (extra = {}) => {
    out.replaceChildren();
    if (current) { if (viewer.value !== files[current] || current in changes) changes[current] = viewer.value; files[current] = viewer.value; }
    const payload = { ...changes, ...extra };
    if (!Object.keys(payload).length) return out.replaceChildren(h("span", { class: "muted" }, "nothing to commit"));
    try {
      hn = await api("POST", `/v1/harnesses/${name}/commits`, { changes: payload, message: message.value.trim() || "edit", parent: hn.head });
      for (const k of Object.keys(changes)) delete changes[k];
      for (const [k, v] of Object.entries(extra)) { if (v === null) delete files[k]; }
      dirty = false; status.textContent = "committed " + hn.head.slice(0, 8); message.value = "";
      drawTree(); if (current && !hn.files.some(f => f.path === current)) { current = ""; viewer.value = ""; pathIn.value = ""; }
      history.replaceChildren(...(await commitsList()));
    } catch (e) { out.replaceChildren(fail(e)); }
  };
  const newFile = async () => { const p = pathIn.value.trim(); if (!p) return; files[p] = files[p] ?? ""; changes[p] = files[p]; hn.files = [...hn.files.filter(f => f.path !== p), { path: p, size: 0, executable: p.endsWith(".sh") }].sort((a, b) => a.path.localeCompare(b.path)); drawTree(); await load(p); dirty = true; status.textContent = "new file: commit to keep it"; };
  const commitsList = async () => (await api("GET", `/v1/harnesses/${name}/commits`)).data.map(c => h("div", { class: "c" }, h("code", {}, c.sha.slice(0, 8)), h("span", {}, c.message.split("\n")[0], " ", h("span", { class: "muted" }, "· ", c.author)), h("span", { class: "muted" }, ago(c.at))));
  const history = h("div", { class: "commits" }, ...(await commitsList()));
  const teams = state.me.teams || [];
  const bar = h("div", { class: "row between", style: "margin-bottom:12px" },
    h("div", { class: "row" }, tag(hn.team ? `team ${hn.team}` : "organization", hn.team ? "orange" : ""), tag(`${hn.cli?.name || ""} ${hn.cli?.version || ""}`.trim()), tag(`head ${hn.head.slice(0, 8)}`), hn.origin ? h("span", { class: "muted" }, "from ", hn.origin) : null),
    h("div", { class: "row" }, h("button", { class: "quiet small", on: { click: () => shell("Conversations", { harness: name }) } }, "Start a conversation"),
      h("a", { class: "btn quiet small", href: `/v1/harnesses/${name}/archive`, download: `${name}.tar.gz` }, "Export"),
      forkButton(name, name + "-copy", teams, true),
      hn.editable ? confirmBtn("Delete harness", "ghost small", async () => { await api("DELETE", `/v1/harnesses/${name}`); shell("Harnesses"); }) : null));
  if (hn.files.length) await load(hn.files.find(f => f.path === "request.sh")?.path || hn.files[0].path);
  return h("div", {}, head(name, hn.description, h("button", { class: "quiet", on: { click: () => shell("Harnesses") } }, "← Harnesses")), bar,
    h("div", { class: "card" }, h("div", { class: "editor" }, h("div", {}, tree, hn.editable ? h("div", { class: "row", style: "margin-top:8px" }, pathIn, h("button", { class: "quiet small", on: { click: newFile } }, "New file")) : null),
        h("div", { class: "viewer" }, h("div", { class: "bar" }, h("span", {}, current || "…"), status, hn.editable ? confirmBtn("remove file", "ghost small", async () => { const p = current; current = ""; await commit({ [p]: null }); }) : null), viewer)),
      hn.editable ? h("div", { class: "row end", style: "margin-top:10px" }, out, message, h("button", { class: "act", on: { click: () => commit() } }, "Commit")) : h("p", { class: "muted", style: "margin-top:10px" }, "Read only: an owner, or a member of its team, edits this harness.")),
    h("div", { class: "grid-2" }, h("div", { class: "card" }, h("h2", {}, "History"), history),
      h("div", { class: "card" }, h("h2", {}, "The package"), h("p", { class: "dim" }, "install.sh puts the CLI in the tools layer once per commit. request.sh reads the client's request from $HG_REQUEST and runs the CLI; the JSON lines it prints whose type starts with response. are Responses stream events the client receives, and they assemble the response. seeds/workspace lands in the workspace once per session; seeds/home in $HOME every turn."),
        h("p", { class: "dim" }, "Accepts: ", (hn.accepts || []).join(", "), ". Egress: install ", (hn.install?.egress || []).join(", ") || "none", "; run ", (hn.run?.egress || []).join(", ") || "none", "."),
        h("a", { href: "/docs/packages/" }, "The package contract, in full →"))));
}
