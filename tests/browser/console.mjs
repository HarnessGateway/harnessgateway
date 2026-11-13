// The console, driven in a real browser against a running platform (spec §20).
//
//   HG_TEST_URL=http://localhost:8000 HG_TEST_KEY=hg_...(an admin key) node tests/browser/console.mjs
//
// tests/browser/test_console.py runs it against a platform it starts itself. It needs Playwright with its
// Chromium, and a platform whose catalog holds the shell package. Against a platform that stays up it leaves
// behind sessions, harnesses, an environment, a provider and a key, all named "browser-…" or "b…".

import { createRequire } from "node:module";

const { chromium } = createRequire(import.meta.url)("playwright");   // found wherever Node looks for modules

const url = process.env.HG_TEST_URL, key = process.env.HG_TEST_KEY;
if (!url || !key) {
  console.error("set HG_TEST_URL and HG_TEST_KEY");
  process.exit(2);
}
const stamp = Date.now().toString(36);
const step = async (name, action) => {
  await action();
  console.log(`ok  ${name}`);
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.setDefaultTimeout(60_000);
page.on("dialog", (dialog) => dialog.accept());
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));

const go = async (hash, heading) => {
  await page.goto(`${url}/#/${hash}`);
  if (heading) await page.waitForSelector(`#content h1:has-text("${heading}")`);
};
const dialog = () => page.locator(".backdrop form");
const submit = () => dialog().locator("button[type=submit]").click();
const chat = () => page.frameLocator("iframe.chat");
/** Send a message in the chat and wait for the turn to end: the reply's actions appear then. */
const say = async (text) => {
  const before = await chat().locator(".msg.assistant .actions").count();
  await chat().locator("#input").fill(text);
  await chat().locator("#input").press("Enter");
  await chat().locator(`.msg.assistant .actions >> nth=${before}`).waitFor();
};

try {
  await step("a wrong key is refused, the right one signs in", async () => {
    await page.goto(url);
    await page.fill("#key", "hg_not_a_key");
    await page.click("#go");
    await page.waitForSelector("#msg:not([hidden])");
    await page.fill("#key", key);
    await page.click("#go");
    await page.waitForSelector("#who:has-text('admin')");
    await page.waitForSelector("#nav .item.on:has-text('Harnesses')");
  });

  await step("a harness is added from the catalog and installs", async () => {
    await go("harnesses", "Harnesses");
    if (!(await page.locator(`a.hcard[href="#/harnesses/shell"]`).count())) await page.click(".hcard.catalog [data-add=shell]");
    await page.waitForSelector(`a.hcard[href="#/harnesses/shell"]`);
    for (let tries = 0; tries < 60 && (await page.locator(`a.hcard[href="#/harnesses/shell"] .pill.warn`).count()); tries++) {
      await page.waitForTimeout(1000);
      await page.reload();
      await page.waitForSelector(`a.hcard[href="#/harnesses/shell"]`);
    }
    await page.click(`a.hcard[href="#/harnesses/shell"]`);
    await page.waitForSelector("#view:has-text('\"name\": \"shell\"')");          // the package's files, as installed
    await page.click(".tabs a:has-text('Installs')");
    await page.waitForSelector("#tab .pill.ok:has-text('ready')");
  });

  await step("a new harness starts from the template, is edited as a draft, and installs", async () => {
    const name = `browser-${stamp}`;
    await go("harnesses", "Harnesses");
    await page.click("#new");
    await dialog().locator("[name=name]").fill(name);
    await submit();
    await page.waitForSelector(`#content h1:has-text("${name}")`);
    await page.waitForSelector(`#tree .f.on:has-text("harness.json")`);
    if (!(await page.inputValue("#ed")).includes(`"name": "${name}"`)) throw new Error("the template was not renamed");
    await page.click("#add");
    await dialog().locator("[name=path]").fill("seeds/mind/NOTES.md");
    await submit();
    await page.waitForSelector(`#tree .f.on:has-text("NOTES.md")`);
    await page.fill("#ed", "# Standing notes\n");
    await page.click("#save");
    await page.waitForSelector(".toast.ok:has-text('Saved')");
    await page.click("#install");
    await page.waitForSelector("#content .tabs a:has-text('Installs')");          // the harness's page, not the draft's
    await page.waitForSelector(`#tree .f:has-text("NOTES.md")`);                  // the installed package holds the file
    await go("harnesses", "Harnesses");
    if (await page.locator(".hcard.draft").count()) throw new Error("the draft was left behind");
  });

  await step("a message runs in the chat, streams its answer, and what it made opens beside it", async () => {
    await go("chat");
    await chat().locator("#harness option[value=shell]").waitFor({ state: "attached" });
    await chat().locator("#harness").selectOption("shell");
    await say("printf '<h1>made in a sandbox</h1>' > page.html; echo wrote the page");
    await chat().locator(".msg.assistant .reply:has-text('wrote the page')").waitFor();
    await chat().locator(".msg.assistant .tile:has-text('page.html')").waitFor();
    await chat().locator("#panel:not([hidden]) #pbody iframe").waitFor();           // the page it made, running
    const shown = chat().frameLocator("#pbody iframe");
    await shown.locator("h1:has-text('made in a sandbox')").waitFor();
    if (!/^sess_/.test(await chat().locator("#session").innerText())) throw new Error("the chat does not say its session");
  });

  await step("the next message continues the session, and a branch starts a chat of its own", async () => {
    await say("cat page.html; echo; echo second > two.txt");
    await chat().locator(".msg.assistant >> nth=1").locator(".reply:has-text('made in a sandbox')").waitFor();
    await chat().locator("#filesbtn").click();                                       // close the panel
    await chat().locator(".msg.assistant >> nth=0").locator("[data-reply=branch]").click();
    await chat().locator(".chats .chat.active:has-text('(branch)')").waitFor();
    await say("ls");
    const listing = await chat().locator(".msg.assistant >> nth=1").locator(".reply").innerText();
    if (!listing.includes("page.html") || listing.includes("two.txt")) throw new Error(`the branch did not start from the first reply: ${listing}`);
  });

  let session;
  await step("the session's transcript, each turn's trace and its files are shown", async () => {
    await go("sessions", "Sessions");
    await page.waitForSelector("#sess table tbody tr");
    await page.click("#sess table tbody tr >> nth=-1 >> a.btn2");                    // the oldest: the first chat
    await page.waitForSelector("#turns .turn >> nth=1");
    session = (await page.locator("#shead .v.mono").first().innerText()).trim();
    await page.waitForSelector("#turns .turn .reply:has-text('wrote the page')");
    await page.click("#turns .turn >> nth=0 >> [data-raw]");
    await page.waitForSelector("#turns .turn >> nth=0 >> .raw:has-text('Sandbox command')");
    await page.frameLocator(".spanel iframe").locator("#plist .row:has-text('two.txt')").waitFor();
    await page.frameLocator(".spanel iframe").locator("#plist .row:has-text('page.html')").click();
    await page.frameLocator(".spanel iframe").frameLocator("#pbody iframe").locator("h1:has-text('made in a sandbox')").waitFor();
  });

  await step("only a session of the harness package builder offers its work as a harness", async () => {
    await go(`sessions/${session}`);                                                 // a shell session
    await page.waitForSelector("#turns .turn");
    if (await page.locator("#mk").count()) throw new Error("a session of another harness offers New harness from workspace");
  });

  await step("a package the builder wrote in its session becomes a draft and installs", async () => {
    // The catalog here holds only the shell package, so the builder is stood in for by the template under
    // its name: the console goes by the harness's name.
    const builder = "harness-package-builder";
    await go("harnesses", "Harnesses");
    if (!(await page.locator(`a.hcard[href="#/harnesses/${builder}"]`).count())) {
      await page.click("#new");
      await dialog().locator("[name=name]").fill(builder);
      await submit();
      await page.waitForSelector(`#tree .f.on:has-text("harness.json")`);
      await page.click("#install");
      await page.waitForSelector("#content .tabs a:has-text('Installs')");
    }
    const name = `b${stamp}`;
    const manifest = JSON.stringify({ contract: 1, name, version: "0.1.0", description: "Written in a session.", apis: [] });
    await go("chat");
    await chat().locator("#new").click();
    await chat().locator(`#harness option[value=${builder}]`).waitFor({ state: "attached" });
    await chat().locator("#harness").selectOption(builder);
    await say(`mkdir -p ${name} && cd ${name} && echo '${manifest}' > harness.json && printf '#!/bin/sh\\n' > install && cp $HG_HARNESS/run run && chmod +x install run && echo packaged`);
    const made = (await chat().locator("#session").innerText()).trim();
    await go(`sessions/${made}`);
    await page.click("#mk");
    await dialog().locator("[name=folder]").fill(name);
    await submit();
    await page.waitForSelector(`#content h1:has-text("${name}")`);
    await page.waitForSelector(`#ed`);
    await page.waitForFunction((n) => document.querySelector("#ed").value.includes(n), name);
    await page.click("#install");
    await page.waitForSelector("#content .tabs a:has-text('Installs')");
    await go("chat");
    await chat().locator(`#harness option[value=${name}]`).waitFor({ state: "attached" });   // it is a model name now
  });

  await step("a provider is added, and its key is never shown", async () => {
    const name = `browser-${stamp}`;
    await go("providers", "Providers");
    await page.click("#add");
    await dialog().locator("[name=name]").fill(name);
    await dialog().locator("[name='api:anthropic']").fill("https://api.example.com/anthropic");
    await dialog().locator("[name=default_model]").fill("a-model");
    await dialog().locator("[name=api_key]").fill("the-secret-key");
    await submit();
    await page.waitForSelector(`#content tr:has-text("${name}") .pill.ok:has-text("set")`);
    if ((await page.content()).includes("the-secret-key")) throw new Error("the provider's key is on the page");
  });

  await step("an environment is added with a network policy and a secret", async () => {
    const name = `browser-${stamp}`;
    await go("environments", "Environments");
    await page.click("#add");
    await dialog().locator("[name=name]").fill(name);
    await dialog().locator("[name=memory_mb]").fill("1024");
    await dialog().locator("[name=allow]").fill("api.github.com\n*.example.com");
    await dialog().locator("[name=variables]").fill("STAGE=test");
    await dialog().locator("#secret").click();
    await dialog().locator(".secret-edit [data-s=name]").fill("GITHUB_TOKEN");
    await dialog().locator(".secret-edit [data-s=hosts]").fill("api.github.com");
    await dialog().locator(".secret-edit [data-s=value]").fill("the-secret-token");
    await submit();
    await page.waitForSelector(`#content tr:has-text("${name}"):has-text("2 allowed"):has-text("1 secret")`);
    await page.click(`#content tr:has-text("${name}") [data-edit]`);                  // it opens again with what was saved
    if ((await dialog().locator("[name=allow]").inputValue()) !== "api.github.com\n*.example.com") throw new Error("the allowed hosts were not kept");
    if (await dialog().locator(".secret-edit [data-s=value]").inputValue()) throw new Error("a secret's value came back");
    await dialog().locator("[data-cancel]").click();
  });

  await step("a key is made, shown once, and listed", async () => {
    await go("keys", "API keys");
    await page.click("#add");
    await dialog().locator("[name=name]").fill(`browser-${stamp}`);
    await dialog().locator("[name=role]").selectOption("runner");
    await submit();
    const shown = await page.locator(".backdrop .secret").innerText();
    if (!shown.startsWith("hg_")) throw new Error(`the new key was not shown: ${shown}`);
    await submit();
    await page.waitForSelector(`#content tr:has-text("browser-${stamp}") .pill:has-text("runner")`);
    if ((await page.content()).includes(shown)) throw new Error("the key is still on the page");
  });

  await step("the playground sends one request and shows the response", async () => {
    await go("playground", "Playground");
    await page.selectOption("#route [name=harness]", "shell");
    await page.fill("#req", JSON.stringify({ model: "shell", input: "echo from the playground", stream: true }));
    await page.click("#send");
    await page.waitForSelector("#res:has-text('from the playground')");
    await page.waitForSelector("#st:has-text('completed')");
  });

  await step("usage and the audit log show what happened", async () => {
    await go("usage", "Usage");
    await page.waitForSelector("#out tr:has-text('shell')");
    await go("audit", "Audit log");
    await page.waitForSelector("#content tr:has-text('harness.add')");
    await page.waitForSelector("#content tr:has-text('key.create')");
  });

  await step("signing out asks for a key again", async () => {
    await page.click("#who");
    await page.click(".menu [data-out]");
    await page.waitForSelector("#signin:not([hidden])");
    await page.reload();
    await page.waitForSelector("#signin:not([hidden])");
  });

  if (errors.length) throw new Error(`the page raised errors: ${errors.join("; ")}`);
} catch (error) {
  console.error(`FAILED: ${error.message}`);
  if (process.env.HG_TEST_SCREENSHOT) await page.screenshot({ path: process.env.HG_TEST_SCREENSHOT, fullPage: true }).catch(() => {});
  process.exitCode = 1;
} finally {
  await browser.close();
}
