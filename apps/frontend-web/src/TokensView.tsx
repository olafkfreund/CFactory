import { useEffect, useState } from "react";
import { fetchTokens, type TokenTotals } from "./api";
import { useCountUp } from "./motion";

const SERVICES = [
  { key: "pfactory", label: "Plan", svc: "PFactory", cls: "plan" },
  { key: "aifactory", label: "Code", svc: "AIFactory", cls: "code" },
  { key: "tfactory", label: "Test", svc: "TFactory", cls: "test" },
] as const;

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

export default function TokensView({ reloadSignal }: { reloadSignal: number }) {
  const [data, setData] = useState<TokenTotals | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetchTokens().then((d) => { setData(d); setErr(null); }).catch((e: unknown) =>
      setErr(e instanceof Error ? e.message : String(e)));
  }, [reloadSignal]);

  const totalTokens = useCountUp(data?.total.total_tokens ?? 0);
  const totalCostCents = useCountUp(Math.round((data?.total.cost_usd ?? 0) * 100));

  // Data-driven: which stages have not yet emitted an RFC-0001 usage block.
  const awaiting = data
    ? SERVICES.filter((s) => !data.by_service[s.key]?.instrumented).map((s) => s.svc)
    : [];

  return (
    <>
      <div className="page-head">
        <h1>Tokens &amp; cost</h1>
        <p>LLM usage across the pipeline, from the RFC-0001 <code>usage</code> block.</p>
      </div>
      {err && <div className="banner banner--error">{err}</div>}

      <div className="mc-stats">
        <div className="mc-stat"><div className="mc-stat-v" style={{ color: "var(--cyan)" }}>{fmtTokens(totalTokens)}</div><div className="mc-stat-l">Total tokens</div></div>
        <div className="mc-stat"><div className="mc-stat-v" style={{ color: "var(--code)" }}>${(totalCostCents / 100).toFixed(2)}</div><div className="mc-stat-l">Total cost</div></div>
        <div className="mc-stat"><div className="mc-stat-v" style={{ color: "var(--muted)" }}>{fmtTokens(data?.total.input_tokens ?? 0)}</div><div className="mc-stat-l">Input</div></div>
        <div className="mc-stat"><div className="mc-stat-v" style={{ color: "var(--muted)" }}>{fmtTokens(data?.total.output_tokens ?? 0)}</div><div className="mc-stat-l">Output</div></div>
      </div>

      <div className="svc-grid" style={{ marginBottom: "1.2rem" }}>
        {SERVICES.map((s) => {
          const t = data?.by_service[s.key];
          const on = t?.instrumented;
          return (
            <div className={`svc-card svc-card--${s.cls}`} key={s.key}>
              <div className="svc-top">
                <span className="svc-name">{s.svc}</span>
                <span className="svc-role-pill">{s.label}</span>
              </div>
              {on ? (
                <>
                  <div className="mc-stat-v" style={{ fontSize: "1.4rem" }}>{fmtTokens(t!.total_tokens)}</div>
                  <div className="svc-role">${t!.cost_usd.toFixed(2)} · {fmtTokens(t!.input_tokens)} in / {fmtTokens(t!.output_tokens)} out</div>
                </>
              ) : (
                <div className="svc-role tk-pending">not instrumented yet</div>
              )}
            </div>
          );
        })}
      </div>

      <div className="table">
        <div className="table-head" style={{ gridTemplateColumns: "1.2fr 2.4fr 1fr 0.8fr" }}>
          <span>WORK ITEM</span><span>TITLE</span><span className="ta-r">TOKENS</span><span className="ta-r">COST</span>
        </div>
        {!data || data.by_work_item.length === 0 ? (
          <div className="table-empty">No token usage recorded yet — services emit it as they finish instrumented runs.</div>
        ) : (
          data.by_work_item.map((w) => (
            <div className="table-row" key={w.correlation_key} style={{ gridTemplateColumns: "1.2fr 2.4fr 1fr 0.8fr" }}>
              <span className="t-key">#{w.correlation_key}</span>
              <span className="t-target">{w.title || "—"}</span>
              <span className="ta-r mono">{fmtTokens(w.total_tokens)}</span>
              <span className="ta-r mono">${w.cost_usd.toFixed(2)}</span>
            </div>
          ))
        )}
      </div>

      <p className="mc-note">
        {awaiting.length === 0
          ? "All three stages report real usage from the RFC-0001 usage block."
          : `Each stage reports tokens once it emits the RFC-0001 usage block — awaiting ${awaiting.join(" & ")}.`}
      </p>
    </>
  );
}
