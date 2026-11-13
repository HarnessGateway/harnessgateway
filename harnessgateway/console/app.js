// The console (spec §20): plain JavaScript, no build step, no endpoints of its own.

import { get, session } from "/api.js";
import { pages as added, signIns, signOut } from "/extensions.js";
import { clear, h, put } from "/ui.js";

const ORDER = { runner: 0, developer: 1, admin: 2 };
const PAGES = [
  { id: "chat", title: "Chat", module: "/pages/chat.js", role: "runner" },
  { id: "sessions", title: "Sessions", module: "/pages/sessions.js", role: "developer" },
  { id: "harnesses", title: "Harnesses", module: "/pages/harnesses.js", role: "developer" },
  { id: "environments", title: "Environments", module: "/pages/environments.js", role: "developer" },
  { id: "providers", title: "Providers", module: "/pages/providers.js", role: "developer" },
  { id: "keys", title: "Keys", module: "/pages/keys.js", role: "admin" },
  { id: "activity", title: "Activity", module: "/pages/activity.js", role: "developer" },
  ...added,
];

const app = document.getElementById("app");
let about = null;

function allowed(page) {
  return ORDER[about.role] >= ORDER[page.role || "runner"];
}

function signIn(message) {
  const key = h("input", { type: "password", placeholder: "hg_…", autofocus: true, id: "key" });
  const problem = h("div", {});
  const enter = async (event) => {
    event.preventDefault();
    try {
      about = await get("/v1/platform", { key: key.value.trim() });
      session.key = key.value.trim();
      route();
    } catch (error) {
      put(clear(problem), h("div", { class: "error" }, error.message));
    }
  };
  put(clear(app), h("form", { class: "signin card", onsubmit: enter },
    h("h1", {}, "HarnessGateway"),
    h("p", { class: "muted" }, "Sign in with an API key. It is kept in this browser tab only."),
    message ? h("div", { class: "error" }, message) : null,
    h("label", { for: "key" }, "API key"), key, problem,
    h("p", {}, h("button", { class: "primary", type: "submit" }, "Sign in")),
    signIns.map((way) => h("p", {}, h("a", { class: "button", href: way.href }, way.title)))));
}

function shell(current) {
  const main = h("main", {});
  put(clear(app),
    h("nav", {},
      h("div", { class: "brand" }, "HarnessGateway"),
      PAGES.filter(allowed).map((page) =>
        h("a", { href: `#/${page.id}`, class: page.id === current.id ? "here" : "" }, page.title)),
      h("div", { class: "about" },
        h("div", {}, `Version ${about.version}`),
        h("div", { class: about.isolation === "none" ? "warn" : "" },
          about.isolation === "none" ? "Isolation: none (sandboxes share the host kernel)" : `Isolation: ${about.isolation}`),
        h("div", {}, `Signed in as ${about.role}`),
        h("div", {}, h("a", { href: "#", onclick: leave }, "Sign out")))),
    main);
  return main;
}

async function leave(event) {
  event.preventDefault();
  session.key = null;
  about = null;
  if (signOut) await signOut().catch(() => {});
  signIn();
}

async function route() {
  if (!about) {
    try {
      about = await get("/v1/platform");       // answers for a key, or for whoever an edition's sign-in says it is
    } catch (error) {
      const hadKey = Boolean(session.key);
      session.key = null;
      if (error.status !== 401) return signIn(error.message);
      return signIn(hadKey ? "That key is no longer valid." : undefined);
    }
  }
  const [id, ...rest] = location.hash.replace(/^#\/?/, "").split("/");
  const page = PAGES.find((candidate) => candidate.id === id && allowed(candidate)) || PAGES[0];
  const main = shell(page);
  try {
    const module = await import(page.module);
    await module.render(main, { about, args: rest.map(decodeURIComponent) });
  } catch (error) {
    put(main, h("div", { class: "error" }, error.message));
  }
}

window.addEventListener("hashchange", route);
route();
