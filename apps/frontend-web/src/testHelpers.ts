// Helpers for the frontend test files (Factory#546).
//
// `noUncheckedIndexedAccess` types `xs[0]` as `T | undefined`, which is correct:
// nothing about an array literal's type says index 0 exists. Tests index arrays
// constantly, and there are two honest ways to satisfy that:
//
//   * Inside an `expect(...)`, optional-chain it. The assertion IS the check —
//     an empty array makes the expectation fail, which is what a test should do.
//   * Anywhere the value has to be non-optional (a component prop, an object
//     spread, a value used further down), use `at()` below. It fails on the spot
//     with the index and the length, rather than propagating an `undefined` that
//     surfaces as a confusing error three frames later.
//
// A non-null assertion (`xs[0]!`) would do neither: it silences the compiler and
// hands `undefined` downstream. It is also a lint warning here, and the gate runs
// with a fixed `--max-warnings` cap.

/** Index into an array, throwing rather than yielding `undefined`. */
export function at<T>(xs: readonly T[], i: number): T {
  const v = xs[i];
  if (v === undefined) {
    throw new Error(`index ${String(i)} is out of range (length ${String(xs.length)})`);
  }
  return v;
}
