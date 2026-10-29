// The sign-in page: a first sign-in with an initial password continues into setting a new one.
const f = document.getElementById("f"), msg = document.getElementById("msg"), go = document.getElementById("go");
const q = new URLSearchParams(location.search), next = q.get("next") || "/";
let stage = "signin", creds = null;
const say = (t, kind = "") => { msg.textContent = t; msg.className = "note " + kind; msg.hidden = !t; };
async function post(path, body) {
  const r = await fetch("/api" + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof d.error === "string" ? d.error : (d.error && d.error.message) || r.statusText);
  return d;
}
function toChange() {
  stage = "change"; document.getElementById("signin").hidden = true; document.getElementById("change").hidden = false;
  go.textContent = "Set password"; say("This is an initial password: choose your own to continue.", "warn");
  f.elements.new.focus();
}
f.addEventListener("submit", async e => {
  e.preventDefault(); go.disabled = true;
  try {
    if (stage === "signin") {
      creds = { username: f.elements.username.value.trim(), password: f.elements.password.value };
      const d = await post("/login", creds);
      if (d.must_change) toChange();
      else location.href = d.role === "deployer" && next === "/" ? "/ops" : next;
    } else {
      if (f.elements.new.value !== f.elements.again.value) throw new Error("the passwords differ");
      await post("/password", { current: creds.password, new: f.elements.new.value });
      const d = await post("/login", { username: creds.username, password: f.elements.new.value });
      location.href = d.role === "deployer" && next === "/" ? "/ops" : next;
    }
  } catch (err) { say(err.message, "bad"); } finally { go.disabled = false; }
});
