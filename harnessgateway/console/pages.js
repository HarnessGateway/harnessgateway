// The organization's console: what a developer does (harnesses, the chat and the playground, sessions, usage) and
// what an admin does for the organization (providers, environments, keys, the audit log). Every call goes to the
// platform as the signed-in person; the platform refuses what the person may not do, so the pages only hide what
// would be refused.
import { BUILDER, actorOf, api, bytes, date, duration, esc, fitting, list, loadCatalog, modelString, num, quiet, short, stream, when } from "/lib.js";
import { $, $$, card, confirm, fail, field, input, kv, modal, pill, reveal, select, stat, statusPill, table, rerender, then, toast } from "/views.js";
import { session } from "/session.js";

export function nav(me) {
  const developer = me.role !== "runner", admin = me.role === "admin";
  const mods = [
    ...(developer ? [{ mod: "Setup", items: [{ id: "harnesses", label: "Harnesses", icon: "box" }, { id: "environments", label: "Environments", icon: "shield" },
                                             { id: "providers", label: "Providers", icon: "globe" }] }] : []),
    { mod: "Workbench", items: [{ id: "chat", label: "Chat", icon: "chat" }, { id: "playground", label: "Playground", icon: "code" }] },
    ...(admin ? [{ mod: "Access", items: [{ id: "keys", label: "API keys", icon: "key" }] }] : []),
    ...(developer ? [{ mod: "Observability", items: [{ id: "sessions", label: "Sessions", icon: "sess" }, { id: "usage", label: "Usage", icon: "usage" },
                                                     ...(admin ? [{ id: "audit", label: "Audit log", icon: "log" }] : [])] }] : [])];
  for (const m of mods) for (const it of m.items) it.mod = m.mod;
  return mods;
}

const top = (title, lead, acts = "") => `<div class="top"><div><h1>${esc(title)}</h1><p class="lead">${esc(lead)}</p></div><div class="acts">${acts}</div></div>`;
const mono = text => `<span class="mono">${esc(text)}</span>`;
const lines = text => String(text || "").split(/[\n,]+/).map(x => x.trim()).filter(Boolean);
const path = name => encodeURIComponent(name);

// ── Chat ──────────────────────────────────────────────────────────────────────────────────────────────────────────
const chat = { title: "Chat", full: true, render(el, params) {
  const open = params[0] ? `#open=${encodeURIComponent(params[0])}` : "";
  el.innerHTML = `<iframe class="chat" src="/chat/index.html${open}" title="Chat"></iframe>`;
} };

// ── Sessions ──────────────────────────────────────────────────────────────────────────────────────────────────────
const RUNNING = ["queued", "in_progress"];
const sessions = { title: "Sessions", async render(el, params, me) {
  if (params[0]) return session.render(el, params, me);
  let rows = [], after = null;
  for (let i = 0; i < 20; i++) {                          // up to 2,000, the most recently active first
    const page = await api("GET", `/v1/sessions?limit=100${after ? `&after=${after}` : ""}`);
    rows.push(...page.data); if (!page.has_more) break; after = page.last_id;
  }
  const within = (r, filter) => filter === "all" || (filter === "running") === RUNNING.includes(r.status);
  const draw = (filter = "all") => {
    $("#sess", el).innerHTML = table([
      { h: "Session", cell: r => `<a href="#/sessions/${path(r.id)}">${mono(r.id)}</a>` + (r.forked_from_response_id ? `<br><span class="muted small">forked from ${esc(short(r.forked_from_response_id))}</span>` : "") },
      { h: "Harness · model", cell: r => `${esc(r.harness || "—")}${r.model ? ` <span class="muted">${esc([r.provider, r.model].filter(Boolean).join(" / "))}</span>` : ""}` },
      { h: "Environment", cell: r => esc(r.environment || "—") },
      { h: "Turns", cell: r => esc(r.turns) }, { h: "Latest turn", cell: r => r.status ? statusPill(r.status) : "—" },
      { h: "Saved", cell: r => bytes(r.bytes) }, { h: "Last active", cell: r => when(r.last_active_at) },
      { h: "", cls: "acts", cell: r => `<a class="btn2" href="#/sessions/${path(r.id)}">Open</a><button class="btn2 danger" data-del="${r.id}">Delete</button>` }],
      rows.filter(r => within(r, filter)), "No sessions yet.");
  };
  const counts = s => rows.filter(r => within(r, s)).length;
  el.innerHTML = top("Sessions", "Every session of this organization, whichever application started it. Open one for its transcript, each turn's trace, and its files.") +
    `<div class="tabs">${["all", "running", "idle"].map(s => `<button data-tab="${s}"${s === "all" ? ' class="on"' : ""}>${s[0].toUpperCase() + s.slice(1)} · ${counts(s)}</button>`).join("")}</div>` +
    `<div class="card" id="sess"></div>`;
  draw();
  el.addEventListener("click", async e => {
    const tab = e.target.closest("[data-tab]");
    if (tab) { $$("[data-tab]", el).forEach(b => b.classList.toggle("on", b === tab)); draw(tab.dataset.tab); return; }
    const del = e.target.closest("[data-del]");
    if (del && await confirm("Delete session", `Delete ${del.dataset.del}, its turns, its snapshots and its files? A session with a turn in progress is refused.`)) {
      try { await api("DELETE", `/v1/sessions/${del.dataset.del}`); toast("Deleted", "ok"); rerender(); } catch (err) { fail(err); }
    }
  });
} };

// ── Playground ────────────────────────────────────────────────────────────────────────────────────────────────────
// A one-off request: pick the environment, the harness, the provider and the model, edit the request, send it, and
// read what comes back beside it.
const playground = { title: "Playground", async render(el) {
  const catalog = await loadCatalog();
  const body = { model: "", input: "Write hello.py that prints hello, then run it.", stream: true };
  el.innerHTML = top("Playground", "One request to this platform, as any OpenAI SDK would send it, and the one response that comes back.") +
    `<div class="route card" id="route">${select("environment", catalog.environments, "default", 'title="Environment"')}<span class="esep">:</span>` +
    `${select("harness", catalog.harnesses.map(h => h.name), "", 'title="Harness"')}<span class="sep">/</span>` +
    `${select("provider", [], "", 'title="Provider"')}<span class="sep">/</span>` +
    `<input name="model" list="pg-models" spellcheck="false" autocomplete="off" title="Model (empty: the default)"><datalist id="pg-models"></datalist>` +
    `<button class="btn" id="send" style="margin-left:auto">Send</button></div>` +
    `<div class="play"><div class="card out"><div class="ch"><h2>Request</h2><span class="muted small">POST /v1/responses</span></div><textarea id="req" spellcheck="false"></textarea></div>` +
    `<div class="card out"><div class="ch"><h2>Response</h2><div class="acts"><span class="muted" id="st"></span></div></div><div id="events"></div><pre id="res">—</pre></div></div>`;
  const route = $("#route", el), req = $("#req", el), res = $("#res", el), events = $("#events", el), st = $("#st", el);
  const esel = $("[name=environment]", route), hsel = $("[name=harness]", route), psel = $("[name=provider]", route), minput = $("[name=model]", route);
  const harness = () => catalog.harnesses.find(h => h.name === hsel.value);
  const picked = () => modelString({ environment: esel.value, harness: hsel.value, provider: psel.hidden ? "" : psel.value, model: minput.value.trim() });
  const sync = () => {                                   // the pickers write the request's model
    try { const b = JSON.parse(req.value); b.model = picked(); req.value = JSON.stringify(b, null, 2); } catch { /* leave the text */ }
  };
  const fillProviders = () => {
    const h = harness(), fits = fitting(catalog, h).map(p => p.name), solo = !fits.length;
    psel.innerHTML = fits.map(n => `<option value="${esc(n)}"${h && n === h.provider ? " selected" : ""}>${esc(n)}</option>`).join("");
    psel.hidden = solo; $$(".sep", route).forEach(x => { x.hidden = solo; }); minput.hidden = solo;
    const model = h && (h.model || (catalog.providers.find(p => p.name === psel.value) || {}).model);
    minput.value = ""; minput.placeholder = model ? `${model} (default)` : "model";
    $("#pg-models", el).innerHTML = model ? `<option value="${esc(model)}">` : "";
    sync();
  };
  const oneEnvironment = catalog.environments.length < 2;
  esel.hidden = oneEnvironment; $(".esep", route).hidden = oneEnvironment;
  req.value = JSON.stringify(body, null, 2);
  if (!catalog.harnesses.length) { route.innerHTML = `<span class="muted">No harness installed yet: add one under Setup → Harnesses.</span>`; return; }
  hsel.addEventListener("change", fillProviders); psel.addEventListener("change", sync); esel.addEventListener("change", sync); minput.addEventListener("input", sync);
  fillProviders();
  $("#send", el).addEventListener("click", async () => {
    let b; try { b = JSON.parse(req.value); } catch (e) { fail(new Error("the request is not valid JSON: " + e.message)); return; }
    events.innerHTML = ""; res.textContent = "…"; st.textContent = "sending"; $("#send", el).disabled = true;
    try {
      if (b.stream) {
        let final = null, text = "";
        await stream("/v1/responses", b, ev => {
          const t = ev.type || "";
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
// How a harness's installs stand: ready for at least one environment's image, still installing, or failed.
function installState(h) {
  const states = (h.installs || []).map(i => i.status);
  if (states.includes("ready")) return "ready";
  if (states.includes("failed") && !states.some(s => s !== "failed")) return "failed";
  return "installing";
}
const installPill = h => ({ ready: "", failed: pill("install failed", "bad"), installing: pill("installing", "warn") }[installState(h)]);
const apiPills = apis => apis.length ? apis.map(a => pill(a, "b")).join(" ") : pill("no model", "q");

const harnesses = { title: "Harnesses", async render(el, params, me) {
  if (params[0]) return harness.render(el, params, me);
  const [rows, drafts, offered] = await Promise.all([list("/v1/harnesses?limit=100"), list("/v1/drafts"), list("/v1/catalog")]);
  const hcard = r => `<a class="hcard" href="#/harnesses/${path(r.name)}"><div class="hh"><b>${esc(r.name)}</b>${r.name === BUILDER ? pill("builder", "run") : ""}${installPill(r)}${r.missing.length ? pill("needs settings", "warn") : ""}</div>` +
    `<div class="hv">${esc(r.version)}</div><div class="hd">${esc(r.description || "")}</div>` +
    `<div class="hf">${apiPills(r.manifest.apis)}<span class="muted small" style="margin-left:auto">${esc([r.provider, r.model].filter(Boolean).join(" / "))}</span></div>` +
    `<div class="ha"><span class="btn2">Open</span></div></a>`;
  const dcard = d => `<a class="hcard draft" href="#/drafts/${path(d.name)}"><div class="hh"><b>${esc(d.name)}</b>${pill("draft", "warn")}</div>` +
    `<div class="hv">${esc(d.based_on ? `from ${d.based_on}` : "")}</div><div class="hd muted">${d.files} file${d.files === 1 ? "" : "s"} · edited ${when(d.updated_at)}</div>` +
    `<div class="ha"><span class="btn2">Edit</span><button class="btn2 danger" data-discard="${esc(d.name)}">Discard</button></div></a>`;
  const ccard = c => `<div class="hcard catalog"><div class="hh"><b>${esc(c.name)}</b>${c.name === BUILDER ? pill("builder", "run") : ""}${c.installed_version ? pill(`installed ${c.installed_version}`, "q") : ""}</div>` +
    `<div class="hv">${esc(c.version)}</div><div class="hd">${esc(c.description || "")}</div><div class="hf">${apiPills(c.apis)}</div>` +
    `<div class="ha"><button class="btn2" data-add="${esc(c.name)}">${c.installed_version ? `Update to ${esc(c.version)}` : "Add"}</button></div></div>`;
  const toAdd = offered.filter(c => c.installed_version !== c.version);
  el.innerHTML = top("Harnesses", "The agents this organization can run. Every harness is a package. Add one that ships with the platform; start one from the template and edit its files; or ask the harness package builder in the chat to write one, then install it and try it in the chat or over the API.",
    `<button class="btn2" id="upload">Upload package</button><input type="file" id="archive" accept=".gz,.tgz" hidden><button class="btn" id="new">New harness</button>`) +
    (drafts.length ? `<h2>Drafts</h2><div class="cards">${drafts.map(dcard).join("")}</div>` : "") +
    `<h2>Installed</h2>` + (rows.length ? `<div class="cards">${rows.map(hcard).join("")}</div>` : `<div class="empty">No harness yet. Add one from the catalog below, or start one with <b>New harness</b>.</div>`) +
    (toAdd.length ? `<h2>Catalog</h2><p class="lead">The harness packages that ship with this platform, as their makers' CLIs come.</p><div class="cards">${toAdd.map(ccard).join("")}</div>` : "");
  $("#new", el).addEventListener("click", () => newDraft());
  $("#upload", el).addEventListener("click", () => $("#archive", el).click());
  $("#archive", el).addEventListener("change", async e => {
    const form = new FormData(); form.append("package", e.target.files[0]);
    try { const h = await api("POST", "/v1/harnesses", form); toast(`Added ${h.name}`, "ok"); location.hash = `#/harnesses/${path(h.name)}`; } catch (err) { fail(err); }
  });
  el.addEventListener("click", async e => {
    const add = e.target.closest("[data-add]");
    if (add) {
      add.disabled = true;
      try { const h = await api("POST", "/v1/harnesses", { catalog: add.dataset.add }); toast(`Added ${h.name}: installing`, "ok"); rerender(); } catch (err) { fail(err); add.disabled = false; }
      return;
    }
    const d = e.target.closest("[data-discard]");
    if (d) {
      e.preventDefault();
      if (await confirm("Discard draft", `Discard ${d.dataset.discard} and its files?`, "Discard")) {
        try { await api("DELETE", `/v1/drafts/${path(d.dataset.discard)}`); rerender(); } catch (err) { fail(err); }
      }
    }
  });
} };

// Editing an installed harness: a draft of its files under the same name; installing the draft upgrades it in place.
async function editInstalled(name) {
  try { await api("POST", "/v1/drafts", { name, harness: name }); } catch (e) { if (e.status !== 409) { fail(e); return; } }   // 409: the draft is there already
  location.hash = `#/drafts/${path(name)}`;
}

function newDraft() {
  return modal({ title: "New harness", submit: "Create", body:
    field("Name", input("name", "", 'required pattern="[a-z0-9][a-z0-9\\-]*" placeholder="my-agent"'),
      "the harness's name, as requests name it in model. The draft starts as a copy of the template: harness.json, install, run and a README that says what to change in each. Or ask the harness package builder in the chat to write one for you"),
    onSubmit: async d => { await api("POST", "/v1/drafts", { name: d.name }); location.hash = `#/drafts/${path(d.name)}`; } });
}

// the file tree + a viewer or editor, shared by the harness view and the draft editor
function tree(files, open) {
  const dirs = new Set();
  for (const f of files) { const parts = f.path.split("/"); for (let i = 1; i < parts.length; i++) dirs.add(parts.slice(0, i).join("/")); }
  const rows = [...files.map(f => ({ path: f.path, dir: false })), ...[...dirs].map(d => ({ path: d, dir: true }))].sort((a, b) => a.path.localeCompare(b.path));
  return rows.map(r => `<div class="${r.dir ? "d" : "f"}${r.path === open ? " on" : ""}" style="padding-left:${8 + 14 * (r.path.split("/").length - 1)}px"${r.dir ? "" : ` data-file="${esc(r.path)}"`}>${esc(r.path.split("/").pop())}${r.dir ? "/" : ""}</div>`).join("");
}
const fileLine = f => `${mono(f.path)} · ${bytes(f.bytes)}${f.executable ? " · executable" : ""}`;
const isText = r => (r.headers.get("content-type") || "").startsWith("text/");
const filePath = p => p.split("/").map(encodeURIComponent).join("/");

const harness = { title: "Harness", async render(el, params, me) {
  const name = params[0], at = `/v1/harnesses/${path(name)}`, admin = me.role === "admin";
  const [h, files, catalog] = await Promise.all([api("GET", at), list(`${at}/files`), loadCatalog()]);
  const m = h.manifest, tab = params[1] || "files", state = installState(h);
  const settings = Object.entries(m.settings || {}), secrets = Object.entries(m.secrets || {});
  const fits = fitting(catalog, { apis: m.apis });
  const note = state === "ready" ? "" : `<div class="note ${state === "failed" ? "bad" : "warn"}">` +
    (state === "failed" ? `The package's <span class="mono">install</span> failed; turns on this harness fail until it succeeds. See <a href="#/harnesses/${path(name)}/installs">Installs</a> for its log.`
      : `The package's <span class="mono">install</span> is running; a turn on this harness waits for it.`) + `</div>`;
  const missing = h.missing.length ? `<div class="note warn">This harness needs a value for ${h.missing.map(mono).join(", ")} before it can run: set ${h.missing.length > 1 ? "them" : "it"} under Settings &amp; secrets.</div>` : "";
  el.innerHTML = `<p><a href="#/harnesses">← Harnesses</a></p>` + top(name, h.description || "",
    `<button class="btn2" id="edit">Edit</button><button class="btn2 danger" id="rm">Uninstall</button>`) + note + missing +
    `<div class="tabs">${[["files", "Files"], ["manifest", "Manifest"], ["config", "Settings & secrets"], ["defaults", "Defaults"], ["installs", "Installs"]].map(([t, l]) => `<a href="#/harnesses/${path(name)}/${t}"${t === tab ? ' class="on"' : ""}>${l}</a>`).join("")}</div>` +
    `<div id="tab"></div>`;
  const body = $("#tab", el);
  if (tab === "files") {
    body.innerHTML = `<div class="ribbon">The package as installed. <b>Edit</b> opens it as a draft; installing the draft upgrades it in place.</div>` +
      `<div class="editor"><div class="tree" id="tree">${tree(files, files[0]?.path)}</div><div><div class="ribbon" id="fname"></div><pre id="view" class="code">…</pre></div></div>`;
    const show = async file => {
      $("#tree", el).innerHTML = tree(files, file);
      const f = files.find(x => x.path === file);
      $("#fname", el).innerHTML = fileLine(f);
      const r = await api("GET", `${at}/files/${filePath(file)}`, undefined, { raw: true });
      $("#view", el).textContent = isText(r) ? await r.text() : `(binary, ${bytes(f.bytes)})`;
    };
    if (files.length) show(files.find(f => f.path === "harness.json")?.path || files[0].path);
    body.addEventListener("click", e => { const f = e.target.closest("[data-file]"); if (f) show(f.dataset.file); });
  } else if (tab === "manifest") {
    const hosts = entries => (entries || []).map(mono).join(", ") || "—";
    body.innerHTML = `<div class="grid two"><div>` + card("Package", kv([["Version", esc(h.version)], ["Digest", mono(short(h.digest))],
      ["Provider APIs", m.apis.length ? m.apis.map(a => pill(a, "b")).join(" ") : "none (it calls no model)"],
      ["Its own folder", m.mind ? mono(`$HOME/${m.mind}`) : "—"],
      ["Run reaches", hosts(m.run?.egress)], ["Install reaches", hosts(m.install?.egress)],
      ["Install may take", `${esc(m.install?.timeout_s)} s`], ["Added", date(h.added_at)]])) + `</div><div>` +
      card("harness.json", `<pre>${esc(JSON.stringify(m, null, 2))}</pre>`) + `</div></div>`;
  } else if (tab === "config") {
    body.innerHTML = `<div class="grid two"><div>` +
      card("Settings", settings.length ? `<form id="settings">${settings.map(([k, s]) => field(k + (s.required ? " *" : ""), input(k, h.settings[k] ?? "", `placeholder="${esc(s.default ?? "")}"`), s.description)).join("")}<button class="btn">Save settings</button></form>` : `<div class="empty">This package declares no settings.</div>`) +
      `</div><div>` + card("Secrets", secrets.length ? `<form id="secrets">${secrets.map(([k, s]) => field(`${k}${s.required ? " *" : ""}`,
        `<div class="row">${h.secrets_set.includes(k) ? pill("set", "ok") : pill("not set", "warn")}${input(k, "", `type="password" autocomplete="off" placeholder="leave empty to keep"${admin ? "" : " disabled"}`)}<button type="button" class="btn2 danger" data-clear="${k}"${h.secrets_set.includes(k) && admin ? "" : " disabled"}>Clear</button></div>`,
        `${s.description || ""} · put into requests to ${s.hosts.join(", ")}`)).join("")}<button class="btn"${admin ? "" : " disabled"}>Save secrets</button><p class="muted small">Values are sealed: they are never shown again and never enter a sandbox.${admin ? "" : " An admin sets them."}</p></form>` : `<div class="empty">This package declares no secrets.</div>`) + `</div></div>`;
    $("#settings", el)?.addEventListener("submit", async e => {
      e.preventDefault();
      const d = Object.fromEntries(new FormData(e.target)); for (const k in d) if (d[k] === "") d[k] = null;
      try { await api("PATCH", at, { settings: d }); toast("Saved", "ok"); rerender(); } catch (err) { fail(err); }
    });
    $("#secrets", el)?.addEventListener("submit", async e => {
      e.preventDefault();
      const d = Object.fromEntries(new FormData(e.target)); for (const k in d) if (d[k] === "") delete d[k];
      if (!Object.keys(d).length) { toast("Nothing to save"); return; }
      try { await api("PATCH", at, { secrets: d }); toast("Sealed", "ok"); rerender(); } catch (err) { fail(err); }
    });
    body.addEventListener("click", async e => {
      const c = e.target.closest("[data-clear]");
      if (c) { try { await api("PATCH", at, { secrets: { [c.dataset.clear]: null } }); rerender(); } catch (err) { fail(err); } }
    });
  } else if (tab === "defaults") {
    body.innerHTML = m.apis.length ? card("Defaults", `<form id="defaults">${field("Provider", select("provider", [["", "— the first provider that speaks this harness's API —"], ...fits.map(p => [p.name, `${p.name} (${p.apis.join(", ")})`])], h.provider || ""), fits.length ? "" : "no provider speaks this harness's API yet: add one under Setup → Providers")}` +
      `${field("Model", input("model", h.model || ""), "the model the CLI is given when a request names none; empty: the provider's default model")}<button class="btn">Save defaults</button></form>`) : `<div class="note">This harness calls no model, so it has no provider or model defaults.</div>`;
    $("#defaults", el)?.addEventListener("submit", async e => {
      e.preventDefault();
      const d = Object.fromEntries(new FormData(e.target));
      try { await api("PATCH", at, { provider: d.provider || null, model: d.model.trim() || null }); toast("Saved", "ok"); } catch (err) { fail(err); }
    });
  } else {
    const installs = await list(`${at}/installs`);
    body.innerHTML = `<div class="ribbon">A package's <span class="mono">install</span> runs once for each environment image it is used with. A turn in an environment it is not installed for installs it first.</div>` +
      `<form class="row card" id="install" style="margin-bottom:14px;align-items:flex-end">${field("Install for environment", select("environment", catalog.environments, "default"))}<button class="btn2" style="margin-bottom:12px">Install</button></form>` +
      `<div class="card">${table([{ h: "Image", cell: i => mono(short(i.image_id.replace(/^sha256:/, ""))) }, { h: "State", cell: i => statusPill(i.status) }, { h: "Updated", cell: i => when(i.updated_at) },
        { h: "Log", cell: i => i.log ? `<details><summary class="muted small">show</summary><pre class="code" style="min-height:0;max-height:320px">${esc(i.log.slice(-8000))}</pre></details>` : "—" }], installs, "Not installed for any image yet.")}</div>`;
    $("#install", el).addEventListener("submit", async e => {
      e.preventDefault();
      try { await api("POST", `${at}/install`, { environment: e.target.environment.value }); toast("Installing", "ok"); setTimeout(rerender, 1500); } catch (err) { fail(err); }
    });
  }
  $("#edit", el).addEventListener("click", () => editInstalled(name));
  $("#rm", el).addEventListener("click", async () => {
    if (await confirm("Uninstall", `Uninstall ${name}? Sessions that used it keep their files; their next turn needs a harness that is installed.`, "Uninstall")) {
      try { await api("DELETE", at); location.hash = "#/harnesses"; } catch (err) { fail(err); }
    }
  });
} };

const drafts = { title: "Draft", async render(el, params) {
  const name = params[0];
  if (!name) { location.hash = "#/harnesses"; return; }
  const at = `/v1/drafts/${path(name)}`;
  let doc = await api("GET", at);
  let open = null, dirty = false;
  el.innerHTML = `<p><a href="#/harnesses">← Harnesses</a></p>` + top(name, doc.based_on ? `Draft · from ${doc.based_on}` : "Draft",
    `<button class="btn2" id="add">Add file</button><button class="btn2" id="del" disabled>Delete file</button><button class="btn2" id="save" disabled>Save</button><button class="btn2 danger" id="discard">Discard</button><button class="btn" id="install">Install</button>`) +
    `<div class="note">Edit the files here; <b>Install</b> packs the draft, installs it and removes the draft. harness.json's <span class="mono">name</span> is the name it installs under, and an installed harness of that name is upgraded; <b>Edit</b> on the harness brings it back here. Then try it: pick it in the chat, or send a request over the API, and watch the session under Observability.</div>` +
    `<div class="editor"><div class="tree" id="tree"></div><div style="display:flex;flex-direction:column;min-height:0"><div class="ribbon" id="fname">Pick a file</div><textarea id="ed" spellcheck="false" style="flex:1;min-height:420px;font-size:12.5px" disabled></textarea></div></div>`;
  const ed = $("#ed", el), saveBtn = $("#save", el), delBtn = $("#del", el);
  const drawTree = () => { $("#tree", el).innerHTML = tree(doc.files, open); };
  const reload = async () => { doc = await api("GET", at); drawTree(); };
  const show = async file => {
    if (dirty && !window.confirm("Discard the unsaved changes?")) return;
    open = file; dirty = false; drawTree();
    $("#fname", el).innerHTML = fileLine(doc.files.find(x => x.path === file));
    const r = await api("GET", `${at}/files/${filePath(file)}`, undefined, { raw: true });
    const text = isText(r);
    ed.value = text ? await r.text() : ""; ed.disabled = !text; ed.placeholder = text ? "" : "(binary file)";
    saveBtn.disabled = true; delBtn.disabled = false;
  };
  const write = (file, text) => api("PUT", `${at}/files/${filePath(file)}`, new Blob([text]));
  const save = async () => {
    if (!open) return;
    try { await write(open, ed.value); } catch (err) { fail(err); return; }
    dirty = false; saveBtn.disabled = true; toast("Saved", "ok");
    await reload();
  };
  ed.addEventListener("input", () => { dirty = true; saveBtn.disabled = false; });
  ed.addEventListener("keydown", e => { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); } });
  saveBtn.addEventListener("click", save);
  $("#add", el).addEventListener("click", () => modal({ title: "Add file", submit: "Add", body: field("Path", input("path", "", 'placeholder="seeds/mind/.config/agent/settings.json" required'), "relative to the package's root; folders are made as needed"),
    onSubmit: async d => { await write(d.path, ""); await reload(); dirty = false; show(d.path.replace(/^\.\//, "")); } }));
  delBtn.addEventListener("click", async () => {
    if (open && await confirm("Delete file", `Delete ${open} from the draft?`)) {
      await api("DELETE", `${at}/files/${filePath(open)}`);
      open = null; dirty = false; ed.value = ""; ed.disabled = true; delBtn.disabled = true; saveBtn.disabled = true; $("#fname", el).textContent = "Pick a file";
      await reload();
    }
  });
  $("#discard", el).addEventListener("click", async () => {
    if (await confirm("Discard draft", `Discard ${name} and its files?`, "Discard")) { await api("DELETE", at); location.hash = "#/harnesses"; }
  });
  $("#install", el).addEventListener("click", async () => {
    if (dirty && !window.confirm("Save the open file first?")) return;
    if (dirty) await save();
    try { const h = await api("POST", `${at}/install`); toast(`Installed ${h.name}`, "ok"); location.hash = `#/harnesses/${path(h.name)}`; } catch (err) { fail(err); }
  });
  el.addEventListener("click", e => { const f = e.target.closest("[data-file]"); if (f) show(f.dataset.file); });
  drawTree();
  if (doc.files.length) show(doc.files.find(f => f.path === "harness.json")?.path || doc.files[0].path);
} };

// ── Providers ─────────────────────────────────────────────────────────────────────────────────────────────────────
const APIS = [["anthropic", "Anthropic Messages", "https://api.anthropic.com", "base URL without /v1"],
              ["openai-responses", "OpenAI Responses", "https://api.openai.com/v1", "base URL ending /v1"],
              ["openai-chat", "OpenAI Chat Completions", "https://api.openai.com/v1", "base URL ending /v1"]];
const providers = { title: "Providers", async render(el, params, me) {
  const rows = await list("/v1/providers?limit=100"), admin = me.role === "admin";
  el.innerHTML = top("Providers", "The organization's model endpoints and their keys. One provider can speak several APIs, so different harnesses use the same key and models. A key is sealed here and put into requests by the egress proxy, toward the provider's host only; a sandbox never holds it." + (admin ? "" : " An admin adds them."),
    admin ? `<button class="btn" id="add">Add provider</button>` : "") +
    `<div class="card">${table([{ h: "Name", cell: p => `<b>${esc(p.name)}</b>` }, { h: "APIs", cell: p => Object.keys(p.apis).map(a => pill(a, "b")).join(" ") },
      { h: "Base URLs", cell: p => Object.values(p.apis).map(mono).join("<br>") }, { h: "Default model", cell: p => p.default_model ? mono(p.default_model) : "—" },
      { h: "Key", cell: p => p.has_key ? pill("set", "ok") : pill("no key", "warn") },
      ...(admin ? [{ h: "", cls: "acts", cell: p => `<button class="btn2" data-edit="${esc(p.name)}">Edit</button><button class="btn2 danger" data-del="${esc(p.name)}">Delete</button>` }] : [])],
      rows, "No provider yet. Harnesses that call a model need one.")}</div>`;
  const dialog = p => modal({ title: p ? `Provider ${p.name}` : "Add provider", submit: "Save", body:
    field("Name", input("name", p ? p.name : "", `${p ? "readonly" : ""} pattern="[a-z0-9][a-z0-9\\-]*" required placeholder="anthropic"`)) +
    APIS.map(([a, label, example, help]) => field(label, input(`api:${a}`, p ? p.apis[a] || "" : "", `placeholder="${esc(example)}"`), `${help}; empty: this provider does not speak it`)).join("") +
    field("Default model", input("default_model", p ? p.default_model || "" : "", 'placeholder="none"'), "what a harness is given when neither the request nor the harness names a model") +
    field("API key", input("api_key", "", `type="password" autocomplete="off"${p && p.has_key ? ' placeholder="leave empty to keep the stored key"' : ""}`), "write-only; never shown again"),
    onSubmit: async d => {
      const apis = Object.fromEntries(APIS.map(([a]) => [a, d[`api:${a}`].trim()]).filter(([, url]) => url));
      const doc = { apis, default_model: d.default_model.trim() || null, ...(d.api_key ? { api_key: d.api_key } : {}) };
      if (p) await api("PATCH", `/v1/providers/${path(p.name)}`, doc); else await api("POST", "/v1/providers", { name: d.name.trim(), ...doc });
      toast("Saved", "ok"); rerender();
    } });
  $("#add", el)?.addEventListener("click", () => dialog(null));
  el.addEventListener("click", async e => {
    const ed = e.target.closest("[data-edit]"); if (ed) { dialog(rows.find(p => p.name === ed.dataset.edit)); return; }
    const d = e.target.closest("[data-del]");
    if (d && await confirm("Delete provider", `Delete ${d.dataset.del}? Harnesses that default to it need another provider.`)) {
      try { await api("DELETE", `/v1/providers/${path(d.dataset.del)}`); rerender(); } catch (err) { fail(err); }
    }
  });
} };

// ── Environments ──────────────────────────────────────────────────────────────────────────────────────────────────
// Where turns run: the image, the limits, what the sandbox may reach, and the variables and secrets it is given.
const NETWORK = { limited: ["limited", "b"], open: ["open", "warn"], none: ["no network", "q"] };
const environments = { title: "Environments", async render(el, params, me) {
  const rows = await list("/v1/environments?limit=100"), admin = me.role === "admin";
  el.innerHTML = top("Environments", "Where turns run. An environment is an image, limits, a network policy, and variables and secrets. Every turn gets a new sandbox built from one; a request picks it by name in model (environment:harness), and default is used when it names none." + (admin ? "" : " An admin changes them."),
    admin ? `<button class="btn" id="add">Add environment</button>` : "") +
    `<div class="card">${table([{ h: "Name", cell: e => `<b>${esc(e.name)}</b>` }, { h: "Image", cell: e => mono(e.image) },
      { h: "Limits", cell: e => `${num(e.cpus)} CPU · ${num(e.memory_mb)} MB · ${num(e.disk_mb)} MB disk · ${duration(e.turn_timeout_s)} a turn` },
      { h: "Network", cell: e => pill(...NETWORK[e.network.mode]) + (e.network.mode === "limited" ? ` <span class="muted small">${e.network.allow.length} allowed${e.network.package_managers ? " + package managers" : ""}</span>` : "") +
        (e.network.deny.length ? ` <span class="muted small">${e.network.deny.length} denied</span>` : "") },
      { h: "Given", cell: e => `${Object.keys(e.variables).length} variable${Object.keys(e.variables).length === 1 ? "" : "s"} · ${Object.keys(e.secrets).length} secret${Object.keys(e.secrets).length === 1 ? "" : "s"}` },
      ...(admin ? [{ h: "", cls: "acts", cell: e => `<button class="btn2" data-edit="${esc(e.name)}">Edit</button>${e.name === "default" ? "" : `<button class="btn2 danger" data-del="${esc(e.name)}">Delete</button>`}` }] : [])],
      rows)}</div>` +
    `<div class="grid two" style="margin-top:14px">` + card("Network entries", `<div class="kv"><div>${mono("host")}</div><div>that host, port 443</div><div>${mono("*.domain")}</div><div>any subdomain, never the domain itself</div><div>${mono("host:8443")}</div><div>another port</div></div>` +
      `<p class="muted small" style="margin-top:10px">A limited network reaches the turn's provider, what its harness package declares, the hosts its secrets are bound to, the entries allowed here and, when turned on, the package managers. A denied entry wins over everything. Whatever the policy, a sandbox reaches the internet only through the egress proxy.</p>`) +
    card("Secrets", `<p class="muted small">A secret is a variable whose value never enters the sandbox: the sandbox holds a placeholder, and the egress proxy puts the real value into requests to the hosts the secret is bound to. With a header name, the proxy sets that header (with the prefix) instead of looking for the placeholder.</p>`) + `</div>`;
  const secretRow = (name = "", s = { hosts: [], header: "", prefix: "" }) => `<div class="row secret-edit" style="margin-bottom:6px">` +
    `<input data-s="name" value="${esc(name)}" placeholder="GITHUB_TOKEN" style="flex:1.2" ${name ? "readonly" : ""}><input data-s="hosts" value="${esc(s.hosts.join(", "))}" placeholder="api.github.com" style="flex:1.6">` +
    `<input data-s="header" value="${esc(s.header || "")}" placeholder="header (optional)"><input data-s="prefix" value="${esc(s.prefix || "")}" placeholder="prefix" style="flex:.6">` +
    `<input data-s="value" type="password" autocomplete="off" placeholder="${name ? "keep" : "value"}"><button type="button" class="btn2 danger" data-unsecret>Remove</button></div>`;
  const dialog = e => {
    const c = e || { name: "", image: "", cpus: 2, memory_mb: 4096, pids: 512, tmp_mb: 1024, disk_mb: 10240, turn_timeout_s: 1800,
                     network: { mode: "limited", allow: [], deny: [], package_managers: true }, variables: {}, secrets: {} };
    const shown = modal({ title: e ? `Environment ${e.name}` : "Add environment", submit: "Save", wide: true, body:
      `<div class="grid two">` + field("Name", input("name", c.name, `${e ? "readonly" : ""} pattern="[a-z0-9][a-z0-9\\-]*" required placeholder="gpu"`), "requests name it in model: gpu:claude-code") +
      field("Image", input("image", c.image, 'placeholder="the platform\'s sandbox image"'), "any Linux image; a harness is installed once for each image it runs in") + `</div>` +
      `<div class="grid">` + field("CPUs", input("cpus", c.cpus, 'type="number" step="0.25" min="0.25" required')) + field("Memory (MB)", input("memory_mb", c.memory_mb, 'type="number" min="256" required')) +
      field("Disk (MB)", input("disk_mb", c.disk_mb, 'type="number" min="64" required'), "the session's saved files") + field("A turn may take (s)", input("turn_timeout_s", c.turn_timeout_s, 'type="number" min="1" required')) + `</div>` +
      `<div class="grid">` + field("Processes", input("pids", c.pids, 'type="number" min="64" required')) + field("/tmp (MB)", input("tmp_mb", c.tmp_mb, 'type="number" min="64" required'), "counts against memory") +
      field("Network", select("mode", [["limited", "limited: only what is allowed"], ["open", "open: any public host"], ["none", "none: no network at all"]], c.network.mode)) +
      field("Package managers", select("package_managers", [["1", "reachable"], ["", "not reachable"]], c.network.package_managers ? "1" : ""), "npm, PyPI, apt and the like") + `</div>` +
      `<div class="grid two">` + field("Allowed", `<textarea name="allow" style="min-height:70px" placeholder="api.github.com&#10;*.example.com">${esc(c.network.allow.join("\n"))}</textarea>`, "one entry a line") +
      field("Denied", `<textarea name="deny" style="min-height:70px" placeholder="evil.example.com">${esc(c.network.deny.join("\n"))}</textarea>`, "wins over everything allowed") + `</div>` +
      field("Variables", `<textarea name="variables" style="min-height:70px" placeholder="NAME=value">${esc(Object.entries(c.variables).map(([k, v]) => `${k}=${v}`).join("\n"))}</textarea>`, "one NAME=value a line; visible inside the sandbox") +
      `<div class="field"><label>Secrets</label><div id="secrets">${Object.entries(c.secrets).map(([k, s]) => secretRow(k, s)).join("")}</div>` +
      `<div><button type="button" class="btn2" id="secret">Add secret</button></div><div class="help">name · hosts it may go to · header and prefix (optional) · value (write-only; leave empty to keep the stored one)</div></div>`,
      onSubmit: async (d, form) => {
        const variables = {};
        for (const line of d.variables.split("\n").map(x => x.trim()).filter(Boolean)) {
          const cut = line.indexOf("=");
          if (cut < 1) throw new Error(`a variable is NAME=value: ${line}`);
          variables[line.slice(0, cut).trim()] = line.slice(cut + 1);
        }
        const secrets = {};
        for (const row of $$(".secret-edit", form)) {
          const v = key => $(`[data-s=${key}]`, row).value.trim();
          if (!v("name")) continue;
          secrets[v("name")] = { hosts: lines(v("hosts")), ...(v("header") ? { header: v("header") } : {}), prefix: $("[data-s=prefix]", row).value,
                                 ...($("[data-s=value]", row).value ? { value: $("[data-s=value]", row).value } : {}) };
        }
        const doc = { cpus: +d.cpus, memory_mb: +d.memory_mb, pids: +d.pids, tmp_mb: +d.tmp_mb, disk_mb: +d.disk_mb, turn_timeout_s: +d.turn_timeout_s,
                      network: { mode: d.mode, allow: lines(d.allow), deny: lines(d.deny), package_managers: !!d.package_managers }, variables, secrets,
                      ...(d.image.trim() ? { image: d.image.trim() } : {}) };
        if (e) await api("PATCH", `/v1/environments/${path(e.name)}`, doc); else await api("POST", "/v1/environments", { name: d.name.trim(), ...doc });
        toast("Saved", "ok"); rerender();
      } });
    const form = $(".backdrop form");
    form.addEventListener("click", ev => {
      if (ev.target.id === "secret") $("#secrets", form).insertAdjacentHTML("beforeend", secretRow());
      else if (ev.target.closest("[data-unsecret]")) ev.target.closest(".secret-edit").remove();
    });
    return shown;
  };
  $("#add", el)?.addEventListener("click", () => dialog(null));
  el.addEventListener("click", async e => {
    const ed = e.target.closest("[data-edit]"); if (ed) { dialog(rows.find(x => x.name === ed.dataset.edit)); return; }
    const d = e.target.closest("[data-del]");
    if (d && await confirm("Delete environment", `Delete ${d.dataset.del}? Requests that name it are refused from then on.`)) {
      try { await api("DELETE", `/v1/environments/${path(d.dataset.del)}`); rerender(); } catch (err) { fail(err); }
    }
  });
} };

// ── API keys ──────────────────────────────────────────────────────────────────────────────────────────────────────
const ROLE_PILL = { admin: "run", developer: "b", runner: "q" };
const keys = { title: "API keys", async render(el) {
  const rows = await list("/v1/keys");
  el.innerHTML = top("API keys", "A key is what an app is built around: name it after the app. A runner key sends requests and reads what they made, which is all an app needs. A developer key also manages harnesses and reads sessions and usage; an admin key does everything in the organization.",
    `<button class="btn" id="add">Create key</button>`) +
    `<div class="card">${table([{ h: "App", cell: k => `<b>${esc(k.name)}</b><br>${mono(k.id)}` }, { h: "Role", cell: k => pill(k.role, ROLE_PILL[k.role] || "q") },
      { h: "Created", cell: k => when(k.created_at) }, { h: "Last used", cell: k => when(k.last_used_at) },
      { h: "", cls: "acts", cell: k => k.revoked_at ? pill("revoked", "bad") : `<button class="btn2 danger" data-del="${k.id}">Revoke</button>` }], rows, "No keys.")}</div>`;
  $("#add", el).addEventListener("click", () => modal({ title: "Create an API key", submit: "Create", body:
    field("App", input("name", "", 'required placeholder="market-desk"'), "the application this key is for") +
    field("Role", select("role", [["runner", "runner: responses, files and what they made"], ["developer", "developer: also harnesses, sessions and usage"], ["admin", "admin: everything in the organization"]])),
    onSubmit: async d => {
      const k = await api("POST", "/v1/keys", { name: d.name.trim(), role: d.role });
      then(() => reveal(`Key for ${k.name}`, "API key", k.key, "Copy it now: it is shown once.").then(rerender));
    } }));
  el.addEventListener("click", async e => {
    const d = e.target.closest("[data-del]");
    if (d && await confirm("Revoke key", "Revoke this key? The app using it stops working at once.", "Revoke")) {
      try { await api("DELETE", `/v1/keys/${d.dataset.del}`); rerender(); } catch (err) { fail(err); }
    }
  });
} };

// ── Usage ─────────────────────────────────────────────────────────────────────────────────────────────────────────
const METERS = [["turns", "Turns", num], ["input_tokens", "Tokens in", num], ["output_tokens", "Tokens out", num], ["sandbox_seconds", "Sandbox time", duration],
                ["network", "Network", bytes], ["snapshot_bytes", "Saved", bytes]];
const usage = { title: "Usage", async render(el) {
  const now = new Date(), first = new Date(now.getFullYear(), now.getMonth(), 1);
  const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  el.innerHTML = top("Usage", "What this organization's turns used: the tokens their harnesses reported, the time their sandboxes ran, the bytes through the egress proxy, and what their snapshots hold.") +
    `<form class="row card" id="f" style="margin-bottom:14px">${field("From", input("start", iso(first), 'type="date"'))}${field("To", input("end", iso(now), 'type="date"'))}${field("Group by", select("group_by", ["harness", "provider", "model", "environment", "day"]))}<button class="btn">Show</button></form><div id="out"></div>`;
  const show = async () => {
    const f = $("#f", el), start = new Date(`${f.start.value}T00:00:00`), end = new Date(new Date(`${f.end.value}T00:00:00`).getTime() + 86400000);
    const u = await api("GET", `/v1/usage?start=${encodeURIComponent(start.toISOString())}&end=${encodeURIComponent(end.toISOString())}&group_by=${f.group_by.value}`);
    const rows = u.data.map(r => ({ ...r, network: (r.bytes_up || 0) + (r.bytes_down || 0) })), totals = {};
    for (const r of rows) for (const [k] of METERS) totals[k] = (totals[k] || 0) + (r[k] || 0);
    $("#out", el).innerHTML = `<div class="grid" style="grid-template-columns:repeat(6,1fr)">${METERS.map(([k, label, show]) => stat(label, show(totals[k] || 0))).join("")}</div>` +
      `<div class="card">${table([{ h: f.group_by.value, cell: r => mono(r.group ?? "—") }, ...METERS.map(([k, label, show]) => ({ h: label, cell: r => show(r[k] || 0) }))], rows, "No usage in this range.")}</div>`;
  };
  $("#f", el).addEventListener("submit", e => { e.preventDefault(); show().catch(fail); });
  await show();
} };

// ── Audit log ─────────────────────────────────────────────────────────────────────────────────────────────────────
const audit = { title: "Audit log", async render(el) {
  const [rows, keyList] = await Promise.all([list("/v1/audit?limit=100"), quiet("/v1/keys")]);
  const who = actorOf(keyList);
  el.innerHTML = top("Audit log", "Every change of standing in this organization, newest first: keys, providers, environments, harnesses, sessions deleted. No secret is ever part of it.") +
    `<div class="card">${table([{ h: "When", cell: r => date(r.at) }, { h: "Who", cell: r => esc(who(r.key_id)) }, { h: "Action", cell: r => mono(r.action) },
      { h: "Target", cell: r => mono(r.target || "") },
      { h: "Detail", cell: r => `<span class="mono small">${esc(Object.entries(r.detail || {}).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(" "))}</span>` }], rows, "Nothing yet.")}</div>`;
} };

export const pages = { chat, sessions, playground, harnesses, drafts, providers, environments, keys, usage, audit };
