// UI smoke test against the default model (Claude Opus 5.5; needs ANTHROPIC_API_KEY). Run: NODE_PATH=$(npm root -g) node tests/ui_test.mjs [baseUrl] [outDir]
import { createRequire } from "module";
const { chromium } = createRequire(import.meta.url)("playwright");
const base = process.argv[2] || "http://localhost:8080";
const out = process.argv[3] || ".";
const browser = await chromium.launch();
const fail = (m) => { console.error("FAIL:", m); process.exitCode = 1; };
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on("pageerror", e => errors.push(e.message));
await page.goto(base);
await page.waitForSelector(".msg.bot");
if (!(await page.textContent(".msg.bot")).includes("Aria")) fail("no greeting");
await page.fill("#input", "flights from Delhi to Goa tomorrow");
await page.click("#send");
await page.waitForSelector(".card .fl", { timeout: 15000 });
const n = await page.locator(".card .fl").count();
console.log("flight rows:", n);
if (n < 1) fail("no flight card");
await page.waitForSelector("#trace .turn");
if (!(await page.textContent("#trace .turn")).includes("search_flights")) fail("trace missing tool call");
// booking lookup with verification
await page.fill("#input", "status of SP101");
await page.click("#send");
await page.waitForFunction(() => document.querySelectorAll(".msg.bot").length >= 3);
await page.screenshot({ path: `${out}/ui_desktop.png` });
// log in as a scenario user
const opt = await page.$$eval("#user option", os => os.map(o => ({ v: o.value, t: o.textContent })).find(o => o.t.includes("★")));
await page.selectOption("#user", opt.v);
await page.waitForSelector("#scenarios .chip.sc");
const chips = await page.locator("#scenarios .chip.sc").count();
console.log("scenario chips for", opt.t, ":", chips);
await page.locator("#scenarios .chip.sc").first().click();
if (!(await page.inputValue("#input"))) fail("scenario chip did not fill input");
if ((await page.textContent("#verif")) !== "logged in") fail("badge not logged in");
// mobile layout
const m = await browser.newPage({ viewport: { width: 390, height: 844 } });
await m.goto(base);
await m.waitForSelector(".msg.bot");
const sw = await m.evaluate(() => document.documentElement.scrollWidth);
if (sw > 392) fail("horizontal scroll on mobile: " + sw);
await m.screenshot({ path: `${out}/ui_mobile.png`, fullPage: false });
if (errors.length) fail("page errors: " + errors.join("; "));
await browser.close();
console.log(process.exitCode ? "UI TEST FAILED" : "UI TEST PASSED");
