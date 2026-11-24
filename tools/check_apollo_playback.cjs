// Open Roxi's published media-fragment URLs in a real browser during the Apollo benchmark.
const {chromium} = require(process.env.ROXI_PLAYWRIGHT_MODULE || "playwright");

async function playback(page, clip) {
  const {start_ms, end_ms} = clip.locator;
  return page.evaluate(async ({url, start, end}) => {
    const audio = new Audio();
    audio.muted = true;
    audio.playbackRate = 2;
    document.body.append(audio);
    try {
      const ready = new Promise((resolve, reject) => {
        audio.addEventListener("canplay", resolve, {once: true});
        audio.addEventListener("error", () => reject(Error("The citation audio could not load.")), {once: true});
      });
      audio.src = url;
      await Promise.race([ready, new Promise((_, reject) => setTimeout(() => reject(Error("Audio load timed out.")), 5000))]);
      const started = audio.currentTime;
      const paused = new Promise(resolve => {
        audio.addEventListener("pause", () => resolve(audio.currentTime), {once: true});
        setTimeout(() => resolve(null), 2000 + 1000 * (end - start));
      });
      await audio.play();
      const stopped = await paused;
      return {started_ms: Math.round(started * 1000), stopped_ms: stopped === null ? null : Math.round(stopped * 1000),
              start_correct: Math.abs(started - start) <= 0.10,
              end_correct: stopped !== null && Math.abs(stopped - end) <= 0.35};
    } finally {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
      audio.remove();
    }
  }, {url: clip.playback_path, start: start_ms / 1000, end: end_ms / 1000});
}

async function main() {
  const browser = await chromium.launch({
    executablePath: process.env.ROXI_BROWSER_EXECUTABLE || undefined,
    args: ["--autoplay-policy=no-user-gesture-required"],
  });
  try {
    const page = await browser.newPage();
    await page.goto(process.env.ROXI_BENCHMARK_BASE + "/login");
    await page.getByLabel("Username").fill(process.env.ROXI_BENCHMARK_USERNAME);
    await page.getByLabel("Password").fill(process.env.ROXI_BENCHMARK_PASSWORD);
    await page.getByRole("button", {name: "Sign in"}).click();
    await page.getByRole("heading", {name: "Workspaces"}).waitFor();
    const report = {};
    for (const [branch, clips] of Object.entries(JSON.parse(process.env.ROXI_BENCHMARK_CLIPS))) {
      report[branch] = [];
      for (const clip of clips) {
        const cue = clip.locator.cue_id;
        try {
          report[branch].push({cue, ...await playback(page, clip)});
        } catch (error) {
          report[branch].push({cue, start_correct: false, end_correct: false, error: error.message});
        }
      }
    }
    console.log(JSON.stringify(report));
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
