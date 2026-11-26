// Every page of the console, in a real browser, against a running installation.
//
//   HG_URL=http://127.0.0.1:8000 HG_KEY=hg_… node tests/e2e/console.cjs
//
// It needs Playwright (`npm install playwright`, or NODE_PATH to one) and an image the host can pull with python3
// in it (HG_IMAGE, default python:3.13-slim). Everything it makes is named console-check-… and removed at the end.
const { chromium } = require("playwright");
const fs = require("fs");
const zlib = require("zlib");

const URL = (process.env.HG_URL || "http://127.0.0.1:8000").replace(/\/$/, ""), KEY = process.env.HG_KEY || "";
const IMAGE = process.env.HG_IMAGE || "python:3.13-slim", SHOTS = process.env.HG_SHOTS || "";
const id = Date.now().toString(36), PROVIDER = `console-check-p${id}`, HARNESS = `console-check-h${id}`, ENVIRONMENT = `console-check-e${id}`;
const LINE = `${ENVIRONMENT}/${HARNESS}/none/none`, WRITTEN = `console-check-w${id}`;        // a package written in a session
const results = [], made = { sessions: new Set(), keys: [] };

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
  // A refused sign-in is tried on purpose, and a message that interrupts waits out the turn it stopped (409).
  page.on("console", message => { if (message.type() === "error" && !/\b(401|409)\b/.test(message.text())) errors.push(message.text()); });
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
  const WHO = (await fetch(`${URL}/v1/platform`, { headers: { Authorization: `Bearer ${KEY}` } })).headers.get("hg-subject");      // as the platform names this key
  const stored = () => page.evaluate(who => JSON.parse(localStorage.getItem(`hg.chats.${who}`)), WHO);       // this person's own list of chats, newest first
  const say = async words => {
    const said = await page.locator(".said").count();
    await page.fill(".composer textarea", words);
    await page.press(".composer textarea", "Enter");
    await page.waitForFunction(count => document.querySelectorAll(".said").length > count, said);
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
    expect((await page.locator(".dialog [data-url]").boundingBox()).width > 250, "there is no room to see the base URL as it is typed");
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
    expect(await page.locator(".tree a").count() === 2, "the template is not install and run");
    expect((await page.textContent(".head h1")).includes(HARNESS) && (await page.inputValue(".code textarea")).includes("response.completed"),
      "the editor is not open on the new harness's run");
    expect((await api("GET", "/v1/harnesses")).data.every(each => each.name !== HARNESS), "it exists before it was created");
  });
  await check("its files are edited, and the working copy is kept between tabs", async () => {
    await page.fill(".code textarea", (await page.inputValue(".code textarea")) + "# Made by the console's own check.\n");
    await tab("Environments");
    await page.waitForSelector(".panel");
    await tab("Files");
    expect((await page.inputValue(".code textarea")).includes("Made by the console"), "the edit was lost on the way to another tab");
    expect(await page.isChecked(".pane .check input"), "run is not marked executable");
    expect(await page.locator(".tabs a").count() === 2, "a harness has more to it than its files and the environments that carry it");
    await shot("07-harness-new");
  });
  await check("it is created, and the platform holds the package", async () => {
    await page.click(".head button.primary");
    await toast(`Created ${HARNESS}`);
    await page.waitForSelector(`h1:has-text("${HARNESS}")`);
    const stored = await api("GET", `/v1/harnesses/${HARNESS}`);
    expect(/^[0-9a-f]{64}$/.test(stored.digest) && Object.keys(stored).sort().join() === "created_at,digest,name,secrets_set,updated_at", "a harness is not its package and nothing else");
    const kept = await fetch(`${URL}/v1/harnesses/${HARNESS}/package`, { headers: { Authorization: `Bearer ${KEY}` } });
    expect(zlib.gunzipSync(Buffer.from(await kept.arrayBuffer())).includes("Made by the console's own check."), "the package stored is not the one edited");
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
    expect(await page.locator(".tree a").count() === 3, "the package does not hold three files");
  });
  let archive = "";
  await check("a harness is exported as its package archive, and imported again", async () => {
    const [download] = await Promise.all([page.waitForEvent("download"), page.click(".head button:has-text('Export')")]);
    expect(download.suggestedFilename() === `${HARNESS}.tar.gz`, `the file is called ${download.suggestedFilename()}`);
    archive = await download.path();
    expect(fs.readFileSync(archive).subarray(0, 2).equals(Buffer.from([0x1f, 0x8b])), "the export is not gzip");
    await go("#/harnesses");
    const chooser = await picked(() => page.click(".top button:has-text('Import')"));
    await chooser.setFiles({ name: `${HARNESS}.tar.gz`, mimeType: "application/gzip", buffer: fs.readFileSync(archive) });
    await page.waitForSelector(".dialog [name=answer]");                     // a package says nothing of its name: the file's is offered
    expect(await page.inputValue(".dialog [name=answer]") === HARNESS, "the import is not offered the file's name");
    await page.click(".dialog button.primary");
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
  await check("the chat offers an environment's harnesses, and a harness that calls no model is run with no provider", async () => {
    await go("#/chat");
    await page.selectOption("select[title=Environment]", ENVIRONMENT);
    expect(await page.inputValue("select[title=Harness]") === HARNESS, "the environment's harness is not offered");
    const offered = await page.locator("select[title=Provider] option").allTextContents();
    expect(offered.includes(PROVIDER) && offered.at(-1) === "none", `the providers offered are ${offered}`);
    await page.selectOption("select[title=Provider]", "none");
    expect(await page.isHidden("input[title=Model]"), "a model is asked for with no provider");
  });
  await check("with no provider to name, the line names none", async () => {
    await page.route("**/v1/providers?*", route => route.fulfill({ json: { object: "list", data: [], has_more: false } }));
    await go("#/sessions"); await go("#/chat");
    expect(await page.inputValue("select[title=Provider]") === "none" && await page.isHidden("input[title=Model]"), "a provider is asked for when there is none");
    await page.unroute("**/v1/providers?*");
    await go("#/sessions"); await go("#/chat");
    await page.selectOption("select[title=Environment]", ENVIRONMENT);
    await page.selectOption("select[title=Provider]", "none");
  });
  await check("a message runs in a sandbox and its reply arrives", async () => {
    const reply = await say("echo hello from the console > note.txt; cat note.txt; cat .started");
    const text = await reply.locator(".text").textContent();
    expect(text.includes("hello from the console") && text.includes("started"), `the reply is "${text.slice(0, 80)}"`);
    expect(await reply.locator(".who").textContent() === LINE, "the reply does not name the model line it ran on");
    expect(await reply.locator(".state").count() === 0, "a completed reply still shows a state");
    await page.waitForSelector(".files-list .row:has-text('note.txt')");
    expect(await page.locator(".tiles .tile:has-text('note.txt')").count() === 1, "the file the turn made is not shown under its reply");
    const chats = await stored();
    expect(chats[0].session && chats[0].exchanges[0].response && chats[0].exchanges[0].status === "completed", "the chat did not keep its session and its response");
    expect(await page.textContent(".chat-head .session") === chats[0].session, "the chat does not show its session");
    made.sessions.add(chats[0].session);
  });
  await check("the next message continues the same session", async () => {
    const reply = await say("cat note.txt && echo second >> note.txt && wc -l < note.txt");
    expect((await reply.locator(".text").textContent()).includes("hello from the console"), "the file of the first turn is not there");
    const chats = await stored();
    expect(chats[0].exchanges.length === 2 && (await api("GET", `/v1/sessions/${chats[0].session}`)).turns === 2, "the second message is not the session's second turn");
    await shot("09-chat");
  });
  await check("a reply is drawn as markdown, and nothing in it runs", async () => {
    const reply = await say("printf '## Done\\n\\n**bold** and `code` and [a link](https://example.com)\\n\\n| a | b |\\n|---|---|\\n| 1 | 2 |\\n\\n- [x] one\\n- [ ] two\\n\\n```python\\nprint(1)\\n```\\n\\n<b id=planted>kept</b><img src=https://example.com/x.png onerror=window.planted=1><script>window.planted=2</script>\\n'");
    const text = reply.locator(".text");
    expect(await text.locator(".heading").textContent() === "Done" && await text.locator("p b").first().textContent() === "bold", "the heading or the strong words are not drawn");
    expect(await text.locator("table td").count() === 2 && await text.locator("li.task input").count() === 2, "the table or the task list is not drawn");
    expect(await text.locator("a[href='https://example.com'][target=_blank][rel~=noopener]").count() === 1, "the link does not open apart");
    expect((await text.locator(".cb code").textContent()).includes("print(1)") && await text.locator(".cb .cbh").textContent() === "pythonCopy", "the code block is not drawn with its language and Copy");
    expect((await text.textContent()).includes("kept"), "words inside a kept tag are lost");
    expect(await page.locator("#planted").count() === 0 && await text.locator("img, script").count() === 0 && !(await page.evaluate(() => window.planted)), "what the agent printed became markup that runs");
  });
  await check("code is highlighted, mathematics typeset and a diagram drawn, by libraries the page is allowed to load", async () => {
    const reply = await say("printf 'Area: $\\pi r^2$\n\n```python\ndef area(r):\n    return 3.14 * r * r\n```\n\n```mermaid\ngraph TD; A-->B;\n```\n'");
    await reply.locator(".cb code .hljs-keyword").first().waitFor({ timeout: 30000 });
    await reply.locator(".math .katex").waitFor({ timeout: 30000 });
    await page.frameLocator(".reply .cb.drawn iframe.viewer").last().locator("svg").waitFor({ timeout: 60000 });
  });
  await check("a failed turn says why, and a reply can be copied", async () => {
    const reply = await say("exit 3");
    await reply.locator(".state.bad:has-text('code 3')").waitFor();
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.locator(".reply").nth(1).hover();
    await page.locator(".reply").nth(1).locator(".actions button[title=Copy]").click();
    expect((await page.evaluate(() => navigator.clipboard.readText())).includes("hello from the console"), "the reply was not copied");
  });
  await check("a file is given with a message, and the harness finds it", async () => {
    const dot = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", "base64");
    await page.setInputFiles(".composer input[type=file]", [{ name: "given.txt", mimeType: "text/plain", buffer: Buffer.from("from the person") },
                                                            { name: "dot.png", mimeType: "image/png", buffer: dot }]);
    await page.waitForSelector(".composer .pending .tile:has-text('given.txt') small:has-text('B')");
    await page.waitForSelector(".composer .pending .tile:has-text('dot.png') small:has-text('B')");
    const reply = await say("cat /turn/files/*/given.txt; ls /turn/files/*/");
    const text = await reply.locator(".text").textContent();
    expect(text.includes("from the person") && text.includes("dot.png"), "the harness did not find the files");
    expect(await page.locator(".said").last().locator(".attached:has-text('given.txt')").count() === 1 && await page.locator(".composer .pending .tile").count() === 0, "the file is not shown with the message");
    const sent = (await api("GET", `/v1/sessions/${(await stored())[0].session}/turns`)).data.at(-1).request.input[0].content;
    expect(sent.map(part => part.type).join() === "input_text,input_file,input_image", `a picture was not sent as an image: ${sent.map(part => part.type)}`);
  });
  await check("what a turn builds opens beside the chat, and runs apart from the console", async () => {
    await say("mkdir -p site && printf '<h1 id=t>built</h1><script>document.getElementById(\"t\").textContent += \" and ran: \" + (function () { try { return sessionStorage.length; } catch (e) { return \"no storage\"; } })()</script>' > site/index.html; printf '# Notes\\n\\nSee [the page](site/index.html).\\n' > notes.md; printf 'x,y\\n1,2\\n3,\"4,5\"\\n' > data.csv");
    await page.waitForSelector(".side .ph .title:has-text('site/index.html')");
    const frame = page.frameLocator(".side .pb iframe");
    await frame.locator("h1:has-text('built and ran: no storage')").waitFor();                     // it runs, and has no origin
    expect(!(await page.getAttribute(".side .pb iframe", "sandbox")).includes("allow-same-origin"), "the page is not sandboxed");
    expect((await page.getAttribute(".side .ph a[title=Download]", "href")).endsWith("site/index.html?download"), "the page cannot be downloaded");
    await page.click(".side .ph button[title='Show the code']");
    await page.waitForSelector(".side .codeview:has-text('<h1 id=t>built</h1>')");
    expect((await page.locator(".tiles").last().locator(".tile b").allTextContents()).join().startsWith("index.html,notes.md,data.csv"), "the turn's files are not shown best first");
    await shot("09-chat-preview");
  });
  await check("a document's links open the session's files, and a table is drawn", async () => {
    await page.locator(".tiles").last().locator(".tile:has-text('notes.md')").click();
    await page.click(".side .doc a.work");
    await page.waitForSelector(".side .ph .title:has-text('site/index.html')");
    await page.click(".side .segment button:has-text('Files')");
    await page.waitForSelector(".files-list .row");
    expect((await page.locator(".files-list .row .nm").allTextContents()).includes("site/index.html"), "the files are not listed");
    await page.click(".files-list .row:has-text('data.csv')");
    await page.waitForSelector(".side table.grid tbody tr >> nth=1");
    expect((await page.locator(".side table.grid tbody tr").last().locator("td").allTextContents()).join("|") === "2|3|4,5", "the table is not read as CSV");
    await page.click(".side .ph button[title=Close]");
    expect(await page.isHidden(".side"), "the panel does not close");
    await page.click(".chat-head button[title=\"The session's files\"]");
    await page.waitForSelector(".side:visible");
  });
  await check("a turn is stopped, and a message sent while one runs interrupts it and steers", async () => {
    await page.fill(".composer textarea", "echo begun; sleep 60; echo never");
    await page.press(".composer textarea", "Enter");
    await page.waitForSelector(".reply .text:has-text('begun')");
    expect(await page.getAttribute(".composer textarea", "placeholder") === "Send a message to interrupt and steer", "the composer does not say a message would interrupt");
    await page.click(".composer button[title=Stop]");
    await page.locator(".reply").last().locator(".state:has-text('Stopped')").waitFor();
    await page.fill(".composer textarea", "echo again; sleep 60; echo never");
    await page.press(".composer textarea", "Enter");
    await page.waitForSelector(".reply .text:has-text('again')");
    const reply = await say("echo steered");
    expect((await reply.locator(".text").textContent()).includes("steered"), "the message that interrupted was not answered");
    await page.locator(".reply").nth(await page.locator(".reply").count() - 2).locator(".state:has-text('Interrupted')").waitFor();
    const chats = await stored();
    expect((await api("GET", `/v1/sessions/${chats[0].session}`)).turns === chats[0].exchanges.length, "an interrupted turn left the session");
  });
  await check("a chat is read again from the API when the page is opened again, and a running turn is followed again", async () => {
    await page.fill(".composer textarea", "sleep 4; echo late");
    await page.press(".composer textarea", "Enter");
    await page.waitForSelector(".reply .working");
    await page.reload();
    await page.waitForSelector(".reply .text:has-text('late')", { timeout: 60000 });
    expect((await page.locator(".reply .text").first().textContent()).includes("hello from the console"), "the first reply is not shown again");
    expect(await page.locator(".rows a.on").count() === 1 && await page.inputValue("select[title=Environment]") === ENVIRONMENT, "the open chat or its model line is not shown again");
    await page.waitForSelector("button[title=Send]");
  });
  await check("a message is taken back, edited and sent again, in a new branch", async () => {
    const before = (await stored())[0];
    await page.locator(".said").last().hover();
    await page.locator(".said").last().locator("button[title^=Rewind]").click();
    await page.waitForSelector(".notice:has-text('Rewound')");
    expect(await page.inputValue(".composer textarea") === "sleep 4; echo late", "the message's words are not back in the composer");
    expect(await page.locator(".said").count() === before.exchanges.length - 1, "the message is still in the conversation");
    await page.fill(".composer textarea", "");
    const reply = await say("cat note.txt; echo edited");
    expect((await reply.locator(".text").textContent()).includes("edited") && (await reply.locator(".text").textContent()).includes("hello from the console"), "the branch does not have the session's files");
    const after = (await stored())[0];
    expect(after.session !== before.session && after.exchanges.length === before.exchanges.length, "the edited message did not start a branch of its own");
    made.sessions.add(after.session);
  });
  await check("a reply is branched into a chat of its own, and a chat is renamed", async () => {
    await page.locator(".reply").first().hover();
    await page.locator(".reply").first().locator("button[title^=Branch]").click();
    await page.waitForSelector(".notice:has-text('A branch')");
    expect(await page.locator(".rows a").count() === 2 && await page.locator(".said").count() === 1, "the branch is not a chat of its own that ends at the reply");
    const reply = await say("cat note.txt | head -1; echo on the branch");
    expect((await reply.locator(".text").textContent()).includes("hello from the console"), "the branch did not continue from the reply");
    const chats = await stored();
    expect(chats[0].session !== chats[1].session && chats[1].exchanges.length > 1, "the chat it came from was changed");
    made.sessions.add(chats[0].session);
    await page.locator(".rows a.on").hover();
    await page.locator(".rows a.on button[title=Rename]").click();
    await page.fill(".rows input.rename", "The branch");
    await page.press(".rows input.rename", "Enter");
    expect(await page.locator(".rows a.on span").textContent() === "The branch" && (await stored())[0].title === "The branch", "the chat was not renamed");
  });
  await check("the list of chats is hidden and shown, and stays as it was left", async () => {
    await page.click(".list-head button[title='Hide chats']");
    expect(await page.isHidden(".chat .list"), "the list is still shown");
    await page.reload();
    await page.waitForSelector(".chat.nolist .chat-head button[title='Show chats']");
    await page.click(".chat-head button[title='Show chats']");
    await page.waitForSelector(".chat .list:visible");
  });
  let direct = "";
  await check("a session another application started is under Sessions, not in the chat, and is continued from there", async () => {
    const response = await api("POST", "/v1/responses", { model: LINE, input: "echo started by the API > api.txt; cat api.txt" });
    direct = response.metadata.hg_session;
    made.sessions.add(direct);
    await go("#/chat");
    expect(await page.locator(".rows a").count() === 2, "the chat lists a session it did not start");
    await go("#/sessions");
    await page.waitForSelector(`.listing a[href="#/sessions/${direct}"]:has-text("started by you")`);
    await shot("10-sessions");
    await go(`#/sessions/${direct}`);
    await page.click("button:has-text('Continue in chat')");
    await page.waitForSelector(".notice:has-text('started elsewhere')");
    const reply = await say("cat api.txt");
    expect((await reply.locator(".text").textContent()).includes("started by the API"), "the chat did not continue the session");
    expect((await api("GET", `/v1/sessions/${direct}`)).turns === 2, "the message did not go to that session");
  });

  await check("a package an agent wrote is added from its link or its offer, and chatted with at once", async () => {
    await go("#/chat");
    await page.selectOption("select[title=Environment]", ENVIRONMENT);
    const reply = await say([`mkdir -p ${WRITTEN} && cd ${WRITTEN}`,
      "printf '#!/bin/sh\\ntrue\\n' > install",
      "cat > run <<'RUN'", "#!/bin/sh", `echo '{"type": "response.output_text.delta", "delta": "hello from the package"}'`,
      `echo '{"type": "response.completed"}'`, "RUN",
      `chmod +x install run && echo "Ready: [Add it as a harness](/console/#/harnesses/add/${WRITTEN})"`].join("\n"));
    made.sessions.add((await stored())[0].session);
    await reply.locator(`a.tile.package:has-text("Add ${WRITTEN} as a harness")`).waitFor();              // the chat's own offer
    const link = reply.locator(".text a:has-text('Add it as a harness')");
    expect(await link.getAttribute("href") === `#/harnesses/from/${(await stored())[0].session}/${WRITTEN}` && await link.getAttribute("target") === null,
      "the agent's link, which names no session, does not lead to this session's folder");
    await link.click();
    await page.waitForSelector(`.dialog h3:has-text("Add ${WRITTEN} as a harness")`);
    expect(await page.inputValue(".dialog [name=name]") === WRITTEN && await page.inputValue(".dialog [name=environment]") === ENVIRONMENT, "the name or the environment is not filled in");
    await shot("09-add-harness");
    await page.click(".dialog button.primary");
    await page.waitForFunction(() => location.hash.startsWith("#/chat/with/"));
    await page.waitForSelector(".composer");
    expect(await page.inputValue("select[title=Harness]") === WRITTEN && (await page.inputValue(".composer textarea")).startsWith("Say hello"), "the chat is not ready on the new harness");
    expect((await api("GET", `/v1/environments/${ENVIRONMENT}`)).harnesses.includes(WRITTEN), "the environment does not carry it");
    await page.press(".composer textarea", "Enter");
    await page.waitForSelector(".reply .text:has-text('hello from the package')", { timeout: 120000 });      // its hello world, on the platform
    await page.waitForSelector("button[title=Send]");
    made.sessions.add((await stored())[0].session);
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

  // API keys
  const signInAs = async key => {
    await page.click("#signout");
    await page.waitForSelector("#signin:not([hidden])");
    await page.fill("#key", key);
    await page.click("#signin button");
    await page.waitForSelector("#shell:not([hidden])");
  };
  const newKey = async (name, more = {}) => {
    await go("#/keys");
    await page.click(".top button:has-text('New key')");
    await page.fill(".dialog [name=name]", name);
    if (more.ends) await page.fill(".dialog [name=ends]", more.ends);
    if (more.scopes) await page.fill(".dialog [name=scopes]", more.scopes);
    await page.click(".dialog button.primary");
    await page.waitForSelector("#new-key .mono");
    return page.textContent("#new-key .mono");
  };
  await check("an API key is made on its page and shown once, and with it the same person signs in: their chats are there", async () => {
    await go("#/chat");
    await page.waitForSelector(".rows a");
    const mine = await page.locator(".rows a").count(), other = await newKey("console-check another key");
    expect(other.startsWith("hg_") && other !== KEY, "the new key is not shown");
    await shot("14-api-keys");
    const listed = (await api("GET", "/v1/keys")).data.find(key => key.name === "console-check another key");
    expect(listed && listed.subject === WHO && listed.scopes.length === 0 && !JSON.stringify(listed).includes(other), "the key is not kept as the owner's, or the API returns it");
    expect(listed.created_by === WHO && listed.hint === other.slice(-4), "the key does not say who made it, or how it ends");
    await page.waitForSelector(`.panel div:has(> div :text("console-check another key")) :text("hg_…${other.slice(-4)}")`);      // how it ends: which key it is
    expect(await page.locator(".panel span:has-text('by you')").count() >= 1 && await page.locator(".panel span:has-text('by the host')").count() >= 1,
      "a row does not say who made its key: the person signed in, and whoever set the installation up");
    made.keys.push(listed.id);
    await go("#/usage"); await go("#/keys");
    await page.waitForSelector(".panel :text('console-check another key')");
    expect(await page.locator("#new-key").count() === 0, "the key is shown a second time");
    await signInAs(other);
    await go("#/chat");
    await page.waitForSelector(".rows a");
    expect(mine > 0 && await page.locator(".rows a").count() === mine, "the same person, with another key, is not shown their chats");
    await go("#/sessions");
    await page.waitForSelector(".listing a:has-text('started by you')");
    await signInAs(KEY);
  });
  await check("a key limited to some actions lists only the pages it may use, and is refused the rest; revoked, it is refused everything", async () => {
    const narrow = await newKey("console-check narrow key", { ends: "2099-12-31", scopes: "responses.*\nsessions.read\nfiles.*\ncontainers.read\nenvironments.read\nharnesses.read\nproviders.read" });
    const kept = (await api("GET", "/v1/keys")).data.find(key => key.name === "console-check narrow key");
    expect(kept.scopes.length === 7 && kept.expires_at.startsWith("2099-12-31"), "its limits were not kept as typed");
    made.keys.push(kept.id);
    await signInAs(narrow);
    const labels = await page.locator("#nav a").allTextContents();
    expect(labels.join() === "Chat,Sessions", `the sidebar of a key limited to chatting is ${labels}`);
    await go("#/providers");                                              // a page that is not theirs: the first that is, in its place
    await page.waitForSelector(".composer");
    const asked = await fetch(`${URL}/v1/usage`, { headers: { Authorization: `Bearer ${narrow}` } });
    expect(asked.status === 403 && (await asked.json()).error.message.includes("which does not allow usage.read"), "a limited key did what it is limited away from");
    await signInAs(KEY);
    expect((await page.locator("#nav a").allTextContents()).join() === "Chat,Providers,Environments,Harnesses,Sessions,Usage,API keys", "the owner's sidebar is not every page");
    await go("#/keys");
    await page.click(".panel div:has(> div :text('console-check narrow key')) button:has-text('Revoke')");
    await page.click(".dialog button.primary");
    await page.waitForSelector(".panel :text('console-check narrow key')", { state: "detached" });
    expect((await fetch(`${URL}/v1/permissions`, { headers: { Authorization: `Bearer ${narrow}` } })).status === 401, "a revoked key still signs in");
  });

  // tidying up, through the console
  await check("a chat is removed from the browser, and its session stays", async () => {
    await go("#/chat");
    for (let left = await page.locator(".rows a").count(); left > 0; left--) {
      await page.locator(".rows a").first().hover();
      await page.locator(".rows a").first().locator("button[title^=Remove]").click();
      await page.click(".dialog button.primary");
      await page.waitForFunction(count => document.querySelectorAll(".rows a").length === count, left - 1);
    }
    expect((await api("GET", `/v1/sessions/${[...made.sessions][0]}`)).turns >= 4, "removing the chat removed its session");
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
  for (const key of made.keys) await api("DELETE", `/v1/keys/${key}`).catch(() => {});
  for (const path of [`environments/${ENVIRONMENT}`, `harnesses/${HARNESS}`, `harnesses/${WRITTEN}`, `providers/${PROVIDER}`]) await api("DELETE", `/v1/${path}`).catch(() => {});
  const failed = results.filter(([ok]) => !ok).length;
  console.log(`\n${results.length - failed} of ${results.length} checks passed`);
  process.exit(failed ? 1 : 0);
});
