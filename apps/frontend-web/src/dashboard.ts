// Shared dashboard scaffolding extracted from App.tsx (#115): the cross-cutting
// types, constants, and pure helpers used by the cockpit shell (App) and its
// Board/Card subtree. Pure extraction — behaviour-preserving, no logic change.
import { overallState, stageState, type TaskState } from "./taskState";
import type { Health, WorkItem } from "./api";
import {
  IconAudit,
  IconBell,
  IconInsights,
  IconPipeline,
  IconRunning,
  IconServices,
  IconSettings,
  IconTokens,
} from "./icons";

export type View =
  | "overview"
  | "needs"
  | "pipeline"
  | "running"
  | "tokens"
  | "audit"
  | "services"
  | "settings";

export type Backend =
  | { kind: "loading" }
  | { kind: "ok"; health: Health }
  | { kind: "error"; message: string };

export const NAV: { id: View; label: string; Icon: typeof IconPipeline }[] = [
  { id: "overview", label: "Mission Control", Icon: IconInsights },
  { id: "needs", label: "Needs you", Icon: IconBell },
  { id: "pipeline", label: "Pipeline", Icon: IconPipeline },
  { id: "running", label: "Active tasks", Icon: IconRunning },
  { id: "tokens", label: "Tokens", Icon: IconTokens },
  { id: "audit", label: "Audit", Icon: IconAudit },
  { id: "services", label: "Services", Icon: IconServices },
  { id: "settings", label: "Settings", Icon: IconSettings },
];

export const STAGES = [
  { key: "pfactory", label: "Plan", svc: "PFactory", cls: "plan" },
  { key: "aifactory", label: "Code", svc: "AIFactory", cls: "code" },
  { key: "tfactory", label: "Test", svc: "TFactory", cls: "test" },
] as const;

// Link out to the OpenObserve / OTLP backend (RFC-0001 v1.3 P2). Build-time
// configurable via VITE_OBSERVE_URL; defaults to the public instance. Set it to
// an empty string at build time to hide the link entirely. A new-tab link (not
// an iframe) keeps it simple and sidesteps cross-origin auth.
export const OBSERVE_URL =
  import.meta.env.VITE_OBSERVE_URL === undefined
    ? "https://observe.freundcloud.org.uk"
    : import.meta.env.VITE_OBSERVE_URL;

// Portal switcher (#149): the four Factory portals as one product. CFactory is
// the current ("cockpit") entry; the others link out (build-time overridable via
// VITE_*_URL, defaulting to the live hosts). First step toward the unified shell —
// SSO handoff + a shared shell package + global search are tracked follow-up.
// import.meta.env values are typed `any`; keep the URLs strictly `string`.
const envUrl = (v: unknown, fallback: string): string => (typeof v === "string" && v ? v : fallback);

export const PORTALS: {
  key: string;
  label: string;
  svc: string;
  accent: string;
  url: string;
  current: boolean;
}[] = [
  { key: "plan", label: "Plan", svc: "PFactory", accent: "var(--plan)", url: envUrl(import.meta.env.VITE_PFACTORY_URL, "https://pfactory.freundcloud.org.uk"), current: false },
  { key: "build", label: "Build", svc: "AIFactory", accent: "var(--code)", url: envUrl(import.meta.env.VITE_AIFACTORY_URL, "https://aifactory.freundcloud.org.uk"), current: false },
  { key: "test", label: "Test", svc: "TFactory", accent: "var(--test)", url: envUrl(import.meta.env.VITE_TFACTORY_URL, "https://tfactory.freundcloud.org.uk"), current: false },
  { key: "cockpit", label: "Cockpit", svc: "CFactory", accent: "var(--cyan)", url: "", current: true },
];

export type StageKey = (typeof STAGES)[number]["key"];

export const TICKER_PREF = "cfactory-ticker-open";

export function relTime(iso: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, (Date.now() - then) / 1000);
  if (s < 90) return "just now";
  const m = s / 60;
  if (m < 90) return `${Math.round(m)}m ago`;
  const h = m / 60;
  if (h < 36) return `${Math.round(h)}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

/** The canonical overall lifecycle state of one work item. */
export function itemState(wi: WorkItem): TaskState {
  return overallState([
    stageState(wi.pfactory.status),
    stageState(wi.aifactory.status),
    stageState(wi.tfactory.status),
  ]);
}
