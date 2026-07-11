// Shared display formatters. Hoisted from the copies that had drifted into
// TaskDetail / RunningTasksView / needsYou / LiveTaskStamp / TokensView /
// CostRoutingPanel so there's ONE house style for tokens, cost, elapsed and age.
//
// Note: MissionControl keeps its own fmtTokens/fmtCost/fmtElapsed/fmtDur — they
// are a deliberately different display style (variable precision, 3-decimal
// sub-dollar, empty-string-when-absent for the `{time && …}` guard, no days
// tier), so merging them would change what the screen shows.

/** Seconds → compact age: "3s" / "4m" / "5h" / "6d". "" for invalid/negative. */
export function fmtAge(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "";
  if (sec < 90) return `${Math.round(sec)}s`;
  const m = sec / 60;
  if (m < 90) return `${Math.round(m)}m`;
  const h = m / 60;
  if (h < 36) return `${Math.round(h)}h`;
  return `${Math.round(h / 24)}d`;
}

// ponytail: hand-rolled, not Intl.NumberFormat compact — Intl emits a capital
// "K" ("1.5K"), the UI uses lowercase "k". Switch only if the UI adopts "K".
/** Compact token count: "999" / "1.5k" / "1.2M". */
export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(Math.round(n));
}

/** USD cost: "—" when absent, "<$0.01" for sub-cent, else "$1.23". */
export function fmtCost(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n > 0 && n < 0.01) return "<$0.01";
  return `$${n.toFixed(2)}`;
}

/** Elapsed seconds → "45s" / "3m 20s" / "1h 5m". "—" when absent. */
export function fmtElapsed(sec: number | null | undefined): string {
  if (sec == null) return "—";
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return `${h}h ${m}m`;
}
