// The console shell: the sidebar (modules and items, collapsible), the header, the account menu, and a hash router
// that renders one page module into #content. Which app this is (user or ops) comes from <body data-app>.
import { api, esc } from "./api.js";
import { $, I, fail, field, ic, modal, toast } from "./ui.js";

const APP = document.body.dataset.app;             // "user" | "ops"
const SIDE = `hg-console-side-${APP}`;
const me = { name: "", role: "", platform: {}, ops: false, api: "" };
let pages = {}, nav = [];

async function boot() {
  Object.assign(me, await api("GET", "/me"));
  if (APP === "ops" && me.role !== "deployer") { location.href = "/"; return; }
  const mod = await import(APP === "ops" ? "./pages_ops.js" : "./pages_user.js");
  pages = mod.pages; nav = mod.nav(me);
  $("#brand").innerHTML = `<img class="logo" src="/static/mascot.png" alt=""><b>HarnessGateway${APP === "ops" ? " · Operations" : ""}</b>` +
    `<button class="ib" id="sideclose" title="Hide the sidebar">${ic(I.side, 18)}</button>`;
  $("#sideopen").innerHTML = ic(I.side, 18);
  $("#sideclose").addEventListener("click", toggleSide); $("#sideopen").addEventListener("click", toggleSide);
  try { document.body.classList.toggle("noside", localStorage.getItem(SIDE) === "closed"); } catch { /* blocked */ }
  $("#sideopen").hidden = !document.body.classList.contains("noside");
  $("#foot").innerHTML = `<a href="https://github.com/harnessgateway/harnessgateway/tree/main/docs" target="_blank" rel="noopener">Docs ↗</a>` +
    `<span style="margin-left:auto">v${esc(me.platform.version || "?")}${(me.platform.extensions || []).length ? " · " + esc(me.platform.extensions.join(", ")) : ""}</span>`;
  $("#who").innerHTML = `<span>${esc(me.name)}</span><span class="av">${esc(me.name.slice(0, 1).toUpperCase())}</span>`;
  $("#who").addEventListener("click", accountMenu);
  renderNav();
  window.addEventListener("hashchange", route);
  route();
}

function toggleSide() {
  const closed = document.body.classList.toggle("noside");
  $("#sideopen").hidden = !closed;
  try { localStorage.setItem(SIDE, closed ? "closed" : "open"); } catch { /* blocked */ }
}

function current() {
  const h = location.hash.replace(/^#\/?/, "");
  const [id, ...rest] = h.split("/");
  return { id: id || nav[0].items[0].id, params: rest.map(decodeURIComponent) };
}

function renderNav() {
  const { id } = current();
  $("#nav").innerHTML = nav.map(m => `<div class="mod">${esc(m.mod)}${m.tag ? ` <span class="pill q">${esc(m.tag)}</span>` : ""}</div>` +
    m.items.map(it => `<a class="item${it.id === id || (id === "drafts" && it.id === "harnesses") ? " on" : ""}" href="#/${it.id}">${ic(I[it.icon] || I.grid)}${esc(it.label)}</a>`).join("")).join("");
}

async function route() {
  const { id, params } = current();
  const page = pages[id] || pages[nav[0].items[0].id];
  renderNav();
  const label = nav.flatMap(m => m.items).find(it => it.id === (id === "drafts" ? "harnesses" : id));
  $("#crumb").innerHTML = `${esc(APP === "ops" ? "Operations" : label && label.mod ? label.mod : "")} / <b>${esc(label ? label.label : page.title)}</b>` +
    (params[0] && id !== "chat" ? ` / <b>${esc(params[0])}</b>` : "");
  const stale = $("#content"), el = stale.cloneNode(false);   // a fresh element: no listeners from the last page
  stale.replaceWith(el);
  el.className = page.full ? "full" : "";
  el.innerHTML = page.full ? "" : `<div class="muted">Loading…</div>`;
  try { await page.render(el, params, me); } catch (e) { el.innerHTML = `<div class="note bad">${esc(e.message || e)}</div>`; }
}

function accountMenu(e) {
  e.stopPropagation();
  let menu = $(".menu");
  if (menu) { menu.remove(); return; }
  menu = document.createElement("div");
  menu.className = "menu";
  menu.innerHTML = `<div class="hd">${esc(me.name)} · ${esc(me.role)}</div>` +
    (APP === "user" && me.ops ? `<button data-go="/ops">Deployer console</button>` : "") +
    (APP === "ops" ? `<button data-go="/">Organization console</button>` : "") +
    `<button data-pw>Change password</button><button data-out>Sign out</button>`;
  $("#who").appendChild(menu);
  const close = () => { menu.remove(); document.removeEventListener("click", close); };
  setTimeout(() => document.addEventListener("click", close), 0);
  menu.querySelector("[data-go]")?.addEventListener("click", ev => { location.href = ev.target.dataset.go; });
  menu.querySelector("[data-pw]").addEventListener("click", changePassword);
  menu.querySelector("[data-out]").addEventListener("click", async () => { await api("POST", "/logout"); location.href = "/login"; });
}

export function changePassword() {
  return modal({ title: "Change password", submit: "Change",
    body: field("Current password", `<input name="current" type="password" autocomplete="current-password" required>`) +
          field("New password", `<input name="new" type="password" autocomplete="new-password" minlength="12" required>`, "at least 12 characters"),
    onSubmit: async d => { await api("POST", "/password", d); toast("Password changed", "ok"); } });
}

boot().catch(fail);
