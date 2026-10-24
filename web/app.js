// The HarnessGateway console: sign in with an account (email and password, GitHub, Google) or an API
// key, then the organization's providers, harnesses (git packages with an editor), the catalog,
// conversations with a workspace pane, rodeos, sessions, teams, members, keys; the deployer's
// dashboard, organizations and workers. Every action is a call to the same routes any client uses.
// No framework, no build step. app.js: helpers, boot, sign-in, the shell. The views are in
// setup.js (providers, catalog, harnesses), build.js (conversations, rodeo, sessions) and team.js.
const $ = (s) => document.querySelector(s);
const state = { health: null, auth: null, key: sessionStorage.getItem("hg.key") || "", me: null, account: null, orgs: [], kind: "", page: "", args: {}, window: 30 };
const h = (tag, attrs = {}, ...kids) => { const e = document.createElement(tag); for (const [k, v] of Object.entries(attrs)) { if (k === "on") for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn); else if (k === "html") e.innerHTML = v; else if (v !== false && v != null) e.setAttribute(k, v); } for (const k of kids.flat()) if (k != null && k !== false) e.append(k instanceof Node ? k : document.createTextNode(k)); return e; };
const svg = (tag, attrs = {}, ...kids) => { const e = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const [k, v] of Object.entries(attrs)) { if (k === "on") for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn); else e.setAttribute(k, v); } for (const k of kids.flat()) if (k != null) e.append(k instanceof Node ? k : document.createTextNode(k)); return e; };
const ICONS = { bolt: "M13 2L4 14h7l-1 8 9-12h-7l1-8z", lasso: "M15 7a5 5 0 1 1-10 0 5 5 0 0 1 10 0zM13 11c2 2 3 5 4 9M17 20l3-1", clock: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2", plug: "M9 2v6M15 2v6M5 8h14l-1 5a6 6 0 0 1-12 0L5 8zM12 19v3", box: "M21 8l-9-5-9 5v8l9 5 9-5V8zM3 8l9 5 9-5M12 13v8", grid: "M3 3h8v8H3zM13 3h8v8h-8zM3 13h8v8H3zM13 13h8v8h-8z", users: "M16 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2M9.5 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM21 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8", user: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", key: "M21 2l-2 2M15.5 7.5l3 3L22 6l-3-3zM15.5 7.5L9 14a5 5 0 1 0 1 1l6.5-6.5", chart: "M3 3v18h18M7 14v4M12 9v9M17 5v13", building: "M3 21h18M5 21V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16M9 7h2M13 7h2M9 11h2M13 11h2M9 15h2M13 15h2", server: "M3 5h18v6H3zM3 13h18v6H3zM7 8h.01M7 16h.01", book: "M4 19.5A2.5 2.5 0 0 1 6.5 17H20M4 19.5A2.5 2.5 0 0 0 6.5 22H20V2H6.5A2.5 2.5 0 0 0 4 4.5v15z", logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9", file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6zM14 2v6h6", chat: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z", team: "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" };
const icon = (name) => svg("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round" }, svg("path", { d: ICONS[name] || ICONS.box }));

async function api(method, path, body, raw) {
  const headers = state.key ? { Authorization: "Bearer " + state.key } : {};
  let payload = body;
  if (body && !(body instanceof Blob) && !(body instanceof FormData) && !(body instanceof ArrayBuffer)) { headers["content-type"] = "application/json"; payload = JSON.stringify(body); }
  const r = await fetch(path, { method, headers, body: payload });
  if (raw) return r;
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch { data = { detail: text }; }
  if (!r.ok) throw new Error(data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : String(r.status));
  return data;
}
const fail = (e) => h("div", { class: "err" }, String(e.message || e));
const tag = (text, cls = "") => h("span", { class: "tag " + cls }, text);
const statusTag = (s) => tag(s, { completed: "ok", failed: "bad", cancelled: "bad", incomplete: "bad", in_progress: "run", queued: "run", running: "run", idle: "", active: "ok", suspended: "bad", alive: "ok", lost: "bad" }[s] || "");
const when = (t) => t ? new Date(t * 1000).toLocaleString() : "";
const ago = (t) => { const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? "just now" : s < 3600 ? Math.round(s / 60) + " min ago" : s < 86400 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " d ago"; };
const num = (n) => n >= 1e9 ? (n / 1e9).toFixed(1) + "B" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(Math.round(n * 10) / 10);
const hours = (s) => s >= 3600 ? (s / 3600).toFixed(1) + " h" : s >= 60 ? Math.round(s / 60) + " min" : Math.round(s) + " s";
const bytes = (b) => b >= 1 << 30 ? (b / (1 << 30)).toFixed(1) + " GB" : b >= 1 << 20 ? (b / (1 << 20)).toFixed(1) + " MB" : b >= 1024 ? Math.round(b / 1024) + " KB" : b + " B";
const field = (label, input) => h("div", { class: "field" }, h("label", {}, label), input);
const copy = (text) => h("button", { class: "ghost small", on: { click: async (e) => { try { await navigator.clipboard.writeText(text); e.target.textContent = "copied"; } catch { e.target.textContent = "select it"; } } } }, "copy");
const empty = (text) => h("div", { class: "empty" }, text);
const isOwner = () => state.kind === "owner";
const head = (title, text, ...right) => h("div", { class: "page-head" }, h("div", {}, h("h1", {}, title), text ? h("p", {}, text) : null), right.length ? h("div", { class: "row" }, ...right) : null);
// a confirmation that lives in the page (the viewer shows no dialogs)
function confirmBtn(label, cls, onConfirm) {
  const b = h("button", { class: cls }, label);
  let armed = false;
  b.addEventListener("click", async () => { if (!armed) { armed = true; b.textContent = "sure? " + label; b.classList.add("danger"); setTimeout(() => { armed = false; b.textContent = label; b.classList.remove("danger"); }, 3000); return; } b.disabled = true; try { await onConfirm(); } catch (e) { b.after(fail(e)); b.disabled = false; } });
  return b;
}
const option = (value, label, selected) => h("option", { value, selected: selected || false }, label || value);

// boot: who is here
async function boot() {
  state.health = await (await fetch("/healthz")).json();
  state.auth = await (await fetch("/auth/providers")).json();
  document.title = "HarnessGateway";
  if (location.hash.startsWith("#reset=")) return login(null, "reset", location.hash.slice(7));
  if (state.key) {
    try {
      if (state.key.startsWith("hgd_")) { await api("GET", "/v1/fleet"); state.kind = "deployer"; state.me = { name: "deployer" }; }
      else { state.me = await api("GET", "/v1/me"); state.kind = state.me.role; }
      return shell();
    } catch (e) { sessionStorage.removeItem("hg.key"); state.key = ""; return login(fail(e)); }
  }
  try {
    const me = await api("GET", "/auth/me");
    state.account = me.account; state.orgs = me.orgs; state.me = { ...me.current, name: me.current.member, teams: (await api("GET", "/v1/me")).teams }; state.kind = me.current.role;
    return shell();
  } catch { /* not signed in */ }
  const error = location.hash.startsWith("#error=") ? { "sign-up-closed": "Sign-up is by invitation on this platform. Ask an owner for one.", "sign-in-cancelled": "The sign-in was cancelled." }[location.hash.slice(7)] : "";
  login(error ? h("div", { class: "notice warn" }, error) : null, location.hash === "#signup" ? "signup" : "signin");
}

// sign in, sign up, reset
function login(note, mode = "signin", resetToken = "") {
  const a = state.auth, open = a.signup === "open";
  const email = h("input", { type: "email", placeholder: "you@company.com", autocomplete: "email" }), pw = h("input", { type: "password", placeholder: mode === "signup" ? "at least 8 characters" : "password", autocomplete: mode === "signup" ? "new-password" : "current-password" }), name = h("input", { placeholder: "your name" }), out = h("div");
  const keyIn = h("input", { placeholder: "hg_… or the deployer key hgd_…", type: "password" });
  const go = async (fn) => { out.replaceChildren(); try { await fn(); } catch (e) { out.replaceChildren(fail(e)); } };
  const signedIn = async (me) => { location.hash = ""; state.account = me.account; state.orgs = me.orgs; state.me = { ...me.current, name: me.current.member }; state.kind = me.current.role; try { state.me.teams = (await api("GET", "/v1/me")).teams; } catch {} shell(); };
  const idp = h("div", { class: "idp" },
    a.github ? h("a", { class: "btn quiet", href: "/auth/github" }, svg("svg", { viewBox: "0 0 24 24", width: 16, height: 16, fill: "currentColor" }, svg("path", { d: "M12 .5C5.7.5.5 5.7.5 12c0 5.1 3.3 9.4 7.9 10.9.6.1.8-.3.8-.6v-2c-3.2.7-3.9-1.4-3.9-1.4-.5-1.3-1.3-1.7-1.3-1.7-1-.7.1-.7.1-.7 1.2.1 1.8 1.2 1.8 1.2 1 1.8 2.7 1.3 3.4 1 .1-.8.4-1.3.7-1.6-2.6-.3-5.3-1.3-5.3-5.7 0-1.3.4-2.3 1.2-3.1-.1-.3-.5-1.5.1-3.1 0 0 1-.3 3.2 1.2a11 11 0 0 1 5.8 0c2.2-1.5 3.2-1.2 3.2-1.2.6 1.6.2 2.8.1 3.1.8.8 1.2 1.8 1.2 3.1 0 4.4-2.7 5.4-5.3 5.7.4.4.8 1.1.8 2.2v3.2c0 .3.2.7.8.6 4.6-1.5 7.9-5.8 7.9-10.9C23.5 5.7 18.3.5 12 .5z" })), "Continue with GitHub") : null,
    a.google ? h("a", { class: "btn quiet", href: "/auth/google" }, svg("svg", { viewBox: "0 0 24 24", width: 16, height: 16 }, svg("path", { fill: "#4285F4", d: "M21.6 12.2c0-.7-.1-1.4-.2-2H12v3.9h5.4a4.6 4.6 0 0 1-2 3v2.5h3.2c1.9-1.7 3-4.3 3-7.4z" }), svg("path", { fill: "#34A853", d: "M12 22c2.7 0 5-.9 6.6-2.4l-3.2-2.5c-.9.6-2 1-3.4 1-2.6 0-4.8-1.8-5.6-4.1H3.1v2.6A10 10 0 0 0 12 22z" }), svg("path", { fill: "#FBBC05", d: "M6.4 13.9a6 6 0 0 1 0-3.8V7.5H3.1a10 10 0 0 0 0 9z" }), svg("path", { fill: "#EA4335", d: "M12 6c1.5 0 2.8.5 3.8 1.5l2.8-2.8A10 10 0 0 0 3.1 7.5l3.3 2.6C7.2 7.8 9.4 6 12 6z" })), "Continue with Google") : null,
    ...(a.identities || []).filter(n => n !== "github" && n !== "google").map(n => h("a", { class: "btn quiet", href: `/auth/${n}` }, icon("key"), `Continue with ${n === "sso" ? "single sign-on" : n}`)));
  const hasIdp = (a.identities || []).length > 0;
  let body;
  if (mode === "reset") {
    body = [h("h2", {}, "Set a new password"), h("p", { class: "sub" }, "Then you are signed in."), field("New password", pw),
      h("button", { class: "act", style: "margin-top:12px", on: { click: () => go(async () => signedIn(await api("POST", "/auth/reset/confirm", { token: resetToken, password: pw.value }))) } }, "Save and sign in")];
  } else if (mode === "forgot") {
    body = [h("h2", {}, "Reset your password"), h("p", { class: "sub" }, "We email you a link that is good for an hour."), field("Email", email),
      h("button", { class: "act", style: "margin-top:12px", on: { click: () => go(async () => { await api("POST", "/auth/reset", { email: email.value.trim() }); out.replaceChildren(h("div", { class: "notice ok" }, "If an account exists for that email, the link is on its way.")); }) } }, "Send the link"),
      h("div", { class: "links" }, h("a", { href: "#", on: { click: (e) => { e.preventDefault(); login(null, "signin"); } } }, "← Back to sign in"))];
  } else if (mode === "key") {
    body = [h("h2", {}, "Sign in with an API key"), h("p", { class: "sub" }, "A member's key opens their organization; the deployer's key opens the platform."), field("API key", keyIn),
      h("button", { class: "act", style: "margin-top:12px", on: { click: () => { state.key = keyIn.value.trim(); sessionStorage.setItem("hg.key", state.key); boot(); } } }, "Sign in"),
      h("div", { class: "links" }, h("a", { href: "#", on: { click: (e) => { e.preventDefault(); login(null, "signin"); } } }, "← Sign in with an account"))];
    keyIn.addEventListener("keydown", (e) => { if (e.key === "Enter") { state.key = keyIn.value.trim(); sessionStorage.setItem("hg.key", state.key); boot(); } });
  } else {
    const signup = mode === "signup";
    const tabs = h("div", { class: "tabs" }, h("button", { class: signup ? "" : "on", on: { click: () => login(null, "signin") } }, "Sign in"), h("button", { class: signup ? "on" : "", on: { click: () => login(null, "signup") } }, "Create account"));
    const submit = () => go(async () => {
      if (signup) {
        const r = await api("POST", "/auth/signup", { email: email.value.trim(), password: pw.value, name: name.value.trim() });
        if (r.verify) return out.replaceChildren(h("div", { class: "notice ok" }, `Check your email. We sent a verification link to ${r.email}.`), h("p", { style: "margin-top:10px" }, h("button", { class: "ghost small", on: { click: () => api("POST", "/auth/resend", { email: r.email }) } }, "Send it again")));
        await signedIn(r);
      } else await signedIn(await api("POST", "/auth/login", { email: email.value.trim(), password: pw.value }));
    });
    for (const el of [email, pw, name]) el.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    body = [h("h2", {}, signup ? "Create your account" : "Welcome back"), h("p", { class: "sub" }, signup ? (open ? "An organization of your own comes with it, on the free plan. Invite your team after." : "Sign-up is by invitation on this platform. Use the email an owner invited.") : "Sign in to your organization."),
      signup && open && state.health.trace_sharing ? h("p", { class: "sub", style: "font-size:12px" }, "Free organizations on this deployment share their session traces, pseudonymized, to train coding agents; an owner turns that off in Organization settings. ", h("a", { href: "/docs/data/", target: "_blank" }, "What is shared")) : null,
      tabs, hasIdp ? idp : null, hasIdp ? h("div", { class: "or" }, "or with email") : null,
      h("div", { class: "stack" }, signup ? field("Name", name) : null, field("Email", email), field("Password", pw)),
      h("button", { class: "act", style: "margin-top:14px", on: { click: submit } }, signup ? "Create account" : "Sign in"),
      h("div", { class: "links" }, a.mail && !signup ? h("a", { href: "#", on: { click: (e) => { e.preventDefault(); login(null, "forgot"); } } }, "Forgot your password?") : h("span"), h("a", { href: "#", on: { click: (e) => { e.preventDefault(); login(null, "key"); } } }, "Use an API key instead"))];
  }
  $("#root").replaceChildren(h("div", { class: "auth" },
    h("div", { class: "side-art" },
      h("a", { class: "brand", href: "/" }, h("span", { class: "wordmark" }, "Harness", h("em", {}, "Gateway"))),
      h("div", {}, h("h1", {}, "Your harnesses.", h("br"), "One wire. ", h("em", {}, "Sandboxed.")), h("p", {}, "The OpenAI Responses API in, your harness package in the middle, the Responses API out. Bring your providers; fork a package from the catalog or write your own; the platform runs every turn in a sandbox it owns and keeps the complete record."), h("img", { class: "mascot", src: "/web/picky.png", alt: "" })),
      h("div", { class: "muted", style: "color:#8FA3C6" }, state.health.signup === "open" ? "harnessgateway.ai" : "self-hosted", " · v", state.health.version, " · ", h("a", { href: "/docs/", style: "color:#B7C6E0" }, "Docs"))),
    h("div", { class: "form" }, h("div", {}, note, ...body, out))));
}

// the shell
const NAV = {
  member: [["Setup", [["Start", "bolt"], ["Providers", "plug"], ["Harnesses", "box"], ["Catalog", "grid"]]], ["Build", [["Conversations", "chat"], ["Rodeo", "lasso"], ["Sessions", "clock"]]], ["Team", [["Teams", "team"], ["Members", "users"], ["Keys", "key"], ["Usage", "chart"], ["Organization", "building"], ["Account", "user"]]]],
  deployer: [["Platform", [["Dashboard", "chart"], ["Organizations", "building"], ["Nodes", "server"]]]],
};
const VIEWS = {};
function pages() { return NAV[state.kind === "deployer" ? "deployer" : "member"].flatMap(([, items]) => items.map(([p]) => p)).filter(p => (p !== "Account" || state.account) && (p !== "Organization" || isOwner())); }
function signOut() { sessionStorage.removeItem("hg.key"); state.key = ""; if (state.account) api("POST", "/auth/logout").catch(() => {}); state.account = null; state.me = null; state.kind = ""; location.hash = ""; boot(); }
function shell(page, args = {}) {
  const all = pages();
  state.page = all.includes(page || state.page) ? (page || state.page) : all[0];
  state.args = args;
  const groups = NAV[state.kind === "deployer" ? "deployer" : "member"];
  const item = (p, ic) => h("button", { class: "item" + (p === state.page ? " on" : ""), on: { click: () => shell(p) } }, icon(ic), p);
  const switcher = state.account ? h("div", { class: "tenant" }, h("select", { on: { change: async (e) => { if (e.target.value === "+") { e.target.value = state.me.org; return shell("Account"); } try { const me = await api("POST", "/auth/switch", { org: e.target.value }); state.orgs = me.orgs; state.me = { ...me.current, name: me.current.member, teams: [] }; state.kind = me.current.role; try { state.me.teams = (await api("GET", "/v1/me")).teams; } catch {} shell(state.page); } catch (err) { $("#root").prepend(fail(err)); } } } },
    ...state.orgs.map(t => h("option", { value: t.org, selected: t.org === state.me.org }, `${t.org} · ${t.role}`)), h("option", { value: "+" }, "+ new organization"))) : null;
  const side = h("aside", { class: "side" }, h("a", { class: "brand", href: "/" }, h("span", { class: "wordmark" }, "Harness", h("em", {}, "Gateway"))), switcher,
    ...groups.flatMap(([g, items]) => [h("div", { class: "group" }, g), ...items.filter(([p]) => all.includes(p)).map(([p, ic]) => item(p, ic))]),
    h("div", { class: "foot" }, h("div", {}, h("b", {}, state.me?.name || ""), state.me?.org ? ` · ${state.me.org}` : ""), h("div", {}, state.kind, " · v", state.health.version),
      h("a", { href: "/docs/", class: "item", style: "padding:6px 0" }, icon("book"), "Docs"), h("button", { class: "item", style: "padding:6px 0", on: { click: signOut } }, icon("logout"), "Sign out")));
  const top = h("div", { class: "topbar" }, h("b", {}, "HarnessGateway"), ...all.map(p => h("button", { class: p === state.page ? "on" : "", on: { click: () => shell(p) } }, p)));
  const main = h("main", { class: "page" }, h("p", { class: "muted" }, "loading…"));
  $("#root").replaceChildren(h("div", { class: "shell" }, side, h("div", { style: "min-width:0" }, top, main)));
  const view = VIEWS[state.page];
  Promise.resolve(view ? view(main, args) : null).then((node) => { if (node) main.replaceChildren(node); }).catch((e) => main.replaceChildren(fail(e)));
}

// the live stream of one turn: POST /v1/responses with stream: true, each event to `onEvent`;
// resolves with the final response object
async function streamTurn(body, onEvent) {
  const r = await api("POST", "/v1/responses", { ...body, stream: true }, true);
  if (!r.ok) { const t = await r.text(); let d; try { d = JSON.parse(t).detail; } catch { d = t; } throw new Error(typeof d === "string" ? d : JSON.stringify(d)); }
  const sid = r.headers.get("x-session-id");
  const reader = r.body.getReader(), dec = new TextDecoder();
  let buf = "", final = null;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      const line = chunk.split("\n").find(l => l.startsWith("data: "));
      if (!line) continue;
      const e = JSON.parse(line.slice(6));
      if (e.type.startsWith("response.") && e.response && ["completed", "failed", "incomplete", "cancelled"].includes(e.response.status)) final = e.response;
      onEvent(e, sid);
    }
  }
  return final;
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

// Start: where a new organization stands, and the next step
VIEWS.Start = async function start() {
  const [providers, harnesses, sessions, members, keys, org] = await Promise.all(["/v1/providers", "/v1/harnesses", "/v1/sessions", "/v1/members", "/v1/keys", "/v1/org"].map(p => api("GET", p)));
  const steps = [
    ["Add a provider", "a model endpoint and its key; the key is sealed on arrival and no sandbox ever sees it", providers.data.length > 0, "Providers", isOwner()],
    ["Fork or write a harness", "a package from the catalog, or any CLI of yours; a git repository the platform hosts", harnesses.data.length > 0, "Catalog", true],
    ["Have a conversation", "a turn in a sandbox, the workspace beside it; or call POST /v1/responses with the snippet below", sessions.data.length > 0, "Conversations", true],
    ["Mint an API key", "any OpenAI SDK talks to this platform with it", keys.data.length > 0, "Keys", true],
    ["Invite your team", "members, teams that own harnesses, owners who run the organization", members.data.length > 1, "Members", isOwner()],
  ];
  const done = steps.filter(s => s[2]).length;
  const row = ([title, text, ok, page, may]) => h("div", { class: "row between", style: "padding:10px 0;border-bottom:1px solid var(--line)" },
    h("div", { class: "row" }, h("span", { class: "tag " + (ok ? "ok" : "") , style: "min-width:54px;text-align:center" }, ok ? "done" : "next"), h("div", {}, h("b", {}, title), h("div", { class: "muted" }, text))),
    may ? h("button", { class: ok ? "ghost small" : "act small", on: { click: () => shell(page) } }, ok ? "open" : "go") : h("span", { class: "muted" }, "an owner does this"));
  const snippet = `from openai import OpenAI\nclient = OpenAI(base_url="${location.origin}/v1", api_key="hg_…")\nr = client.responses.create(model="${harnesses.data[0]?.name || "my-harness"}", input="Create hello.py that prints hello and run it")\nprint(r.status, r.output_text)`;
  return h("div", {}, head(`Welcome, ${state.me.name}`, `${org.title || org.name} · ${org.plan} plan · ${done} of ${steps.length} steps done`),
    h("div", { class: "grid-2" },
      h("div", { class: "card" }, h("h2", {}, "Getting to a first turn"), ...steps.map(row)),
      h("div", {}, h("div", { class: "card" }, h("h2", {}, "From any OpenAI SDK"), h("pre", { class: "light" }, snippet), h("p", { class: "muted" }, "The model is the harness name, or harness/provider/model. Stream, continue with previous_response_id, run in the background, steer, cancel: the Responses API, as the SDK knows it."), h("a", { href: "/docs/requests/" }, "Requests, in full →")),
        h("div", { class: "card" }, h("h2", {}, "What this costs"), h("p", { class: "dim" }, "The platform meters the sandbox's compute, the bytes it sends out and the storage it keeps. Tokens are between you and your provider: shown, never billed here."), org.trace_sharing ? h("p", { class: "dim" }, "This organization shares its session traces, pseudonymized, to train coding agents. ", isOwner() ? h("a", { href: "#", on: { click: (e) => { e.preventDefault(); shell("Organization"); } } }, "Change that in Organization settings.") : null) : null, h("a", { href: "#", on: { click: (e) => { e.preventDefault(); shell("Usage"); } } }, "Usage →")))));
};
