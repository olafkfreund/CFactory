/**
 * CFactory cockpit screenshot capture for the docs site.
 *
 * Drives a running cockpit via Playwright (headless Chromium) and saves a
 * curated set of PNGs to docs/assets/blog/<gallery>/ for the captioned
 * gallery page.
 *
 * Auth recipe (proven against the live cluster):
 *   The cockpit's React SPA calls its backend over relative /api/* paths with
 *   NO browser-side token — the frontend's own nginx injects the CFactory API
 *   key (CFACTORY_API_KEYS) into every /api/ request before proxying to the
 *   backend. So the *frontend* service is the clean capture target: port-forward
 *   it and the page authenticates itself. This also sidesteps oauth2-proxy,
 *   which only fronts the public host, not the in-cluster service.
 *
 *     kubectl port-forward -n factory svc/cfactory-frontend 3110:80
 *     CFACTORY_URL=http://localhost:3110 \
 *     PLAYWRIGHT_CHROMIUM_EXECUTABLE=<chrome path> \
 *     node scripts/capture-screenshots.mjs
 *
 * The script is intentionally tolerant: a missed view or selector logs a
 * warning and continues, capturing whatever it can.
 */

const _pw = await import(process.env.PLAYWRIGHT_CORE_PATH || "playwright-core");
const chromium = _pw.chromium ?? _pw.default?.chromium;
import * as path from "node:path";
import * as fs from "node:fs";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const URL = process.env.CFACTORY_URL ?? "http://localhost:3110";
const CHROME =
  process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ||
  "/etc/profiles/per-user/olafkfreund/bin/google-chrome-stable";

const OUT_DIR = path.resolve(
  __dirname,
  "..",
  "docs",
  "assets",
  "blog",
  "2026-06-21",
);

fs.mkdirSync(OUT_DIR, { recursive: true });

const log = (m) => console.log(m);
const warn = (m) => console.warn(`  ! ${m}`);

async function shoot(page, name) {
  try {
    await page.screenshot({ path: path.join(OUT_DIR, name), fullPage: false });
    log(`  + ${name}`);
  } catch (e) {
    warn(`shoot ${name}: ${e.message}`);
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Click a left-nav item by its visible label; returns true on success. */
async function nav(page, label) {
  try {
    const btn = page.locator(`nav.nav button:has-text("${label}")`).first();
    await btn.click({ timeout: 5000 });
    await sleep(1200);
    return true;
  } catch (e) {
    warn(`nav "${label}": ${e.message}`);
    return false;
  }
}

/** Open a task-detail modal by matching card text; returns true on success. */
async function openCardContaining(page, needle) {
  try {
    const card = page
      .locator(".card-wi--clickable", { hasText: needle })
      .first();
    await card.scrollIntoViewIfNeeded({ timeout: 4000 });
    await card.click({ timeout: 5000 });
    // Wait for the modal dialog.
    await page.locator('[role="dialog"][aria-modal="true"]').first().waitFor({
      state: "visible",
      timeout: 5000,
    });
    await sleep(1400);
    return true;
  } catch (e) {
    warn(`openCard "${needle}": ${e.message}`);
    return false;
  }
}

async function closeModal(page) {
  try {
    await page.keyboard.press("Escape");
    await sleep(500);
  } catch {
    /* ignore */
  }
}

async function main() {
  log(`CFactory capture → ${URL}`);
  log(`chrome: ${CHROME}`);
  log(`out:    ${OUT_DIR}`);

  const browser = await chromium.launch({
    executablePath: CHROME,
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const ctx = await browser.newContext({
    viewport: { width: 1600, height: 1000 },
    deviceScaleFactor: 2,
  });
  const page = await ctx.newPage();
  page.on("console", (m) => {
    if (m.type() === "error") warn(`console: ${m.text().slice(0, 120)}`);
  });

  try {
    await page.goto(URL, { waitUntil: "networkidle", timeout: 30000 });
  } catch (e) {
    warn(`initial goto: ${e.message}`);
  }
  await sleep(2500);

  // 1. Mission Control (overview / landing).
  await nav(page, "Mission Control");
  await shoot(page, "01-mission-control.png");

  // 2. Pipeline board — the plan -> code -> test columns.
  await nav(page, "Pipeline");
  await sleep(1000);
  await shoot(page, "02-pipeline-board.png");

  // Reveal finished work items so the "done" tasks are on the board too.
  try {
    await page.locator('button:has-text("Finished")').first().click({ timeout: 3000 });
    await sleep(900);
    await shoot(page, "03-pipeline-with-finished.png");
  } catch (e) {
    warn(`finished toggle: ${e.message}`);
  }

  // 3. Task detail with the live execution DAG — a completed multi-stage run.
  //    LinkLite (037) has an 18-node code-stage graph at 100%.
  if (await openCardContaining(page, "LinkLite")) {
    await shoot(page, "04-task-detail-linklite-dag.png");
    // Scroll the modal body to surface more of the DAG / artifacts.
    try {
      await page.locator(".td-body").first().evaluate((el) => (el.scrollTop = 320));
      await sleep(700);
      await shoot(page, "05-task-detail-dag-scrolled.png");
    } catch (e) {
      warn(`scroll modal: ${e.message}`);
    }
    await closeModal(page);
  }

  // 4. Task detail — a successful / done run (216).
  if (await openCardContaining(page, "Go hello-world greeting library")) {
    await shoot(page, "06-task-detail-success.png");
    await closeModal(page);
  }

  // 5. Task detail — a FAILED run (the AWS 3-tier bench).
  //    It may live under Finished; try by title fragment.
  if (
    (await openCardContaining(page, "3-tier")) ||
    (await openCardContaining(page, "aws"))
  ) {
    await shoot(page, "07-task-detail-failed.png");
    await closeModal(page);
  }

  // 6. Active tasks view — in-progress runs with live progress.
  await nav(page, "Active tasks");
  await sleep(1200);
  await shoot(page, "08-active-tasks.png");

  // Open an in-progress task from the active view for the live DAG.
  if (
    (await openCardContaining(page, "kubejob")) ||
    (await openCardContaining(page, "go hello"))
  ) {
    await shoot(page, "09-active-task-detail.png");
    await closeModal(page);
  }

  // 7. Tokens — billing-mode usage (metered $ vs subscription/local tokens+time).
  await nav(page, "Tokens");
  await sleep(1200);
  await shoot(page, "10-tokens-usage.png");

  // 8. Audit — the signed audit trail.
  await nav(page, "Audit");
  await sleep(1200);
  await shoot(page, "11-audit.png");

  // 9. Services — upstream factory health.
  await nav(page, "Services");
  await sleep(1200);
  await shoot(page, "12-services.png");

  // 10. Settings — copilot provider + token settings.
  await nav(page, "Settings");
  await sleep(1200);
  await shoot(page, "13-settings.png");

  // 11. Copilot assistant — open the floating FAB.
  try {
    await page
      .locator('button[aria-label="Toggle Copilot"]')
      .first()
      .click({ timeout: 4000 });
    await sleep(1200);
    await shoot(page, "14-copilot.png");
  } catch (e) {
    warn(`copilot: ${e.message}`);
  }

  // 12. Back to Mission Control for a clean hero-style full shot.
  await nav(page, "Mission Control");
  await sleep(1000);
  await shoot(page, "15-overview-hero.png");

  await browser.close();
  log("done.");
}

main().catch((e) => {
  console.error("FATAL", e);
  process.exit(1);
});
