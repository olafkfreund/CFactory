import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import ErrorBoundary from "./ErrorBoundary";

// Error boundary (#146). The legacy server renderer used by the other frontend
// tests (react-dom/server, no jsdom) does not invoke class error boundaries, so
// we can't drive a live catch here — instead we assert the two pieces that DO
// run in this environment: the static error→state mapping (what React calls to
// enter the fallback) and that a healthy child passes through untouched. The
// rendered fallback card itself is verified visually in the PR screenshot.
describe("ErrorBoundary", () => {
  it("maps a thrown error into fallback state via getDerivedStateFromError", () => {
    const err = new Error("kaboom in a view");
    expect(ErrorBoundary.getDerivedStateFromError(err)).toEqual({ error: err });
  });

  it("passes a healthy child through unchanged", () => {
    const html = renderToStaticMarkup(
      <ErrorBoundary>
        <div>all good</div>
      </ErrorBoundary>,
    );
    expect(html).toBe("<div>all good</div>");
  });
});
