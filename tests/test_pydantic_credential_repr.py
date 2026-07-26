"""Guard: no credential-bearing pydantic field may render its secret.

Factory#377, the pydantic half of Factory#372. ``cfactory.routes_git_config``
declared ``credential: str`` on a ``BaseModel``, so every rendering surface
printed the PAT verbatim::

    >>> repr(GitCredentialUpdate(credential="glpat-SUPERSECRET123"))
    "GitCredentialUpdate(credential='glpat-SUPERSECRET123')"

A FastAPI request model is not a debug-only surface: it is the object bound to a
local in the traceback frame when the handler's downstream call raises, so an
unhandled exception writes the credential into the log, the error sink and any
crash reporter attached to them. RFC-0020 §3.4 promises this credential is
"never logged"; that promise was false for as long as the model was reprable.

Two guards, mirroring ``tests/test_provider_credential_repr.py`` in the hub:

* :func:`test_no_credential_field_renders_its_secret` is the generic one. It
  parses every module under ``apps/backend/cfactory`` with :mod:`ast` -- no
  imports, no third-party dependency -- and fails any credential-named field on
  a ``BaseModel``/``BaseSettings`` that is neither ``SecretStr`` nor
  ``repr=False``. A model added next month is covered without anyone
  remembering this file exists. That is the whole point: pinning today's models
  by name would pass forever while the next ``token: str`` sails in.
* The behavioural tests below construct the real models with a fake secret and
  prove the secret is absent from ``repr()``, ``str()`` and ``model_dump()`` --
  and, just as importantly, that the true value still reaches the point of use.
  A credential masked everywhere including where it is needed is a broken
  feature, not a secure one.

WHICH FIX APPLIES WHERE
    ``SecretStr`` is preferred and is what request models use: it masks
    ``repr()``, ``str()``, ``model_dump()`` and ``model_dump_json()`` alike,
    where ``repr=False`` masks only the first two and still leaks through a
    ``model_dump()`` that lands in a log line.

    ``repr=False`` is accepted only where ``SecretStr`` would break the feature:
    :class:`cfactory.config.Settings` interpolates its tokens directly
    (``f"Bearer {settings.upstream_token}"``), and under ``SecretStr`` that line
    still compiles while silently sending ``Bearer **********``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from cfactory.config import Settings
from cfactory.routes_git_config import GitCredentialUpdate

_PACKAGE_DIR = Path(__file__).resolve().parents[1] / "apps" / "backend" / "cfactory"

# Vendored from the hub (shared/factory-contracts); it is guarded there, and its
# `*_tokens` fields are usage counts rather than credentials.
_VENDORED = {"_contracts"}

# Words that name a secret. Checked against the FINAL underscore-separated
# segment, because the last segment is what the field actually holds:
# `github_token` is a token, but `token_file` is a path, `token_env` is the name
# of an environment variable and `api_key_preview` is a deliberately masked
# prefix. Matching anywhere in the name flags all three and trains people to
# suppress the guard, which is worse than not having it.
_SECRET_WORDS = frozenset(
    {
        "apikey",
        "credential",
        "credentials",
        "passphrase",
        "passwd",
        "password",
        "pat",
        "secret",
        "secrets",
        "token",
    }
)

# `key` alone is a lookup key in this codebase (`correlation_key`, `card_key`),
# so it counts only when the preceding segment makes it a credential.
_KEY_QUALIFIERS = frozenset(
    {"access", "api", "credential", "encryption", "master", "private", "secret", "signing", "ssh"}
)

_MODEL_BASES = frozenset({"BaseModel", "BaseSettings"})


def _is_secret_name(name: str) -> bool:
    segments = name.lower().strip("_").split("_")
    if segments[-1] in _SECRET_WORDS:
        return True
    return segments[-1] in {"key", "keys"} and len(segments) > 1 and segments[-2] in _KEY_QUALIFIERS


def _is_str_annotation(node: ast.expr) -> bool:
    """True for ``str``/``SecretStr`` and unions thereof, false for containers.

    ``credentials: dict[str, Entry]`` mentions ``str`` but holds no secret of its
    own -- the secret, if any, lives on ``Entry`` and is guarded there.
    """
    text = ast.unparse(node)
    if any(c in text for c in ("dict[", "Dict[", "list[", "List[", "Mapping[")):
        return False
    names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
    return bool(names & {"str", "SecretStr"})


def _has_repr_false(default: ast.expr | None) -> bool:
    """True when the assigned default is ``Field(..., repr=False)``."""
    if not isinstance(default, ast.Call):
        return False
    func = default.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name != "Field":
        return False
    return any(
        kw.arg == "repr" and isinstance(kw.value, ast.Constant) and kw.value.value is False
        for kw in default.keywords
    )


def _is_pydantic_model(node: ast.ClassDef) -> bool:
    for base in node.bases:
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        if name in _MODEL_BASES:
            return True
    return False


def _package_modules() -> list[Path]:
    return sorted(p for p in _PACKAGE_DIR.rglob("*.py") if not _VENDORED & set(p.parts))


def test_package_is_present() -> None:
    """Fail loudly rather than passing vacuously if the tree moves."""
    assert _package_modules(), f"no modules found under {_PACKAGE_DIR}"


def test_no_credential_field_renders_its_secret() -> None:
    """Every credential-named pydantic field is ``SecretStr`` or ``repr=False``."""
    offenders: list[str] = []

    for module in _package_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not _is_pydantic_model(node):
                continue
            for stmt in node.body:
                if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                    continue
                field = stmt.target.id
                if not _is_secret_name(field) or not _is_str_annotation(stmt.annotation):
                    continue
                if "SecretStr" in ast.unparse(stmt.annotation) or _has_repr_false(stmt.value):
                    continue
                offenders.append(
                    f"{module.relative_to(_PACKAGE_DIR)}:{stmt.lineno} {node.name}.{field} "
                    f"-- credential renders in repr()/model_dump(); annotate it "
                    f"`SecretStr`, or `Field(..., repr=False)` if the value must "
                    f"stay a plain str at the point of use"
                )

    assert not offenders, "credential-bearing pydantic fields leak their secret:\n  " + "\n  ".join(
        offenders
    )


def test_git_credential_update_masks_the_pat() -> None:
    """The model from the live report: masked on every rendering surface."""
    secret = "glpat-SUPERSECRET123"  # noqa: S105 - fake, this is the leak probe
    body = GitCredentialUpdate(credential=secret)

    assert secret not in repr(body)
    assert secret not in str(body)
    assert secret not in str(body.model_dump())
    assert secret not in body.model_dump_json()


def test_git_credential_update_still_yields_the_real_pat() -> None:
    """Masking must not reach the point of use, or the feature is broken.

    ``routes_git_config.put_git_credential`` hands this value to
    ``git_config_ops.set_git_credential``, which encrypts and stores it. If the
    handler stored ``'**********'`` the credential would be masked *and* useless.
    """
    secret = "glpat-SUPERSECRET123"  # noqa: S105 - fake, this is the leak probe
    assert GitCredentialUpdate(credential=secret).credential.get_secret_value() == secret


def test_settings_does_not_render_its_credentials() -> None:
    """Settings is a process singleton in every configuration traceback frame."""
    secret = "upstream-SUPERSECRET456"  # noqa: S105 - fake, this is the leak probe
    settings = Settings(
        upstream_token=secret,
        aifactory_token=secret,
        github_token=secret,
        git_provider_token=secret,
        ollama_api_key=secret,
        api_keys=secret,
        mcp_secret=secret,
        credential_key=secret,
        audit_hmac_secret=secret,
    )

    assert secret not in repr(settings)
    assert secret not in str(settings)
    # repr=False hides the field from rendering only. The value must survive, or
    # every outbound `Authorization: Bearer` header would be built from nothing.
    assert settings.upstream_token == secret
    assert settings.credential_key == secret
    assert settings.audit_hmac_secret == secret
