// Another way to sign in, added by an extension (spec §19, §20): its button on the sign-in form, the console
// working with no key once the extension says who is calling, and its way of signing out.
//
//   HG_TEST_URL=http://… node tests/browser/signin.mjs      (tests/browser/test_console.py runs it)

import { createRequire } from "node:module";

const { chromium } = createRequire(import.meta.url)("playwright");

const url = process.env.HG_TEST_URL;
const browser = await chromium.launch();
const page = await browser.newPage();
page.setDefaultTimeout(30_000);
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
try {
  await page.goto(url);
  await page.waitForSelector("#key");                                   // nobody is signed in: the form
  await page.click("a:has-text('Sign in the other way')");              // the extension's button
  await page.waitForSelector("nav >> text=Signed in as developer");     // no key was ever typed
  await page.click("nav >> text=Sessions");
  await page.waitForSelector("h1 >> text=Sessions");
  if (await page.locator("nav >> text=Keys").count()) throw new Error("a developer was shown an admin's page");
  await page.reload();
  await page.waitForSelector("nav >> text=Signed in as developer");     // still signed in after a reload
  await page.click("text=Sign out");
  await page.waitForSelector("#key");
  await page.reload();
  await page.waitForSelector("#key");                                   // and signed out for good
  if (errors.length) throw new Error(`the page raised errors: ${errors.join("; ")}`);
  console.log("signed in and out the extension's way");
} catch (error) {
  console.error(`FAILED: ${error.message}`);
  process.exitCode = 1;
} finally {
  await browser.close();
}
