// ArtifactsPanel — RFC-0015 §3.3 readable spec/plan/tasks artifacts.
//
// PFactory emit renders spec.md / plan.md / tasks.md from the machine contract —
// the human-readable mirror. This panel renders that Markdown in the cockpit so a
// reviewer reads the plan as docs, not as stringified structures (the durable fix
// for the "Overview renders a raw object" bug class called out in the RFC).
//
// We reuse the repo's existing safe Markdown renderer (src/Markdown.tsx), which
// renders a GFM subset to React nodes WITHOUT dangerouslySetInnerHTML — so
// producer/LLM-authored Markdown can't inject HTML/script. (react-markdown would
// be a net-new dependency; the in-repo renderer already covers headings, lists,
// emphasis and code and matches the cockpit's no-raw-HTML posture.)
//
// ADDITIVE + GUARDED: with no artifacts the panel renders nothing, so a task
// whose producer didn't emit the mirror looks exactly as it did before.

import { useState } from "react";

import type { Artifacts } from "./api";
import Markdown from "./Markdown";

const TABS = [
  { key: "spec", label: "Spec" },
  { key: "plan", label: "Plan" },
  { key: "tasks", label: "Tasks" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

export default function ArtifactsPanel({ artifacts }: { artifacts: Artifacts | null | undefined }) {
  // Only offer tabs for artifacts that are actually present (and non-blank).
  const present = TABS.filter((t) => {
    const v = artifacts?.[t.key];
    return typeof v === "string" && v.trim() !== "";
  });

  // Default to the first present artifact; keep the hook order stable by always
  // calling useState before the early return.
  const [active, setActive] = useState<TabKey>(present[0]?.key ?? "spec");

  if (present.length === 0) return null; // nothing to render -> render nothing

  // The active tab might point at an artifact that isn't present (e.g. it was
  // removed between renders); fall back to the first present one.
  const current = present.some((t) => t.key === active) ? active : (present[0]?.key ?? "spec");
  const body = artifacts?.[current] ?? "";

  return (
    <section className="td-section artifacts" data-testid="td-artifacts">
      <h3>Spec / plan / tasks</h3>
      {present.length > 1 && (
        <div className="artifacts-tabs" role="tablist" aria-label="Plan artifacts">
          {present.map((t) => (
            <button
              key={t.key}
              role="tab"
              aria-selected={t.key === current}
              className={`artifacts-tab ${t.key === current ? "artifacts-tab--active" : ""}`}
              onClick={() => {
                setActive(t.key);
              }}
            >
              {t.label}
            </button>
          ))}
        </div>
      )}
      <div className="artifacts-body" data-testid={`artifacts-body-${current}`}>
        <Markdown text={body} className="artifacts-md" />
      </div>
    </section>
  );
}
