// TraceabilityPanel — RFC-0015 §4 D2 requirement->test traceability matrix.
//
// Renders the verification.traceability[] block as an AC x test x VAL x verdict
// matrix: one row per acceptance criterion showing its covering test(s), the VAL
// level it reached, and the pass/fail/not_run/skipped verdict. This is the
// cockpit half of D2 — TFactory computes the mapping; CFactory surfaces it so a
// reviewer can see, at a glance, which requirements are actually verified.
//
// Everything is ADDITIVE and GUARDED, mirroring ReadinessPanel/CostRoutingPanel:
//   - no traceability at all -> a muted "not available" note (never a crash,
//     never an empty table). The caller may also choose not to mount us.
//   - a value we don't recognise is rendered as text, NEVER as a raw object
//     handed to React as a child (the ReadinessPanel evidence-dict bug class).

import type { TraceabilityRow } from "./api";

// Defensive stringifier: traceability rows are `.passthrough()`, so a future
// producer could put a non-string where we expect text. Render primitives as
// text and anything object-shaped as compact JSON, so we never throw
// "Objects are not valid as a React child".
function asText(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return "[unrenderable]"; // e.g. a circular structure — never crash the matrix
  }
}

const STATUS_LABEL: Record<string, string> = {
  passed: "passed",
  failed: "failed",
  not_run: "not run",
  skipped: "skipped",
};

function statusClass(status: string): string {
  const s = status.toLowerCase();
  if (s === "passed") return "trace-verdict--passed";
  if (s === "failed") return "trace-verdict--failed";
  if (s === "skipped") return "trace-verdict--skipped";
  return "trace-verdict--notrun"; // not_run / anything unknown
}

export default function TraceabilityPanel({
  rows,
}: {
  rows: TraceabilityRow[] | null | undefined;
}) {
  // Degrade cleanly: an absent (or empty) matrix shows a muted note rather than
  // an empty table — the cockpit stays honest about "we have no coverage map".
  if (!rows || rows.length === 0) {
    return (
      <div className="td-empty" data-testid="td-traceability-empty">
        <b>Traceability</b> requirement&rarr;test matrix not available for this task
      </div>
    );
  }

  return (
    <section className="td-section trace" data-testid="td-traceability">
      <h3>Traceability matrix</h3>
      <table className="trace-matrix" data-testid="trace-matrix">
        <thead>
          <tr>
            <th>requirement</th>
            <th>tests</th>
            <th>VAL</th>
            <th>verdict</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const status = asText(r.status) || "not_run";
            const tests = Array.isArray(r.tests) ? r.tests : [];
            return (
              <tr key={`${asText(r.ac_id)}-${String(i)}`} data-testid="trace-row">
                <td className="trace-ac">
                  <span className="trace-ac-id">{asText(r.ac_id)}</span>
                  {r.ac_text != null && asText(r.ac_text) !== "" && (
                    <span className="trace-ac-text" title={asText(r.ac_text)}>
                      {asText(r.ac_text)}
                    </span>
                  )}
                </td>
                <td className="trace-tests">
                  {tests.length === 0 ? (
                    // No covering test is itself a finding (a traceability gap) —
                    // surfaced honestly, never blank.
                    <span className="trace-gap" title="No covering test for this requirement">
                      &mdash; no test
                    </span>
                  ) : (
                    <ul className="trace-test-list">
                      {tests.map((t, ti) => (
                        <li key={ti}>
                          <code>{asText(t)}</code>
                        </li>
                      ))}
                    </ul>
                  )}
                </td>
                <td className="trace-val">
                  {r.val_level != null && asText(r.val_level) !== "" ? asText(r.val_level) : "—"}
                </td>
                <td>
                  <span className={`trace-verdict ${statusClass(status)}`}>
                    {STATUS_LABEL[status.toLowerCase()] ?? status}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
