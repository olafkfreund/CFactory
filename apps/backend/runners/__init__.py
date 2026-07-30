"""Vendored fleet runner layers.

Not CFactory code: everything under here is carried byte-for-byte from another
repo and guarded by a CI drift gate. It lives OUTSIDE ``apps/backend/cfactory``
on purpose — the ruff/mypy ratchet and ``ruff format --check`` are scoped to that
package, so our formatters can never rewrite a vendored file and break the gate.

See ``runners/github/__init__.py`` for what is vendored and from where.
"""
