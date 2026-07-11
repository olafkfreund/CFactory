// StageGates (#167, epic Factory#270) — the per-stage machine-decision verdicts
// on the task detail's stage cards: the routing tier the router actually picked
// (with its precedence source, Factory#272), the prompt-injection scan verdict
// (Factory#273), the dependency-review signal + findings (TFactory#650), and
// the judge-vote split behind a verdict (TFactory#649).
//
// Everything is ADDITIVE and GUARDED: with none of the new fields present the
// component renders nothing, so an old-envelope task looks exactly as before.

import type { ServiceState } from "./api";

export default function StageGates({ extra }: { extra: ServiceState["extra"] }) {
  if (!extra) return null;
  const tier = extra.routing?.tier;
  const tierSource = extra.routing?.tier_source;
  const scan = extra.injection_scan;
  const dep = extra.dependency_review;
  const votes = extra.votes;
  const voteRows = votes?.votes ?? [];
  const hasVotes =
    votes != null && (voteRows.length > 0 || votes.majority != null || votes.dissent != null);
  if (!tier && !scan && !dep && !hasVotes) return null;

  const depFindings = dep?.findings ?? [];
  const depTip = depFindings
    .map((f) => [f.package, f.severity, f.reason].filter(Boolean).join(" - "))
    .join("\n");

  return (
    <div className="td-stage-gates" data-testid="td-stage-gates">
      {tier && (
        <span
          className="td-gate td-gate--tier"
          title={tierSource ? `Routing tier picked by: ${tierSource}` : "Routing tier"}
        >
          tier: {tier}
        </span>
      )}
      {scan?.verdict && (
        <span
          className={`td-gate ${scan.verdict === "flagged" ? "td-gate--bad" : "td-gate--ok"}`}
          title={scan.reason ?? ""}
        >
          injection scan: {scan.verdict}
        </span>
      )}
      {dep?.status && (
        <span
          className={`td-gate ${dep.status === "fail" ? "td-gate--bad" : dep.status === "warn" ? "td-gate--warn" : "td-gate--ok"}`}
          title={depTip}
        >
          dependency review: {dep.status}
          {depFindings.length > 0 &&
            ` (${String(depFindings.length)} finding${depFindings.length === 1 ? "" : "s"})`}
        </span>
      )}
      {hasVotes && (
        <details className="td-votes">
          <summary>
            judge votes
            {votes.majority != null && `: ${String(votes.majority)} majority`}
            {votes.dissent != null && ` / ${String(votes.dissent)} dissent`}
            {votes.verdict ? ` (${votes.verdict})` : ""}
          </summary>
          {voteRows.length > 0 && (
            <ul>
              {voteRows.map((v, i) => (
                <li key={i}>
                  <span>{v.judge ?? v.model ?? `judge ${String(i + 1)}`}</span>
                  <span className={`td-vote td-vote--${v.verdict === "fail" ? "fail" : "pass"}`}>
                    {v.verdict ?? "-"}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </details>
      )}
    </div>
  );
}
