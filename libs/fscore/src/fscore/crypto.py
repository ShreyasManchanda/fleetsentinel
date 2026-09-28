"""Envelope encryption for crypto-shredding (ADR-04).

Model
-----
* Every data subject (driver) has ONE random 256-bit data-encryption key (DEK).
* The DEK is stored ONLY wrapped by a Vault Transit key-encryption key (KEK ``fs-kek``) in ``subject_key.wrapped_dek``.
* PII (driver name, license, ``driver_ref_enc`` inside events) is AES-256-GCM encrypted under the DEK.
* Erasure = destroy the wrapped DEK (``wrapped_dek := NULL``). Every ciphertext copy (Postgres, Kafka, lake, backups) is
  then unreadable and :func:`decrypt_or_erased` renders ``"[erased]"``.

Wire format of a ciphertext blob (self-describing, so a replayed Kafka event knows which key to ask for)::

    [1 byte len(key_id)] [key_id utf-8] [12 byte random nonce] [ciphertext || 16 byte GCM tag]

The ``key_id`` header is authenticated (it is folded into the GCM associated data), so swapping a header onto another
subject ciphertext is detected.
"""
from __future__ import annotations

import base64
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ERASED_MARKER = "[erased]"
NONCE_LEN = 12
DEK_LEN = 32
TAG_LEN = 16
MAX_KEY_ID_LEN = 255


class CryptoError(Exception):
    """Base class for crypto failures."""


class DecryptionError(CryptoError):
    """Ciphertext failed authentication (tampered, wrong key, wrong associated data) or is malformed."""


class KeyDestroyed(CryptoError):
    """The subject wrapped DEK was destroyed (erasure). The data is permanently unreadable."""


class KeyNotFound(CryptoError):
    """No subject_key row exists for the key id."""


# ---------------------------------------------------------------- primitives
def generate_dek() -> bytes:
    """Fresh random 256-bit data key."""
    return os.urandom(DEK_LEN)


def _aad(key_id: bytes, aad: bytes) -> bytes:
    return key_id + b"\x00" + aad


def encrypt(dek: bytes, key_id: str, plaintext: bytes, aad: bytes = b"") -> bytes:
    """AES-256-GCM with a fresh random 96-bit nonce. Returns ``len || key_id || nonce || ciphertext || tag``."""
    if len(dek) != DEK_LEN:
        raise CryptoError("DEK must be 32 bytes")
    kid = key_id.encode("utf-8")
    if not kid or len(kid) > MAX_KEY_ID_LEN:
        raise CryptoError("key_id must be 1..255 bytes")
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(dek).encrypt(nonce, plaintext, _aad(kid, aad))
    return bytes([len(kid)]) + kid + nonce + ct


def parse_envelope(blob: bytes) -> tuple[str, bytes, bytes]:
    """Split a blob into (key_id, nonce, ciphertext||tag). Raises DecryptionError if malformed."""
    if not blob:
        raise DecryptionError("empty ciphertext")
    n = blob[0]
    if n == 0 or len(blob) < 1 + n + NONCE_LEN + TAG_LEN:
        raise DecryptionError("truncated ciphertext")
    try:
        key_id = blob[1 : 1 + n].decode("utf-8")
    except UnicodeDecodeError as e:
        raise DecryptionError("bad key_id header") from e
    nonce = blob[1 + n : 1 + n + NONCE_LEN]
    return key_id, nonce, blob[1 + n + NONCE_LEN :]


def key_id_of(blob: bytes) -> str:
    """The subject key id a blob was encrypted under (needed to look the DEK up)."""
    return parse_envelope(blob)[0]


def decrypt(dek: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    key_id, nonce, ct = parse_envelope(blob)
    try:
        return AESGCM(dek).decrypt(nonce, ct, _aad(key_id.encode("utf-8"), aad))
    except InvalidTag as e:
        raise DecryptionError("authentication failed (tampered ciphertext, wrong key or wrong AAD)") from e
    except ValueError as e:  # bad key length
        raise DecryptionError(str(e)) from e


# ---------------------------------------------------------------- key wrappers
class KeyWrapper(Protocol):
    """Wraps/unwraps DEKs under the KEK. Implementations must never expose the KEK."""

    def wrap(self, dek: bytes) -> bytes: ...
    def unwrap(self, wrapped: bytes) -> bytes: ...
    def wrap_many(self, deks: Sequence[bytes]) -> list[bytes]: ...
    def unwrap_many(self, wrapped: Sequence[bytes]) -> list[bytes]: ...


class VaultTransitWrapper:
    """Production wrapper: Vault Transit ``aes256-gcm96`` key (default ``fs-kek``).

    ``wrap_many``/``unwrap_many`` use Transit *batch_input* so seeding 120k drivers costs ~120 Vault calls, not 120k.
    Wrapped blobs are the UTF-8 bytes of the Vault ``vault:v1:...`` ciphertext string (stored in ``subject_key.wrapped_dek``).
    """

    def __init__(
        self,
        url: str = "http://localhost:8200",
        token: str = "root",  # noqa: S107  dev-only default; real deployments inject VAULT_TOKEN / k8s auth
        key_name: str = "fs-kek",
        mount_point: str = "transit",
        client: Any | None = None,
        batch_size: int = 500,
    ):
        if client is None:
            import hvac

            client = hvac.Client(url=url, token=token)
        self._client = client
        self._key = key_name
        self._mount = mount_point
        self._batch = batch_size

    def wrap(self, dek: bytes) -> bytes:
        r = self._client.secrets.transit.encrypt_data(
            name=self._key, plaintext=base64.b64encode(dek).decode(), mount_point=self._mount
        )
        return r["data"]["ciphertext"].encode()

    def unwrap(self, wrapped: bytes) -> bytes:
        r = self._client.secrets.transit.decrypt_data(
            name=self._key, ciphertext=wrapped.decode(), mount_point=self._mount
        )
        return base64.b64decode(r["data"]["plaintext"])

    def wrap_many(self, deks: Sequence[bytes]) -> list[bytes]:
        out: list[bytes] = []
        for i in range(0, len(deks), self._batch):
            chunk = deks[i : i + self._batch]
            r = self._client.secrets.transit.encrypt_data(
                name=self._key,
                batch_input=[{"plaintext": base64.b64encode(d).decode()} for d in chunk],
                mount_point=self._mount,
            )
            results = r["data"]["batch_results"]
            if len(results) != len(chunk):
                raise CryptoError("Vault returned a short batch")
            out.extend(x["ciphertext"].encode() for x in results)
        return out

    def unwrap_many(self, wrapped: Sequence[bytes]) -> list[bytes]:
        out: list[bytes] = []
        for i in range(0, len(wrapped), self._batch):
            chunk = wrapped[i : i + self._batch]
            r = self._client.secrets.transit.decrypt_data(
                name=self._key,
                batch_input=[{"ciphertext": w.decode()} for w in chunk],
                mount_point=self._mount,
            )
            results = r["data"]["batch_results"]
            if len(results) != len(chunk):
                raise CryptoError("Vault returned a short batch")
            out.extend(base64.b64decode(x["plaintext"]) for x in results)
        return out


class LocalDevWrapper:
    """DEV / TEST ONLY key wrapper: the KEK is a local secret, NOT protected by an HSM/Vault.

    Exists so unit tests, CI and a laptop with no Vault container can exercise the exact same envelope format. Never use
    it where real data subjects exist. Wrapped form = ``nonce || AES-256-GCM(kek, dek)``.
    """

    INSECURE = True

    def __init__(self, kek: bytes | None = None):
        if kek is None:
            kek = b"fleetsentinel-dev-only-kek-000000"[:DEK_LEN]
        if len(kek) != DEK_LEN:
            raise CryptoError("KEK must be 32 bytes")
        self._aead = AESGCM(kek)

    def wrap(self, dek: bytes) -> bytes:
        nonce = os.urandom(NONCE_LEN)
        return nonce + self._aead.encrypt(nonce, dek, b"fs-dek-wrap")

    def unwrap(self, wrapped: bytes) -> bytes:
        if len(wrapped) < NONCE_LEN + TAG_LEN:
            raise DecryptionError("bad wrapped DEK")
        try:
            return self._aead.decrypt(wrapped[:NONCE_LEN], wrapped[NONCE_LEN:], b"fs-dek-wrap")
        except InvalidTag as e:
            raise DecryptionError("wrapped DEK failed authentication") from e

    def wrap_many(self, deks: Sequence[bytes]) -> list[bytes]:
        return [self.wrap(d) for d in deks]

    def unwrap_many(self, wrapped: Sequence[bytes]) -> list[bytes]:
        return [self.unwrap(w) for w in wrapped]


# ---------------------------------------------------------------- subject key store
@dataclass(frozen=True, slots=True)
class SubjectKeyRow:
    """The columns of ``subject_key`` that matter for decryption."""

    subject_key_id: str
    wrapped_dek: bytes | None


_UNSET: Any = object()


class SubjectKeyStore:
    """Unwraps subject DEKs with a small in-process LRU (+TTL) cache.

    Cache entries are keyed by ``(subject_key_id, wrapped_dek)``: a destroyed row (``wrapped_dek IS NULL``) raises
    :class:`KeyDestroyed` before the cache is even consulted, so a stale cached plaintext DEK can never resurrect
    erased data on any instance that re-reads the row. ``invalidate`` evicts the local copy immediately on erasure and the
    TTL bounds staleness of the unwrap step itself.
    """

    def __init__(
        self,
        wrapper: KeyWrapper,
        loader: Callable[[str], SubjectKeyRow | None] | None = None,
        max_size: int = 1024,
        ttl_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._wrapper = wrapper
        self._loader = loader
        self._max = max_size
        self._ttl = ttl_s
        self._clock = clock
        self._cache: OrderedDict[tuple[str, bytes], tuple[float, bytes]] = OrderedDict()
        self._lock = threading.Lock()
        self.unwraps = 0  # number of real KEK unwrap calls (cache misses); useful in tests and metrics

    def dek(self, subject_key_id: str, wrapped_dek: bytes | None = _UNSET) -> bytes:
        if wrapped_dek is _UNSET:
            if self._loader is None:
                raise KeyNotFound("no loader configured and no wrapped_dek supplied")
            row = self._loader(subject_key_id)
            if row is None:
                raise KeyNotFound(subject_key_id)
            wrapped_dek = row.wrapped_dek
        if wrapped_dek is None:
            self.invalidate(subject_key_id)
            raise KeyDestroyed(subject_key_id)
        ck = (subject_key_id, bytes(wrapped_dek))
        now = self._clock()
        with self._lock:
            hit = self._cache.get(ck)
            if hit is not None and now - hit[0] <= self._ttl:
                self._cache.move_to_end(ck)
                return hit[1]
        dek = self._wrapper.unwrap(bytes(wrapped_dek))
        with self._lock:
            self.unwraps += 1
            self._cache[ck] = (now, dek)
            self._cache.move_to_end(ck)
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
        return dek

    def invalidate(self, subject_key_id: str) -> int:
        """Evict every cached DEK of a subject (call on erasure). Returns how many entries were dropped."""
        with self._lock:
            dead = [k for k in self._cache if k[0] == subject_key_id]
            for k in dead:
                del self._cache[k]
            return len(dead)

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)


def decrypt_or_erased(
    store: SubjectKeyStore,
    blob: bytes | None,
    wrapped_dek: bytes | None = _UNSET,
    aad: bytes = b"",
) -> str | None:
    """Decrypt a UTF-8 PII blob, or return ``"[erased]"`` when its subject key was destroyed.

    ``None``/empty blob -> ``None`` (nothing was stored). Tampered ciphertext still raises :class:`DecryptionError`:
    an integrity failure must never be silently rendered as an erasure.
    """
    if not blob:
        return None
    key_id = key_id_of(blob)
    try:
        dek = store.dek(key_id, wrapped_dek)
    except KeyDestroyed:
        return ERASED_MARKER
    return decrypt(dek, blob, aad).decode("utf-8")


# Associated-data conventions (part of the contract between the seeder, normalizer and API): bind each ciphertext to the
# field it protects so a name blob cannot be swapped into the license column.
AAD_DRIVER_NAME = b"driver.name"
AAD_DRIVER_LICENSE = b"driver.license"
AAD_DRIVER_REF = b"driver.ref"
