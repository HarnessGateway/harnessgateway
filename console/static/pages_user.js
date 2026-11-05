// The organization's console: what a developer does in its projects (harnesses, the chat and the playground, keys,
// sessions, usage) and what an admin does for the organization (providers, egress, accounts, projects, the audit log).
// Every call goes to the platform as the signed-in person, in the project the header picks; the platform refuses
// what the person may not do, so the pages only hide what would be refused.
import { METERS, actorOf, api, bytes, date, esc, meter, short, stream, when } from "./api.js";
import { $, $$, card, confirm, fail, field, input, kv, modal, pill, reveal, select, stat, statusPill, table, rerender, then, toast } from "./ui.js";
import { session } from "./session.js";

export function nav(me) {
  const admin = me.role === "admin";
  const mods = [
    { mod: "Setup", items: [{ id: "harnesses", label: "Harnesses", icon: "box" }, { id: "providers", label: "Providers", icon: "globe" },
                           { id: "egress", label: "Egress", icon: "shield" }] },
    { mod: "Workbench", items: [{ id: "chat", label: "Chat", icon: "chat" }, { id: "playground", label: "Playground", icon: "code" }] },
    { mod: "Access", items: [{ id: "keys", label: "API keys", icon: "key" },
                            ...(admin ? [{ id: "projects", label: "Projects", icon: "grid" }, { id: "accounts", label: "Accounts", icon: "users" }] : [])] },
    { mod: "Observability", items: [{ id: "sessions", label: "Sessions", icon: "sess" }, { id: "usage", label: "Usage", icon: "usage" },
                                   ...(admin ? [{ id: "audit", label: "Audit log", icon: "log" }] : [])] }];
  for (const m of mods) for (const it of m.items) it.mod = m.mod;
  return mods;
}

const list = async path => (await api("GET", path)).data || [];
const quiet = path => list(path).catch(() => []);
const projectName = (me, id) => (me.projects.find(p => p.id === id) || {}).name || id || "—";
const setupLink = token => `${location.origin}/setup?token=${encodeURIComponent(token)}`;
const invitation = (title, token) => reveal(title, "Invitation link", setupLink(token),
  "Give this link to the person: it is shown once, works once, and expires in seven days. They choose their password with it.");
const top = (title, lead, acts = "") => `<div class="top"><div><h1>${esc(title)}</h1><p class="lead">${esc(lead)}</p></div><div class="acts">${acts}</div></div>`;

// ── Chat ──────────────────────────────────────────────────────────────────────────────────────────────────────────
const chat = { title: "Chat", full: true, render(el, params) {
  const open = params[0] ? `#open=${encodeURIComponent(params[0])}` : "";
  el.innerHTML = `<iframe class="chat" src="/chat/${open}" title="Chat"></iframe>`;
} };

// ── Sessions ──────────────────────────────────────────────────────────────────────────────────────────────────────
const sessions = { title: "Sessions", async render(el, params, me) {
  if (params[0]) return session.render(el, params, me);
  const client = actorOf(await quiet("/v1/keys"), me.role === "admin" ? await quiet("/v1/accounts") : []);
  const wide = me.role === "admin" && !me.project;
  let rows = [], after = null;
  for (let i = 0; i < 20; i++) {                          // up to 2,000, newest first
    const page = await api("GET", `/v1/containers?limit=100${after ? `&after=${after}` : ""}`);
    rows.push(...page.data); if (!page.has_more) break; after = page.last_id;
  }
  const draw = (filter = "all") => {
    const shown = rows.filter(r => filter === "all" || r.metadata.state === filter);
    $("#sess", el).innerHTML = table([
      { h: "Session", cell: r => `<a href="#/sessions/${encodeURIComponent(r.id)}"><b>${esc(r.name)}</b></a><br><span class="mono">${esc(r.id)}</span>` },
      { h: "Harness · model", cell: r => `${esc(r.metadata.harness)}${r.metadata.model ? ` <span class="muted">${esc(r.metadata.model)}</span>` : ""}` },
      ...(wide ? [{ h: "Project", cell: r => esc(projectName(me, r.metadata.project)) }] : []),
      { h: "Created by", cell: r => esc(client(r.metadata.created_by)) + (r.metadata.user ? `<br><span class="muted small">user ${esc(r.metadata.user)}</span>` : "") },
      { h: "Turns", cell: r => esc(r.metadata.turns) }, { h: "State", cell: r => statusPill(r.metadata.state) },
      { h: "Saved", cell: r => bytes(r.metadata.bytes) }, { h: "Last turn", cell: r => when(r.last_active_at) },
      { h: "", cls: "acts", cell: r => `<a class="btn2" href="#/sessions/${encodeURIComponent(r.id)}">Open</a><button class="btn2 danger" data-del="${r.id}">Delete</button>` }], shown, "No sessions yet.");
  };
  const counts = s => rows.filter(r => s === "all" || r.metadata.state === s).length;
  el.innerHTML = top("Sessions", `Every sandbox session of ${wide ? "this organization" : "this project"}, whichever application started it. Open one for its transcript, traces and workspace.`) +
    `<div class="tabs">${["all", "running", "saving", "idle"].map(s => `<button data-tab="${s}"${s === "all" ? ' class="on"' : ""}>${s[0].toUpperCase() + s.slice(1)} · ${counts(s)}</button>`).join("")}</div>` +
    `<div class="card" id="sess"></div>`;
  draw();
  el.addEventListener("click", async e => {
    const tab = e.target.closest("[data-tab]");
    if (tab) { $$("[data-tab]", el).forEach(b => b.classList.toggle("on", b === tab)); draw(tab.dataset.tab); return; }
    const del = e.target.closest("[data-del]");
    if (del && await confirm("Delete session", `Delete ${del.dataset.del}, its snapshots and files? A running session is refused.`)) {
      try { await api("DELETE", `/v1/containers/${del.dataset.del}`); toast("Deleted", "ok"); rerender(); } catch (err) { fail(err); }
    }
  });
} };

// ── Playground ────────────────────────────────────────────────────────────────────────────────────────────────────
// A one-off request: pick the harness, the provider and the model as the chat does, edit the request, send it, and
// read what comes back beside it. The catalog comes from /v1/models, which every key may read.
function catalogOf(models) {
  const harnesses = {};
  for (const m of models) {
    const [name, provider, ...rest] = m.id.split("/");
    const h = harnesses[name] = harnesses[name] || { name, providers: [], model: "" };
    if (provider) { h.providers.push(provider); h.model = h.model || rest.join("/"); }
  }
  return Object.values(harnesses);
}
const playground = { title: "Playground", async render(el) {
  const harnesses = catalogOf(await list("/v1/models"));
  const body = { model: "", input: "Write hello.py that prints hello, then run it.", stream: true };
  el.innerHTML = top("Playground", "One request to this platform, as any OpenAI SDK would send it, and the one response that comes back.") +
    `<div class="route card" id="route">${select("harness", harnesses.map(h => h.name), "", 'title="Harness"')}<span class="sep">/</span>` +
    `${select("provider", [], "", 'title="Provider"')}<span class="sep">/</span>` +
    `<input name="model" list="pg-models" spellcheck="false" autocomplete="off" title="Model (empty: the harness's default)"><datalist id="pg-models"></datalist>` +
    `<button class="btn" id="send" style="margin-left:auto">Send</button></div>` +
    `<div class="play"><div class="card out"><div class="ch"><h2>Request</h2><span class="muted small">POST /v1/responses</span></div><textarea id="req" spellcheck="false"></textarea></div>` +
    `<div class="card out"><div class="ch"><h2>Response</h2><div class="acts"><span class="muted" id="st"></span></div></div><div id="events"></div><pre id="res">—</pre></div></div>`;
  const route = $("#route", el), req = $("#req", el), res = $("#res", el), events = $("#events", el), st = $("#st", el);
  const hsel = $("[name=harness]", route), psel = $("[name=provider]", route), minput = $("[name=model]", route);
  const harness = () => harnesses.find(h => h.name === hsel.value);
  const picked = () => { const h = harness(); return h && h.providers.length ? [h.name, psel.value, minput.value.trim()].filter(Boolean).join("/") : hsel.value; };
  const sync = () => {                                   // the pickers write the request's model
    try { const b = JSON.parse(req.value); b.model = picked(); req.value = JSON.stringify(b, null, 2); } catch { /* leave the text */ }
  };
  const fillProviders = () => {
    const h = harness(), solo = !h || !h.providers.length;
    psel.innerHTML = (h ? h.providers : []).map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    psel.hidden = solo; $$(".sep", route).forEach(x => { x.hidden = solo; }); minput.hidden = solo;
    minput.value = ""; minput.placeholder = h && h.model ? `${h.model} (default)` : "model";
    $("#pg-models", el).innerHTML = h && h.model ? `<option value="${esc(h.model)}">` : "";
    sync();
  };
  req.value = JSON.stringify(body, null, 2);
  if (!harnesses.length) { route.innerHTML = `<span class="muted">No harness installed yet: add one under Setup → Harnesses.</span>`; return; }
  hsel.addEventListener("change", fillProviders); psel.addEventListener("change", sync); minput.addEventListener("input", sync);
  fillProviders();
  $("#send", el).addEventListener("click", async () => {
    let b; try { b = JSON.parse(req.value); } catch (e) { fail(new Error("the request is not valid JSON: " + e.message)); return; }
    events.innerHTML = ""; res.textContent = "…"; st.textContent = "sending"; $("#send", el).disabled = true;
    try {
      if (b.stream) {
        let final = null, text = "";
        await stream("/v1/responses", b, ev => {
          const t = ev.type || ev.data.type || "";
          if (t === "response.output_text.delta") text += ev.data.delta || "";
          else events.insertAdjacentHTML("beforeend", `<div class="ev"><b>${esc(t)}</b>${t.endsWith("item.done") ? " " + esc(ev.data.item?.type || "") : ""}</div>`);
          if (["response.completed", "response.failed", "response.incomplete"].includes(t)) final = ev.data.response;
          st.textContent = t; events.scrollTop = events.scrollHeight;
        });
        res.textContent = JSON.stringify(final || { output_text: text }, null, 2);
        st.textContent = final ? final.status : "ended";
      } else {
        const r = await api("POST", "/v1/responses", b);
        res.textContent = JSON.stringify(r, null, 2); st.textContent = r.status;
      }
    } catch (e) { res.textContent = e.message; st.textContent = "error"; } finally { $("#send", el).disabled = false; }
  });
} };

// ── Harnesses ─────────────────────────────────────────────────────────────────────────────────────────────────────
const harnesses = { title: "Harnesses", async render(el, params, me) {
  if (params[0]) return harness.render(el, params, me);
  const wide = me.role === "admin" && !me.project;
  const [doc, drafts] = await Promise.all([api("GET", "/v1/harnesses"), api("GET", "/drafts")]);
  const rows = doc.data;
  const hcard = r => `<a class="hcard" href="#/harnesses/${encodeURIComponent(r.name)}"><div class="hh"><b>${esc(r.name)}</b>${r.name === "harness-package-builder" ? pill("built-in", "q") : ""}${wide ? pill(projectName(me, r.project), "b") : ""}</div>` +
    `<div class="hv">${esc(r.version)}</div><div class="hd">${esc(r.description || "")}</div>` +
    `<div class="hf">${r.provider_apis.length ? r.provider_apis.map(a => pill(a, "b")).join(" ") : pill("no model", "q")}<span class="muted small" style="margin-left:auto">${esc([r.default_provider, r.default_model].filter(Boolean).join(" / "))}</span></div>` +
    `<div class="ha"><span class="btn2">Open</span></div></a>`;
  const dcard = d => `<a class="hcard draft" href="#/drafts/${encodeURIComponent(d.name)}"><div class="hh"><b>${esc(d.name)}</b>${pill("draft", "warn")}</div>` +
    `<div class="hv">${esc(d.based_on ? `from ${d.based_on}` : "")}</div><div class="hd muted">${d.files} file${d.files === 1 ? "" : "s"} · edited ${when(d.updated_at)}</div>` +
    `<div class="ha"><span class="btn2">Edit</span><button class="btn2 danger" data-discard="${esc(d.name)}">Discard</button></div></a>`;
  el.innerHTML = top("Harnesses", `The agents ${wide ? "this organization's projects" : "this project"} can run. Every harness is a package: start one from the template and edit its files, or ask the built-in harness package builder in the chat to write one, then install it and try it in the chat or over the API.`,
    wide ? "" : `<button class="btn" id="new">New harness</button>`) +
    (wide ? `<div class="note">Pick a project in the header to write, edit or install harnesses: a harness belongs to one.</div>` : "") +
    (drafts.data.length ? `<h2>Drafts</h2><div class="cards">${drafts.data.map(dcard).join("")}</div>` : "") +
    `<h2>Installed</h2>` + (rows.length ? `<div class="cards">${rows.map(hcard).join("")}</div>` : `<div class="empty">No harness yet. <b>New harness</b> starts one from the template.</div>`);
  $("#new", el)?.addEventListener("click", () => newDraft().catch(fail));
  el.addEventListener("click", async e => {
    const d = e.target.closest("[data-discard]");
    if (d) {
      e.preventDefault();
      if (await confirm("Discard draft", `Discard ${d.dataset.discard} and its files?`, "Discard")) {
        try { await api("DELETE", `/drafts/${d.dataset.discard}`); rerender(); } catch (err) { fail(err); }
      }
    }
  });
} };

// Editing an installed harness: a draft of its files under the same name; installing the draft upgrades it in place.
async function editInstalled(name) {
  const existing = (await api("GET", "/drafts")).data.some(d => d.name === name);
  if (!existing) { try { await api("POST", "/drafts", { name, edit: true }); } catch (e) { fail(e); return; } }
  location.hash = `#/drafts/${encodeURIComponent(name)}`;
}

async function newDraft() {
  modal({ title: "New harness", submit: "Create", body:
    field("Name", input("name", "", 'required pattern="[a-z0-9][a-z0-9._\\-]*" placeholder="claude-code"'),
      "the harness's name, as requests name it in model. The draft starts as a copy of the template: harness.json, install, run and a README that says what to change in each. Or ask the harness package builder in the chat to write one for you"),
    onSubmit: async d => { await api("POST", "/drafts", { name: d.name }); location.hash = `#/drafts/${encodeURIComponent(d.name)}`; } });
}

// the file tree + a viewer or editor, shared by the harness view and the draft editor
function tree(files, open) {
  const dirs = new Set();
  for (const f of files) { const parts = f.path.split("/"); for (let i = 1; i < parts.length; i++) dirs.add(parts.slice(0, i).join("/")); }
  const rows = [...files.map(f => ({ path: f.path, dir: false })), ...[...dirs].map(d => ({ path: d, dir: true }))].sort((a, b) => a.path.localeCompare(b.path));
  return rows.map(r => `<div class="${r.dir ? "d" : "f"}${r.path === open ? " on" : ""}" style="padding-left:${8 + 14 * (r.path.split("/").length - 1)}px"${r.dir ? "" : ` data-file="${esc(r.path)}"`}>${esc(r.path.split("/").pop())}${r.dir ? "/" : ""}</div>`).join("");
}
const EDITOR_CSS = "editor";

const harness = { title: "Harness", async render(el, params, me) {
  const name = params[0];
  const [h, files, providers] = await Promise.all([api("GET", `/v1/harnesses/${encodeURIComponent(name)}`), api("GET", `/v1/harnesses/${encodeURIComponent(name)}/files`), list("/v1/providers")]);
  const fitting = providers.filter(p => h.provider_apis.includes(p.api));
  const settings = Object.entries(h.settings), secrets = Object.entries(h.secrets), mcp = Object.entries(h.mcp || {});
  const tab = params[1] || "files";
  const wide = me.role === "admin" && !me.project;
  el.innerHTML = `<p><a href="#/harnesses">← Harnesses</a></p>` + top(name, (wide ? `Project ${projectName(me, h.project)} · ` : "") + (h.description || ""),
    (wide ? "" : `<button class="btn2" id="edit">Edit</button>`) + `<button class="btn2 danger" id="rm">Uninstall</button>`) +
    `<div class="tabs">${[["files", "Files"], ["manifest", "Manifest"], ["config", "Settings & secrets"], ["defaults", "Defaults"]].map(([t, l]) => `<a href="#/harnesses/${encodeURIComponent(name)}/${t}"${t === tab ? ' class="on"' : ""}>${l}</a>`).join("")}</div>` +
    `<div id="tab"></div>`;
  const body = $("#tab", el);
  if (tab === "files") {
    body.innerHTML = `<div class="ribbon">The package as installed. <b>Edit</b> opens it as a draft; installing the draft upgrades it in place.</div>` +
      `<div class="${EDITOR_CSS}"><div class="tree" id="tree">${tree(files.data, files.data[0]?.path)}</div><div><div class="ribbon" id="fname"></div><pre id="view" class="code">…</pre></div></div>`;
    const show = async path => {
      $("#tree", el).innerHTML = tree(files.data, path);
      const f = files.data.find(x => x.path === path);
      $("#fname", el).innerHTML = `<span class="mono">${esc(path)}</span> · ${bytes(f.bytes)}${f.executable ? " · executable" : ""}`;
      const r = await api("GET", `/v1/harnesses/${encodeURIComponent(name)}/files/${path}`, undefined, { raw: true });
      $("#view", el).textContent = (r.headers.get("content-type") || "").startsWith("text/") || path.endsWith(".json") ? await r.text() : `(binary, ${bytes(f.bytes)})`;
    };
    if (files.data.length) show(files.data.find(f => f.path === "harness.json")?.path || files.data[0].path);
    body.addEventListener("click", e => { const f = e.target.closest("[data-file]"); if (f) show(f.dataset.file); });
  } else if (tab === "manifest") {
    body.innerHTML = `<div class="grid two"><div>` + card("Package", kv([["Version", esc(h.version)], ["Digest", `<span class="mono">${esc(short(h.digest))}</span>`],
      ["Provider APIs", h.provider_apis.map(a => pill(a, "b")).join(" ") || "none (no model)"],
      ["Run egress", h.egress.length ? h.egress.map(e => `<span class="mono">${esc(e)}</span>`).join(", ") : "—"], ["Install egress", h.install_egress.map(e => `<span class="mono">${esc(e)}</span>`).join(", ") || "—"],
      ["Persist", h.persist.map(p => `<span class="mono">${esc(p)}</span>`).join(", ") || "—"], ["Installed", date(h.created_at)], ["Updated", date(h.updated_at)]])) + `</div><div>` +
      (mcp.length ? card("MCP catalog", table([{ h: "Label", cell: ([l]) => esc(l) }, { h: "URL", cell: ([, s]) => `<span class="mono">${esc(s.url)}</span>` }, { h: "Tools", cell: ([, s]) => esc((s.allowed_tools || []).join(", ") || "all") }], mcp)) : card("MCP catalog", `<div class="empty">This package declares no MCP servers.</div>`)) + `</div></div>`;
  } else if (tab === "config") {
    body.innerHTML = `<div class="grid two"><div>` +
      card("Settings", settings.length ? `<form id="settings">${settings.map(([k, s]) => field(k + (s.required ? " *" : ""), input(k, s.value ?? "", `placeholder="${esc(s.default ?? "")}"`), s.description)).join("")}<button class="btn">Save settings</button></form>` : `<div class="empty">This package declares no settings.</div>`) +
      `</div><div>` + card("Secrets", secrets.length ? `<form id="secrets">${secrets.map(([k, s]) => field(`${k}${s.required ? " *" : ""} ${s.set ? pill("set", "ok") : pill("not set", "warn")}`,
        `<div class="row">${input(k, "", 'type="password" autocomplete="off" placeholder="leave empty to keep"')}<button type="button" class="btn2 danger" data-clear="${k}"${s.set ? "" : " disabled"}>Clear</button></div>`,
        `${s.description || ""} · reaches ${s.hosts.join(", ")}`)).join("")}<button class="btn">Save secrets</button><p class="muted small">Values are sealed; they are never shown again and never enter a sandbox.</p></form>` : `<div class="empty">This package declares no secrets.</div>`) + `</div></div>`;
    $("#settings", el)?.addEventListener("submit", async e => {
      e.preventDefault();
      const d = Object.fromEntries(new FormData(e.target)); for (const k in d) if (d[k] === "") d[k] = null;
      try { await api("PATCH", `/v1/harnesses/${name}`, { settings: d }); toast("Saved", "ok"); rerender(); } catch (err) { fail(err); }
    });
    $("#secrets", el)?.addEventListener("submit", async e => {
      e.preventDefault();
      const d = Object.fromEntries(new FormData(e.target)); for (const k in d) if (d[k] === "") delete d[k];
      if (!Object.keys(d).length) { toast("Nothing to save"); return; }
      try { await api("PATCH", `/v1/harnesses/${name}`, { secrets: d }); toast("Sealed", "ok"); rerender(); } catch (err) { fail(err); }
    });
    body.addEventListener("click", async e => {
      const c = e.target.closest("[data-clear]");
      if (c) { try { await api("PATCH", `/v1/harnesses/${name}`, { secrets: { [c.dataset.clear]: null } }); rerender(); } catch (err) { fail(err); } }
    });
  } else {
    body.innerHTML = h.provider_apis.length ? card("Defaults", `<form id="defaults">${field("Provider", select("default_provider", [["", "— the organization's only matching provider —"], ...fitting.map(p => [p.name, `${p.name} (${p.api})`])], h.default_provider || ""), fitting.length ? "" : "no provider speaks this harness's API yet: add one under Setup → Providers")}` +
      `${field("Model", input("default_model", h.default_model || ""), "the model name the CLI is given when a request names none")}<button class="btn">Save defaults</button></form>`) : `<div class="note">This harness calls no model, so it has no provider or model defaults.</div>`;
    $("#defaults", el)?.addEventListener("submit", async e => { e.preventDefault(); try { await api("PATCH", `/v1/harnesses/${name}`, Object.fromEntries(new FormData(e.target))); toast("Saved", "ok"); } catch (err) { fail(err); } });
  }
  $("#edit", el)?.addEventListener("click", () => editInstalled(name));
  $("#rm", el).addEventListener("click", async () => {
    if (await confirm("Uninstall", `Uninstall ${name}? Existing sessions keep running on their pinned package.`, "Uninstall")) {
      try { await api("DELETE", `/v1/harnesses/${name}`); location.hash = "#/harnesses"; } catch (err) { fail(err); }
    }
  });
} };

const drafts = { title: "Draft", async render(el, params, me) {
  const name = params[0];
  if (!name) { location.hash = "#/harnesses"; return; }
  let doc = await api("GET", `/drafts/${encodeURIComponent(name)}/files`);
  let open = null, dirty = false;
  el.innerHTML = `<p><a href="#/harnesses">← Harnesses</a></p>` + top(name, doc.based_on ? `Draft · ${doc.based_on}` : "Draft",
    `<button class="btn2" id="add">Add file</button><button class="btn2" id="del" disabled>Delete file</button><button class="btn2" id="save" disabled>Save</button><button class="btn2 danger" id="discard">Discard</button><button class="btn" id="install">Install</button>`) +
    `<div class="note">Edit the files here; <b>Install</b> packs the draft, installs it and removes the draft. harness.json's <span class="mono">name</span> is the name it installs under, and an installed harness of that name is upgraded; <b>Edit</b> on the harness brings it back here. Then try it: pick it in the chat, or send a request over the API, and watch the session under Observability.</div>` +
    `<div class="${EDITOR_CSS}"><div class="tree" id="tree"></div><div style="display:flex;flex-direction:column;min-height:0"><div class="ribbon" id="fname">Pick a file</div><textarea id="ed" spellcheck="false" style="flex:1;min-height:420px;font-size:12.5px" disabled></textarea></div></div>`;
  const ed = $("#ed", el), saveBtn = $("#save", el), delBtn = $("#del", el);
  const drawTree = () => { $("#tree", el).innerHTML = tree(doc.data, open); };
  const show = async path => {
    if (dirty && !window.confirm("Discard the unsaved changes?")) return;
    open = path; dirty = false; drawTree();
    const f = doc.data.find(x => x.path === path);
    $("#fname", el).innerHTML = `<span class="mono">${esc(path)}</span> · ${bytes(f.bytes)}${f.executable ? " · executable" : ""}`;
    const r = await api("GET", `/drafts/${encodeURIComponent(name)}/files/${path}`, undefined, { raw: true });
    const text = (r.headers.get("content-type") || "").startsWith("text/");
    ed.value = text ? await r.text() : ""; ed.disabled = !text; ed.placeholder = text ? "" : "(binary file)";
    saveBtn.disabled = true; delBtn.disabled = false;
  };
  const save = async () => {
    if (!open) return;
    const r = await fetch(`/api/drafts/${encodeURIComponent(name)}/files/${open}`, { method: "PUT", headers: { "Content-Type": "text/plain" }, body: ed.value });
    if (!r.ok) { fail(new Error((await r.json()).error || r.statusText)); return; }
    dirty = false; saveBtn.disabled = true; toast("Saved", "ok");
    doc = await api("GET", `/drafts/${encodeURIComponent(name)}/files`); drawTree();
  };
  ed.addEventListener("input", () => { dirty = true; saveBtn.disabled = false; });
  ed.addEventListener("keydown", e => { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); } });
  saveBtn.addEventListener("click", save);
  $("#add", el).addEventListener("click", () => modal({ title: "Add file", submit: "Add", body: field("Path", input("path", "", 'placeholder="seeds/home/.config/agent/settings.json" required'), "relative to the package root; folders are created"),
    onSubmit: async d => {
      const r = await fetch(`/api/drafts/${encodeURIComponent(name)}/files/${d.path}`, { method: "PUT", headers: { "Content-Type": "text/plain" }, body: "" });
      if (!r.ok) throw new Error((await r.json()).error || r.statusText);
      doc = await api("GET", `/drafts/${encodeURIComponent(name)}/files`); dirty = false; show(d.path);
    } }));
  delBtn.addEventListener("click", async () => {
    if (open && await confirm("Delete file", `Delete ${open} from the draft?`)) {
      await api("DELETE", `/drafts/${encodeURIComponent(name)}/files/${open}`); doc = await api("GET", `/drafts/${encodeURIComponent(name)}/files`);
      open = null; dirty = false; ed.value = ""; ed.disabled = true; delBtn.disabled = true; saveBtn.disabled = true; $("#fname", el).textContent = "Pick a file"; drawTree();
    }
  });
  $("#discard", el).addEventListener("click", async () => {
    if (await confirm("Discard draft", `Discard ${name} and its files?`, "Discard")) { await api("DELETE", `/drafts/${encodeURIComponent(name)}`); location.hash = "#/harnesses"; }
  });
  $("#install", el).addEventListener("click", async () => {
    if (dirty && !window.confirm("Save the open file first?")) return;
    if (dirty) await save();
    try { const h = await api("POST", `/drafts/${encodeURIComponent(name)}/install`); toast(`Installed ${h.name}`, "ok"); location.hash = `#/harnesses/${encodeURIComponent(h.name)}`; } catch (err) { fail(err); }
  });
  el.addEventListener("click", e => { const f = e.target.closest("[data-file]"); if (f) show(f.dataset.file); });
  drawTree();
  if (doc.data.length) show(doc.data.find(f => f.path === "harness.json")?.path || doc.data[0].path);
} };

// ── Providers ─────────────────────────────────────────────────────────────────────────────────────────────────────
const providers = { title: "Providers", async render(el, params, me) {
  const rows = await list("/v1/providers"), admin = me.role === "admin";
  el.innerHTML = top("Providers", "The organization's model endpoints and their keys, shared by every project. A key is sealed here and swapped in at the gateway; a sandbox never sees it." + (admin ? "" : " An admin adds them."),
    admin ? `<button class="btn" id="add">Add provider</button>` : "") +
    `<div class="card">${table([{ h: "Name", cell: p => `<b>${esc(p.name)}</b>` }, { h: "API shape", cell: p => pill(p.api, "b") },
      { h: "Base URL", cell: p => `<span class="mono">${esc(p.base_url)}</span>` }, { h: "Updated", cell: p => when(p.updated_at) },
      ...(admin ? [{ h: "", cls: "acts", cell: p => `<button class="btn2" data-edit="${p.name}" data-api="${p.api}" data-url="${esc(p.base_url)}">Edit</button><button class="btn2 danger" data-del="${p.name}">Delete</button>` }] : [])],
      rows, "No provider yet. Harnesses that call a model need one.")}</div>`;
  const dialog = (name = "", apiShape = "anthropic", url = "") => modal({ title: name ? `Provider ${name}` : "Add provider", submit: "Save", body:
    field("Name", input("name", name, `${name ? "readonly" : ""} pattern="[a-z0-9][a-z0-9._\\-]*" required placeholder="anthropic"`)) +
    field("API shape", select("api", [["anthropic", "anthropic (Messages)"], ["openai", "openai (Responses / Chat Completions)"], ["google", "google (Gemini)"]], apiShape)) +
    field("Base URL", input("base_url", url || { anthropic: "https://api.anthropic.com", openai: "https://api.openai.com", google: "https://generativelanguage.googleapis.com" }[apiShape], 'required')) +
    field("API key", input("key", "", `type="password" autocomplete="off"${name ? ' placeholder="leave empty to keep the stored key"' : " required"}`), "write-only; never shown again"),
    onSubmit: async d => { await api("PUT", `/v1/providers/${d.name}`, { api: d.api, base_url: d.base_url, ...(d.key ? { key: d.key } : {}) }); toast("Saved", "ok"); rerender(); } });
  $("#add", el)?.addEventListener("click", () => dialog());
  el.addEventListener("click", async e => {
    const ed = e.target.closest("[data-edit]"); if (ed) { dialog(ed.dataset.edit, ed.dataset.api, ed.dataset.url); return; }
    const d = e.target.closest("[data-del]");
    if (d && await confirm("Delete provider", `Delete ${d.dataset.del}? Harnesses defaulting to it fall back to the organization's other providers.`)) {
      try { await api("DELETE", `/v1/providers/${d.dataset.del}`); rerender(); } catch (err) { fail(err); }
    }
  });
} };

// ── Egress ────────────────────────────────────────────────────────────────────────────────────────────────────────
const egress = { title: "Egress", async render(el, params, me) {
  let entries = (await api("GET", "/v1/egress")).egress;
  const harnessesRows = await list("/v1/harnesses"), admin = me.role === "admin";
  const draw = () => { $("#entries", el).innerHTML = table([
    { h: "Entry", cell: e => `<span class="mono">${esc(e)}</span>` }, { h: "", cell: e => e.startsWith("!") ? pill("deny", "bad") : pill("allow", "ok") },
    ...(admin ? [{ h: "", cls: "acts", cell: e => `<button class="btn2 danger" data-rm="${esc(e)}">Remove</button>` }] : [])], entries, "Nothing allowed: sandboxes reach only their provider through the gateway."); };
  el.innerHTML = top("Egress", "What every sandbox of this organization may reach, besides its provider. A harness adds its own entries; a deny entry wins." + (admin ? "" : " An admin changes the organization's entries.")) +
    `<div class="grid two"><div class="card"><div class="ch"><h2>Organization entries</h2></div>${admin ? `<form class="row" id="add" style="margin-bottom:12px">${input("entry", "", 'placeholder="api.github.com · *.npmjs.org · !evil.example.com · host:8443" required')}<button class="btn">Add</button></form>` : ""}<div id="entries"></div></div>` +
    `<div><div class="card"><h2>Syntax</h2><div class="kv"><div><span class="mono">host</span></div><div>that host, port 443</div><div><span class="mono">*.domain</span></div><div>any subdomain, never the domain itself</div><div><span class="mono">host:8443</span></div><div>another port</div><div><span class="mono">!host</span></div><div>deny, even if a harness allows it</div><div><span class="mono">*</span></div><div>everything (deny entries still apply)</div></div></div>` +
    card("Added by harnesses", table([{ h: "Harness", cell: h => esc(h.name) }, { h: "Run egress", cell: h => h.egress.map(e => `<span class="mono">${esc(e)}</span>`).join(", ") || "—" }], harnessesRows.filter(h => h.egress.length), "No harness adds entries.")) + `</div></div>`;
  draw();
  const save = async next => { try { entries = (await api("PUT", "/v1/egress", { egress: next })).egress; draw(); toast("Saved", "ok"); } catch (err) { fail(err); } };
  $("#add", el)?.addEventListener("submit", e => { e.preventDefault(); const v = e.target.entry.value.trim(); if (v) { save([...entries, v]); e.target.reset(); } });
  el.addEventListener("click", e => { const b = e.target.closest("[data-rm]"); if (b) save(entries.filter(x => x !== b.dataset.rm)); });
} };

// ── API keys ──────────────────────────────────────────────────────────────────────────────────────────────────────
const keys = { title: "API keys", async render(el, params, me) {
  const rows = await list("/v1/keys"), admin = me.role === "admin", wide = admin && !me.project;
  const kind = k => k.role === "admin" ? pill("admin · organization", "run") : k.run_only ? pill("run-only", "b") : pill("developer", "b");
  el.innerHTML = top("API keys", "Keys your applications use with any OpenAI SDK. A run-only key sends requests and reads the sessions it made: what an application holds. A developer key does everything a developer does in its project; an admin key, everything in the organization.",
    `<button class="btn" id="add">Create key</button>`) +
    `<div class="card">${table([{ h: "Key", cell: k => `<span class="mono">${esc(k.hint)}</span>` }, { h: "Label", cell: k => esc(k.label || "—") },
      { h: "Kind", cell: kind }, ...(wide ? [{ h: "Project", cell: k => esc(k.project ? projectName(me, k.project) : "—") }] : []),
      { h: "Expires", cell: k => k.expires_at ? date(k.expires_at) : "never" }, { h: "Created", cell: k => when(k.created_at) }, { h: "Last used", cell: k => when(k.last_used_at) },
      { h: "", cls: "acts", cell: k => `<button class="btn2 danger" data-del="${k.id}">Revoke</button>` }], rows, "No keys.")}</div>`;
  const kinds = [["run_only", "run-only — responses, its own sessions and files, models"], ["developer", "developer — everything in the project"],
                 ...(admin ? [["admin", "admin — everything in the organization"]] : [])];
  const projects = me.projects.map(p => [p.id, p.name]);
  $("#add", el).addEventListener("click", () => modal({ title: "Create an API key", submit: "Create", body:
    field("Label", input("label", "", 'placeholder="my-app"')) + field("Kind", select("kind", kinds)) +
    (admin ? field("Project", select("project", projects, me.project || (projects[0] || [""])[0]), "for a run-only or developer key") : "") +
    field("Expires in (days)", input("expires_in_days", "", 'type="number" min="1" max="3650" placeholder="never"')),
    onSubmit: async d => {
      const body = { label: d.label, role: d.kind === "admin" ? "admin" : "developer", run_only: d.kind === "run_only",
                     project: d.kind === "admin" ? null : (d.project || me.project), ...(d.expires_in_days ? { expires_in_days: +d.expires_in_days } : {}) };
      const k = await api("POST", "/v1/keys", body);
      then(() => reveal("Your new key", "API key", k.key, "Copy it now: it is shown once.").then(rerender));
    } }));
  el.addEventListener("click", async e => {
    const d = e.target.closest("[data-del]");
    if (d && await confirm("Revoke key", "Revoke this key? Applications using it stop working at once.", "Revoke")) {
      try { await api("DELETE", `/v1/keys/${d.dataset.del}`); rerender(); } catch (err) { fail(err); }
    }
  });
} };

// ── Usage ─────────────────────────────────────────────────────────────────────────────────────────────────────────
export function usagePage(path, groups, lead) {
  return { title: "Usage", async render(el) {
    const now = new Date(), first = new Date(now.getFullYear(), now.getMonth(), 1);
    const iso = d => d.toISOString().slice(0, 10);
    el.innerHTML = top("Usage", lead) + `<form class="row card" id="f" style="margin-bottom:14px">${field("From", input("start", iso(first), 'type="date"'))}${field("To", input("end", iso(now), 'type="date"'))}${field("Group by", select("group_by", groups))}<button class="btn">Show</button></form><div id="out"></div>`;
    const show = async () => {
      const f = $("#f", el), start = Math.floor(new Date(f.start.value).getTime() / 1000), end = Math.floor(new Date(f.end.value).getTime() / 1000) + 86400;
      const u = await api("GET", `${path}?start=${start}&end=${end}${f.group_by.value ? `&group_by=${f.group_by.value}` : ""}`);
      const rows = Object.entries(u.data).sort(), totals = {};
      for (const [, m] of rows) for (const k in m) totals[k] = (totals[k] || 0) + m[k];
      $("#out", el).innerHTML = `<div class="grid" style="grid-template-columns:repeat(5,1fr)">${u.meters.map(m => stat(METERS[m], meter(m, totals[m] || 0))).join("")}</div>` +
        (f.group_by.value ? `<div class="card">${table([{ h: f.group_by.value, cell: ([g]) => `<span class="mono">${esc(g)}</span>` }, ...u.meters.map(m => ({ h: METERS[m], cell: ([, d]) => meter(m, d[m] || 0) }))], rows, "No usage in this range.")}</div>` : "");
    };
    $("#f", el).addEventListener("submit", e => { e.preventDefault(); show().catch(fail); });
    await show();
  } };
}
const usage = usagePage("/v1/usage", [["", "total"], ["day", "day"], ["project", "project"], ["harness", "harness"], ["session", "session"], ["user", "end user"]],
  "What the sandboxes used: allocated CPU and memory over time, bytes through the door, storage held (the organization's, so an admin sees it for the whole organization).");

// ── Accounts (admins) ─────────────────────────────────────────────────────────────────────────────────────────────
const accounts = { title: "Accounts", async render(el, params, me) {
  const rows = await list("/v1/accounts");
  const status = a => a.status === "active" ? pill("active", "ok") : a.status === "invited" ? pill("invited", "warn") : pill("disabled", "bad");
  el.innerHTML = top("Accounts", "Who signs in to this organization. An admin manages everything in it; a developer works in the projects it belongs to. A new account is invited: give the person the link, and they choose their password.",
    `<button class="btn" id="add">Invite</button>`) +
    `<div class="card">${table([{ h: "Account", cell: a => `<b>${esc(a.name || a.email)}</b>${a.id === me.account.id ? ' <span class="muted">(you)</span>' : ""}<br><span class="muted small">${esc(a.email)}</span>` },
      { h: "Role", cell: a => pill(a.role, a.role === "admin" ? "run" : "b") + (a.platform_admin ? " " + pill("platform admin", "q") : "") }, { h: "Status", cell: status },
      { h: "Last sign-in", cell: a => when(a.signed_in_at) }, { h: "Created", cell: a => when(a.created_at) },
      { h: "", cls: "acts", cell: a => a.id === me.account.id ? "" :
        `<button class="btn2" data-role="${a.id}" data-to="${a.role === "admin" ? "developer" : "admin"}">Make ${a.role === "admin" ? "developer" : "admin"}</button>` +
        `<button class="btn2" data-reset="${a.id}">Reset password</button>` +
        (a.status === "disabled" ? `<button class="btn2" data-status="active" data-id="${a.id}">Enable</button>` : `<button class="btn2 danger" data-status="disabled" data-id="${a.id}">Disable</button>`) }], rows)}</div>`;
  $("#add", el).addEventListener("click", () => modal({ title: "Invite an account", submit: "Invite", body:
    field("Email", input("email", "", 'type="email" required placeholder="dev@example.com"'), "their login") + field("Name", input("name", "")) +
    field("Role", select("role", [["developer", "developer — works in the projects you add them to"], ["admin", "admin — everything in the organization"]])) +
    (me.account.platform_admin ? field("Platform scope", select("platform_admin", [["", "no"], ["1", "yes — the platform console too"]]), "sees every organization, the nodes and the configuration") : ""),
    onSubmit: async d => {
      const a = await api("POST", "/v1/accounts", { email: d.email, name: d.name, role: d.role, platform_admin: !!d.platform_admin });
      then(() => invitation(`Invitation for ${a.email}`, a.invitation).then(rerender));
    } }));
  el.addEventListener("click", async e => {
    const r = e.target.closest("[data-role]");
    if (r) { try { await api("PATCH", `/v1/accounts/${r.dataset.role}`, { role: r.dataset.to }); rerender(); } catch (err) { fail(err); } return; }
    const rs = e.target.closest("[data-reset]");
    if (rs && await confirm("Reset password", "Every sign-in of this account ends, and a new invitation link lets them choose a password.", "Reset")) {
      try { const a = await api("POST", `/v1/accounts/${rs.dataset.reset}/reset`); then(() => invitation(`Reset for ${a.email}`, a.invitation)); } catch (err) { fail(err); }
      return;
    }
    const st = e.target.closest("[data-status]");
    if (st && (st.dataset.status === "active" || await confirm("Disable account", "Their sign-ins and keys stop working at once.", "Disable"))) {
      try { await api("PATCH", `/v1/accounts/${st.dataset.id}`, { status: st.dataset.status }); rerender(); } catch (err) { fail(err); }
    }
  });
} };

// ── Projects (admins) ─────────────────────────────────────────────────────────────────────────────────────────────
const projects = { title: "Projects", async render(el, params, me) {
  if (params[0]) return project.render(el, params, me);
  const rows = await list("/v1/projects");
  el.innerHTML = top("Projects", "The unit a team builds in: its harnesses, the developers who belong to it, the keys its applications hold, and their sessions. Every new project starts with the harness package builder installed.",
    `<button class="btn" id="add">New project</button>`) +
    `<div class="card">${table([{ h: "Project", cell: p => `<a href="#/projects/${encodeURIComponent(p.id)}"><b>${esc(p.name)}</b></a><br><span class="mono">${esc(p.id)}</span>` },
      { h: "Developers", cell: p => p.members }, { h: "Harnesses", cell: p => p.harnesses }, { h: "Created", cell: p => when(p.created_at) },
      { h: "", cls: "acts", cell: p => `<a class="btn2" href="#/projects/${encodeURIComponent(p.id)}">Open</a>` }], rows)}</div>`;
  $("#add", el).addEventListener("click", () => modal({ title: "New project", submit: "Create", body:
    field("Name", input("name", "", 'required pattern="[a-z0-9][a-z0-9._\\-]*" placeholder="market-desk"'), "lowercase letters, digits, dots, dashes and underscores"),
    onSubmit: async d => { const p = await api("POST", "/v1/projects", { name: d.name }); location.href = `/#/projects/${encodeURIComponent(p.id)}`; location.reload(); } }));
} };

const project = { title: "Project", async render(el, params, me) {
  const id = params[0];
  const [p, members, all] = await Promise.all([api("GET", `/v1/projects/${encodeURIComponent(id)}`), list(`/v1/projects/${encodeURIComponent(id)}/members`), list("/v1/accounts")]);
  const candidates = all.filter(a => a.role !== "admin" && a.status !== "disabled" && !members.some(m => m.id === a.id));
  el.innerHTML = `<p><a href="#/projects">← Projects</a></p>` + top(p.name, `Project · ${p.harnesses} harness${p.harnesses === 1 ? "" : "es"} · ${p.members} developer${p.members === 1 ? "" : "s"}`,
    `<button class="btn2" id="rename">Rename</button><button class="btn2 danger" id="del">Delete</button>`) +
    `<div class="grid two"><div>${card("Developers", table([{ h: "Account", cell: m => `<b>${esc(m.name || m.email)}</b><br><span class="muted small">${esc(m.email)}</span>` }, { h: "Status", cell: m => statusPill(m.status) },
      { h: "", cls: "acts", cell: m => `<button class="btn2 danger" data-rm="${m.id}">Remove</button>` }], members, "No developer yet. Admins work in every project."),
      candidates.length ? `<button class="btn" id="addm">Add developer</button>` : "")}</div>` +
    `<div>${card("Working in it", `<p class="muted small">Pick <b>${esc(p.name)}</b> in the header to see its harnesses, sessions and keys. Developers see it as soon as they belong to it; an application holds a run-only key of it.</p>`)}</div></div>`;
  $("#addm", el)?.addEventListener("click", () => modal({ title: "Add a developer", submit: "Add", body:
    field("Account", select("account", candidates.map(a => [a.id, `${a.name || a.email} <${a.email}>`]))),
    onSubmit: async d => { await api("PUT", `/v1/projects/${encodeURIComponent(id)}/members/${d.account}`); rerender(); } }));
  $("#rename", el).addEventListener("click", () => modal({ title: "Rename project", submit: "Rename", body: field("Name", input("name", p.name, 'required pattern="[a-z0-9][a-z0-9._\\-]*"')),
    onSubmit: async d => { await api("PATCH", `/v1/projects/${encodeURIComponent(id)}`, { name: d.name }); rerender(); } }));
  $("#del", el).addEventListener("click", async () => {
    if (await confirm("Delete project", `Delete ${p.name}, its harnesses and keys? A project with sessions is refused: delete them first.`)) {
      try { await api("DELETE", `/v1/projects/${encodeURIComponent(id)}`); location.hash = "#/projects"; } catch (err) { fail(err); }
    }
  });
  el.addEventListener("click", async e => {
    const rm = e.target.closest("[data-rm]");
    if (rm && await confirm("Remove developer", "They lose access to this project's harnesses, sessions and keys.", "Remove")) {
      try { await api("DELETE", `/v1/projects/${encodeURIComponent(id)}/members/${rm.dataset.rm}`); rerender(); } catch (err) { fail(err); }
    }
  });
} };

// ── Audit log ─────────────────────────────────────────────────────────────────────────────────────────────────────
export function auditPage(path, lead) {
  return { title: "Audit log", async render(el) {
    const [rows, keys, accounts] = await Promise.all([list(`${path}?limit=100`), quiet("/v1/keys"), quiet("/v1/accounts")]);
    const who = actorOf(keys, accounts);
    el.innerHTML = top("Audit log", lead) +
      `<div class="card">${table([{ h: "When", cell: r => date(r.at) }, { h: "Who", cell: r => esc(who(r.actor)) }, { h: "Action", cell: r => `<span class="mono">${esc(r.action)}</span>` },
        { h: "Target", cell: r => `<span class="mono">${esc(r.target || "")}</span>` },
        { h: "Detail", cell: r => `<span class="mono small">${esc(Object.entries(r.detail || {}).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(" "))}</span>` }], rows, "Nothing yet.")}</div>`;
  } };
}
const audit = auditPage("/v1/audit", "Every change of standing in this organization, newest first: sign-ins and their failures, accounts, keys, projects, providers, egress, harnesses.");

export const pages = { chat, sessions, playground, harnesses, drafts, providers, egress, keys, usage, accounts, projects, audit };
