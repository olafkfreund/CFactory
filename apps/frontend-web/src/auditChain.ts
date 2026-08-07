/** How the HMAC-chain verdict (#309) is presented on the Audit view. */

/** Map a verdict to a status-pill class and the words next to it.
 *
 * `forked` is amber, not red: an unacknowledged concurrent append (#306) means
 * the append serialisation regressed, which wants a human — but every HMAC in it
 * still recomputes, so calling it tampering would be a lie. Red is reserved for
 * evidence that the record itself changed, which is the only thing that should
 * ever make this surface shout.
 *
 * An unrecognised verdict is shown as itself and treated as un-green (#431): a
 * value this build has never heard of is not evidence of health. */
export function verdictPill(verdict: string): { cls: string; label: string } {
  if (verdict === "ok") return { cls: "ok", label: "chain intact" };
  if (verdict === "tampered") return { cls: "fail", label: "TAMPER EVIDENCE" };
  if (verdict === "forked") return { cls: "warn", label: "unexplained fork" };
  return { cls: "warn", label: verdict };
}
