# CFactory's recorded divergences from the shared standard

**This file is LOCAL to CFactory.** Everything else in `standards/` is vendored
byte-for-byte from the Factory hub and compared by the drift gate in
`.github/workflows/code-quality.yml`; this file is not vendored and not compared.
It exists because the alternative to a recorded exemption is an unrecorded one,
and an unrecorded divergence is indistinguishable from an accident (Factory#513).

Each entry names the rule it diverges from, the reason, and what would have to
change to close it. An entry with no route back is a decision, not a backlog
item, and says so.

---

## 1. `eslint --max-warnings 239`, not `0`

**Diverges from:** coding-standards.md rule 2.2 (`eslint --max-warnings=0`).

**Where:** `apps/frontend-web/package.json`, the `lint` script.

**Reason.** The frontend predates the standard and carries a warning backlog.
Rule 4.6 makes adoption a ratchet rather than a big-bang rewrite, and a numeric
cap is the ratchet: it cannot rise, so the backlog can only shrink. Setting it
to `0` today would not raise the bar, it would turn the gate off by making every
PR red, and a gate everyone bypasses is worse than a cap everyone respects.

**Route back:** lower the number as warnings are cleared, to `0`. The cap is
tighten-only in exactly the sense rule 4.6 describes - raising it is a
regression, and lowering it is the work.

## 2. Prettier checks an explicit allowlist, not the whole tree

**Diverges from:** coding-standards.md rule 2.7 (Prettier owns formatting).

**Where:** `apps/frontend-web/package.json`, `format:check` runs over four named
files rather than `src/**`.

**Reason.** `src/index.css` is hand-authored, and its layout carries meaning
Prettier does not preserve - running `prettier --write` over it reflows the file
and loses that structure. The rest of `src/**` is simply not yet formatted, which
is the same ratchet story as entry 1.

**Route back:** widen the allowlist file by file as each is formatted, until it
is `src/**` minus a named `index.css` exclusion. The `index.css` exclusion is a
DECISION and is expected to remain; the narrow allowlist around it is the
backlog. Those two are separate and should not be closed as one.

---

## What is NOT an exemption

The Python half of the standard applies here in full and is enforced: this repo
has 162 Python files, `standards/ruff.toml` and `standards/mypy.ini` are vendored
and drift-gated, and the diff-scoped ratchet holds new and touched code to them.
Factory#513 originally supposed the Python configs were "genuinely not
applicable" to CFactory; they are, and they were already vendored - at the repo
root, with the pin in a workflow, which is what #513 actually fixed.

`standards/tsconfig.base.json` **is adopted in full** as of Factory#546, so it is
not an exemption either. `apps/frontend-web/tsconfig.json` extends the vendored
baseline and re-opens nothing; the 88 errors that adoption surfaced (the 87 #546
measured, plus one the tree gained in between) are fixed rather than suppressed.
No `any`, no `@ts-expect-error`, no new non-null assertion was used to reach
zero - counted before and after, all still zero.

Two gates hold it, because the config alone proves nothing:

* `npm run typecheck` (`test.yml`, every PR) fails on a real type error.
* `tests/test_tsconfig_tightens.py` fails if the child config drops its
  `extends` or sets any baseline flag back to `false`. That second case is the
  one worth naming: `tsc` stays GREEN when a hole is re-opened, because a
  re-opened hole is exactly a thing that makes errors go away. Verified by
  mutation both ways.
