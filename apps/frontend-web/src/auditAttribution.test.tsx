/** What the Audit view says about who it can NAME (#251 part b).
 *
 * The ACTOR column reads `unattributed:key-<digest>` both when no IdP is
 * configured (no row will ever name a person) and when one is but tokens are
 * quietly failing to verify. A compliance reader cannot tell those apart from
 * the column alone, and this flow is presented as Article 14 human oversight.
 */
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AttributionLine } from "./AuditView";

const render = (attribution?: string) =>
  renderToStaticMarkup(<AttributionLine attribution={attribution} />);

describe("AttributionLine", () => {
  it("says the trail names a client, not a person, when no issuer is configured", () => {
    const html = render("unattributed");
    expect(html).toContain("names a client, not a person");
    expect(html).toContain("who approved this");
  });

  it("stays silent when the trail can name a person", () => {
    expect(render("oidc")).toBe("");
  });

  it("stays silent when the backend does not send the field", () => {
    // Frontend and backend images roll separately. An older backend omits
    // `attribution`; absent means "not stated", so claiming either way would
    // be inventing an answer.
    expect(render(undefined)).toBe("");
  });

  it("stays silent on a value this build has never heard of", () => {
    // #431: an unknown token must not be coerced into a plausible one. A
    // stricter future mode (say, per-request attestation) is not a reason to
    // render "names a client" at a deployment that may well name people.
    expect(render("mtls-attested")).toBe("");
  });
});
