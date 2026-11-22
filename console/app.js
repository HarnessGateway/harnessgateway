// The console's shell: signing in, the sidebar, and a hash router that draws one page into <main>. Plain
// JavaScript modules, no build step.
import { get, session } from "./api.js";
import { chat } from "./chat.js";
import { pages } from "./edition.js";
import { environments } from "./environments.js";
import { harnesses } from "./harnesses.js";
import { providers } from "./providers.js";
import { sessions } from "./sessions.js";
import { add, clear, h, icon } from "./ui.js";
import { usage } from "./usage.js";

const NAV = [
  ["", [["chat", "Chat", chat]]],
  ["Resources", [["providers", "Providers", providers], ["environments", "Environments", environments], ["harnesses", "Harnesses", harnesses]]],
  ["Activity", [["sessions", "Sessions", sessions], ["usage", "Usage", usage]]],
];
for (const added of pages) {                    // an edition's pages, each in the group it names
  const group = NAV.find(([name]) => name === added.group) || NAV[NAV.push([added.group, []]) - 1];
  group[1].push([added.id, added.label, added.page, added.icon]);
}
const PAGES = Object.fromEntries(NAV.flatMap(([, items]) => items).map(([id, , page]) => [id, page]));
const $ = id => document.getElementById(id);
let signedIn = false;

function signIn(message = "") {
  signedIn = false;
  $("shell").hidden = true;
  $("signin").hidden = false;
  $("signin-error").textContent = message;
  $("signin-error").hidden = !message;
  $("key").value = "";
  $("key").focus();
}

async function start() {
  try {
    const platform = await get("/v1/platform");
    $("about").textContent = `v${platform.version} · isolation ${platform.isolation}`;
  } catch (error) {
    session.key = null;
    return signIn(error.status === 401 ? "That key is not valid." : error.message);
  }
  signedIn = true;
  $("signin").hidden = true;
  $("shell").hidden = false;
  route();
}

async function route() {
  if (!signedIn) return;
  const [id, ...params] = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  const current = id in PAGES ? id : "chat";
  add(clear($("nav")), NAV.map(([group, items]) => [group && h("div", { class: "group" }, group),
    items.map(([name, label, , glyph = name]) => h("a", { href: `#/${name}`, class: name === current ? "on" : "" }, icon(glyph), label))]));
  const main = h("main", { id: "page" });         // a fresh element: nothing of the last page is left listening
  $("page").replaceWith(main);
  try { await PAGES[current](main, params); } catch (error) { add(clear(main), h("p", { class: "bad" }, error.message)); }
}

$("signin").addEventListener("submit", event => { event.preventDefault(); session.key = $("key").value.trim(); start(); });
$("signout").addEventListener("click", () => { session.key = null; signIn(); });
window.addEventListener("hg-signed-out", () => { if (signedIn) { session.key = null; signIn("Sign in again: that key is no longer valid."); } });
window.addEventListener("hashchange", route);
session.key ? start() : signIn();
