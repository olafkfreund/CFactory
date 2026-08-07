import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ChainLine } from "./AuditView";
import { verdictPill } from "./auditChain";
import { ChainReportSchema, type ChainReport } from "./api";

describe("verdictPill", () => {
  it("greens only on ok", () => {
    expect(verdictPill("ok")).toEqual({ cls: "ok", label: "chain intact" });
  });

  it("reds on tamper evidence", () => {
    expect(verdictPill("tampered").cls).toBe("fail");
  });

  it("ambers an unexplained fork rather than calling it tampering", () => {
    // Every HMAC in a fork recomputes (#306) — red would be a lie, green would
    // hide a regression of the #310 append serialisation.
    expect(verdictPill("forked").cls).toBe("warn");
  });

  it("never greens a verdict it has never heard of", () => {
    // #431: an unknown value renders as itself and is not treated as health.
    expect(verdictPill("quarantined")).toEqual({ cls: "warn", label: "quarantined" });
  });
});

describe("ChainReportSchema", () => {
  it("accepts a finding kind this build does not know", () => {
    const parsed = ChainReportSchema.parse({
      verdict: "tampered",
      rows: 5372,
      checked_at: "2026-08-07T10:00:00Z",
      findings: [{ id: 2178, kind: "some-future-kind", detail: "x" }],
      acknowledged_forks: [],
    });
    expect(parsed.findings[0].kind).toBe("some-future-kind");
  });
});

// The four payloads below are VERBATIM from the live cockpit's audit table
// (5,378 rows, the standing fork at entry 2178), served by this branch's
// `GET /api/audit/chain` inside the pod against a snapshot of it. They are here
// so the sentence an operator reads is asserted against real numbers, not
// invented ones.
const LIVE_UNACKNOWLEDGED: ChainReport = {
  verdict: "forked",
  rows: 5378,
  checked_at: "2026-08-07T09:09:37.670369Z",
  findings: [
    {
      id: 2178,
      kind: "forked",
      detail:
        "shares parent a170ebe79c... with [2177]; every HMAC involved is valid, " +
        "so this is a concurrent append (#306)",
    },
  ],
  acknowledged_forks: [],
};

const LIVE_ACKNOWLEDGED: ChainReport = {
  ...LIVE_UNACKNOWLEDGED,
  verdict: "ok",
  checked_at: "2026-08-07T09:10:43.390089Z",
  findings: [],
  acknowledged_forks: [2178],
};

const LIVE_TAMPERED: ChainReport = {
  ...LIVE_ACKNOWLEDGED,
  verdict: "tampered",
  checked_at: "2026-08-07T09:11:31.322127Z",
  findings: [
    { id: 5000, kind: "mutated", detail: "entry_hash is not the HMAC of this row's fields" },
  ],
};

describe("ChainLine, on the live cockpit's numbers", () => {
  it("states the known fork without standing red", () => {
    const html = renderToStaticMarkup(<ChainLine chain={LIVE_ACKNOWLEDGED} err={null} />);
    expect(html).toContain("status-pill ok");
    expect(html).toContain("5,378 rows scanned, every HMAC recomputed");
    expect(html).toContain("0 tamper findings");
    expect(html).toContain("1 known fork (write race, #306)");
    // Green, but not silent: an acknowledged fork is still on screen.
    expect(html).not.toContain("banner--error");
  });

  it("reds on a real mutation even while the known fork is acknowledged", () => {
    const html = renderToStaticMarkup(<ChainLine chain={LIVE_TAMPERED} err={null} />);
    expect(html).toContain("status-pill fail");
    expect(html).toContain("TAMPER EVIDENCE");
    expect(html).toContain("1 tamper finding");
    expect(html).toContain("entry 5000: mutated");
    expect(html).toContain("1 known fork (write race, #306)");
  });

  it("ambers a fork nobody has explained", () => {
    const html = renderToStaticMarkup(<ChainLine chain={LIVE_UNACKNOWLEDGED} err={null} />);
    expect(html).toContain("status-pill warn");
    expect(html).toContain("1 unexplained fork");
    expect(html).toContain("0 tamper findings");
  });

  it("never reads as healthy when the check itself failed", () => {
    // The #306 failure mode was a control nobody could see the state of. A
    // surface that renders green while it knows nothing repeats it.
    const html = renderToStaticMarkup(<ChainLine chain={null} err="audit chain error: HTTP 503" />);
    expect(html).toContain("check unavailable");
    expect(html).toContain("the chain state is unknown, not healthy");
    expect(html).not.toContain("status-pill ok");
  });

  it("does not read as healthy before the check has answered", () => {
    const html = renderToStaticMarkup(<ChainLine chain={null} err={null} />);
    expect(html).toContain("checking");
    expect(html).not.toContain("status-pill ok");
  });
});
