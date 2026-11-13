// The console's shell (spec §20): signing in, the sidebar (modules and their pages, collapsible), the header, the
// account menu, and a hash router that renders one page into #content. Plain JavaScript, no build step, and no
// endpoints of its own: every page calls the platform's API as the person who signed in.
import { get, session } from "/api.js";
import { pages as added, signIns, signOut } from "/extensions.js";
import { esc } from "/lib.js";
import { nav as ownNav, pages as ownPages } from "/pages.js";
import { $, I, ic } from "/views.js";

const ORDER = { runner: 0, developer: 1, admin: 2 };
const SIDE = "hg-console-side";
let me = null;                                       // /v1/platform: the role, the version, the isolation
let pages = {}, nav = [];

// A page another edition adds: its module's render(root, {about, args}), in a module of the sidebar it names.
function extended(page) {
  return { title: page.title, async render(el, params) {
    el.replaceChildren();
    await (await import(page.module)).render(el, { about: me, args: params });
  } };
}
function build() {
  pages = { ...ownPages }; nav = ownNav(me);
  for (const page of added) {
    if (ORDER[me.role] < ORDER[page.role || "runner"]) continue;
    pages[page.id] = extended(page);
    const title = page.group || "Organization";
    let mod = nav.find(m => m.mod === title);
    if (!mod) nav.push(mod = { mod: title, items: [] });
    mod.items.push({ id: page.id, label: page.title, icon: page.icon || "org", mod: title });
  }
}

function signIn(message = "") {
  me = null;
  document.body.className = "login";
  const form = $("#signin"), msg = $("#msg");
  form.hidden = false;
  msg.textContent = message; msg.hidden = !message;
  $("#ways").innerHTML = signIns.map(way => `<a class="btn2 way" href="${esc(way.href)}">${esc(way.title)}</a>`).join("");
  $("#key").value = ""; $("#key").focus();
}
$("#signin").addEventListener("submit", async event => {
  event.preventDefault();
  const key = $("#key").value.trim();
  try {
    me = await get("/v1/platform", { key });
    session.key = key;
    start();
  } catch (error) { $("#msg").textContent = error.message; $("#msg").hidden = false; }
});
async function leave() {
  session.key = null;
  if (signOut) await signOut().catch(() => {});
  signIn();
}
window.addEventListener("hg-signed-out", () => { if (me) { session.key = null; signIn("Sign in again: the key is no longer valid."); } });

async function boot() {
  try {
    me = await get("/v1/platform");                    // answers for a key, or for whoever an edition's sign-in says it is
  } catch (error) {
    const hadKey = Boolean(session.key);
    session.key = null;
    return signIn(error.status !== 401 ? error.message : hadKey ? "That key is no longer valid." : "");
  }
  start();
}

function start() {
  document.body.className = "";
  $("#signin").hidden = true;
  build();
  $("#brand").innerHTML = `<img class="logo" src="/mascot.png" alt=""><b>HarnessGateway</b>` +
    `<button class="ib" id="sideclose" title="Hide the sidebar">${ic(I.side, 18)}</button>`;
  $("#sideopen").innerHTML = ic(I.side, 18);
  $("#sideclose").onclick = toggleSide; $("#sideopen").onclick = toggleSide;
  try { document.body.classList.toggle("noside", localStorage.getItem(SIDE) === "closed"); } catch { /* blocked */ }
  $("#sideopen").hidden = !document.body.classList.contains("noside");
  const unsafe = me.isolation === "none";
  $("#foot").innerHTML = `<a href="https://github.com/HarnessGateway/harnessgateway#readme" target="_blank" rel="noopener">Docs ↗</a>` +
    (unsafe ? `<span class="pill warn" title="Sandboxes share this host's kernel: for development and trusted code. Install gVisor to isolate them.">no isolation</span>` : "") +
    `<span style="margin-left:auto">v${esc(me.version || "?")}</span>`;
  $("#who").innerHTML = `<span>${esc(me.role)}</span><span class="av">${esc(me.role.slice(0, 1).toUpperCase())}</span>`;
  $("#who").onclick = accountMenu;
  route();
}

function toggleSide() {
  const closed = document.body.classList.toggle("noside");
  $("#sideopen").hidden = !closed;
  try { localStorage.setItem(SIDE, closed ? "closed" : "open"); } catch { /* blocked */ }
}

function current() {
  const [id, ...rest] = location.hash.replace(/^#\/?/, "").split("/");
  return { id: id || nav[0].items[0].id, params: rest.map(decodeURIComponent) };
}

function renderNav() {
  const { id } = current();
  $("#nav").innerHTML = nav.map(m => `<div class="mod">${esc(m.mod)}</div>` +
    m.items.map(it => `<a class="item${it.id === id || (id === "drafts" && it.id === "harnesses") ? " on" : ""}" href="#/${it.id}">${ic(I[it.icon] || I.grid)}${esc(it.label)}</a>`).join("")).join("");
}

async function route() {
  if (!me) return;
  const { id, params } = current();
  const page = pages[id] || pages[nav[0].items[0].id];
  renderNav();
  const label = nav.flatMap(m => m.items).find(it => it.id === (id === "drafts" ? "harnesses" : id));
  $("#crumb").innerHTML = `${esc(label && label.mod ? label.mod : "")} / <b>${esc(label ? label.label : page.title)}</b>` +
    (params[0] && id !== "chat" ? ` / <b>${esc(params[0])}</b>` : "");
  const stale = $("#content"), el = stale.cloneNode(false);   // a fresh element: no listeners from the last page
  stale.replaceWith(el);
  el.className = page.full ? "full" : "";
  el.innerHTML = page.full ? "" : `<div class="muted">Loading…</div>`;
  try { await page.render(el, params, me); } catch (e) { el.innerHTML = `<div class="note bad">${esc(e.message || e)}</div>`; }
}
window.addEventListener("hashchange", route);

function accountMenu(e) {
  e.stopPropagation();
  let menu = $(".menu");
  if (menu) { menu.remove(); return; }
  menu = document.createElement("div");
  menu.className = "menu";
  menu.innerHTML = `<div class="hd">Signed in as ${esc(me.role)}<br>Sandboxes: ${esc(me.driver)}, isolation ${esc(me.isolation)}</div>` +
    `<button data-out>Sign out</button>`;
  $("#who").appendChild(menu);
  const close = () => { menu.remove(); document.removeEventListener("click", close); };
  setTimeout(() => document.addEventListener("click", close), 0);
  menu.querySelector("[data-out]").addEventListener("click", leave);
}

boot();
