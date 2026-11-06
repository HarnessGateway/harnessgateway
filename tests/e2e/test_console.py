"""The console as a person uses it: the invitation activates the account on the setup page, the organization and
platform consoles open, sign-out and sign-in work, and a used invitation is refused (SPEC §9.3)."""
from playwright.sync_api import expect

PASSWORD = "a-browser-chosen-password"


def test_setup_sign_out_and_sign_in(page, stack):
    page.goto(stack.invitation)
    expect(page.locator("#setup")).to_be_visible()
    expect(page.locator("#signin")).to_be_hidden()
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
    page.goto(f"{stack.console}/ops")                                                # the deployer is a platform admin
    expect(page.locator("#content h1")).to_have_text("Health")
    expect(page.locator("#content")).to_contain_text("Database")
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
