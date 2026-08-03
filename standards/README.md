# Factory shared coding standards

This directory is the **single source of truth** for the fleet's code-quality
bar. See [`coding-standards.md`](./coding-standards.md) for the normative rules.

## Files

| File | What it is |
|---|---|
| `coding-standards.md` | The normative standard (Python, TypeScript, cross-cutting, CI). |
| `ruff.toml` | Shared Python lint baseline. Services `extend` it; tighten-only. |
| `mypy.ini` | Shared `mypy --strict` baseline. Services inherit; tighten-only. |
| `tsconfig.base.json` | Shared TypeScript strictness baseline. Services `extends` it; tighten-only. |
| `.editorconfig` | Copy to each repo root. |

## How services consume it

**Today (until `factory-core` is published - epic Factory#154):** each service
vendors a pinned copy and a CI **drift gate** diffs the copy against this hub at a
pinned SHA, so a service cannot silently fork the baseline. Per-service configs
may only TIGHTEN (a config-lint check enforces this).

### The vendored contract

Every service vendors the same four files into its own `standards/`, plus the
pin:

| Vendored file | Compared how |
|---|---|
| `ruff.toml` | body only (leading comment/blank lines stripped both sides, so a copy may carry a provenance header) |
| `mypy.ini` | body only |
| `.editorconfig` | body only |
| `coding-standards.md` | **byte-exact** - see below |
| `.hub-sha` | not compared; it *is* the pin |

`coding-standards.md` is compared byte-exact and carries no header. The
body-only comparator strips lines starting with `#`, which in Markdown is every
heading - 58 of this file's 198 lines - so a stripped compare would let headings
and whole section titles drift unnoticed. Byte-exact is both stricter and
simpler, and costs nothing because the copy is a plain `cp`.

`standards/.hub-sha` is the pin for this directory, and `.hub-sha` is the one
pin filename for any vendored DIRECTORY fleet-wide. Tooling that wants to know
which hub commit a service's `standards/` is on reads that path in every repo,
with no per-service special case.

The rule is scoped to directories deliberately (Factory#514). It binds a
directory whose contents ARE the vendored set, which is what makes "beside it" a
defined location. Sets vendored as individual files scattered through a service
tree have no such directory; they pin in their gate's workflow, and that is
permitted only while `scripts/check_pin_freshness.py` declares the gate and reads
those pins fleet-wide. A workflow SHA no tooling reads is still not a pin.

### Re-vendoring after a hub change

```sh
HUB=<hub commit sha>
for f in ruff.toml mypy.ini .editorconfig coding-standards.md; do
  curl -fsSL "https://raw.githubusercontent.com/olafkfreund/Factory/$HUB/standards/$f" -o "standards/$f"
done
printf '%s\n' "$HUB" > standards/.hub-sha
```

A hub change is inert until a service bumps its pin; that bump is deliberate and
per-service. Never register a file in a service's gate before that service has
the file.

### The gate fails closed

Per standard rule 4.7, the drift gate must fail when it cannot reach the hub.
A skipped diff is not a passed diff, and a job named "blocking" that green-lights
on an unreachable baseline blocks nothing. Do not wrap the hub checkout in
`continue-on-error: true`, and do not `continue` past a failed download.

**Target:** publish these as part of a versioned `factory-core` / `factory-standards`
package consumed via pinned semver, so the baseline is a dependency, not a copy.

## Tighten-only rule

A service config may add rules or lower numeric caps. It may **not** remove a
selected rule category, raise a complexity cap, or disable a gate. This keeps the
fleet bar monotonic.
