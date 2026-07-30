import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ActionResult } from "./TaskActions";
import { ExecuteResultSchema } from "./api";

// #244: a refused action has to say WHY.
//
// `/api/actions/execute` answers 200 with an envelope, so the upstream refusal
// arrives as `{status_code: 409, ok: false, body: {...}}` — the backend sets the
// top-level `error` only for transport/validation failures, never for an
// upstream non-2xx. The banner used to render `error` alone, so a gate-refused
// approve showed a status code and an endpoint and nothing about the cause.
//
// All four factories are FastAPI, so a refusal body is `{"detail": "..."}` (or
// the RFC-0020 §3.7 refused-stage `{"detail": {reason, message}}`). Payloads go
// through ExecuteResultSchema here so the test also pins the wire contract.

const noop = () => {};

const render = (envelope: unknown) =>
  renderToStaticMarkup(
    <ActionResult result={ExecuteResultSchema.parse(envelope)} onClose={noop} />,
  );

describe("ActionResult failure banner (#244)", () => {
  it("names the reason a gate-blocked plan refused the approve", () => {
    const html = render({
      status_code: 409,
      ok: false,
      body: { detail: "cannot approve a plan that failed the automated review gates" },
      steps: [
        {
          method: "POST",
          endpoint: "/api/plan/sessions/001-add-an-is-palindrome-helper-python/approve",
          status_code: 409,
          ok: false,
        },
      ],
    });
    expect(html).toContain("HTTP 409");
    expect(html).toContain("cannot approve a plan that failed the automated review gates");
  });

  it("shows the human sentence of a refused-stage {reason, message} detail", () => {
    const html = render({
      status_code: 409,
      ok: false,
      body: { detail: { reason: "gates_failed", message: "security lens scored 0.70 of 0.75" } },
    });
    expect(html).toContain("security lens scored 0.70 of 0.75");
    // The machine-readable code is for branching, not for a human banner.
    expect(html).not.toContain("gates_failed");
  });

  it("prefers the backend's own error over the body", () => {
    const html = render({
      status_code: 0,
      ok: false,
      error: "unknown target_service: 'nope'",
      body: { detail: "not this one" },
    });
    expect(html).toContain("unknown target_service");
    expect(html).not.toContain("not this one");
  });

  it("falls back to the status code when the body explains nothing", () => {
    const html = render({ status_code: 500, ok: false, body: "<html>gateway</html>" });
    expect(html).toContain("HTTP 500");
    expect(html).toContain("act-result--fail");
  });

  it("says nothing extra on success", () => {
    const html = render({ status_code: 200, ok: true, body: { detail: "ignored when ok" } });
    expect(html).toContain("Done.");
    expect(html).not.toContain("ignored when ok");
    expect(html).not.toContain("act-result--fail");
  });
});
