"""Tests for the editor token hand-off (#73).

Covers the pure helpers in ``cfactory.connect`` (scheme allow-list + redirect
builder), ``KeyStore.preferred_key`` selection, and the ``GET /connect/vscode``
endpoint behaviour: 302 with token + echoed state, OPEN-mode empty token, and
rejection of disallowed redirect schemes.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from cfactory.app import create_app
from cfactory.auth import KeyStore, keystore_dep
from cfactory.connect import build_redirect, validate_redirect

CALLBACK = "vscode://olafkfreund.factory-vscode/auth-callback"


def _client(keys: dict[str, set[str]] | None) -> TestClient:
    app = create_app()
    if keys is not None:
        app.dependency_overrides[keystore_dep] = lambda: KeyStore(keys)
    # Don't chase the vscode:// redirect — httpx can't, and we want to inspect it.
    return TestClient(app, follow_redirects=False)


# --------------------------------------------------------------------------
# validate_redirect
# --------------------------------------------------------------------------

def test_validate_redirect_accepts_editor_schemes():
    for scheme in ("vscode", "vscode-insiders", "vscodium", "cursor", "windsurf", "trae", "positron"):
        assert validate_redirect(f"{scheme}://ext/auth-callback") is None


def test_validate_redirect_rejects_non_editor_schemes():
    for bad in (
        "https://evil.example/steal",
        "http://localhost/x",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,x",
        "ftp://host/x",
    ):
        assert validate_redirect(bad) is not None


def test_validate_redirect_rejects_missing_or_schemeless():
    assert validate_redirect(None) is not None
    assert validate_redirect("") is not None
    assert validate_redirect("   ") is not None
    assert validate_redirect("/just/a/path") is not None


# --------------------------------------------------------------------------
# build_redirect
# --------------------------------------------------------------------------

def test_build_redirect_appends_token_and_state():
    url = build_redirect(CALLBACK, "tok-123", "nonce-abc")
    parts = urlsplit(url)
    assert parts.scheme == "vscode"
    q = parse_qs(parts.query)
    assert q["token"] == ["tok-123"]
    assert q["state"] == ["nonce-abc"]


def test_build_redirect_preserves_existing_query():
    url = build_redirect("vscode://ext/cb?foo=bar", "t", "s")
    q = parse_qs(urlsplit(url).query)
    assert q["foo"] == ["bar"]
    assert q["token"] == ["t"]
    assert q["state"] == ["s"]


def test_build_redirect_percent_encodes_values():
    url = build_redirect(CALLBACK, "a b/c&d", "x=y")
    # Raw token chars must be encoded, not leak into the query structure.
    assert "a b/c&d" not in url
    q = parse_qs(urlsplit(url).query)
    assert q["token"] == ["a b/c&d"]
    assert q["state"] == ["x=y"]


# --------------------------------------------------------------------------
# KeyStore.preferred_key
# --------------------------------------------------------------------------

def test_preferred_key_open_mode_is_none():
    assert KeyStore({}).preferred_key() is None


def test_preferred_key_prefers_write_over_read():
    ks = KeyStore({"ro": {"read"}, "rw": {"read", "write"}})
    assert ks.preferred_key() == "rw"


def test_preferred_key_falls_back_to_read_then_any():
    assert KeyStore({"ro": {"read"}}).preferred_key() == "ro"
    assert KeyStore({"weird": set()}).preferred_key() == "weird"


# --------------------------------------------------------------------------
# GET /connect/vscode
# --------------------------------------------------------------------------

def test_connect_open_mode_redirects_with_empty_token():
    client = _client(None)  # OPEN: no keys configured
    resp = client.get("/connect/vscode", params={"redirect": CALLBACK, "state": "abc"})
    assert resp.status_code == 302
    loc = resp.headers["location"]
    # keep_blank_values so the empty open-mode token (token=) isn't dropped.
    q = parse_qs(urlsplit(loc).query, keep_blank_values=True)
    assert q["state"] == ["abc"]
    assert q["token"] == [""]  # no token in open mode


def test_connect_configured_hands_back_preferred_key():
    client = _client({"ro": {"read"}, "rw": {"read", "write"}})
    resp = client.get("/connect/vscode", params={"redirect": CALLBACK, "state": "n1"})
    assert resp.status_code == 302
    q = parse_qs(urlsplit(resp.headers["location"]).query)
    assert q["token"] == ["rw"]
    assert q["state"] == ["n1"]


def test_connect_rejects_disallowed_scheme():
    client = _client(None)
    resp = client.get(
        "/connect/vscode",
        params={"redirect": "https://evil.example/grab", "state": "abc"},
    )
    assert resp.status_code == 400
    assert "scheme" in resp.text.lower()


def test_connect_rejects_empty_state():
    client = _client(None)
    resp = client.get("/connect/vscode", params={"redirect": CALLBACK, "state": "  "})
    assert resp.status_code == 400


def test_connect_requires_redirect_and_state():
    client = _client(None)
    assert client.get("/connect/vscode", params={"state": "abc"}).status_code == 422
    assert client.get("/connect/vscode", params={"redirect": CALLBACK}).status_code == 422
