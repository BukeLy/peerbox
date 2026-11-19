// Renders tools/how-it-works.html into how-it-works.gif. Run from docs/ with playwright-core installed
// (npm i playwright-core && npx playwright-core install webkit) and ffmpeg on the PATH:
//   node tools/gif.js
const { webkit } = require("playwright-core");
const { execFileSync } = require("node:child_process");
const { mkdtempSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const path = require("node:path");

const FPS = 12;
const LENGTH = 13;

(async () => {
  const frames = mkdtempSync(path.join(tmpdir(), "peerbox-gif-"));
  const browser = await webkit.launch();
  const page = await browser.newPage({ viewport: { width: 960, height: 460 }, deviceScaleFactor: 2 });
  await page.goto("file://" + path.resolve("tools/how-it-works.html"));
  await page.evaluate(() => document.fonts.ready);
  for (let i = 0; i < FPS * LENGTH; i++) {
    await page.evaluate((t) => render(t), i / FPS);
    await page.screenshot({ path: path.join(frames, `${String(i).padStart(4, "0")}.png`) });
  }
  await browser.close();
  // captured at 2x, scaled down for sharp text; one palette for the whole loop
  execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-framerate", String(FPS), "-i", path.join(frames, "%04d.png"),
    "-vf", "scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=160:stats_mode=full[p];[b][p]paletteuse=dither=sierra2_4a",
    "-loop", "0", "how-it-works.gif"]);
  rmSync(frames, { recursive: true });
  console.log("how-it-works.gif written");
})();
