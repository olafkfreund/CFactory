# factory_common (vendored from the Factory hub)

This package is a **byte-identical vendored copy** of the canonical
`shared/factory-common/factory_common/` layer in the
[Factory hub](https://github.com/olafkfreund/Factory) - the single source of
truth for the fleet's deduped, stdlib-only utility primitives (epic Factory#154,
issue Factory#161):

- `factory_common.logsafe` - `sanitize_log()`, the CWE-117 / `py/log-injection`
  fix: escapes CR/LF and control characters in a value before it reaches a log
  message, so untrusted input cannot forge a log record.
- `factory_common.secrets` - the canonical secret-pattern table + `redact()` /
  `scan()` / `contains_secret()`.
- `factory_common.http` - the Cloudflare-friendly typed `urllib` JSON client.
- `factory_common.url_safety` - `assert_safe_outbound_url()`, the fleet's one
  SSRF guard for an outbound URL a caller chose.
- `factory_common.client_errors` - `InputRejectedError`, the exception
  `assert_safe_outbound_url` raises. `cfactory/error_ref.py` RE-EXPORTS this
  class rather than defining its own: `client_error()` gates on `isinstance`,
  so two same-named classes would silently downgrade a safe message to a
  correlation id (CFactory#414).

It sits beside `cfactory/` rather than inside it because the modules use
absolute imports (`from factory_common.http import ...`), so the package must be
top-level. `apps/backend` is already the import root (`PYTHONPATH=apps/backend`
in the test workflow), so `from factory_common.logsafe import sanitize_log`
resolves without any path juggling - the same layout AIFactory uses.

## Why vendored (not pip-installed)

The fleet vendors shared layers byte-for-byte behind a drift gate rather than
publishing a package. This keeps CI and the coder pod dependency-free (the layer
is stdlib-only and importable anywhere) while a gate guarantees the copy cannot
silently drift from the hub.

## Do not edit here

These files are owned by the hub. To change the behaviour, land the change in
`shared/factory-common/` in the Factory hub first, then re-vendor here and bump
`.hub-sha` to the new hub commit.

## Pinned hub commit

See `.hub-sha`.

## Consumers in this repo

`sanitize_log` guards the untrusted values interpolated into log messages in
`cfactory/issue_import.py`, `cfactory/mcp.py` and `cfactory/routes_services.py`;
see `apps/backend/tests/test_logsafe_vendored.py` for the behaviour lock.

`assert_safe_outbound_url` has exactly one caller, `cfactory/git_base_url.py`,
which the three git-connection read sites route through -- the provider
factory, the install callback and the install-token mint. See
`tests/test_git_base_url_ssrf.py`, which drives those read sites rather than
the helper. `InputRejectedError` is re-exported by `cfactory/error_ref.py` and
used across the routes layer.
