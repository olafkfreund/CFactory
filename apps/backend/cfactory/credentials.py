"""Encrypted per-connection git credentials (RFC-0020 §3.4, phases 3 and 8).

Phase 2 made "which host and which project" a tenant resource and deliberately
stopped short of the credential, so a multi-tenant deployment still reached every
tenant's project with the *operator's* environment token. This module is the
other half: a credential a tenant owns, encrypted at rest, that no read API ever
returns.

**Envelope encryption, inside CFactory's own database.** A key-encryption key
(KEK) arrives from the environment as ``CFACTORY_CREDENTIAL_KEY``. It never
encrypts a credential directly: each record gets its own random 256-bit data key
(DEK), the credential is sealed with the DEK, and the DEK is wrapped with the
KEK. Both layers are AES-GCM — authenticated, so a wrong key or a tampered
record FAILS rather than returning plausible garbage.

Two properties fall out of that shape, and both are the reason for it:

* **Rotation never touches the plaintext.** Each row stores the ``key_version``
  that wrapped its DEK. Rotating means unwrapping the DEK with the old KEK and
  re-wrapping it with the new one — the sealed credential is not decrypted, not
  re-encrypted, and not read. See :func:`rewrap`.
* **A different backend later is a re-wrap, not a data migration.** ``key_version``
  is opaque text, so pointing it at a KMS/HSM-held KEK (Factory#314/#315, which
  are still open) changes where the *wrapping* key comes from and leaves the
  stored record format untouched.

**The tenant AND the connection are bound INTO the ciphertext.** Both AES-GCM
layers take the tenant id and the connection id as associated data, so a record
lifted from tenant B's row into tenant A's does not decrypt — and neither does
one lifted from tenant A's GitLab connection onto tenant A's GitHub connection.
The isolation is cryptographic, not only a WHERE clause. The WHERE clause is
still there; this is the guard for when it is wrong.

Phase 8 is what added the connection to that binding, because phase 8 is what
made a tenant able to have more than one. A record sealed before it — bound to
the tenant only — is marked :data:`LEGACY_AAD_VERSION` and re-sealed onto the
connection-bound binding the first time it is read (and at boot, see
:meth:`cfactory.cards.CardStore.adopt_legacy_git_config`). Nothing is decrypted
to disk to do it: the plaintext exists in memory for the length of one re-seal.

**Nothing here fails open.** No KEK configured means storing a credential is
refused with an error that says so; it never writes plaintext, and it never
derives a key from something that is not one. A KEK that cannot decrypt an
existing record yields *no credential*, which the board reports as
``credential_missing`` and keeps serving.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel
from sqlalchemy import DateTime, Index, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from .config import DEFAULT_TENANT, Settings
from .db import Base

logger = logging.getLogger(__name__)

# The environment variable carrying the KEK, named here so every error message
# and every doc page can quote one string.
KEY_ENV = "CFACTORY_CREDENTIAL_KEY"

# AES-256-GCM: a 32-byte key and a 96-bit nonce, which is the size GCM is
# specified for and the only one that keeps the counter construction safe.
_KEY_BYTES = 32
_NONCE_BYTES = 12

# The key id assumed when the operator supplies bare key material with no
# ``<id>:`` prefix — the single-key deployment, which is the common one.
_DEFAULT_KEY_ID = "v1"

# How long a key id may be. It is stored per record and echoed in the panel, so
# it is bounded like every other stored string here.
_KEY_ID_MAX = 32

# The associated-data shape a record was sealed under. 2 binds (tenant,
# connection) and is what every seal produces; 1 is the pre-phase-8 tenant-only
# binding, which only a record written before RFC-0020 phase 8 carries.
#
# ponytail: a one-release bridge. Once every deployment has booted phase 8 with
# its key present, every row reports 2 and the legacy branch (and this constant)
# can be deleted — the panel shows nothing that depends on it.
LEGACY_AAD_VERSION = 1
AAD_VERSION = 2


def _now() -> datetime:
    return datetime.now(UTC)


class CredentialError(ValueError):
    """A credential that cannot be stored or read.

    Rendered as a 503 over REST (it means the *deployment* is not able to hold
    credentials, not that the caller sent something wrong) and as an error
    payload over MCP. Its message never contains key material or a credential.
    """


# ── the key ring ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KeyRing:
    """The KEKs this process can unwrap with. The FIRST one is what it wraps with.

    A list rather than a single key because rotation needs both ends present at
    once: the new key to wrap onto, every old key to unwrap from. Drop an old key
    only once every record reports the new ``key_version``.
    """

    keys: tuple[tuple[str, bytes], ...]

    @property
    def active(self) -> tuple[str, bytes]:
        return self.keys[0]

    def find(self, key_id: str) -> bytes | None:
        """The key material for *key_id*, or None if this process does not hold it."""
        return next((material for name, material in self.keys if name == key_id), None)

    def __repr__(self) -> str:
        """Key IDS, never key material — this object ends up in tracebacks."""
        return f"KeyRing(ids={[name for name, _ in self.keys]!r})"


def _decode_key(spec: str, position: int) -> tuple[str, bytes]:
    """One ``<id>:<base64>`` entry (or bare base64) as (id, 32 raw bytes).

    Deliberately strict. The alternative — accepting any string and hashing it
    into 32 bytes — turns a typo into a silently weaker key and turns a
    passphrase into something that looks like a key and is not. Refusing here
    means the operator finds out at deploy time, with an error that says how to
    generate a real one.
    """
    key_id, sep, material = spec.partition(":")
    if not sep:
        key_id, material = _DEFAULT_KEY_ID, spec
    key_id = key_id.strip()
    if not key_id or len(key_id) > _KEY_ID_MAX:
        raise CredentialError(
            f"{KEY_ENV} entry {position} has an unusable key id "
            f"(expected '<key-id>:<base64 key>', key id at most {_KEY_ID_MAX} characters)"
        )
    try:
        raw = base64.b64decode(material.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise CredentialError(
            f"{KEY_ENV} entry {position} (key id {key_id!r}) is not valid base64. "
            f"Generate one with: python -c "
            f'"import base64,os;print(base64.b64encode(os.urandom({_KEY_BYTES})).decode())"'
        ) from None
    if len(raw) != _KEY_BYTES:
        raise CredentialError(
            f"{KEY_ENV} entry {position} (key id {key_id!r}) decodes to {len(raw)} bytes; "
            f"AES-256-GCM needs exactly {_KEY_BYTES}"
        )
    return key_id, raw


def load_keyring(settings: Settings) -> KeyRing | None:
    """The configured key ring, or ``None`` when no KEK is set at all.

    ``None`` is the fail-closed state, not an error: a deployment that holds no
    credentials is legitimate, and it is only *storing* one that has to be
    refused. Raises :class:`CredentialError` when a KEK is set but unusable —
    that is a misconfiguration, and silently ignoring it would leave a deployment
    that believes it can hold credentials and cannot.
    """
    spec = (settings.credential_key or "").strip()
    if not spec:
        return None
    entries = [part for part in (p.strip() for p in spec.split(",")) if part]
    keys = tuple(_decode_key(entry, position) for position, entry in enumerate(entries, start=1))
    if not keys:
        return None
    seen = [name for name, _ in keys]
    if len(set(seen)) != len(seen):
        raise CredentialError(f"{KEY_ENV} lists the same key id twice: {seen}")
    return KeyRing(keys)


def require_keyring(settings: Settings) -> KeyRing:
    """The key ring, or refuse. The guard on every credential WRITE."""
    keyring = load_keyring(settings)
    if keyring is None:
        raise CredentialError(
            f"cannot store a credential: {KEY_ENV} is not set, so there is nothing to "
            "encrypt it with. Set it to '<key-id>:<base64 of 32 random bytes>' and "
            "restart. A credential is never written unencrypted."
        )
    return keyring


# ── sealing ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Sealed:
    """One sealed credential: the wrapped data key, the ciphertext, and which KEK.

    Exactly the four stored columns, so the crypto is testable without a
    database and the store layer never assembles a nonce by hand.
    """

    key_version: str
    wrapped_key: bytes
    ciphertext: bytes
    # Which associated-data shape sealed this record: 2 binds (tenant,
    # connection), 1 is the pre-phase-8 tenant-only binding. Defaulted to the
    # current one so nothing can create a legacy record by forgetting a field.
    aad_version: int = AAD_VERSION

    def __repr__(self) -> str:
        """Sizes, never bytes — a sealed value still deserves not to be printed."""
        return (
            f"Sealed(key_version={self.key_version!r}, aad_version={self.aad_version}, "
            f"bytes={len(self.ciphertext)})"
        )


def _scope(tenant: str, connection: int | None) -> str:
    """The identity a record is cryptographically bound to.

    ``connection is None`` is the LEGACY (pre-phase-8) binding — tenant only —
    and is reachable exactly one way: a stored record whose ``aad_version`` says
    it was sealed that way. Every seal this module performs binds a connection,
    which is what stops a sealed record being replayed from one of a tenant's
    connections onto another.

    Length-prefixed on the current path (``4:acme/7``) so the components cannot
    be confused with one another: a tenant id is header-derived text that may
    contain a slash, and without the prefix tenant ``acme/7`` with no connection
    would produce the same associated data as tenant ``acme`` on connection 7.
    The legacy strings are reproduced byte-for-byte instead, because changing
    them would make every existing record undecryptable — which is the outage
    this bridge exists to avoid.
    """
    if connection is None:
        return tenant
    return f"{len(tenant)}:{tenant}/{connection}"


def _kek_aad(tenant: str, key_version: str, connection: int | None) -> bytes:
    """Associated data binding a wrapped DEK to its scope AND its KEK version."""
    return f"cfactory/git-credential/kek/{_scope(tenant, connection)}/{key_version}".encode()


def _dek_aad(tenant: str, connection: int | None) -> bytes:
    """Associated data binding a sealed credential to its tenant and connection.

    Deliberately does NOT include the key version: that is what lets
    :func:`rewrap` rotate the KEK without ever decrypting the credential.
    """
    return f"cfactory/git-credential/dek/{_scope(tenant, connection)}".encode()


def _encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """``nonce || ciphertext||tag`` — one blob, so one column stores one value."""
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def _decrypt(key: bytes, blob: bytes, aad: bytes, *, what: str) -> bytes:
    """The inverse, refusing rather than returning anything on a failed tag."""
    if len(blob) <= _NONCE_BYTES:
        raise CredentialError(f"stored {what} is truncated")
    try:
        return AESGCM(key).decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], aad)
    except InvalidTag:
        raise CredentialError(
            f"stored {what} did not decrypt: the key does not match this record, or the "
            "record was altered. Nothing is returned — AES-GCM authenticates, so a wrong "
            "key fails rather than producing garbage."
        ) from None


def seal(secret: str, *, tenant: str, connection: int, keyring: KeyRing) -> Sealed:
    """Seal *secret* for *tenant*'s *connection* under the key ring's ACTIVE key.

    ``connection`` is not optional and is not defaulted: a credential belongs to
    exactly one connection (RFC-0020 §3.4 + phase 8), and binding it in here is
    what makes a stolen record unusable on any other connection — including
    another of the same tenant's.
    """
    key_version, kek = keyring.active
    dek = AESGCM.generate_key(bit_length=_KEY_BYTES * 8)
    return Sealed(
        key_version=key_version,
        wrapped_key=_encrypt(kek, dek, _kek_aad(tenant, key_version, connection)),
        ciphertext=_encrypt(dek, secret.encode(), _dek_aad(tenant, connection)),
        aad_version=AAD_VERSION,
    )


def _binding(sealed: Sealed, connection: int) -> int | None:
    """The connection this record is bound to, or ``None`` for a legacy record.

    The ONE place the legacy binding is chosen, and it is chosen from the stored
    ``aad_version`` rather than from anything a caller passes — so a caller cannot
    ask for the weaker binding, and a record written by this release can never be
    read under it.
    """
    return None if sealed.aad_version == LEGACY_AAD_VERSION else connection


def _unwrap_dek(sealed: Sealed, *, tenant: str, connection: int, keyring: KeyRing) -> bytes:
    kek = keyring.find(sealed.key_version)
    if kek is None:
        raise CredentialError(
            f"this deployment does not hold key {sealed.key_version!r}, which wraps the "
            f"stored credential for tenant {tenant!r}. Add it back to {KEY_ENV} (older "
            "keys stay listed after the active one) or store the credential again."
        )
    return _decrypt(
        kek,
        sealed.wrapped_key,
        _kek_aad(tenant, sealed.key_version, _binding(sealed, connection)),
        what="data key",
    )


def unseal(sealed: Sealed, *, tenant: str, connection: int, keyring: KeyRing) -> str:
    """The credential stored for *tenant*'s *connection*, or raise.

    Never returns a partial value, and never returns anything for a record sealed
    against a different tenant or a different connection — AES-GCM authenticates
    the associated data, so the wrong binding fails the tag.
    """
    dek = _unwrap_dek(sealed, tenant=tenant, connection=connection, keyring=keyring)
    return _decrypt(
        dek, sealed.ciphertext, _dek_aad(tenant, _binding(sealed, connection)), what="credential"
    ).decode()


def rewrap(sealed: Sealed, *, tenant: str, connection: int, keyring: KeyRing) -> Sealed | None:
    """*sealed* re-wrapped onto the active KEK, or ``None`` if it is already there.

    The credential is NOT decrypted: only the data key moves keys, so a rotation
    never materialises a single plaintext token anywhere. That is the whole
    reason each record has its own data key rather than being encrypted with the
    KEK directly.

    A LEGACY record (``aad_version`` 1) is left alone here even when its key has
    moved, because re-wrapping cannot change the binding of the ciphertext: that
    one needs :func:`reseal`, which does decrypt — once, in memory.
    """
    active_id, active_kek = keyring.active
    if sealed.key_version == active_id:
        return None
    dek = _unwrap_dek(sealed, tenant=tenant, connection=connection, keyring=keyring)
    return Sealed(
        key_version=active_id,
        wrapped_key=_encrypt(
            active_kek, dek, _kek_aad(tenant, active_id, _binding(sealed, connection))
        ),
        ciphertext=sealed.ciphertext,
        aad_version=sealed.aad_version,
    )


def reseal(sealed: Sealed, *, tenant: str, connection: int, keyring: KeyRing) -> Sealed | None:
    """A legacy record re-sealed onto the connection-bound binding, or ``None``.

    The phase-8 upgrade step for a credential written before connections existed.
    The plaintext is unsealed and re-sealed IN MEMORY and is never returned, never
    logged and never written anywhere but back into the sealed columns — the
    caller gets a :class:`Sealed`, exactly as it does from :func:`seal`.

    ``None`` when the record already carries the current binding, so calling this
    on every read (or every boot) is a no-op after the first time. Raises
    :class:`CredentialError` when the record cannot be unsealed at all, which
    leaves the stored row untouched and readable again once the right key is back.
    """
    if sealed.aad_version == AAD_VERSION:
        return None
    secret = unseal(sealed, tenant=tenant, connection=connection, keyring=keyring)
    return seal(secret, tenant=tenant, connection=connection, keyring=keyring)


# ── storage ──────────────────────────────────────────────────────────────────


class GitCredentialRow(Base):
    """One CONNECTION's sealed git credential. Exactly one row per connection.

    Its own table rather than a column on the connection: the two have different
    lifetimes (replacing a credential must not disturb a verified connection, and
    clearing one must not clear the other) and, more to the point, a credential
    column on the connection table is the thing a future ``SELECT *`` on the
    connection accidentally returns.

    ``tenant_id`` stays even though the connection already carries one: it is half
    the cryptographic binding, so reading it from this row rather than from a join
    means an unsealing attempt cannot be talked into the wrong tenant by a
    mis-scoped query.
    """

    __tablename__ = "tenant_git_credential"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), default=DEFAULT_TENANT, server_default=DEFAULT_TENANT, index=True
    )
    # The connection this credential authenticates. NULL only on a record written
    # before RFC-0020 phase 8 and not yet adopted — see
    # :meth:`cfactory.cards.CardStore.adopt_legacy_git_config`.
    connection_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Which KEK wraps this record's data key. The forward-compatibility hinge:
    # swapping the KEK source (env today, a KMS once Factory#314/#315 land) is a
    # re-wrap of this row, never a change to the stored format.
    key_version: Mapped[str] = mapped_column(String(_KEY_ID_MAX))
    # Which associated-data shape sealed it (see AAD_VERSION). Server-defaulted to
    # the LEGACY value, because the only rows that can predate the column are
    # legacy ones; every row this code writes sets it explicitly.
    aad_version: Mapped[int] = mapped_column(
        Integer, default=AAD_VERSION, server_default=str(LEGACY_AAD_VERSION)
    )
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    # One credential per connection, enforced by the database rather than by an
    # application check that loses a race between two concurrent first writes.
    # NULLs are distinct in both SQLite and PostgreSQL, so an unadopted legacy row
    # does not collide with anything.
    __table_args__ = (Index("ix_tenant_git_credential_connection", "connection_id", unique=True),)

    def sealed(self) -> Sealed:
        return Sealed(self.key_version, self.wrapped_key, self.ciphertext, self.aad_version)


class CredentialInfo(BaseModel):
    """Everything a READ is allowed to know about a credential.

    That there is one, when it was last written, and which key wraps it — which
    is what an operator needs to answer "is this connection authenticated?" and
    "has the rotation reached every record yet?". There is deliberately no field
    for the credential, not even a masked prefix: a stored last-four is still a
    stored fragment of a secret, and nothing on this board needs one.
    """

    configured: bool
    # "tenant" for a stored one (the name predates connections and is kept so a
    # cockpit built against phase 3 keeps parsing it), "env" for the deployment's,
    # "none" for neither.
    source: str
    updated_at: datetime | None = None
    key_version: str | None = None


def _no_credential() -> str | None:
    return None


NO_CREDENTIAL = CredentialInfo(configured=False, source="none")


@dataclass(frozen=True)
class Credential:
    """A credential a provider call may FETCH — never one this object holds.

    ``info`` is the masked view every read surface renders. ``token()`` is the
    injection point: it is called once, at the moment a provider is built, so
    the plaintext exists for the duration of one call and is never a field on an
    object anything keeps. That is the RFC-0020 §3.4 requirement — "injected per
    invocation, never a long-lived global" — expressed as a type rather than as
    a convention somebody has to remember.
    """

    # default_factory rather than the NO_CREDENTIAL constant: a pydantic model is
    # mutable as far as dataclasses is concerned, so a shared default is refused.
    info: CredentialInfo = field(default_factory=lambda: NO_CREDENTIAL)
    fetch: Callable[[], str | None] = field(default=_no_credential, repr=False)

    @property
    def configured(self) -> bool:
        return self.info.configured

    def token(self) -> str | None:
        """The plaintext, for ONE provider invocation. Audited by the fetcher."""
        return self.fetch()

    def __repr__(self) -> str:
        """Whether there is one — never which one."""
        return f"Credential(configured={self.info.configured}, source={self.info.source!r})"


def env_credential(token: str | None) -> Credential:
    """The deployment-wide environment credential (the pre-phase-3 bridge).

    Still supported, and still what a single-tenant deployment runs on: RFC-0020
    §3.4 moves credential custody INTO the tenant, it does not delete the way
    every existing deploy is configured. A tenant credential wins where both are
    present.
    """
    if not token:
        return Credential()
    return Credential(CredentialInfo(configured=True, source="env"), lambda: token)
