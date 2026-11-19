// Renders index.html's hero (the glass name, the mailbox and the slogan) into social.jpg, the 1280×640 card shown for
// links to the site and the repo (GitHub's social preview takes 2:1 under 1 MB). Run from docs/ with playwright-core
// installed (npm i playwright-core) and Google Chrome: node tools/card.js [light|dark]
const { chromium } = require("playwright-core");
const path = require("node:path");

const scheme = process.argv[2] || "dark";

(async () => {
  // the glass words are masked by SVG files, which Chrome fetches with CORS; file:// has no origin without this flag
  const browser = await chromium.launch({ channel: "chrome", args: ["--allow-file-access-from-files"] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 640 }, colorScheme: scheme, reducedMotion: "reduce" });
  await page.goto("file://" + path.resolve("index.html"));
  // the card keeps the letterhead and the slogan; the nav, lede and install bar are for visitors
  await page.addStyleTag({ content: `
    .nav, .lede, .install { display: none; }
    .hero { min-height: 640px; padding: 0 0 12px; }
    .hero-body { margin-top: 40px; }
  ` });
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: "social.jpg", type: "jpeg", quality: 90 });
  await browser.close();
  console.log("social.jpg written");
})();
