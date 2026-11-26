// The console's shell: signing in, the sidebar, and a hash router that draws one page into <main>. Plain
// JavaScript modules, no build step. Extensions add pages and ways to sign in, and replace nothing. The sidebar
// lists the pages the person signed in may use: each names the action it needs, and the platform says which they may do.
import { toast } from "./components/feedback.js";
import { get, session } from "./lib/api.js";
import { add, clear, h, icon } from "./lib/dom.js";
import { editions } from "./lib/editions.js";
import { chat } from "./pages/chat/index.js";
import { environments } from "./pages/environments.js";
import { harnesses } from "./pages/harnesses/index.js";
import { keys } from "./pages/keys.js";
import { providers } from "./pages/providers.js";
import { sessions } from "./pages/sessions.js";
import { usage } from "./pages/usage.js";

const own = (id, label, page, action) => ({ id, label, page, action, icon: id });
// The three definitions are listed for whoever may write them: their pages are where they are built.
const NAV = [
  ["", [own("chat", "Chat", chat, "responses.write")]],
  ["Resources", [own("providers", "Providers", providers, "providers.write"), own("environments", "Environments", environments, "environments.write"),
                 own("harnesses", "Harnesses", harnesses, "harnesses.write")]],
  ["Activity", [own("sessions", "Sessions", sessions, "sessions.read"), own("usage", "Usage", usage, "usage.read")]],
  ["Organization", [own("keys", "API keys", keys, "keys.read")]],
];
const { pages, signIns, signOuts, refused } = await editions(new Set(NAV.flatMap(([, items]) => items.map(item => item.id))));
for (const added of pages) {                    // an extension's pages, each in the group it names
  const group = NAV.find(([name]) => name === added.group) || NAV[NAV.push([added.group, []]) - 1];
  group[1].push(added);
}
const PAGES = Object.fromEntries(NAV.flatMap(([, items]) => items).map(item => [item.id, item]));
const $ = id => document.getElementById(id);
let signedIn = false;
const listed = item => !item.action || session.allows.has(item.action);

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
    session.allows = new Set((await get("/v1/permissions")).allows);
    const platform = session.allows.has("platform.read") ? await get("/v1/platform") : null;
    $("about").textContent = platform ? `v${platform.version} · isolation ${platform.isolation}` : "";
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
  const shown = NAV.map(([group, items]) => [group, items.filter(listed)]).filter(([, items]) => items.length);
  const current = PAGES[id] && listed(PAGES[id]) ? PAGES[id] : shown.length ? shown[0][1][0] : null;      // the first that is theirs
  add(clear($("nav")), shown.map(([group, items]) => [group && h("div", { class: "group" }, group),
    items.map(item => h("a", { href: `#/${item.id}`, class: item === current ? "on" : "" }, icon(item.icon), item.label))]));
  const main = h("main", { id: "page" });         // a fresh element: nothing of the last page is left listening
  if ($("page").leave) $("page").leave();         // and a page that follows something stops following it
  $("page").replaceWith(main);
  if (!current) return void add(main, h("p", { class: "muted" }, "Nothing here is yours to use yet: ask whoever administers this organization."));
  try { await current.page(main, params); } catch (error) { add(clear(main), h("p", { class: "bad" }, error.message)); }
}

$("signin").addEventListener("submit", event => { event.preventDefault(); session.key = $("key").value.trim(); start(); });
$("signout").addEventListener("click", async () => {      // a sign-in made elsewhere is ended there, while the credential is held
  await Promise.allSettled(signOuts.map(end => end()));
  session.key = null;
  signIn();
});
window.addEventListener("hg-signed-out", () => { if (signedIn) { session.key = null; signIn("Sign in again: that key is no longer valid."); } });
window.addEventListener("hashchange", route);
add($("signin-ways"), signIns.map(way => h("a", { class: "way", href: way.href }, way.label)));     // each leads away,
refused.forEach(why => toast(why, true));
const arrived = /^#credential=(.+)$/.exec(location.hash);       // and comes back with a credential in the address:
if (arrived) {                                                  // it is taken, and the address no longer holds it
  session.key = decodeURIComponent(arrived[1]);
  history.replaceState(null, "", location.pathname + location.search);
}
session.key ? start() : signIn();
