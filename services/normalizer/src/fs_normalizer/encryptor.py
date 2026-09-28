"""Pluggable driver_ref encryption. The Vault-Transit backed implementation belongs to stream C; this module holds the
interface and a local AES-256-GCM dev implementation that emits the same self-describing wire format as fscore.crypto:
``[len(key_id)] [key_id] [12 B nonce] [ciphertext || 16 B tag]`` (key_id is authenticated as associated data).
"""
from __future__ import annotations

import base64
import os
from collections import OrderedDict
from collections.abc import Sequence
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_DEV_FALLBACK_KEY = bytes(range(32))     # NEVER for production; only used when no key is configured


class Encryptor(Protocol):
    def encrypt(self, plaintext: str, context: str = "") -> bytes: ...

    def encrypt_many(self, plaintexts: Sequence[str], context: str = "") -> list[bytes]: ...


class AesGcmDevEncryptor:
    """Local dev encryptor (one static key). Production replaces it with a Vault Transit / per-subject DEK one."""

    def __init__(self, key: bytes | None = None, key_id: str = "dev-driver-key") -> None:
        key = key or _DEV_FALLBACK_KEY
        if len(key) != 32:
            raise ValueError("AES-256-GCM key must be 32 bytes")
        self._aead = AESGCM(key)
        self._kid = key_id.encode()
        if not (1 <= len(self._kid) <= 255):
            raise ValueError("key_id must be 1..255 bytes")
        self.is_fallback_key = key == _DEV_FALLBACK_KEY

    @classmethod
    def from_b64(cls, b64: str, key_id: str = "dev-driver-key") -> AesGcmDevEncryptor:
        return cls(base64.b64decode(b64) if b64 else None, key_id)

    def encrypt(self, plaintext: str, context: str = "") -> bytes:
        nonce = os.urandom(12)
        aad = self._kid + b"\x00" + context.encode()
        return bytes([len(self._kid)]) + self._kid + nonce + self._aead.encrypt(nonce, plaintext.encode(), aad)

    def encrypt_many(self, plaintexts: Sequence[str], context: str = "") -> list[bytes]:
        return [self.encrypt(p, context) for p in plaintexts]

    def decrypt(self, blob: bytes, context: str = "") -> str:
        """Test/dev helper."""
        n = blob[0]
        kid, nonce, ct = blob[1 : 1 + n], blob[1 + n : 13 + n], blob[13 + n :]
        return self._aead.decrypt(nonce, ct, kid + b"\x00" + context.encode()).decode()


class CachingEncryptor:
    """Encrypt each distinct (context, plaintext) once per process (bounded LRU).

    Drivers repeat across millions of events; re-encrypting the same plaintext under the same key with a reused nonce
    reveals nothing new (identical message), and it removes a per-event AES call / Vault round trip from the hot path.
    """

    def __init__(self, inner: Encryptor, capacity: int = 200_000) -> None:
        self._inner = inner
        self._cap = capacity
        self._c: OrderedDict[tuple[str, str], bytes] = OrderedDict()

    def encrypt(self, plaintext: str, context: str = "") -> bytes:
        k = (context, plaintext)
        v = self._c.get(k)
        if v is None:
            v = self._inner.encrypt(plaintext, context)
            self._c[k] = v
            if len(self._c) > self._cap:
                self._c.popitem(last=False)
        else:
            self._c.move_to_end(k)
        return v

    def encrypt_many(self, plaintexts: Sequence[str], context: str = "") -> list[bytes]:
        local: dict[str, bytes] = {}
        missing: list[str] = []
        for p in dict.fromkeys(plaintexts):
            hit = self._c.get((context, p))
            if hit is None:
                missing.append(p)
            else:
                local[p] = hit
        if missing:
            for p, enc in zip(missing, self._inner.encrypt_many(missing, context), strict=True):
                local[p] = enc
                self._c[(context, p)] = enc
            while len(self._c) > self._cap:
                self._c.popitem(last=False)
        return [local[p] for p in plaintexts]