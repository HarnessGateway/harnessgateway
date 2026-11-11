"""The console as a person uses it (SPEC §10, §14): the invitation activates the account on the setup page, the
organization and platform consoles open with the new pages, sign-out and sign-in work, SSO stays out of the way until
it is configured, a used invitation is refused, and a deployment's own page module joins both consoles."""
import httpx
from playwright.sync_api import expect

PASSWORD = "a-browser-chosen-password"


def test_setup_sign_out_and_sign_in(page, stack):
    page.goto(stack.invitation)
    expect(page.locator("#setup")).to_be_visible()
    expect(page.locator("#signin")).to_be_hidden()
    expect(page.locator("#sso")).to_be_hidden()                                      # no identity provider configured
    page.fill("input[name=new]", PASSWORD)
    page.fill("input[name=again]", PASSWORD + "x")
    page.click("#go")
    expect(page.locator("#msg")).to_contain_text("the passwords differ")
    page.fill("input[name=again]", PASSWORD)
    page.click("#go")
    page.wait_for_url(f"{stack.console}/")
    expect(page.locator("#who")).to_contain_text(stack.email)
    expect(page.locator("#nav")).to_contain_text("Harnesses")
    expect(page.locator("#content")).to_contain_text("harness-package-builder")      # installed by init
    expect(page.locator("#content")).to_contain_text("installing")                   # no worker ran its install yet
    page.goto(f"{stack.console}/ops")                                                # the deployer is a platform admin
    expect(page.locator("#content h1")).to_have_text("Health")
    expect(page.locator("#content")).to_contain_text("Redis")
    page.click("#nav a[href='#/boxes']")
    expect(page.locator("#content h1")).to_have_text("Boxes")
    expect(page.locator("#content")).to_contain_text("No box is live")
    page.click("#nav a[href='#/orgs']")
    expect(page.locator("#content")).to_contain_text("standby")
    page.click("#who")
    page.click("button[data-out]")
    page.wait_for_url(f"{stack.console}/login")
    page.goto(f"{stack.console}/")                                                   # signed out: back to the login
    page.wait_for_url(f"{stack.console}/login")
    page.fill("input[name=email]", stack.email)
    page.fill("input[name=password]", "not-the-password")
    page.click("#go")
    expect(page.locator("#msg")).to_contain_text("unknown email or wrong password")
    page.fill("input[name=password]", PASSWORD)
    page.click("#go")
    page.wait_for_url(f"{stack.console}/")
    expect(page.locator("#who")).to_contain_text(stack.email)


def test_a_used_invitation_is_refused(page, stack):
    page.goto(stack.invitation)
    page.fill("input[name=new]", PASSWORD)
    page.fill("input[name=again]", PASSWORD)
    page.click("#go")
    expect(page.locator("#msg")).to_contain_text("unknown, used or expired")
    expect(page).to_have_url(stack.invitation)


def test_the_console_relays_the_api_and_the_sso_status(stack):
    r = httpx.get(f"{stack.console}/api/sso")
    assert r.status_code == 200 and r.json() == {"object": "sso", "enabled": False, "name": "", "required": False}
    r = httpx.get(f"{stack.console}/api/sso/start", follow_redirects=False)
    assert r.status_code == 302 and "/login?error=" in r.headers["location"]       # not configured: back to the login
    r = httpx.get(f"{stack.console}/api/v1/harnesses")
    assert r.status_code == 401                                                      # no cookie, no relay
    r = httpx.get(f"{stack.api}/v1/harnesses", headers={"Authorization": f"Bearer {stack.admin_key}"})
    assert [h["name"] for h in r.json()["data"]] == ["harness-package-builder"]


def sign_in(page, stack):
    page.goto(f"{stack.console}/login")
    page.fill("input[name=email]", stack.email)
    page.fill("input[name=password]", PASSWORD)
    page.click("#go")
    page.wait_for_url(f"{stack.console}/")


def test_a_deployments_own_pages_join_both_consoles(page, stack):
    sign_in(page, stack)
    expect(page.locator("#nav a")).to_have_text(["Harnesses", "Providers", "Egress", "Chat", "Playground", "API keys", "Accounts",
                                                 "Single sign-on", "Sessions", "Usage", "Audit log", "Notes"])
    expect(page.locator("#nav .mod")).to_have_text(["Setup", "Workbench", "Access", "Observability"])   # joined a group
    page.click("#nav a[href='#/notes']")
    expect(page.locator("#content h1")).to_have_text("Notes")
    expect(page.locator("#note")).to_have_text(f"{stack.email} · admin")
    expect(page.locator("#crumb")).to_have_text("Observability / Notes")
    page.goto(f"{stack.console}/ops")
    expect(page.locator("#nav .mod")).to_have_text(["Platform", "Deployment"])                    # a group of its own
    page.click("#nav a[href='#/notes']")
    expect(page.locator("#note")).to_have_text(f"{stack.email} · admin")
    assert httpx.get(f"{stack.console}/pages/notes.js").headers["content-type"] == "text/javascript; charset=utf-8"
    assert httpx.get(f"{stack.console}/pages/../server.py").status_code == 404


def test_an_admin_configures_sso_and_a_person_signs_in_through_it(page, stack, idp):
    sign_in(page, stack)
    page.click("#nav a[href='#/sso']")
    expect(page.locator("#content h1")).to_have_text("Single sign-on")
    expect(page.locator("#content")).to_contain_text("Not configured")
    expect(page.locator("#content")).to_contain_text(f"{stack.console}/sso/callback")     # what to register at the provider
    page.click("#edit")
    page.fill(".modal input[name=name]", "Fake IdP")
    page.fill(".modal input[name=issuer]", "https://idp.invalid")                         # discovery fails: nothing is saved
    page.fill(".modal input[name=client_id]", idp.client_id)
    page.fill(".modal input[name=client_secret]", idp.client_secret)
    page.fill(".modal input[name=domains]", "sso.example.test")
    page.click(".modal button[type=submit]")
    expect(page.locator(".toast.bad")).to_contain_text("idp.invalid does not resolve")
    page.fill(".modal input[name=issuer]", idp.url)
    page.click(".modal button[type=submit]")
    expect(page.locator(".toast.ok")).to_contain_text("Saved")
    expect(page.locator("#content")).to_contain_text("Fake IdP")
    expect(page.locator("#content")).to_contain_text(idp.url)
    expect(page.locator("#content")).to_contain_text("set (write-only)")
    assert idp.client_secret not in page.content()                                      # the secret is never sent back

    page.click("#who")
    page.click("button[data-out]")
    page.wait_for_url(f"{stack.console}/login")
    expect(page.locator("#ssogo")).to_have_text("Sign in with Fake IdP")
    page.click("#ssogo")                                     # to the provider, which signs in at once, and back
    page.wait_for_url(f"{stack.console}/")
    expect(page.locator("#who")).to_contain_text(idp.name)
    expect(page.locator("#nav")).not_to_contain_text("Accounts")                         # a developer, by default

    page.click("#who")
    page.click("button[data-out]")
    page.wait_for_url(f"{stack.console}/login")
    sign_in(page, stack)
    page.click("#nav a[href='#/accounts']")
    expect(page.locator("#content")).to_contain_text(idp.email)
    page.click("#nav a[href='#/sso']")
    page.click("#remove")
    page.click(".modal button[type=submit]")
    expect(page.locator("#content")).to_contain_text("Not configured")
    assert httpx.get(f"{stack.console}/api/sso").json()["enabled"] is False
