// CostRoutingPanel — RFC-0014 (#124) cost observability.
//
// Surfaces, for one task, the PRE-EXECUTION cost-aware routing decision PFactory
// emitted (routing class, autonomy verdict, per-role models, runtime,
// budget_mode) alongside the ACTUAL rolled-up spend CFactory already tracks
// (RFC-0001 usage): estimate vs actual (with variance), per-model cost, and the
// router's rationale.
//
// Everything is ADDITIVE and GUARDED: with no cost-routing data the component
// renders nothing, so a task detail with no routing block / no actuals looks
// exactly as it did before this feature. Metered display reuses the billing-mode
// rule — real `$` only for metered work; a subscription/local run shows tokens
// instead of a dollar estimate, mirroring LiveTaskStamp / the tokens view.

import type { CostRouting } from "./api";

// Sub-cent costs shouldn't read $0.00 (mirrors LiveTaskStamp.fmtCost).
function fmtCost(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n > 0 && n < 0.01) return "<$0.01";
  return `$${n.toFixed(2)}`;
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(Math.round(n));
}

// Signed variance, e.g. "+$0.30 over" / "−$0.10 under". Null when there's no
// metered estimate-vs-actual to compare (subscription/local or pre-RFC-0014).
function varianceLabel(v: number | null): { text: string; over: boolean } | null {
  if (v == null) return null;
  if (v === 0) return { text: "on estimate", over: false };
  const over = v > 0;
  const mag = fmtCost(Math.abs(v));
  return { text: `${over ? "+" : "−"}${mag} ${over ? "over" : "under"}`, over };
}

const ROLE_ORDER = ["planning", "coding", "qa", "test_gen"] as const;

export default function CostRoutingPanel({ data }: { data: CostRouting | undefined }) {
  if (!data) return null;
  const { routing, estimate_vs_actual: eva, actual_by_model } = data;

  // Nothing to show: no routing decision AND no actual spend → render as before.
  const hasActuals = data.actual_total_tokens > 0 || (data.actual_cost_usd ?? 0) > 0;
  if (!routing && !hasActuals) return null;

  const variance = varianceLabel(eva.variance_usd);
  const models = routing?.phase_models ?? {};
  // Stable role ordering first, then any extra roles the router emitted.
  const roleKeys = [
    ...ROLE_ORDER.filter((r) => r in models),
    ...Object.keys(models).filter((r) => !ROLE_ORDER.includes(r as (typeof ROLE_ORDER)[number])),
  ];
  const modelRows = Object.entries(actual_by_model).sort(
    (a, b) => b[1].total_tokens - a[1].total_tokens,
  );

  return (
    <section className="td-section crp" data-testid="td-cost-routing">
      <h3>Cost & routing</h3>

      {/* Routing class + autonomy verdict + budget mode — the headline badges. */}
      {routing && (
        <div className="crp-badges">
          {routing.routing_class && (
            <span className={`crp-badge crp-badge--class-${routing.routing_class}`}>
              {routing.routing_class}
            </span>
          )}
          {routing.autonomy && (
            <span
              className={`crp-badge crp-badge--autonomy-${routing.autonomy}`}
              title={routing.autonomy_reason ?? ""}
            >
              {routing.autonomy}
            </span>
          )}
          {routing.runtime && <span className="crp-badge crp-badge--muted">{routing.runtime}</span>}
          {routing.budget_mode && (
            <span
              className={`crp-badge crp-badge--budget-${routing.budget_mode}`}
              title="Budget mode"
            >
              budget: {routing.budget_mode}
            </span>
          )}
        </div>
      )}

      {/* Estimate vs actual — the heart of the panel. */}
      <div className="crp-eva" data-testid="crp-eva">
        <div className="crp-eva-cell">
          <span className="crp-eva-label">estimate</span>
          <span className="crp-eva-val">{fmtCost(eva.estimate_usd)}</span>
        </div>
        <div className="crp-eva-cell">
          <span className="crp-eva-label">actual</span>
          <span className="crp-eva-val crp-eva-val--actual">{fmtCost(eva.actual_usd)}</span>
        </div>
        <div className="crp-eva-cell">
          <span className="crp-eva-label">ceiling</span>
          <span className="crp-eva-val">{fmtCost(eva.ceiling_usd)}</span>
        </div>
        {variance && (
          <div className="crp-eva-cell">
            <span className="crp-eva-label">variance</span>
            <span
              className={`crp-eva-val crp-variance crp-variance--${variance.over ? "over" : "under"}`}
            >
              {variance.text}
            </span>
          </div>
        )}
      </div>
      {/* When there's no metered estimate (subscription/local), show tokens so the
          panel is still honest about spend without a notional dollar figure. */}
      {eva.estimate_usd == null && (
        <div className="crp-note" title="No metered cost — subscription / local run">
          {fmtTokens(data.actual_total_tokens)} tokens (no metered cost)
        </div>
      )}

      {/* Per-role model picks vs per-model actual spend. */}
      {roleKeys.length > 0 && (
        <div className="crp-roles" data-testid="crp-roles">
          {roleKeys.map((role) => (
            <div className="crp-role" key={role}>
              <span className="crp-role-name">{role}</span>
              <span className="crp-role-model">{models[role]}</span>
            </div>
          ))}
        </div>
      )}
      {modelRows.length > 0 && (
        <table className="crp-models" data-testid="crp-models">
          <thead>
            <tr>
              <th>model</th>
              <th>tokens</th>
              <th>cost</th>
            </tr>
          </thead>
          <tbody>
            {modelRows.map(([model, row]) => (
              <tr key={model}>
                <td>{model}</td>
                <td>{fmtTokens(row.total_tokens)}</td>
                <td>{row.cost_usd > 0 ? fmtCost(row.cost_usd) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* The router's human-readable rationale (why this pick). */}
      {routing?.rationale && (
        <p className="crp-rationale" title="Router rationale">
          {routing.rationale}
        </p>
      )}
    </section>
  );
}
