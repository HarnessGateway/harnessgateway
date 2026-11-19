// The console, driven in a real browser against a running platform (spec §20).
//
//   HG_TEST_URL=http://localhost:8000 HG_TEST_KEY=hg_...(an admin key) node tests/browser/console.mjs
//
// tests/browser/test_console.py runs it against a platform it starts itself. It needs Playwright with its
// Chromium, and a platform whose catalog holds the shell package. Against a platform that stays up it leaves
// behind sessions, harnesses, two environments, a provider and a key, all named "browser-…" or "b…", and the
// default environment carrying the harnesses it made.

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
/** Have the default environment (or another) carry a harness, over the API. */
const carry = async (harness, environment = "default") => {
  const at = `${url}/v1/environments/${environment}`, headers = { Authorization: `Bearer ${key}`, "Content-Type": "application/json" };
  const { harnesses } = await (await fetch(at, { headers })).json();
  const done = await fetch(at, { method: "PATCH", headers, body: JSON.stringify({ harnesses: [...new Set([...harnesses, harness])] }) });
  if (!done.ok) throw new Error(`${environment} could not carry ${harness}: ${await done.text()}`);
};
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
    await page.waitForSelector("#nav .item.on:has-text('Providers')");     // Setup's first page is where one lands
    const sidebar = (await page.locator("#nav .mod, #nav .item").allTextContents()).map((text) => text.trim());
    const setup = sidebar.slice(sidebar.indexOf("Setup") + 1, sidebar.indexOf("Workbench"));
    if (setup.join(", ") !== "Providers, Environments, Harnesses") throw new Error(`Setup lists ${setup.join(", ")}`);
  });

  const known = await (await fetch(`${url}/v1/environments`, { headers: { Authorization: `Bearer ${key}` } })).json();
  if (known.data.some((e) => e.harnesses.length)) console.log("skip  with no harness carried, the chat says where to tick one (this platform has some)");
  else await step("with no harness carried by an environment, the chat says where to tick one", async () => {
    await go("chat");
    await chat().locator(".hello:has-text('No environment carries a harness yet')").waitFor();
    if (await chat().locator("#route").isVisible()) throw new Error("the chat offers a picker with nothing to pick");
    await chat().locator("#input").fill("hi");
    await chat().locator("#input").press("Enter");
    await page.waitForTimeout(500);
    if (await chat().locator(".msg").count()) throw new Error("a message was sent with no harness to run it");
    await chat().locator(".hello a").click();
    await page.waitForSelector("#content h1:has-text('Environments')");
  });

  await step("a harness is added from the catalog, and an environment that carries it installs it", async () => {
    await go("harnesses", "Harnesses");
    if (!(await page.locator(`a.hcard[href="#/harnesses/shell"]`).count())) await page.click(".hcard.catalog [data-add=shell]");
    await page.waitForSelector(`a.hcard[href="#/harnesses/shell"]`);
    await page.click(`a.hcard[href="#/harnesses/shell"]`);
    await page.waitForSelector("#view:has-text('\"name\": \"shell\"')");          // the package's files, as added
    await go("environments", "Environments");
    await page.click(`#content tr:has-text("default") [data-edit]`);
    const shell = dialog().locator(".group:has-text('Harnesses') [name=harness][value=shell]");
    if (!(await shell.isChecked())) await shell.check();
    await submit();
    await page.waitForSelector("#content h1:has-text('default')");                  // the environment's own page
    await page.waitForSelector(`#content tr:has-text("shell") .pill.ok:has-text("ready")`);
    await go("harnesses/shell/environments");
    await page.waitForSelector(`#tab tr:has-text("default") .pill.ok:has-text("ready")`);
    if (!(await page.locator("#tab:has-text('default/shell/none/none')").count())) throw new Error("the harness does not show its model string");
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
    await page.waitForSelector("#content .tabs a:has-text('Environments')");      // the harness's page, not the draft's
    await page.waitForSelector(`#tree .f:has-text("NOTES.md")`);                  // the installed package holds the file
    await go("harnesses", "Harnesses");
    if (await page.locator(".hcard.draft").count()) throw new Error("the draft was left behind");
  });

  await step("a message runs in the chat, streams its answer, and what it made opens beside it", async () => {
    await go("chat");
    await chat().locator("#harness option[value=shell]").waitFor({ state: "attached" });
    await chat().locator("#harness").selectOption("shell");
    if (!(await chat().locator("#environment").isVisible())) throw new Error("the chat does not show the environment");
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
      await page.waitForSelector("#content .tabs a:has-text('Environments')");
    }
    await carry(builder);
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
    await page.waitForSelector("#content .tabs a:has-text('Environments')");
    await page.waitForSelector(".note:has-text('No environment carries this harness')");     // added, and carried by none yet
    await carry(name);
    await go("chat");
    await chat().locator(`#harness option[value=${name}]`).waitFor({ state: "attached" });   // a request may name it now
  });

  await step("a provider is a name, an API shape, a base URL, its models and a key, and the key is never shown", async () => {
    const name = `browser-${stamp}`;
    const baseUrl = () => dialog().locator("[name=base_url]").inputValue();
    await go("providers", "Providers");
    await page.click("#add");
    const asked = await dialog().locator("input, select").evaluateAll((fields) => fields.map((f) => f.name).join(", "));
    if (asked !== "name, shape, base_url, api_key") throw new Error(`the provider form asks for: ${asked}`);
    const shapes = await dialog().locator("[name=shape] option").evaluateAll((options) => options.map((o) => o.value).join(", "));
    if (shapes !== "anthropic, openai-responses, openai-chat, gemini") throw new Error(`the API shapes offered are: ${shapes}`);
    if ((await baseUrl()) !== "https://api.anthropic.com") throw new Error(`anthropic's base URL is not filled in: ${await baseUrl()}`);
    await dialog().locator("[name=shape]").selectOption("openai-chat");
    if ((await baseUrl()) !== "https://api.openai.com/v1") throw new Error(`the base URL did not follow the shape: ${await baseUrl()}`);
    await dialog().locator("[name=name]").fill(name);
    await dialog().locator("[name=base_url]").fill("https://api.example.com/v1");
    await dialog().locator("[name=shape]").selectOption("anthropic");              // a URL that was typed stays
    await dialog().locator("[name=shape]").selectOption("openai-chat");
    if ((await baseUrl()) !== "https://api.example.com/v1") throw new Error("a typed base URL was replaced");
    await dialog().locator("[name=models]").fill("model-a\nmodel-b");
    await dialog().locator("[name=api_key]").fill("the-secret-key");
    await submit();
    await page.waitForSelector(`#content tr:has-text("${name}"):has-text("openai-chat"):has-text("https://api.example.com/v1") .pill.ok:has-text("set")`);
    if ((await page.content()).includes("the-secret-key")) throw new Error("the provider's key is on the page");

    const stored = await (await fetch(`${url}/v1/providers/${name}`, { headers: { Authorization: `Bearer ${key}` } })).json();
    const apis = Object.entries(stored.apis).map(([api, at]) => `${api}=${at}`).join(" ");          // one shape is one API
    if (apis !== "openai-chat=https://api.example.com/v1" || stored.models.join(" ") !== "model-a model-b") {
      throw new Error(`the provider was stored as ${JSON.stringify(stored)}`);
    }
    await page.click(`#content tr:has-text("${name}") [data-edit]`);                // it opens again as it was saved
    if ((await dialog().locator("[name=shape]").inputValue()) !== "openai-chat" || (await baseUrl()) !== "https://api.example.com/v1" ||
        (await dialog().locator("[name=models]").inputValue()) !== "model-a\nmodel-b") {
      throw new Error("the provider did not open as it was saved");
    }
    await dialog().locator("[data-cancel]").click();
  });

  await step("an environment is set up by its script, carries the harnesses ticked, and its network is closed, limited or open", async () => {
    const name = `browser-${stamp}`;
    const mode = () => dialog().locator("[name=mode]"), rules = () => dialog().locator("#limited");
    await go("environments", "Environments");
    await page.click("#add");
    const groups = await dialog().locator(".group").evaluateAll((all) => all.map((group) =>
      `${group.querySelector("h3").textContent}: ${[...new Set([...group.querySelectorAll("[name]")].map((f) => f.name))].join(" ")}`));
    const wanted = ["General: name image", "Setup: script setup_egress setup_timeout_s", "Harnesses: harness",
                    "Resources: cpus memory_mb pids disk_mb tmp_mb turn_timeout_s",
                    "Network access: mode allow package_managers deny", "Variables and secrets: variables"];
    if (groups.join(" | ") !== wanted.join(" | ")) throw new Error(`the environment form is grouped as: ${groups.join(" | ")}`);
    if (!(await dialog().locator(".group:has-text('Variables and secrets') #secret").count())) throw new Error("secrets are not with the variables");
    const modes = await mode().locator("option").evaluateAll((options) => options.map((o) => o.value).join(", "));
    if (modes !== "closed, limited, open") throw new Error(`the network modes offered are: ${modes}`);
    if ((await mode().inputValue()) !== "limited" || !(await rules().isVisible())) throw new Error("a new environment does not start limited, with its rules shown");
    if (!(await dialog().locator("[name=package_managers]").isChecked())) throw new Error("a new environment does not allow package managers");
    for (const [other, denies] of [["closed", false], ["open", true]]) {     // open may still refuse named hosts
      await mode().selectOption(other);
      if (await rules().isVisible()) throw new Error(`a ${other} network is offered an allow list`);
      if ((await dialog().locator("[name=deny]").isVisible()) !== denies) throw new Error(`a ${other} network ${denies ? "is not" : "is"} offered a deny list`);
    }
    await mode().selectOption("limited");
    await dialog().locator("[name=name]").fill(name);
    await dialog().locator("[name=memory_mb]").fill("1024");
    await dialog().locator("[name=allow]").fill("api.github.com\n*.example.com");
    await dialog().locator("[name=deny]").fill("bad.example.com");
    await dialog().locator("[name=variables]").fill("STAGE=test");
    await dialog().locator("#secret").click();
    await dialog().locator(".secret-edit [data-s=name]").fill("GITHUB_TOKEN");
    await dialog().locator(".secret-edit [data-s=hosts]").fill("api.github.com");
    await dialog().locator(".secret-edit [data-s=value]").fill("the-secret-token");
    await dialog().locator("[name=script]").fill('echo "setting up with $GITHUB_TOKEN"');
    await dialog().locator("[name=harness][value=shell]").check();
    await submit();
    await page.waitForSelector(`#content h1:has-text("${name}")`);                 // its own page: the build, then the installs
    await page.waitForSelector(`#content .pill.ok:has-text("set up")`);
    await page.waitForSelector(`#content tr:has-text("shell") .pill.ok:has-text("ready")`);
    await page.click("#content summary:has-text('log') >> nth=0");
    const log = await page.locator("#content details pre >> nth=0").innerText();
    if (!log.includes("setting up with hgs_") || log.includes("the-secret-token")) throw new Error(`the setup saw the secret, or did not run: ${log}`);
    await go("environments", "Environments");
    await page.waitForSelector(`#content tr:has-text("${name}"):has-text("2 allowed + package managers"):has-text("1 denied"):has-text("1 secret"):has-text("shell")`);
    if (!(await page.locator("#content th:has-text('Variables and secrets')").count())) throw new Error("the table does not name its variables and secrets column");
    await page.click(`#content tr:has-text("${name}") [data-edit]`);                  // it opens again with what was saved
    if ((await dialog().locator("[name=allow]").inputValue()) !== "api.github.com\n*.example.com") throw new Error("the allowed hosts were not kept");
    if (await dialog().locator(".secret-edit [data-s=value]").inputValue()) throw new Error("a secret's value came back");
    await dialog().locator("[data-cancel]").click();

    await page.waitForSelector(`#content tr:has-text("default"):has-text("the platform's sandbox image")`);
    await page.click("#add");                                                         // and one that reaches its provider only
    await dialog().locator("[name=name]").fill(`closed-${stamp}`);
    await mode().selectOption("closed");
    await submit();
    await page.waitForSelector(`#content h1:has-text("closed-${stamp}")`);
    await go("environments", "Environments");
    await page.waitForSelector(`#content tr:has-text("closed-${stamp}") .pill:has-text("closed")`);
    const stored = await (await fetch(`${url}/v1/environments/closed-${stamp}`, { headers: { Authorization: `Bearer ${key}` } })).json();
    if (stored.network.mode !== "closed" || stored.network.allow.length || stored.network.deny.length) {
      throw new Error(`the closed environment was stored as ${JSON.stringify(stored.network)}`);
    }
  });

  await step("the chat names the environment, the harness, the provider and the model of each message", async () => {
    const harness = `p${stamp}`, named = `browser-${stamp}`;                         // the environment and the provider made above
    await go("harnesses", "Harnesses");
    await page.click("#new");                                                        // the template, as a harness that calls a model
    await dialog().locator("[name=name]").fill(harness);
    await submit();
    await page.waitForSelector(`#tree .f.on:has-text("harness.json")`);
    await page.waitForFunction((n) => document.querySelector("#ed").value.includes(n), harness);
    await page.fill("#ed", JSON.stringify({ ...JSON.parse(await page.inputValue("#ed")), apis: ["openai-chat"] }, null, 2));
    await page.click("#save");
    await page.waitForSelector(".toast.ok:has-text('Saved')");
    await page.click("#install");
    await page.waitForSelector("#content .tabs a:has-text('Environments')");
    await carry(harness);
    await carry(harness, named);

    await go("chat");
    await chat().locator("#new").click();
    await chat().locator("#environment").selectOption(named);
    await chat().locator(`#harness option[value=${harness}]`).waitFor({ state: "attached" });   // what it carries
    await chat().locator("#harness").selectOption(harness);
    await chat().locator("#provider").selectOption(named);
    const offered = await chat().locator("#models option").evaluateAll((options) => options.map((o) => o.value).join(" "));
    if (offered !== "model-a model-b" || (await chat().locator("#model").inputValue()) !== "model-a") {
      throw new Error(`the chat offers the provider's models as: ${offered}`);
    }
    const ran = 'echo "ran in $HG_ENVIRONMENT on $HG_PROVIDER over $HG_PROVIDER_API with $HG_MODEL"';
    await say(ran);
    await chat().locator(`.msg.assistant .reply:has-text("ran in ${named} on ${named} over openai-chat with model-a")`).waitFor();
    await chat().locator("#model").fill("model-b");                                  // the next message, another model
    await chat().locator("#environment").selectOption("default");
    await say(ran);
    await chat().locator(`.msg.assistant .reply:has-text("ran in default on ${named} over openai-chat with model-b")`).waitFor();
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
    await page.selectOption("#route [name=environment]", "default");
    await page.selectOption("#route [name=harness]", "shell");
    if (JSON.parse(await page.inputValue("#req")).model !== "default/shell/none/none") throw new Error("the playground did not write the model string");
    await page.fill("#req", JSON.stringify({ model: "default/shell/none/none", input: "echo from the playground", stream: true }));
    await page.click("#send");
    await page.waitForSelector("#res:has-text('from the playground')");
    await page.waitForSelector("#st:has-text('completed')");
  });

  await step("usage and the audit log show what happened", async () => {
    await go("usage", "Usage");
    await page.waitForSelector("#out tr:has-text('shell')");
    const meters = await page.locator("#out th").evaluateAll((heads) => heads.slice(1).map((h) => h.textContent).join(", "));
    const wanted = "Turns, Tokens in, Tokens out, Sandbox time, CPU time, Peak memory, Network out, Network in, Storage";
    if (meters !== wanted) throw new Error(`usage is shown as: ${meters}`);
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
