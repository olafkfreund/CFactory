// Self-healing for stale bundles. Vite fingerprints the entry bundle
// (assets/index-<hash>.js); a deploy changes that hash. index.html is served
// no-cache (see nginx.conf.template), so polling it always reflects the latest
// build. When the deployed hash differs from the one this tab is running, the
// tab is on an old bundle — reload once to pick up the new one. This means a
// future deploy no longer requires a manual hard refresh to take effect.

/** The entry-bundle filename this tab booted from, e.g. "index-BJEieDdT.js". */
function currentBundle(): string | null {
  for (const s of Array.from(document.scripts)) {
    const m = s.src.match(/assets\/(index-[A-Za-z0-9_-]+\.js)/);
    if (m) return m[1];
  }
  return null;
}

async function deployedBundle(): Promise<string | null> {
  const html = await fetch("/", { cache: "no-store" }).then((r) => r.text());
  const m = html.match(/assets\/(index-[A-Za-z0-9_-]+\.js)/);
  return m ? m[1] : null;
}

export function startVersionWatch(intervalMs = 60_000): void {
  const mine = currentBundle();
  if (!mine) return; // dev server / unexpected markup — nothing to compare against
  let reloading = false;

  const check = async () => {
    if (reloading || document.hidden) return;
    try {
      const latest = await deployedBundle();
      if (latest && latest !== mine) {
        reloading = true;
        window.location.reload();
      }
    } catch {
      // offline or transient — try again next tick
    }
  };

  window.setInterval(check, intervalMs);
  // Also re-check when the tab regains focus, so a backgrounded cockpit catches
  // up immediately instead of waiting out the interval.
  window.addEventListener("visibilitychange", () => {
    if (!document.hidden) void check();
  });
}
