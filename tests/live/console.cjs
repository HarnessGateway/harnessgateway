// Every page of the console, in a real browser, against a running installation.
//
//   HG_URL=http://127.0.0.1:8000 HG_KEY=hg_… node tests/live/console.cjs
//
// It needs Playwright (`npm install playwright`, or NODE_PATH to one) and an image the host can pull with python3
// in it (HG_IMAGE, default python:3.13-slim). Everything it makes is named console-check-… and removed at the end.
const { chromium } = require("playwright");
const fs = require("fs");

const URL = (process.env.HG_URL || "http://127.0.0.1:8000").replace(/\/$/, ""), KEY = process.env.HG_KEY || "";
const IMAGE = process.env.HG_IMAGE || "python:3.13-slim", SHOTS = process.env.HG_SHOTS || "";
const id = Date.now().toString(36), PROVIDER = `console-check-p${id}`, HARNESS = `console-check-h${id}`, ENVIRONMENT = `console-check-e${id}`;
const LINE = `${ENVIRONMENT}/${HARNESS}/none/none`;
const results = [], made = { sessions: new Set() };

const api = async (method, path, body) => {
  const response = await fetch(URL + path, { method, body: body && JSON.stringify(body), headers: { Authorization: `Bearer ${KEY}` } });
  return response.status === 204 ? null : response.json();
};

(async () => {
  if (!KEY) throw new Error("set HG_KEY to an API key of the installation");
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error" && !/401/.test(message.text())) errors.push(message.text()); });
  page.setDefaultTimeout(15000);

  const check = async (name, run) => {
    try { await run(); results.push([true, name]); console.log(`  ok    ${name}`); }
    catch (error) { results.push([false, name]); console.log(`  FAIL  ${name}\n        ${String(error.message).split("\n")[0]}`); }
  };
  const expect = (truth, why) => { if (!truth) throw new Error(why); };
  const go = async hash => { await page.evaluate(to => { location.hash = to; }, hash); await page.waitForTimeout(250); };
  // A toast is taken away once it is seen, so the next wait is never answered by an earlier one.
  const toast = async text => { await page.waitForSelector(`.toast:has-text("${text}")`); await page.evaluate(() => document.getElementById("toasts").replaceChildren()); };
  const shot = async name => { if (SHOTS) { await page.waitForTimeout(350); await page.screenshot({ path: `${SHOTS}/${name}.png` }); } };
  const tab = async label => { await page.click(`.tabs a:has-text("${label}")`); await page.waitForTimeout(200); };
  const labelled = label => `div:has(> label:text-is("${label}")) > input`;
  const picked = async action => { const [chooser] = await Promise.all([page.waitForEvent("filechooser"), action()]); return chooser; };
  const say = async words => {
    await page.fill(".composer textarea", words);
    await page.press(".composer textarea", "Enter");
    await page.waitForSelector(`.said:has-text(${JSON.stringify(words.slice(0, 30))})`);
    await page.waitForSelector("button[title=Send]:visible", { timeout: 180000 });
    return page.locator(".reply").last();
  };

  await page.goto(URL + "/");                    // the root leads to the console
  await page.waitForURL(URL + "/console/");

  await check("a wrong key is refused, in words", async () => {
    await page.fill("#key", "hg_not-a-key");
    await page.click("#signin button");
    await page.waitForSelector("#signin-error:has-text('not valid')");
    expect(await page.isHidden("#shell"), "the console opened without a key");
  });
  await check("a key signs in, and is kept in the tab only", async () => {
    await shot("00-sign-in");
    await page.fill("#key", KEY);
    await page.click("#signin button");
    await page.waitForSelector("#shell:not([hidden])");
    expect(await page.evaluate(() => sessionStorage.getItem("hg.key") && !localStorage.getItem("hg.key")), "the key is not in sessionStorage alone");
    expect(/^v\d/.test(await page.textContent("#about")), "the platform's version is not shown");
  });

  // providers
  await check("a provider is added: name, shape and URL, key", async () => {
    await go("#/providers");
    await page.click(".top button:has-text('New provider')");
    await page.fill(".dialog [name=name]", PROVIDER);
    await page.selectOption(".dialog [data-shape]", "anthropic");
    expect(await page.inputValue(".dialog [data-url]") === "https://api.anthropic.com", "picking a shape did not offer its usual address");
    await page.fill(".dialog [data-url]", "https://api.example.com");
    await page.fill(".dialog [name=key]", "sk-console-check");
    await shot("02-provider-new");
    await page.click(".dialog button.primary");
    await page.waitForSelector(`.card:has-text("${PROVIDER}") .pill:has-text("key set")`);
    const stored = await api("GET", `/v1/providers/${PROVIDER}`);
    expect(stored.apis.anthropic === "https://api.example.com" && stored.secrets_set.includes("key"), "it was not stored as typed");
    expect(!JSON.stringify(stored).includes("sk-console-check"), "the API returned the key");
  });
  await check("a provider is edited: a second shape with the same key, and the key is kept", async () => {
    await page.click(`.card:has-text("${PROVIDER}") button:has-text("Edit")`);
    expect(await page.getAttribute(".dialog [name=name]", "readonly") !== null, "an existing provider's name can be typed over");
    await page.click(".dialog button.link");
    expect(await page.locator(".dialog [data-shape]").count() === 2, "a second shape was not added");
    await page.click(".dialog button.primary");
    await page.waitForSelector(`.card:has-text("${PROVIDER}") .line >> nth=1`);
    const stored = await api("GET", `/v1/providers/${PROVIDER}`);
    expect(Object.keys(stored.apis).length === 2 && stored.secrets_set.includes("key"), "the second shape or the key was lost");
    await shot("01-providers");
  });

  // harnesses
  await check("a new harness starts as a working package, in the editor", async () => {
    await go("#/harnesses");
    await page.click(".top button:has-text('New harness')");
    await page.fill(".dialog [name=name]", HARNESS);
    await page.click(".dialog button.primary");
    await page.waitForSelector(".tree a[title='run']");
    expect(await page.locator(".tree a").count() === 3, "the template is not harness.json, install and run");
    expect((await page.inputValue(".code textarea")).includes(`"name": "${HARNESS}"`), "harness.json does not name the harness");
    expect((await api("GET", "/v1/harnesses")).data.every(each => each.name !== HARNESS), "it exists before it was created");
  });
  await check("its files are edited, and the working copy is kept between tabs", async () => {
    const manifest = JSON.parse(await page.inputValue(".code textarea"));
    manifest.description = "Made by the console's own check.";
    manifest.settings = { GREETING: { description: "What it says first.", default: "hi" } };
    await page.fill(".code textarea", JSON.stringify(manifest, null, 2) + "\n");
    await tab("Settings");
    await page.waitForSelector(labelled("GREETING"));
    await tab("Files");
    expect((await page.inputValue(".code textarea")).includes("Made by the console"), "the edit was lost on the way to another tab");
    await page.click(".tree a[title='run']");
    expect(await page.isChecked(".pane .check input"), "run is not marked executable");
    await shot("07-harness-new");
  });
  await check("it is created, and the platform holds the package", async () => {
    await page.click(".head button.primary");
    await toast(`Created ${HARNESS}`);
    await page.waitForSelector(`h1:has-text("${HARNESS}")`);
    const stored = await api("GET", `/v1/harnesses/${HARNESS}`);
    expect(stored.manifest.description === "Made by the console's own check." && "GREETING" in stored.manifest.settings, "the manifest stored is not the one edited");
    expect(await page.isDisabled(".head button.primary"), "Save is offered with nothing to save");
  });
  await check("a file is added and edited, and saving replaces the package", async () => {
    await page.click(".tree button:has-text('File')");
    await page.fill(".dialog [name=answer]", "docs/NOTES.md");
    await page.click(".dialog button.primary");
    await page.fill(".code textarea", "# Notes\n");
    await page.waitForSelector(".head .pill:has-text('Unsaved')");
    await page.keyboard.press("Control+s");
    await toast("Saved");
    await page.waitForSelector(".tree a[title='docs/NOTES.md']");
    expect(await page.locator(".tree a").count() === 4, "the package does not hold four files");
  });
  await check("a setting is set on the harness", async () => {
    await tab("Settings");
    await page.fill(labelled("GREETING"), "hello there");
    await page.click(".head button.primary");
    await toast("Saved");
    expect((await api("GET", `/v1/harnesses/${HARNESS}`)).settings.GREETING === "hello there", "the setting was not stored");
  });
  let archive = "";
  await check("a harness is exported as its package archive, and imported again", async () => {
    const [download] = await Promise.all([page.waitForEvent("download"), page.click(".head button:has-text('Export')")]);
    expect(download.suggestedFilename() === `${HARNESS}-0.1.tar.gz`, `the file is called ${download.suggestedFilename()}`);
    archive = await download.path();
    expect(fs.readFileSync(archive).subarray(0, 2).equals(Buffer.from([0x1f, 0x8b])), "the export is not gzip");
    await go("#/harnesses");
    const chooser = await picked(() => page.click(".top button:has-text('Import')"));
    await chooser.setFiles({ name: `${HARNESS}.tar.gz`, mimeType: "application/gzip", buffer: fs.readFileSync(archive) });
    await toast(`Saving replaces ${HARNESS}`);
    await page.waitForSelector(".tree a[title='docs/NOTES.md']");
    await page.click(".head button:has-text('Discard')");
    await page.waitForSelector(".head .pill:has-text('Unsaved')", { state: "hidden" });
  });

  // environments
  await check("a new environment is filled in across its tabs before it exists", async () => {
    await go("#/environments");
    await page.click(".top button:has-text('New environment')");
    await page.fill("input[placeholder='dev']", ENVIRONMENT);
    await page.fill("input[placeholder='node:22']", IMAGE);
    await tab("Resources");
    await page.fill(labelled("Turn time limit (s)"), "120");
    await page.fill(labelled("Memory (MB)"), "512");
    await tab("Network");
    await page.fill("textarea >> nth=0", "example.com\n*.example.org");
    await tab("Variables");
    await page.click("button:has-text('Add variable')");
    await page.fill(".rows .row input >> nth=0", "TEAM");
    await page.fill(".rows .row input >> nth=1", "console");
    await tab("Secrets");
    await page.click("button:has-text('Add secret')");
    for (const [at, value] of ["CHECK_TOKEN", "example.com", "Authorization", "Bearer ", "s3cret-value"].entries()) await page.fill(`.rows .row input >> nth=${at}`, value);
    await shot("05-environment-secrets");
    await tab("Scripts");
    await page.fill(".code textarea", "#!/bin/sh\necho started > /work/.started\n");
    await shot("06-environment-scripts");
    await tab("Harnesses");
    await page.check(`label.option:has-text("${HARNESS}") input`);
    await tab("General");
    expect(await page.inputValue("input[placeholder='dev']") === ENVIRONMENT, "the name was lost between tabs");
    expect((await api("GET", "/v1/environments")).data.every(each => each.name !== ENVIRONMENT), "it exists before it was created");
    await shot("04-environment-new");
  });
  await check("it is created as it was filled in", async () => {
    await page.click(".head button.primary");
    await toast(`Created ${ENVIRONMENT}`);
    const stored = await api("GET", `/v1/environments/${ENVIRONMENT}`);
    expect(stored.image === IMAGE && stored.timeout_s === 120 && stored.memory_mb === 512, "its image or limits are not what was typed");
    expect(stored.network.mode === "limited" && stored.network.allow.join() === "example.com,*.example.org", "its allowed hosts are not what was typed");
    expect(stored.variables.TEAM === "console" && stored.scripts.startup.includes(".started"), "its variable or its script is missing");
    expect(stored.secrets.CHECK_TOKEN.header === "Authorization" && stored.secrets_set.includes("CHECK_TOKEN"), "its secret is missing");
    expect(stored.harnesses.join() === HARNESS && !JSON.stringify(stored).includes("s3cret-value"), "its harness is missing, or the API returned a secret");
  });
  await check("an edit to an environment keeps the secret it does not retype", async () => {
    await tab("Network");
    await page.click("label.option:has-text('Open')");
    expect(await page.isHidden("text=Allowed hosts"), "allowed hosts are offered for an open network");
    await page.click("label.option:has-text('Limited')");
    await tab("Resources");
    await page.fill(labelled("Turn time limit (s)"), "180");
    await page.waitForSelector(".head .pill:has-text('Unsaved')");
    await page.click(".head button.primary");
    await toast("Saved");
    const stored = await api("GET", `/v1/environments/${ENVIRONMENT}`);
    expect(stored.timeout_s === 180 && stored.secrets_set.includes("CHECK_TOKEN") && stored.network.allow.length === 2, "the edit lost something");
  });
  await check("an environment is exported without its secrets' values, and imported as a draft", async () => {
    const [download] = await Promise.all([page.waitForEvent("download"), page.click(".head button:has-text('Export')")]);
    const exported = JSON.parse(fs.readFileSync(await download.path(), "utf8"));
    expect(exported.name === ENVIRONMENT && exported.secrets.CHECK_TOKEN.hosts[0] === "example.com" && !("value" in exported.secrets.CHECK_TOKEN), "the export is wrong");
    await go("#/environments");
    await shot("03-environments");
    const chooser = await picked(() => page.click(".top button:has-text('Import')"));
    await chooser.setFiles({ name: "copy.environment.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify({ ...exported, name: `${ENVIRONMENT}-copy` })) });
    await toast("Imported");
    expect(await page.inputValue("input[placeholder='dev']") === `${ENVIRONMENT}-copy`, "the imported name is not shown");
    await tab("Secrets");
    expect(await page.inputValue(".rows .row input >> nth=0") === "CHECK_TOKEN", "the imported secret is not shown");
    await page.click(".head button:has-text('Cancel')");
    await page.waitForSelector(".cards");
    expect((await api("GET", "/v1/environments")).data.every(each => each.name !== `${ENVIRONMENT}-copy`), "cancelling an import created it");
  });
  await check("the harness shows the environment that carries it", async () => {
    await go(`#/harnesses/${HARNESS}/environments`);
    expect(await page.isChecked(`label.option:has-text("${ENVIRONMENT}") input`), "the environment is not ticked");
    await go("#/harnesses");
    await page.waitForSelector(`.card:has-text("${HARNESS}") .pill:has-text("${ENVIRONMENT}")`);
    await shot("08-harnesses");
  });

  // chat
  await check("the chat offers what can run, and a harness that calls no model needs no provider", async () => {
    await go("#/chat");
    await page.selectOption("select[title=Environment]", ENVIRONMENT);
    expect(await page.inputValue("select[title=Harness]") === HARNESS, "the environment's harness is not offered");
    expect(await page.isHidden("select[title=Provider]") && await page.isHidden("input[title=Model]"), "a provider is asked for by a harness that calls none");
  });
  await check("a harness no provider can serve says which provider to add, and is not what a new chat starts on", async () => {
    await page.route("**/v1/providers?*", route => route.fulfill({ json: { object: "list", data: [], has_more: false } }));
    await go("#/sessions"); await go("#/chat");
    const speaking = (await api("GET", "/v1/harnesses")).data.find(each => each.manifest.apis.length);
    const carrying = speaking && (await api("GET", "/v1/environments")).data.find(each => each.harnesses.includes(speaking.name) && each.harnesses.length > 1);
    if (carrying) {                                // the installation has a harness that calls a model
      await page.selectOption("select[title=Environment]", carrying.name);
      expect(await page.inputValue("select[title=Harness]") !== speaking.name, "a new chat starts on a harness that cannot run yet");
      await page.selectOption("select[title=Harness]", speaking.name);
      await page.waitForSelector(".line-bar .needs:visible");
      expect((await page.textContent(".line-bar .needs")).includes(speaking.manifest.apis[0]), "it does not say which API a provider must speak");
      await page.fill(".composer textarea", "hello"); await page.press(".composer textarea", "Enter");
      await toast("Add a provider");
      await shot("09-chat-needs-provider");
    }
    await page.unroute("**/v1/providers?*");
    await go("#/sessions"); await go("#/chat");
    await page.selectOption("select[title=Environment]", ENVIRONMENT);
  });
  await check("a message runs in a sandbox and its reply arrives", async () => {
    const reply = await say("echo hello from the console > note.txt; cat note.txt; cat .started");
    const text = await reply.locator(".text").textContent();
    expect(text.includes("hello from the console") && text.includes("started"), `the reply is "${text.slice(0, 80)}"`);
    expect(await reply.locator(".who").textContent() === LINE, "the reply does not name the model line it ran on");
    expect(await reply.locator(".state").count() === 0, "a completed reply still shows a state");
    await page.waitForSelector(".made .file:has-text('note.txt')");
    const chats = await page.evaluate(() => JSON.parse(localStorage.getItem("hg.chats")));
    expect(chats[0].session && chats[0].exchanges[0].response, "the chat did not keep its session and its response");
    made.sessions.add(chats[0].session);
  });
  await check("the next message continues the same session", async () => {
    const reply = await say("cat note.txt && echo second >> note.txt && wc -l < note.txt");
    expect((await reply.locator(".text").textContent()).includes("hello from the console"), "the file of the first turn is not there");
    const chats = await page.evaluate(() => JSON.parse(localStorage.getItem("hg.chats")));
    expect(chats[0].exchanges.length === 2 && (await api("GET", `/v1/sessions/${chats[0].session}`)).turns === 2, "the second message is not the session's second turn");
    await shot("09-chat");
  });
  await check("what an agent prints is text, never markup", async () => {
    const reply = await say("echo '<b id=planted>bold</b><img src=x onerror=window.planted=1>'");
    expect((await reply.locator(".text").textContent()).includes("<b id=planted>"), "the reply does not show the text");
    expect(await page.locator("#planted").count() === 0 && !(await page.evaluate(() => window.planted)), "output became markup");
  });
  await check("a failed turn says why", async () => {
    const reply = await say("exit 3");
    await reply.locator(".state:has-text('failed')").waitFor();
    expect((await reply.locator(".state").textContent()).includes("code 3"), "the failure does not say why");
  });
  await check("a chat is read again from the API when the page is opened again", async () => {
    await page.reload();
    await page.waitForSelector(".said >> nth=3");
    await page.waitForSelector(".reply >> nth=3");
    expect((await page.locator(".reply .text").first().textContent()).includes("hello from the console"), "the first reply is not shown again");
    expect(await page.locator(".list a.on").count() === 1, "the open chat is not marked in the list");
    expect(await page.inputValue("select[title=Environment]") === ENVIRONMENT, "the chat's model line is not picked again");
  });
  let direct = "";
  await check("a session another application started is under Sessions, and not in the chat", async () => {
    const response = await api("POST", "/v1/responses", { model: LINE, input: "echo started by the API" });
    direct = response.metadata.hg_session;
    made.sessions.add(direct);
    await go("#/chat");
    expect(await page.locator(".list a").count() === 1, "the chat lists a session it did not start");
    await go("#/sessions");
    await page.waitForSelector(`.listing a[href="#/sessions/${direct}"]`);
    await shot("10-sessions");
  });

  // sessions
  await check("a session is its conversation, with each turn's payloads", async () => {
    const [mine] = [...made.sessions];
    await go(`#/sessions/${mine}`);
    await page.waitForSelector(".turn >> nth=3");
    expect((await page.locator(".said").first().textContent()).includes("echo hello from the console"), "the request's input is not shown");
    expect((await page.locator(".reply .text").first().textContent()).includes("hello from the console"), "the response's text is not shown");
    expect(await page.locator(".turn .pill:has-text('failed')").count() === 1, "the failed turn is not marked");
    await page.click(".turn button:has-text('Payloads') >> nth=0");
    const raw = await page.locator(".raw:visible pre").allTextContents();
    expect(raw.some(text => text.includes(`"model": "${LINE}"`)) && raw.some(text => text.includes('"object": "response"')), "the request and response payloads are not shown");
    await shot("11-session");
  });
  await check("a session is deleted", async () => {
    await go(`#/sessions/${direct}`);
    await page.click(".head button:has-text('Delete')");
    await page.click(".dialog button.primary");
    await page.waitForSelector(".listing");
    expect(await page.locator(`.listing a[href="#/sessions/${direct}"]`).count() === 0, "it is still listed");
    made.sessions.delete(direct);
  });

  // usage
  await check("usage shows processor time, memory, network and storage, for a period", async () => {
    await go("#/usage");
    expect((await page.locator(".stat .label").allTextContents()).join() === "CPU,Memory,Network,Storage", "the four measures are not shown");
    const all = await page.locator(".stat .value").first().textContent();
    expect(/^[\d.,]+CPU-(seconds|minutes|hours)$/.test(all), `processor time reads "${all}"`);
    await shot("12-usage");
    await page.click(".segment button:has-text('Today')");
    await page.waitForSelector(".segment button.on:has-text('Today')");
    expect(await page.evaluate(() => location.hash) === "#/usage/today", "the period is not in the address");
  });

  // tidying up, through the console
  await check("a chat is removed from the browser, and its session stays", async () => {
    await go("#/chat");
    await page.hover(".list a");
    await page.click(".list a button");
    await page.click(".dialog button.primary");
    await page.waitForSelector(".list a", { state: "detached" });
    expect((await api("GET", `/v1/sessions/${[...made.sessions][0]}`)).turns === 4, "removing the chat removed its session");
  });
  await check("the environment, the harness and the provider are deleted", async () => {
    for (const [kind, name] of [["environments", ENVIRONMENT], ["harnesses", HARNESS]]) {
      await go(`#/${kind}/${name}`);
      await page.click(".head button:has-text('Delete')");
      await page.click(".dialog button.primary");
      await page.waitForSelector(".cards");
      expect(await page.locator(`.card:has-text("${name}")`).count() === 0, `${name} is still listed`);
    }
    await go("#/providers");
    await page.click(`.card:has-text("${PROVIDER}") button:has-text("Delete")`);
    await page.click(".dialog button.primary");
    await page.waitForSelector(`.card:has-text("${PROVIDER}")`, { state: "detached" });
  });
  await check("with nothing that can run, the chat says what to set up", async () => {
    await page.route("**/v1/environments?*", route => route.fulfill({ json: { object: "list", data: [], has_more: false } }));
    await go("#/sessions"); await go("#/chat");
    await page.waitForSelector(".start .step >> nth=2");
    expect(await page.locator(".composer").count() === 0, "a message can be typed with nothing to run it");
    await shot("13-chat-setup");
    await page.unroute("**/v1/environments?*");
  });
  await check("signing out forgets the key", async () => {
    await page.click("#signout");
    await page.waitForSelector("#signin:not([hidden])");
    expect(await page.evaluate(() => sessionStorage.getItem("hg.key")) === null, "the key is still in the tab");
  });
  await check("no page raised an error", async () => expect(errors.length === 0, errors.join(" | ")));

  await browser.close();
})().catch(error => { console.log(`  FAIL  ${error.message}`); results.push([false, "the run"]); }).finally(async () => {
  for (const session of made.sessions) await api("DELETE", `/v1/sessions/${session}`).catch(() => {});
  for (const path of [`environments/${ENVIRONMENT}`, `harnesses/${HARNESS}`, `providers/${PROVIDER}`]) await api("DELETE", `/v1/${path}`).catch(() => {});
  const failed = results.filter(([ok]) => !ok).length;
  console.log(`\n${results.length - failed} of ${results.length} checks passed`);
  process.exit(failed ? 1 : 0);
});
