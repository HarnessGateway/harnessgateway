// The organization's console: the workbench (everyone) and the organization's settings (admins).
import { METERS, api, bytes, date, esc, meter, short, stream, when } from "./api.js";
import { $, $$, card, confirm, copy, fail, field, input, kv, modal, pill, pre, reveal, select, stat, statusPill, table, rerender, then, toast } from "./ui.js";

export function nav(me) {
  const work = { mod: "Workbench", items: [{ id: "chat", label: "Chat", icon: "chat" }, { id: "playground", label: "Playground", icon: "code" }] };
  const mods = me.role === "member" ? [work] : [
    { mod: "Setup", items: [{ id: "providers", label: "Providers", icon: "globe" }, { id: "harnesses", label: "Harnesses", icon: "box" },
                           { id: "egress", label: "Egress", icon: "shield" }] },
    work,
    { mod: "Access", items: [{ id: "keys", label: "API keys", icon: "key" }, { id: "members", label: "Members", icon: "users" }] },
    { mod: "Observability", items: [{ id: "sessions", label: "Sessions", icon: "sess" }, { id: "traces", label: "Traces", icon: "search" },
                                   { id: "usage", label: "Usage", icon: "usage" }] }];
  for (const m of mods) for (const it of m.items) it.mod = m.mod;
  return mods;
}

const list = async path => (await api("GET", path)).data || [];
const top = (title, lead, acts = "") => `<div class="top"><div><h1>${esc(title)}</h1><p class="lead">${esc(lead)}</p></div><div class="acts">${acts}</div></div>`;

// ── Chat ──────────────────────────────────────────────────────────────────────────────────────────────────────────
const chat = { title: "Chat", full: true, render(el, params) {
  const open = params[0] ? `#open=${encodeURIComponent(params[0])}` : "";
  el.innerHTML = `<iframe class="chat" src="/chat/${open}" title="Chat"></iframe>`;
} };

// ── Sessions ──────────────────────────────────────────────────────────────────────────────────────────────────────
const sessions = { title: "Sessions", async render(el, params, me) {
  let rows = [], after = null;
  for (let i = 0; i < 20; i++) {                          // up to 2,000, newest first
    const page = await api("GET", `/v1/containers?limit=100${after ? `&after=${after}` : ""}`);
    rows.push(...page.data); if (!page.has_more) break; after = page.last_id;
  }
  const draw = (filter = "all") => {
    const shown = rows.filter(r => filter === "all" || r.metadata.state === filter);
    $("#sess", el).innerHTML = table([
      { h: "Session", cell: r => `<b>${esc(r.name)}</b><br><span class="mono">${esc(r.id)}</span>` },
      { h: "Harness · model", cell: r => `${esc(r.metadata.harness)}${r.metadata.model ? ` <span class="muted">${esc(r.metadata.model)}</span>` : ""}` },
      { h: "Turns", cell: r => esc(r.metadata.turns) }, { h: "State", cell: r => statusPill(r.metadata.state) },
      { h: "Saved", cell: r => bytes(r.metadata.bytes) }, { h: "Last turn", cell: r => when(r.last_active_at) },
      { h: "", cls: "acts", cell: r => `<a class="btn2" href="#/chat/${encodeURIComponent(r.id)}">Open in Chat</a><button class="btn2" data-files="${r.id}">Files</button>` +
          (r.metadata.last_response_id && me.role !== "member" ? `<a class="btn2" href="#/traces/${encodeURIComponent(r.metadata.last_response_id)}">Trace</a>` : "") +
          `<button class="btn2 danger" data-del="${r.id}">Delete</button>` }], shown, "No sessions yet: start one in Chat or through the API.");
  };
  const counts = s => rows.filter(r => s === "all" || r.metadata.state === s).length;
  el.innerHTML = top("Sessions", "Every sandbox session of this organization, including ones your applications started through the API.",
    `<a class="btn" href="#/chat">New chat</a>`) +
    `<div class="tabs">${["all", "running", "saving", "idle"].map(s => `<button data-tab="${s}"${s === "all" ? ' class="on"' : ""}>${s[0].toUpperCase() + s.slice(1)} · ${counts(s)}</button>`).join("")}</div>` +
    `<div class="card" id="sess"></div>`;
  draw();
  el.addEventListener("click", async e => {
    const tab = e.target.closest("[data-tab]");
    if (tab) { $$("[data-tab]", el).forEach(b => b.classList.toggle("on", b === tab)); draw(tab.dataset.tab); return; }
    const files = e.target.closest("[data-files]");
    if (files) { workspaceFiles(files.dataset.files); return; }
    const del = e.target.closest("[data-del]");
    if (del && await confirm("Delete session", `Delete ${del.dataset.del}, its snapshots and files? A running session is refused.`)) {
      try { await api("DELETE", `/v1/containers/${del.dataset.del}`); toast("Deleted", "ok"); rerender(); } catch (err) { fail(err); }
    }
  });
} };

async function workspaceFiles(sid) {
  const files = await list(`/v1/containers/${sid}/files?limit=100`);
  await modal({ title: `Workspace of ${sid}`, wide: true, body:
    table([{ h: "Path", cell: f => esc(f.path.replace(/^\/workspace\//, "")) }, { h: "Size", cell: f => bytes(f.bytes) },
           { h: "", cls: "acts", cell: f => `<a class="btn2" href="/api/v1/containers/${sid}/files/${f.id}/content" download="${esc(f.path.split("/").pop())}">Download</a>` }],
          files, "No files saved yet.") });
}

// ── Playground ────────────────────────────────────────────────────────────────────────────────────────────────────
const playground = { title: "Playground", async render(el, params, me) {
  const models = (await list("/v1/models")).map(m => m.id);
  const body = { model: models[0] || "shell", input: "Write hello.py that prints hello, then run it.", stream: true };
  el.innerHTML = top("Playground", "A raw Responses request to this platform, and what comes back.") +
    `<div class="play"><div class="card out"><div class="ch"><h2>Request</h2><div class="acts"><button class="btn2" data-snip="curl">curl</button><button class="btn2" data-snip="python">Python</button><button class="btn2" data-snip="ts">TypeScript</button></div></div>` +
    `<div class="row" style="margin-bottom:10px">${select("model", models, body.model)}<button class="btn" id="send">Send</button></div>` +
    `<textarea id="req" spellcheck="false">${esc(JSON.stringify(body, null, 2))}</textarea></div>` +
    `<div class="card out"><div class="ch"><h2>Response</h2><div class="acts"><span class="muted" id="st"></span></div></div><div id="events" style="max-height:40%;overflow:auto;margin-bottom:10px"></div><pre id="res">—</pre></div></div>`;
  const req = $("#req", el), res = $("#res", el), events = $("#events", el), st = $("#st", el);
  $("select[name=model]", el).addEventListener("change", e => {
    try { const b = JSON.parse(req.value); b.model = e.target.value; req.value = JSON.stringify(b, null, 2); } catch { /* leave the text */ }
  });
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
  el.addEventListener("click", e => {
    const s = e.target.closest("[data-snip]");
    if (!s) return;
    let b; try { b = JSON.parse(req.value); } catch { b = {}; }
    const base = me.api.replace(/\/$/, "") + "/v1", j = JSON.stringify(b, null, 2);
    const snippets = {
      curl: `curl ${base}/responses -H "authorization: Bearer $HG_API_KEY" -H 'content-type: application/json' -d '${j.replace(/'/g, "'\\''")}'`,
      python: `from openai import OpenAI\nclient = OpenAI(base_url="${base}", api_key=os.environ["HG_API_KEY"])\nr = client.responses.create(**${j})\nprint(r.output_text)`,
      ts: `import OpenAI from "openai";\nconst client = new OpenAI({ baseURL: "${base}", apiKey: process.env.HG_API_KEY });\nconst r = await client.responses.create(${j});\nconsole.log(r.output_text);`,
    };
    modal({ title: `As ${s.dataset.snip}`, wide: true, body: pre(snippets[s.dataset.snip]) + `<p class="muted small">Use an API key from the organization (admins: API keys page). The console's own keys never leave the server.</p>`, submit: "Copy", onSubmit: () => copy(snippets[s.dataset.snip]) });
  });
} };

// ── Harnesses ─────────────────────────────────────────────────────────────────────────────────────────────────────
const harnesses = { title: "Harnesses", async render(el, params, me) {
  if (params[0]) return harness.render(el, params, me);
  const [doc, drafts] = await Promise.all([api("GET", "/v1/harnesses"), api("GET", "/drafts")]);
  const rows = doc.data, builtins = doc.builtins || [], installed = new Set(rows.map(r => r.name));
  const missing = builtins.filter(b => !installed.has(b));
  const sourceOf = r => r.source?.builtin ? "built-in" : r.source?.git ? "git" : "created";
  const hcard = r => {
    const src = sourceOf(r);
    return `<a class="hcard" href="#/harnesses/${encodeURIComponent(r.name)}"><div class="hh"><b>${esc(r.name)}</b>${src === "built-in" ? pill("built-in", "q") : src === "git" ? pill("git", "b") : ""}</div>` +
      `<div class="hv">${esc(r.version)}${r.extends ? ` · overlay on ${esc(r.extends)}` : ""}</div><div class="hd">${esc(r.description || "")}</div>` +
      `<div class="hf">${r.provider_apis.length ? r.provider_apis.map(a => pill(a, "b")).join(" ") : pill("no model", "q")}<span class="muted small" style="margin-left:auto">${esc([r.default_provider, r.default_model].filter(Boolean).join(" / "))}</span></div>` +
      `<div class="ha"><span class="btn2">${src === "built-in" ? "View" : "Edit"}</span><button class="btn2" data-fork="${esc(r.name)}">Fork</button></div></a>`;
  };
  const dcard = d => `<a class="hcard draft" href="#/drafts/${encodeURIComponent(d.name)}"><div class="hh"><b>${esc(d.name)}</b>${pill("draft", "warn")}</div>` +
    `<div class="hv">${esc(d.based_on || "")}</div><div class="hd muted">${d.files} file${d.files === 1 ? "" : "s"} · edited ${when(d.updated_at)}</div>` +
    `<div class="ha"><span class="btn2">Edit</span><button class="btn2 danger" data-discard="${esc(d.name)}">Discard</button></div></a>`;
  el.innerHTML = top("Harnesses", "The agents this organization can run. Open any to read it, fork it into your own, or start a new one.",
    `<button class="btn2" id="git">Install from git</button>${missing.length ? `<button class="btn2" id="builtin">Install a built-in</button>` : ""}<button class="btn" id="new">New harness</button>`) +
    (drafts.data.length ? `<h2>Drafts</h2><div class="cards">${drafts.data.map(dcard).join("")}</div>` : "") +
    `<h2>Installed</h2>` + (rows.length ? `<div class="cards">${rows.map(hcard).join("")}</div>` : `<div class="empty">No harness installed.</div>`);
  $("#new", el).addEventListener("click", () => newDraft(rows.filter(r => !r.extends).map(r => r.name)));
  $("#git", el).addEventListener("click", () => modal({ title: "Install from git", submit: "Install", body:
    field("Repository URL", input("url", "", 'placeholder="https://github.com/acme/agents" required')) + field("Ref", input("ref", "main")) + field("Path in the repository", input("path", ".")),
    onSubmit: async d => { await api("POST", "/v1/harnesses", { git: { url: d.url, ref: d.ref, path: d.path } }); toast("Installed", "ok"); rerender(); } }));
  $("#builtin", el)?.addEventListener("click", () => modal({ title: "Install a built-in", submit: "Install", body: field("Package", select("builtin", missing)),
    onSubmit: async d => { await api("POST", "/v1/harnesses", { builtin: d.builtin }); toast("Installed", "ok"); rerender(); } }));
  el.addEventListener("click", async e => {
    const f = e.target.closest("[data-fork]");
    if (f) { e.preventDefault(); forkDraft(f.dataset.fork); return; }
    const d = e.target.closest("[data-discard]");
    if (d) {
      e.preventDefault();
      if (await confirm("Discard draft", `Discard ${d.dataset.discard} and its files?`, "Discard")) {
        try { await api("DELETE", `/drafts/${d.dataset.discard}`); rerender(); } catch (err) { fail(err); }
      }
    }
  });
} };

// Editing an installed harness that is the organization's own: a draft under the same name; installing it upgrades.
async function editInstalled(name) {
  const existing = (await api("GET", "/drafts")).data.some(d => d.name === name);
  if (!existing) { try { await api("POST", "/drafts", { name, fork: name }); } catch (e) { fail(e); return; } }
  location.hash = `#/drafts/${encodeURIComponent(name)}`;
}

function forkDraft(source) {
  modal({ title: `Fork ${source}`, submit: "Fork", body: field("Draft name", input("name", `${source}-fork`, 'required pattern="[a-z0-9][a-z0-9._\\-]*"'),
    "the new package's name; its files are copied and edited here, then installed under this name"),
    onSubmit: async d => { await api("POST", "/drafts", { name: d.name, fork: source }); location.hash = `#/drafts/${encodeURIComponent(d.name)}`; } });
}
function newDraft(bases) {
  modal({ title: "New harness", submit: "Create", body:
    field("Name", input("name", "", 'required pattern="[a-z0-9][a-z0-9._\\-]*" placeholder="acme-reviewer"')) +
    field("Kind", select("template", [["overlay", "overlay — your instructions, skills, hooks and MCP catalog on a built-in CLI"], ["base", "base package — a new CLI, from the template"]])) +
    field("Extends (overlay)", select("extends", bases), "the base harness an overlay builds on"),
    onSubmit: async d => { await api("POST", "/drafts", { name: d.name, template: d.template, extends: d.extends }); location.hash = `#/drafts/${encodeURIComponent(d.name)}`; } });
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
  const builtin = !!h.source?.builtin, source = builtin ? "built-in" : h.source?.git ? `git · ${h.source.git.url}@${h.source.git.ref || ""}` : "created here";
  el.innerHTML = `<p><a href="#/harnesses">← Harnesses</a></p>` + top(name, h.description || "",
    `${builtin ? "" : `<button class="btn2" id="edit">Edit</button>`}<button class="btn2" id="fork">Fork</button><button class="btn2 danger" id="rm">Uninstall</button>`) +
    `<div class="tabs">${[["files", "Files"], ["manifest", "Manifest"], ["config", "Settings & secrets"], ["defaults", "Defaults"]].map(([t, l]) => `<a href="#/harnesses/${encodeURIComponent(name)}/${t}"${t === tab ? ' class="on"' : ""}>${l}</a>`).join("")}</div>` +
    `<div id="tab"></div>`;
  const body = $("#tab", el);
  if (tab === "files") {
    body.innerHTML = `<div class="ribbon">${pill(source, builtin ? "q" : "b")} ${h.extends ? `overlay on <b>${esc(h.extends)}</b> · the overlay's files are marked · ` : ""}${builtin ? "read-only: <b>Fork</b> to edit a copy" : "<b>Edit</b> opens it as a draft; installing the draft upgrades it in place"}</div>` +
      `<div class="${EDITOR_CSS}"><div class="tree" id="tree">${tree(files.data, files.data[0]?.path)}</div><div><div class="ribbon" id="fname"></div><pre id="view" class="code">…</pre></div></div>`;
    const show = async path => {
      $("#tree", el).innerHTML = tree(files.data, path);
      const f = files.data.find(x => x.path === path);
      $("#fname", el).innerHTML = `<span class="mono">${esc(path)}</span> · ${bytes(f.bytes)}${f.executable ? " · executable" : ""}${f.layer === "overlay" ? ` · ${pill("overlay", "b")}` : ""}`;
      const r = await api("GET", `/v1/harnesses/${encodeURIComponent(name)}/files/${path}`, undefined, { raw: true });
      $("#view", el).textContent = (r.headers.get("content-type") || "").startsWith("text/") || path.endsWith(".json") ? await r.text() : `(binary, ${bytes(f.bytes)})`;
    };
    if (files.data.length) show(files.data.find(f => f.path === "harness.json")?.path || files.data[0].path);
    body.addEventListener("click", e => { const f = e.target.closest("[data-file]"); if (f) show(f.dataset.file); });
  } else if (tab === "manifest") {
    body.innerHTML = `<div class="grid two"><div>` + card("Package", kv([["Version", esc(h.version)], ["Extends", esc(h.extends || "—")], ["Base digest", `<span class="mono">${esc(short(h.base_digest))}</span>`],
      ["Overlay digest", `<span class="mono">${esc(short(h.overlay_digest || "—"))}</span>`], ["Provider APIs", h.provider_apis.map(a => pill(a, "b")).join(" ") || "none (no model)"],
      ["Run egress", h.egress.length ? h.egress.map(e => `<span class="mono">${esc(e)}</span>`).join(", ") : "—"], ["Install egress", h.install_egress.map(e => `<span class="mono">${esc(e)}</span>`).join(", ") || "—"],
      ["Persist", h.persist.map(p => `<span class="mono">${esc(p)}</span>`).join(", ") || "—"], ["Source", esc(source)], ["Installed", date(h.created_at)]])) + `</div><div>` +
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
  $("#fork", el).addEventListener("click", () => forkDraft(name));
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
    `<div class="note">Edit the files here; <b>Install</b> packs the draft and installs it (the platform validates the manifest and scripts). harness.json's <span class="mono">name</span> is the name it installs under — an installed harness of that name is upgraded; an overlay keeps only harness.json and seeds/.</div>` +
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
  $("#add", el).addEventListener("click", () => modal({ title: "Add file", submit: "Add", body: field("Path", input("path", "", 'placeholder="seeds/home/.claude/skills/review/SKILL.md" required'), "relative to the package root; folders are created"),
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
  const rows = await list("/v1/providers");
  el.innerHTML = top("Providers", "Model endpoints and their keys. A key is sealed here and swapped in at the gateway; a sandbox never sees it.",
    `<button class="btn" id="add">Add provider</button>`) +
    `<div class="card">${table([{ h: "Name", cell: p => `<b>${esc(p.name)}</b>` }, { h: "API shape", cell: p => pill(p.api, "b") },
      { h: "Base URL", cell: p => `<span class="mono">${esc(p.base_url)}</span>` }, { h: "Updated", cell: p => when(p.updated_at) },
      { h: "", cls: "acts", cell: p => `<button class="btn2" data-edit="${p.name}" data-api="${p.api}" data-url="${esc(p.base_url)}">Replace key</button><button class="btn2 danger" data-del="${p.name}">Delete</button>` }],
      rows, "No provider yet. Harnesses that call a model need one.")}</div>`;
  const dialog = (name = "", apiShape = "anthropic", url = "") => modal({ title: name ? `Provider ${name}` : "Add provider", submit: "Save", body:
    field("Name", input("name", name, `${name ? "readonly" : ""} pattern="[a-z0-9][a-z0-9._\\-]*" required placeholder="anthropic"`)) +
    field("API shape", select("api", [["anthropic", "anthropic (Messages)"], ["openai", "openai (Responses / Chat Completions)"], ["google", "google (Gemini)"]], apiShape)) +
    field("Base URL", input("base_url", url || { anthropic: "https://api.anthropic.com", openai: "https://api.openai.com", google: "https://generativelanguage.googleapis.com" }[apiShape], 'required')) +
    field("API key", input("key", "", 'type="password" autocomplete="off" required'), "write-only"),
    onSubmit: async d => { await api("PUT", `/v1/providers/${d.name}`, { api: d.api, base_url: d.base_url, key: d.key }); toast("Saved", "ok"); rerender(); } });
  $("#add", el).addEventListener("click", () => dialog());
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
  const harnessesRows = await list("/v1/harnesses");
  const draw = () => { $("#entries", el).innerHTML = table([
    { h: "Entry", cell: e => `<span class="mono">${esc(e)}</span>` }, { h: "", cell: e => e.startsWith("!") ? pill("deny", "bad") : pill("allow", "ok") },
    { h: "", cls: "acts", cell: e => `<button class="btn2 danger" data-rm="${esc(e)}">Remove</button>` }], entries, "Nothing allowed: sandboxes reach only their provider through the gateway."); };
  el.innerHTML = top("Egress", "What every sandbox of this organization may reach, besides its provider. A harness adds its own entries; a deny entry wins.") +
    `<div class="grid two"><div class="card"><div class="ch"><h2>Organization entries</h2></div><form class="row" id="add" style="margin-bottom:12px">${input("entry", "", 'placeholder="api.github.com · *.npmjs.org · !evil.example.com · host:8443" required')}<button class="btn">Add</button></form><div id="entries"></div></div>` +
    `<div><div class="card"><h2>Syntax</h2><div class="kv"><div><span class="mono">host</span></div><div>that host, port 443</div><div><span class="mono">*.domain</span></div><div>any subdomain, never the domain itself</div><div><span class="mono">host:8443</span></div><div>another port</div><div><span class="mono">!host</span></div><div>deny, even if a harness allows it</div><div><span class="mono">*</span></div><div>everything (deny entries still apply)</div></div></div>` +
    card("Added by harnesses", table([{ h: "Harness", cell: h => esc(h.name) }, { h: "Run egress", cell: h => h.egress.map(e => `<span class="mono">${esc(e)}</span>`).join(", ") || "—" }], harnessesRows.filter(h => h.egress.length), "No harness adds entries.")) + `</div></div>`;
  draw();
  const save = async next => { try { entries = (await api("PUT", "/v1/egress", { egress: next })).egress; draw(); toast("Saved", "ok"); } catch (err) { fail(err); } };
  $("#add", el).addEventListener("submit", e => { e.preventDefault(); const v = e.target.entry.value.trim(); if (v) { save([...entries, v]); e.target.reset(); } });
  el.addEventListener("click", e => { const b = e.target.closest("[data-rm]"); if (b) save(entries.filter(x => x !== b.dataset.rm)); });
} };

// ── API keys ──────────────────────────────────────────────────────────────────────────────────────────────────────
const keys = { title: "API keys", async render(el, params, me) {
  const rows = await list("/v1/keys");
  el.innerHTML = top("API keys", "Keys your applications use with any OpenAI SDK. `run` keys send requests; `admin` keys also manage the organization.",
    `<button class="btn" id="add">Create key</button>`) +
    `<div class="card">${table([{ h: "Key", cell: k => `<span class="mono">${esc(k.hint)}</span>` }, { h: "Label", cell: k => esc(k.label || "—") },
      { h: "Scope", cell: k => pill(k.scope, k.scope === "admin" ? "run" : "b") }, { h: "Created", cell: k => when(k.created_at) }, { h: "Last used", cell: k => when(k.last_used_at) },
      { h: "", cls: "acts", cell: k => `<button class="btn2 danger" data-del="${k.id}">Revoke</button>` }], rows, "No keys.")}</div>`;
  $("#add", el).addEventListener("click", () => modal({ title: "Create an API key", submit: "Create", body:
    field("Label", input("label", "", 'placeholder="my-app"')) + field("Scope", select("scope", [["run", "run — responses, sessions, files"], ["admin", "admin — everything"]])),
    onSubmit: async d => { const k = await api("POST", "/v1/keys", d); then(() => reveal("Your new key", "API key", k.key, "Copy it now: it is shown once.").then(rerender)); } }));
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
const usage = usagePage("/v1/usage", [["", "total"], ["day", "day"], ["harness", "harness"], ["session", "session"]],
  "What this organization's sandboxes used: allocated CPU and memory over time, bytes through the door, storage held.");

// ── Traces ────────────────────────────────────────────────────────────────────────────────────────────────────────
const traces = { title: "Traces", async render(el, params) {
  el.innerHTML = top("Traces", "What happened in one turn: the harness exchange, every provider call through the gateway, every connection through the door.") +
    `<form class="row card" id="f" style="margin-bottom:14px">${input("rid", params[0] || "", 'placeholder="resp_…" required')}<button class="btn">Look up</button></form><div id="out"></div>`;
  const show = async rid => {
    const t = await api("GET", `/v1/traces/${encodeURIComponent(rid)}`);
    const kinds = { harness: "Harness", gateway: "Gateway", door: "Door" };
    $("#out", el).innerHTML = `<p class="muted">session <span class="mono">${esc(t.session_id)}</span> · ${t.records.length} record${t.records.length === 1 ? "" : "s"}</p>` +
      (t.records.map(r => `<details class="sec" open><summary>${esc(kinds[r.kind] || r.kind)} · ${date(r.at)}${r.kind === "harness" ? ` · ${esc(r.data.status)}${r.data.hg_reason ? ` (${esc(r.data.hg_reason)})` : ""}` : ""}</summary>${pre(r.data)}</details>`).join("") || `<div class="empty">No records yet.</div>`);
  };
  $("#f", el).addEventListener("submit", e => { e.preventDefault(); show(e.target.rid.value.trim()).catch(fail); });
  if (params[0]) show(params[0]).catch(fail);
} };

// ── Members (console users) ───────────────────────────────────────────────────────────────────────────────────────
export function usersPage(title, lead, roles) {
  return { title, async render(el, params, me) {
    const rows = (await api("GET", "/users")).data;
    el.innerHTML = top(title, lead, `<button class="btn" id="add">Add user</button>`) +
      `<div class="card">${table([{ h: "User", cell: u => `<b>${esc(u.name)}</b>${u.name === me.name ? ' <span class="muted">(you)</span>' : ""}` }, { h: "Role", cell: u => pill(u.role, "b") },
        { h: "Password", cell: u => u.must_change ? pill("initial, must change", "warn") : pill("set", "ok") }, { h: "Created", cell: u => when(u.created_at) },
        { h: "", cls: "acts", cell: u => u.name === me.name ? "" : `<button class="btn2 danger" data-del="${esc(u.name)}">Delete</button>` }], rows)}</div>`;
    $("#add", el).addEventListener("click", () => modal({ title: "Add user", submit: "Create", body:
      field("Username", input("username", "", 'required pattern="[A-Za-z0-9._\\-]+"')) + field("Role", select("role", roles)),
      onSubmit: async d => { const u = await api("POST", "/users", d); then(() => reveal(`User ${u.name}`, "Initial password", u.password, "Give it to them: it is shown once and must be changed at first sign-in.").then(rerender)); } }));
    el.addEventListener("click", async e => {
      const d = e.target.closest("[data-del]");
      if (d && await confirm("Delete user", `Delete ${d.dataset.del}?`)) { try { await api("DELETE", "/users", { username: d.dataset.del }); rerender(); } catch (err) { fail(err); } }
    });
  } };
}
const members = usersPage("Members", "Who can sign in to this organization's console. Admins manage everything; members use the workbench.",
  [["member", "member — the workbench"], ["admin", "admin — the workbench and the organization"]]);

export const pages = { chat, sessions, playground, harnesses, drafts, providers, egress, keys, usage, traces, members };
