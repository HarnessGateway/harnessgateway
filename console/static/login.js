// The sign-in page. /setup?token=hgi_… is the invitation link: it activates the account (or finishes a password
// reset) by setting a password, and signs in.
const f = document.getElementById("f"), msg = document.getElementById("msg"), go = document.getElementById("go");
const q = new URLSearchParams(location.search), next = q.get("next") || "/", token = location.pathname === "/setup" ? q.get("token") || "" : "";
const say = (t, kind = "") => { msg.textContent = t; msg.className = "note " + kind; msg.hidden = !t; };
async function post(path, body) {
  const r = await fetch("/api" + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof d.error === "string" ? d.error : (d.error && d.error.message) || r.statusText);
  return d;
}
// SSO, when the organization configured it: the button goes to the identity provider and comes back signed in.
if (!token) fetch("/api/sso").then(r => r.ok ? r.json() : null).then(sso => {
  if (!sso || !sso.enabled) return;
  const local = q.get("local") === "1" || q.get("error");
  document.getElementById("sso").hidden = false;
  document.getElementById("ssogo").textContent = `Sign in with ${sso.name || "SSO"}`;
  document.getElementById("ssogo").href = "/api/sso/start?next=" + encodeURIComponent(next);
  document.getElementById("ssohelp").textContent = sso.required && !local ? "Passwords are turned off for this organization." : "or with a password:";
  if (sso.required && !local) { document.getElementById("signin").hidden = true; go.hidden = true; document.getElementById("local").hidden = false;
    for (const name of ["email", "password"]) f.elements[name].disabled = true; }
}).catch(() => {});
if (q.get("error")) say(q.get("error"), "bad");
if (token) {
  document.getElementById("signin").hidden = true; document.getElementById("setup").hidden = false;
  for (const name of ["email", "password"]) f.elements[name].disabled = true;      // hidden fields take no part in validation
  for (const name of ["new", "again"]) f.elements[name].required = true;
  go.textContent = "Set password and sign in"; say("You were invited: choose a password to activate your account.", "warn");
  f.elements.new.focus();
}
f.addEventListener("submit", async e => {
  e.preventDefault(); go.disabled = true;
  try {
    if (token) {
      if (f.elements.new.value !== f.elements.again.value) throw new Error("the passwords differ");
      await post("/accept", { token, password: f.elements.new.value });
    } else {
      await post("/login", { email: f.elements.email.value.trim(), password: f.elements.password.value });
    }
    location.href = next;
  } catch (err) { say(err.message, "bad"); } finally { go.disabled = false; }
});
