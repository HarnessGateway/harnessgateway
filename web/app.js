// The HarnessGateway app: one page. Sign in, add a provider, add a harness, start a session with the
// harness of your choice, type, press Enter. A message sent while a turn runs supersedes it: the
// running turn is stopped and the new message runs on the workspace it left, the way a coding
// agent's web view takes the next message. Every action is a call to the same routes any client
// uses. No framework, no build step.
const $ = (s) => document.querySelector(s);
const state = { health: null, auth: null, key: sessionStorage.getItem("hg.key") || "", me: null, account: null, orgs: [],
                harnesses: [], providers: [], sessions: [], sid: "", view: "chat", previous: "", live: 0 };
const h = (tag, attrs = {}, ...kids) => { const e = document.createElement(tag); for (const [k, v] of Object.entries(attrs)) { if (k === "on") for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn); else if (v !== false && v != null) e.setAttribute(k, v); } for (const k of kids.flat()) if (k != null && k !== false) e.append(k instanceof Node ? k : document.createTextNode(k)); return e; };

async function api(method, path, body, raw) {
  const headers = state.key ? { Authorization: "Bearer " + state.key } : {};
  let payload = body;
  if (body && !(body instanceof Blob) && !(body instanceof ArrayBuffer)) { headers["content-type"] = "application/json"; payload = JSON.stringify(body); }
  const r = await fetch(path, { method, headers, body: payload });
  if (raw) return r;
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch { data = { detail: text }; }
  if (!r.ok) throw new Error(data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : String(r.status));
  return data;
}
const fail = (e) => h("div", { class: "err" }, String(e.message || e));
const tag = (text, cls = "") => h("span", { class: "tag " + cls }, text);
const ago = (t) => { const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? "just now" : s < 3600 ? Math.round(s / 60) + " min ago" : s < 86400 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " d ago"; };
const field = (label, input) => h("div", { class: "field" }, h("label", {}, label), input);
const option = (value, label, selected) => h("option", { value, selected: selected || false }, label || value);
const empty = (text) => h("div", { class: "empty" }, text);
const isOwner = () => state.me?.role === "owner";
// a confirmation that lives in the page (the browser's dialogs are not used)
function confirmBtn(label, onConfirm) {
  const b = h("button", { class: "ghost small" }, label);
  let armed = false;
  b.addEventListener("click", async () => { if (!armed) { armed = true; b.textContent = "sure? " + label; b.classList.add("danger"); setTimeout(() => { armed = false; b.textContent = label; b.classList.remove("danger"); }, 3000); return; } b.disabled = true; try { await onConfirm(); } catch (e) { b.after(fail(e)); b.disabled = false; } });
  return b;
}

// boot: who is here
async function boot() {
  state.health = await (await fetch("/healthz")).json();
  state.auth = await (await fetch("/auth/providers")).json();
  if (state.key) {
    try { state.me = await api("GET", "/v1/me"); return app(); }
    catch (e) { sessionStorage.removeItem("hg.key"); state.key = ""; return login(fail(e)); }
  }
  try {
    const me = await api("GET", "/auth/me");
    state.account = me.account; state.orgs = me.orgs; state.me = { ...me.current, name: me.current.member };
    return app();
  } catch { /* not signed in */ }
  login(null, location.hash === "#signup" ? "signup" : "signin");
}

// sign in, create an account, or use a key
function login(note, mode = "signin") {
  const open = state.auth.signup === "open";
  const email = h("input", { type: "email", placeholder: "you@company.com", autocomplete: "email" });
  const pw = h("input", { type: "password", placeholder: mode === "signup" ? "at least 8 characters" : "password", autocomplete: mode === "signup" ? "new-password" : "current-password" });
  const name = h("input", { placeholder: "your name" }), out = h("div");
  const keyIn = h("input", { placeholder: "hg_…", type: "password" });
  const go = async (fn) => { out.replaceChildren(); try { await fn(); } catch (e) { out.replaceChildren(fail(e)); } };
  const signedIn = (me) => { location.hash = ""; state.account = me.account; state.orgs = me.orgs; state.me = { ...me.current, name: me.current.member }; app(); };
  const useKey = () => { state.key = keyIn.value.trim(); sessionStorage.setItem("hg.key", state.key); boot(); };
  let body;
  if (mode === "key") {
    body = [h("h2", {}, "Sign in with an API key"), h("p", { class: "sub" }, "A member's key opens their organization."), field("API key", keyIn),
      h("button", { class: "act", style: "margin-top:12px", on: { click: useKey } }, "Sign in"),
      h("div", { class: "links" }, h("a", { href: "#", on: { click: (e) => { e.preventDefault(); login(null, "signin"); } } }, "← Sign in with an account"))];
    keyIn.addEventListener("keydown", (e) => { if (e.key === "Enter") useKey(); });
  } else {
    const signup = mode === "signup";
    const tabs = h("div", { class: "tabs" }, h("button", { class: signup ? "" : "on", on: { click: () => login(null, "signin") } }, "Sign in"), h("button", { class: signup ? "on" : "", on: { click: () => login(null, "signup") } }, "Create account"));
    const submit = () => go(async () => signedIn(signup
      ? await api("POST", "/auth/signup", { email: email.value.trim(), password: pw.value, name: name.value.trim() })
      : await api("POST", "/auth/login", { email: email.value.trim(), password: pw.value })));
    for (const el of [email, pw, name]) el.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    body = [h("h2", {}, signup ? "Create your account" : "Welcome back"),
      h("p", { class: "sub" }, signup ? (open ? "An organization of your own comes with it." : "Sign-up is by invitation on this platform. Use the email an owner invited.") : "Sign in to your organization."),
      tabs, h("div", { class: "stack" }, signup ? field("Name", name) : null, field("Email", email), field("Password", pw)),
      h("button", { class: "act", style: "margin-top:14px", on: { click: submit } }, signup ? "Create account" : "Sign in"),
      h("div", { class: "links" }, h("span"), h("a", { href: "#", on: { click: (e) => { e.preventDefault(); login(null, "key"); } } }, "Use an API key instead"))];
  }
  $("#root").replaceChildren(h("div", { class: "auth" },
    h("div", { class: "side-art" },
      h("a", { class: "brand", href: "/" }, h("span", { class: "wordmark" }, "Harness", h("em", {}, "Gateway"))),
      h("div", {}, h("h1", {}, "Your harnesses.", h("br"), "One wire. ", h("em", {}, "Sandboxed.")), h("p", {}, "The OpenAI Responses API in, your harness package in the middle, the Responses API out. Add a provider, add a harness, start a session. The platform runs every turn in a sandbox it owns and keeps the record."), h("img", { class: "mascot", src: "/web/picky.png", alt: "" })),
      h("div", { class: "muted", style: "color:#8FA3C6" }, "v", state.health.version, " · ", h("a", { href: "/docs/", style: "color:#B7C6E0" }, "Docs"))),
    h("div", { class: "form" }, h("div", {}, note, ...body, out))));
}

function signOut() { sessionStorage.removeItem("hg.key"); state.key = ""; if (state.account) api("POST", "/auth/logout").catch(() => {}); state.account = null; state.me = null; state.sid = ""; state.previous = ""; boot(); }

// the app: Setup and Sessions on the left; a session is the conversation
async function load() {
  [state.harnesses, state.providers, state.sessions] = (await Promise.all(["/v1/harnesses", "/v1/providers", "/v1/sessions"].map(p => api("GET", p)))).map(x => x.data);
}
async function app(view = "sessions", sid = "") {
  state.view = view;
  state.sid = sid;
  try { await load(); } catch (e) { return $("#root").replaceChildren(fail(e)); }
  if (!state.harnesses.length && view !== "setup") state.view = "setup";    // nothing to talk to yet: set up first
  const nav = (name, label) => h("button", { class: "item" + (state.view === name || (name === "sessions" && state.view === "chat") ? " on" : ""), on: { click: () => app(name) } }, label);
  const side = h("aside", { class: "side" },
    h("a", { class: "brand", href: "/" }, h("span", { class: "wordmark" }, "Harness", h("em", {}, "Gateway"))),
    state.account && state.orgs.length > 1 ? h("div", { class: "tenant" }, h("select", { on: { change: async (e) => { try { const me = await api("POST", "/auth/switch", { org: e.target.value }); state.orgs = me.orgs; state.me = { ...me.current, name: me.current.member }; app("sessions"); } catch (err) { $("#root").prepend(fail(err)); } } } },
      ...state.orgs.map(t => h("option", { value: t.org, selected: t.org === state.me.org }, `${t.title || t.org} · ${t.role}`)))) : null,
    h("nav", { class: "nav" }, nav("setup", "Setup"), nav("sessions", "Sessions")),
    h("div", { class: "foot" },
      h("a", { class: "item", href: "/docs/" }, "Docs"),
      h("button", { class: "item", on: { click: signOut } }, "Sign out"),
      h("div", { class: "who" }, h("b", {}, state.me.name), ` · ${state.me.org_title || state.me.org} · ${state.me.role} · v${state.health.version}`)));
  const main = h("main", { class: "main" }, h("p", { class: "muted" }, "loading…"));
  $("#root").replaceChildren(h("div", { class: "app" }, side, main));
  const page = state.view === "setup" ? setup() : state.view === "chat" ? chat(sid) : sessionsPage();
  Promise.resolve(page).then(node => main.replaceChildren(node)).catch(e => main.replaceChildren(fail(e)));
}

// every session the member can see, newest first; open one, or start one
function sessionsPage() {
  const rows = state.sessions.map(s => h("button", { class: "session", on: { click: () => app("chat", s.id) } },
    h("div", { class: "t" }, s.title || "(untitled)"),
    h("div", { class: "row" }, tag(s.harness, "blue"), isOwner() ? h("span", { class: "muted" }, s.member) : null, s.status === "running" ? tag("running", "run") : null,
      h("span", { class: "muted" }, `${s.responses.length} turn${s.responses.length === 1 ? "" : "s"} · ${ago(s.created_at)}`))));
  return h("div", {},
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Sessions"), h("p", {}, "A session is a conversation with one harness in one workspace. Open one to continue it, or start a new one.")),
      h("button", { class: "act", on: { click: () => app("chat", "") } }, "+ New session")),
    rows.length ? h("div", { class: "sessions" }, ...rows) : empty("No sessions yet. Start one."));
}

// the text of a response's messages
const outputText = (r) => (r.output || []).filter(i => i.type === "message").flatMap(i => i.content || []).map(c => c.text || "").join("");
// one output item, drawn
function itemNode(item) {
  const d = (cls, kind, title, ...body) => h("details", { class: "item" }, h("summary", {}, h("span", { class: "k k-" + cls }, kind), h("code", {}, title)), ...body);
  switch (item.type) {
    case "reasoning": return d("reasoning", "thinking", (item.summary || []).map(s => s.text).join(" ").slice(0, 90), h("pre", { class: "light" }, (item.summary || []).map(s => s.text).join("\n")));
    case "shell_call": return d("shell", "shell", (item.action?.commands || []).join(" && "), h("pre", {}, (item.action?.commands || []).join("\n")));
    case "shell_call_output": return d("shell", "output", (item.output || []).map(o => `exit ${o.outcome?.exit_code ?? "?"}`).join(", "), h("pre", {}, (item.output || []).map(o => (o.stdout || "") + (o.stderr ? "\n" + o.stderr : "")).join("\n") || "(no output)"));
    case "apply_patch_call": return d("patch", item.operation?.type?.replace("_file", "") || "patch", item.operation?.path || "", h("pre", {}, item.operation?.diff || ""));
    case "apply_patch_call_output": return d("patch", "result", item.status, h("pre", {}, item.output || ""));
    case "function_call": return d("fn", "tool", item.name, h("pre", {}, item.arguments || ""));
    case "function_call_output": return d("fn", "result", item.status || "", h("pre", {}, item.output || ""));
    case "mcp_call": return d("mcp", "mcp", `${item.server_label}.${item.name}`, h("pre", {}, (item.arguments || "") + (item.output ? "\n→ " + item.output : "") + (item.error ? "\n✗ " + item.error : "")));
    case "web_search_call": return d("fn", "search", item.action?.query || "", h("pre", {}, JSON.stringify(item.action || {}, null, 1)));
    case "message": return h("div", { class: "bubble assistant" }, (item.content || []).map(c => c.text || "").join(""));
    default: return d("fn", item.type, "", h("pre", {}, JSON.stringify(item, null, 1)));
  }
}
const endNote = (r) => r.status === "completed" ? null : h("div", { class: "notice " + (r.metadata?.incomplete_reason === "superseded" ? "" : "warn") },
  r.metadata?.incomplete_reason === "superseded" ? "stopped for the next message" : r.metadata?.incomplete_reason === "client_disconnected" ? "stopped: the request was closed" : r.status + (r.error ? `: ${r.error.message}` : ""));

// a session: the thread, and the composer; Enter sends
async function chat(sid) {
  const pick = (name) => state.harnesses.find(x => x.name === name) || {};
  const harnessSel = h("select", {}, ...state.harnesses.map(x => option(x.name, x.name)));
  const providerSel = h("select", {}, option("", "package default"), ...state.providers.map(p => option(p.name)));
  const modelIn = h("input", { placeholder: "model (default)", style: "width:150px" });
  const defaults = () => { const d = pick(harnessSel.value).defaults || {}; providerSel.value = d.provider || (state.providers[0]?.name || ""); modelIn.placeholder = d.model ? `model (${d.model})` : "model (default)"; };
  harnessSel.addEventListener("change", defaults); defaults();
  const thread = h("div", { class: "thread" });
  const box = h("textarea", { placeholder: state.harnesses.length ? "Type a message and press Enter. Shift+Enter for a new line." : "Add a harness first.", rows: 2, disabled: !state.harnesses.length });
  const note = h("div");
  const title = h("h1", {}, "New session");
  const scroll = () => { window.scrollTo(0, document.documentElement.scrollHeight); };
  const userBubble = (text) => thread.append(h("div", { class: "bubble user" }, text));
  const renderResponse = (r) => { for (const item of r.output || []) thread.append(itemNode(item)); const n = endNote(r); if (n) thread.append(n); };
  let session = null;
  state.previous = "";

  // one turn's stream, live, into a bubble that fills as text arrives
  const consume = async (res, live) => {
    const reader = res.body.getReader(), dec = new TextDecoder(); let buf = "", text = "";
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true }); let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        const line = chunk.split("\n").find(l => l.startsWith("data: ")); if (!line) continue;
        const e = JSON.parse(line.slice(6));
        if (e.type === "response.created" && e.response) { state.previous = e.response.id; state.sid = e.response.metadata?.session_id || state.sid; }
        if (e.type === "response.output_text.delta") { text += e.delta; live.textContent = text; scroll(); }
        else if (e.type === "response.output_item.done" && e.item.type !== "message") { live.before(itemNode(e.item)); scroll(); }
        else if (e.type === "response.output_item.done" && e.item.type === "message") { live.before(itemNode(e.item)); text = ""; live.textContent = ""; }
        else if (e.response && ["completed", "failed", "incomplete", "cancelled"].includes(e.response.status)) {
          const n = endNote(e.response); if (n) live.replaceWith(n); else live.remove(); scroll();   // the note sits where this turn was
        }
      }
    }
    live.remove();
  };
  const follow = async (r) => {
    const live = h("div", { class: "bubble assistant streaming" }, h("span", { class: "cursor" })); thread.append(live); scroll();
    state.live++;
    try { await consume(await api("GET", `/v1/responses/${r.id}?stream=true`, null, true), live); }
    catch (e) { note.replaceChildren(fail(e)); }
    finally { state.live--; }
  };

  const submit = async () => {
    const text = box.value.trim(); if (!text) return;
    note.replaceChildren(); box.value = "";
    const model = [harnessSel.value, providerSel.value, modelIn.value.trim()].filter(Boolean).join("/");
    const body = { model, input: text, stream: true };
    if (state.previous) body.previous_response_id = state.previous;         // a running turn is superseded by this one
    userBubble(text);
    const live = h("div", { class: "bubble assistant streaming" }, h("span", { class: "cursor" })); thread.append(live); scroll();
    harnessSel.disabled = true;
    if (title.textContent === "New session") title.textContent = text.split("\n")[0].slice(0, 120);
    state.live++;
    try {
      const res = await api("POST", "/v1/responses", body, true);
      if (!res.ok) { const t = await res.text(); let d; try { d = JSON.parse(t).detail; } catch { d = t; } throw new Error(typeof d === "string" ? d : JSON.stringify(d)); }
      state.sid = res.headers.get("x-session-id") || state.sid;
      await consume(res, live);
    } catch (e) { live.remove(); note.replaceChildren(fail(e)); if (!state.previous) harnessSel.disabled = false; }
    finally { state.live--; }
  };
  box.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } });

  if (sid) {
    session = await api("GET", `/v1/sessions/${sid}`);
    title.textContent = session.title || "(untitled)";
    harnessSel.value = session.harness; harnessSel.disabled = true; defaults();
    for (const r of session.turns) { userBubble(r.text || ""); state.previous = r.id; if (["queued", "in_progress"].includes(r.status)) follow(r); else renderResponse(r); }
    setTimeout(scroll, 0);
  }
  return h("div", { class: "convo" },
    h("div", { class: "convo-head" }, h("div", { style: "min-width:0" }, h("a", { href: "#", class: "muted", on: { click: (e) => { e.preventDefault(); app("sessions"); } } }, "← Sessions"), title, h("p", { class: "muted" }, sid ? `${session.harness} · session ${sid}` : "Each message is a Responses request; the workspace persists across turns.")),
      h("div", { class: "row" }, field("Harness", harnessSel), field("Provider", providerSel), field("Model", modelIn))),
    thread,
    h("div", { class: "composer" }, box, note, h("p", { class: "muted", style: "margin:4px 0 0" }, "Enter sends. A message sent while a turn runs stops that turn and runs the new message on the workspace it left.")));
}

// setup: providers and harnesses
async function setup() {
  const owner = isOwner();
  const out = h("div");
  // providers
  const pname = h("input", { placeholder: "deepseek", pattern: "[a-z0-9][a-z0-9._-]*" }), url = h("input", { placeholder: "https://api.deepseek.com/anthropic" }), key = h("input", { type: "password", placeholder: "the provider's API key (sealed on arrival)" });
  const apiSel = h("select", {}, option("anthropic-messages", "Anthropic Messages (Claude Code, Pi, Hermes, Kilo, DeepSeek Harness…)"), option("openai-chat", "OpenAI chat completions (Cline, OpenAI-compatible CLIs)"), option("openai-responses", "OpenAI Responses (Codex)"), option("none", "no key: an open endpoint"));
  const saveProvider = async () => { out.replaceChildren(); try { await api("PUT", `/v1/providers/${pname.value.trim()}`, { url: url.value.trim(), api: apiSel.value, key: key.value }); app("setup"); } catch (e) { out.replaceChildren(fail(e)); } };
  const providerRows = state.providers.map(p => h("tr", {}, h("td", {}, h("b", {}, p.name)), h("td", {}, tag(p.api, "blue")), h("td", { class: "mono" }, p.url),
    h("td", {}, owner ? h("div", { class: "row end" }, confirmBtn("delete", async () => { await api("DELETE", `/v1/providers/${p.name}`); app("setup"); })) : null)));
  // harnesses
  const cat = await api("GET", "/v1/catalog");
  const hname = h("input", { placeholder: "claude-code", pattern: "[a-z0-9][a-z0-9._-]*" });
  const source = h("select", {}, ...cat.data.map(p => option(p.name, `${p.title || p.name} · ${p.cli.name} ${p.cli.version}`.trim())), option("upload", "a package archive (.tar.gz) from your machine"));
  const file = h("input", { type: "file", accept: ".tar,.tgz,.tar.gz,application/gzip,application/x-tar", hidden: true });
  const intro = h("p", { class: "muted" });
  const describe = () => { const p = cat.data.find(x => x.name === source.value); intro.textContent = p ? p.intro : "The archive is the package: harness.json, install.sh, request.sh and whatever else it needs."; file.hidden = source.value !== "upload"; if (p && !hname.value) hname.placeholder = p.name === "template" ? "my-harness" : p.name; };
  source.addEventListener("change", describe); describe();
  const addHarness = async () => {
    out.replaceChildren();
    const name = hname.value.trim() || hname.placeholder;
    try {
      if (source.value === "upload") {
        if (!file.files[0]) throw new Error("choose the package archive");
        const r = await api("PUT", `/v1/harnesses/${name}`, await file.files[0].arrayBuffer(), true);
        if (!r.ok) throw new Error((await r.json()).detail || r.status);
      } else await api("POST", `/v1/harnesses/${name}`, { catalog: source.value });
      app("sessions");
    } catch (e) { out.replaceChildren(fail(e)); }
  };
  const harnessRows = state.harnesses.map(hn => h("tr", {}, h("td", {}, h("b", {}, hn.name)), h("td", {}, `${hn.cli?.name || ""} ${hn.cli?.version || ""}`), h("td", {}, hn.defaults?.provider ? tag(hn.defaults.provider, "blue") : h("span", { class: "muted" }, "named in the request")), h("td", {}, hn.description),
    h("td", {}, h("div", { class: "row end" }, h("a", { class: "btn ghost small", href: `/v1/harnesses/${hn.name}/archive`, download: `${hn.name}.tar.gz` }, "export"), owner ? confirmBtn("delete", async () => { await api("DELETE", `/v1/harnesses/${hn.name}`); app("setup"); }) : null))));
  return h("div", { class: "setup" },
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Providers and harnesses"), h("p", {}, "A provider is a model endpoint and its key; the key is sealed on arrival and swapped in at the gateway per request, so no sandbox ever sees it. A harness is a package that runs a CLI: fork one from the catalog or upload your own."))),
    h("div", { class: "card" }, h("h2", {}, "Providers"), h("div", { class: "table-wrap" }, providerRows.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "API shape"), h("th", {}, "URL"), h("th"))), h("tbody", {}, ...providerRows)) : empty("No providers yet. Add the endpoint a harness should call.")),
      owner ? h("div", { style: "margin-top:14px" }, h("h3", {}, "Add or replace a provider"), h("div", { class: "grid-2" }, field("Name", pname), field("API shape", apiSel), field("URL (https)", url), field("Key", key)), h("div", { class: "row end", style: "margin-top:10px" }, h("button", { class: "act", on: { click: saveProvider } }, "Save provider"))) : h("p", { class: "muted" }, "An owner adds providers.")),
    h("div", { class: "card" }, h("h2", {}, "Harnesses"), h("div", { class: "table-wrap" }, harnessRows.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "CLI"), h("th", {}, "Provider"), h("th", {}, "Description"), h("th"))), h("tbody", {}, ...harnessRows)) : empty("No harnesses yet. Add one from the catalog to start.")),
      owner ? h("div", { style: "margin-top:14px" }, h("h3", {}, "Add a harness"), h("div", { class: "grid-2" }, field("From", source), field("Name", hname)), file, intro, h("div", { class: "row end", style: "margin-top:10px" }, h("button", { class: "act", on: { click: addHarness } }, "Add harness"))) : h("p", { class: "muted" }, "An owner adds harnesses."),
      h("p", { class: "muted", style: "margin-top:12px" }, "From the command line: ", h("code", {}, "hg harness pull NAME ./pkg"), ", edit, ", h("code", {}, "hg harness push NAME ./pkg"), ". ", h("a", { href: "/docs/packages/" }, "The package contract"))),
    out,
    h("div", { class: "card flat" }, h("h3", {}, "From any OpenAI SDK"), h("pre", { class: "light" }, `from openai import OpenAI\nclient = OpenAI(base_url="${location.origin}/v1", api_key="hg_…")\nr = client.responses.create(model="${state.harnesses[0]?.name || "my-harness"}", input="Create hello.py that prints hello and run it")\nprint(r.status, r.output_text)`),
      h("p", { class: "muted" }, "The model is the harness name, or harness/provider/model. A key comes from ", h("code", {}, "POST /v1/members/{name}/keys"), " or ", h("code", {}, "hg keys new"), "; ", h("a", { href: "/docs/api/" }, "the API"), ".")));
}
