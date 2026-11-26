// Extensions as a chain, in a real browser: what two extensions add to the console is all there, in order, and
// nothing of the console's is replaced. Run by tests/e2e/chain.sh against a server that loads the sample edition
// and a second extension after it.
const { chromium } = require("playwright");

const URL = (process.env.HG_URL || "").replace(/\/$/, ""), KEY = process.env.HG_KEY || "";
const results = [];

(async () => {
  const browser = await chromium.launch(), page = await (await browser.newContext()).newPage();
  const errors = [];
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  page.setDefaultTimeout(15000);
  const check = async (name, run) => {
    try { await run(); results.push(true); console.log(`PASS  ${name}`); }
    catch (error) { results.push(false); console.log(`FAIL  ${name}\n      ${String(error.message).split("\n")[0]}`); }
  };
  const expect = (truth, why) => { if (!truth) throw new Error(why); };

  await check("an extension's way to sign in is on the sign-in form, beside the console's own", async () => {
    await page.goto(`${URL}/console/`);
    await page.waitForSelector("#signin:not([hidden])");
    const way = page.locator("#signin-ways a.way");
    expect(await way.count() === 1 && await way.textContent() === "Sign in with the sample edition", "the sample edition's sign-in is not offered");
    expect(await way.getAttribute("href") === "/v1/sample/signin" && await page.isVisible("#key"), "it does not lead away, or the key's field is gone");
  });
  await check("a credential that arrives in the address signs in, and the address no longer holds it", async () => {
    await page.goto("about:blank");                                // it arrives from elsewhere: the console is loaded anew
    await page.goto(`${URL}/console/#credential=${encodeURIComponent(KEY)}`);
    await page.waitForSelector("#shell:not([hidden])");
    expect(!page.url().includes(KEY) && !page.url().includes("credential"), `the address still holds it: ${page.url()}`);
    expect(await page.evaluate(() => sessionStorage.getItem("hg.key")) === KEY, "the credential is not kept in the tab");
    expect((await page.textContent("#about")).startsWith("v"), "the console is not signed in");
  });
  await check("both extensions' pages are in the sidebar, in the order the extensions were named, after the console's own", async () => {
    const labels = await page.locator("#nav a").allTextContents();
    expect(labels.join() === "Chat,Providers,Environments,Harnesses,Sessions,Usage,Audit,Notes,API keys", `the sidebar is ${labels}`);
  });
  await check("a page whose name is taken is refused, on the page and in the browser's console, and takes nothing's place", async () => {
    await page.reload();
    await page.waitForSelector(".toast.bad:has-text('second: there is already a page named audit')");
    expect(errors.some(text => text.includes("second: there is already a page named audit")), "the browser's console does not say so");
    expect(await page.locator("#nav a:has-text('Taken')").count() === 0, "the page whose name was taken is listed");
  });
  await check("each extension's page draws what its own route returns", async () => {
    await page.evaluate(() => { location.hash = "#/audit"; });
    await page.waitForSelector("main h1:has-text('Audit')");
    await page.waitForSelector(".panel .mono:has-text('keys.created')");                        // the first key, made on the host
    await page.evaluate(() => { location.hash = "#/notes"; });
    await page.waitForSelector("main h1:has-text('Notes')");
    await page.waitForSelector(".panel .mono:has-text('keys.created')");
  });
  await check("the console's own pages are its own still", async () => {
    await page.evaluate(() => { location.hash = "#/providers"; });
    await page.waitForSelector("main h1:has-text('Providers')");
    const files = await Promise.all(["extensions.json", "x/sample/edition.js", "x/second/edition.js", "edition.js"].map(path => fetch(`${URL}/console/${path}`).then(answer => answer.status)));
    expect(files.join() === "200,200,200,404", `the console serves ${files}`);
  });
  await check("signing out tells each extension first, while the credential is still held, and then forgets it", async () => {
    await page.click("#signout");
    await page.waitForSelector("#signin:not([hidden])");
    expect(await page.evaluate(() => window.sampleSignedOutWith) === KEY, "the sample edition was not told, or was told too late");
    expect(await page.evaluate(() => sessionStorage.getItem("hg.key")) === null, "the credential is still in the tab");
  });
  await browser.close();
})().catch(error => { console.log(`FAIL  ${error.message}`); results.push(false); }).finally(() => {
  console.log(`${results.filter(Boolean).length} of ${results.length} passed`);
  process.exit(results.length && results.every(Boolean) ? 0 : 1);
});
