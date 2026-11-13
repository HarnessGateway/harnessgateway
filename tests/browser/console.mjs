// The console, driven in a real browser against a running platform (spec §20).
//
//   HG_TEST_URL=http://localhost:8000 HG_TEST_KEY=hg_...(an admin key) node tests/browser/console.mjs
//
// tests/browser/test_console.py runs it against a platform it starts itself. It needs Playwright with its
// Chromium, and a platform whose catalog holds the shell package. Against a platform that stays up it leaves
// behind a session, an environment, a provider and a key, all named "browser-…".

import { createRequire } from "node:module";

const { chromium } = createRequire(import.meta.url)("playwright");   // found wherever Node looks for modules

const url = process.env.HG_TEST_URL, key = process.env.HG_TEST_KEY;
if (!url || !key) {
  console.error("set HG_TEST_URL and HG_TEST_KEY");
  process.exit(2);
}
const stamp = Date.now().toString(36);
const steps = [];
const step = async (name, action) => {
  await action();
  steps.push(name);
  console.log(`ok  ${name}`);
};

const browser = await chromium.launch();
const page = await browser.newPage();
page.setDefaultTimeout(60_000);
page.on("dialog", (dialog) => dialog.accept());
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));

try {
  await step("a wrong key is refused, the right one signs in", async () => {
    await page.goto(url);
    await page.fill("#key", "hg_not_a_key");
    await page.click("button:has-text('Sign in')");
    await page.waitForSelector(".error");
    await page.fill("#key", key);
    await page.click("button:has-text('Sign in')");
    await page.waitForSelector("nav >> text=Signed in as admin");
  });

  await step("a harness is added from the catalog and installs", async () => {
    await page.click("nav >> text=Harnesses");
    await page.waitForSelector("#catalog");
    if (!(await page.locator("#harness-shell").count())) {
      await page.selectOption("#catalog", "shell");
      await page.click(".card >> text=Add >> nth=0");
    }
    await page.waitForSelector("#harness-shell");
    for (let tries = 0; tries < 60 && !(await page.locator("#harness-shell .pill.good").count()); tries++) {
      await page.waitForTimeout(1000);
      await page.reload();
      await page.waitForSelector("#harness-shell");
    }
    await page.waitForSelector("#harness-shell .pill.good >> text=ready");
  });

  await step("a message runs, streams its answer, and shows the file it made", async () => {
    await page.click("nav >> text=Chat");
    await page.waitForSelector("#message");
    await page.selectOption("select >> nth=1", "shell");
    await page.fill("#message", "echo hello from the console > made.txt; cat made.txt");
    await page.click("button:has-text('Send')");
    await page.waitForSelector(".turn .text >> text=hello from the console");
    await page.waitForSelector(".turn .foot .pill.good >> text=completed");
    await page.waitForSelector("text=made.txt");
  });

  await step("the next message continues the same session", async () => {
    await page.waitForSelector("text=The next message continues from resp_");
    await page.fill("#message", "cat made.txt; echo second turn");
    await page.click("button:has-text('Send')");
    await page.waitForSelector(".turn >> nth=1 >> text=second turn");
    await page.waitForSelector(".turn >> nth=1 >> .pill.good");
  });

  await step("a failing command shows as failed with its reason", async () => {
    await page.fill("#message", "echo before; exit 3");
    await page.click("button:has-text('Send')");
    await page.waitForSelector(".turn >> nth=2 >> .pill.bad >> text=failed");
    await page.waitForSelector(".turn >> nth=2 >> text=reason: harness_failed");
  });

  await step("the session, its turns, a trace and a file are shown", async () => {
    await page.click("nav >> text=Sessions");
    await page.click("tr.pick >> nth=0");
    await page.waitForSelector("h2 >> text=Turns");
    await page.waitForSelector("td >> text=work/made.txt");
    await page.click("tbody >> nth=0 >> tr.pick >> nth=0");
    await page.waitForSelector("h2 >> text=Sandbox command");
    await page.waitForSelector("pre:has-text(\"/harness/run\")");
    await page.click("a >> text=work/made.txt");
    await page.waitForSelector("pre >> text=hello from the console");
  });

  await step("an environment is created", async () => {
    await page.click("nav >> text=Environments");
    const editor = page.locator(".card >> nth=-1 >> textarea");
    const definition = JSON.parse(await editor.inputValue());
    definition.name = `browser-${stamp}`;
    definition.memory_mb = 1024;
    await editor.fill(JSON.stringify(definition));
    await page.click("button:has-text('Create')");
    await page.waitForSelector(`h3 >> text=browser-${stamp}`);
  });

  await step("an invalid environment is refused with the field named", async () => {
    const editor = page.locator(".card >> nth=-1 >> textarea");
    await editor.fill(JSON.stringify({ name: `browser-bad-${stamp}`, cpus: 1000 }));
    await page.click("button:has-text('Create')");
    await page.waitForSelector(".error >> text=cpus");
  });

  await step("a provider is added and its key is never shown", async () => {
    await page.click("nav >> text=Providers");
    const form = page.locator(".card >> nth=-1");
    await form.locator("input >> nth=0").fill(`browser-${stamp}`);
    await form.locator("input >> nth=1").fill("https://api.example.com/anthropic");
    await form.locator("input[type=password]").fill("a-secret-key-value");
    await form.locator("button:has-text('Add')").click();
    await page.waitForSelector(`h3 >> text=browser-${stamp}`);
    await page.waitForSelector(".pill.good >> text=key set");
    if ((await page.content()).includes("a-secret-key-value")) throw new Error("the key is on the page");
  });

  await step("a key is created and shown once", async () => {
    await page.click("nav >> text=Keys");
    await page.fill("input[placeholder='what it is for']", `browser-${stamp}`);
    await page.click("button:has-text('Create')");
    const made = await page.textContent("#new-key");
    if (!made.startsWith("hg_")) throw new Error(`not a key: ${made}`);
  });

  await step("usage and the audit record are shown", async () => {
    await page.click("nav >> text=Activity");
    await page.waitForSelector("td >> text=shell");
    await page.waitForSelector("td >> text=harness.add");
    await page.waitForSelector("td >> text=key.create");
  });

  await step("signing out returns to the sign-in form", async () => {
    await page.click("text=Sign out");
    await page.waitForSelector("#key");
  });

  if (errors.length) throw new Error(`the page raised errors: ${errors.join("; ")}`);
  console.log(`\n${steps.length} steps passed`);
} catch (error) {
  await page.screenshot({ path: process.env.HG_TEST_SCREENSHOT || "console-failure.png", fullPage: true });
  console.error(`FAILED after "${steps.at(-1) ?? "start"}": ${error.message}`);
  process.exitCode = 1;
} finally {
  await browser.close();
}
