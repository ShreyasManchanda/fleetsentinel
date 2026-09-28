"""Fake-driver PII encryption: AES-256-GCM under a per-driver DEK, DEK wrapped by the Vault Transit key fs-kek in batches.

Ciphertext layout (bytea): 0x01 | nonce(12) | AES-GCM ciphertext+tag, AAD = subject_key_id (utf-8).
wrapped_dek layout: the Vault Transit ciphertext string ("vault:v1:...") as bytes, or, ONLY when Vault is unreachable / --local,
the clearly labelled local-dev form  b"local-dev-kek:v1:" + base64(nonce|AES-GCM(DEK)). The local form is NOT secure (fixed dev key).
"""
from __future__ import annotations

import base64
import os
from typing import Protocol

import numpy as np
import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = structlog.get_logger("fs_simulator.crypto")
VERSION = b"\x01"
KEK_NAME = "fs-kek"
LOCAL_DEV_KEK = bytes(range(32))     # DEV ONLY, deliberately public. Never used when Vault Transit is reachable.
LOCAL_PREFIX = b"local-dev-kek:v1:"
VAULT_BATCH = 500


def new_deks(n: int, rng: np.random.Generator | None = None) -> list[bytes]:
    """n random 256-bit DEKs. Pass an rng ONLY for reproducible tests; production seeds use the OS CSPRNG."""
    if rng is not None:
        return [rng.bytes(32) for _ in range(n)]
    return [os.urandom(32) for _ in range(n)]


def encrypt_pii(dek: bytes, subject_key_id: str, plaintext: str, nonce: bytes | None = None) -> bytes:
    nonce = nonce or os.urandom(12)
    return VERSION + nonce + AESGCM(dek).encrypt(nonce, plaintext.encode(), subject_key_id.encode())


def decrypt_pii(dek: bytes, subject_key_id: str, blob: bytes) -> str:
    if blob[:1] != VERSION:
        raise ValueError("unknown ciphertext version")
    return AESGCM(dek).decrypt(blob[1:13], blob[13:], subject_key_id.encode()).decode()


class KekWrapper(Protocol):
    label: str

    def wrap_many(self, deks: list[bytes]) -> list[bytes]: ...


class LocalDevKek:
    label = "LOCAL-DEV-KEK (insecure fixed key; Vault Transit was not used)"

    def wrap_many(self, deks: list[bytes]) -> list[bytes]:
        aes = AESGCM(LOCAL_DEV_KEK)
        out = []
        for d in deks:
            nonce = os.urandom(12)
            out.append(LOCAL_PREFIX + base64.b64encode(nonce + aes.encrypt(nonce, d, b"fs-kek")))
        return out

    def unwrap(self, blob: bytes) -> bytes:
        raw = base64.b64decode(blob[len(LOCAL_PREFIX):])
        return AESGCM(LOCAL_DEV_KEK).decrypt(raw[:12], raw[12:], b"fs-kek")


class VaultTransitKek:
    """Wraps DEKs with Transit key fs-kek in batches of VAULT_BATCH via hvac (one HTTP call per batch, not per DEK)."""
    label = "vault-transit:fs-kek"

    def __init__(self, client, key: str = KEK_NAME, batch: int = VAULT_BATCH):
        self.client, self.key, self.batch = client, key, batch
        self.calls = 0

    def wrap_many(self, deks: list[bytes]) -> list[bytes]:
        out: list[bytes] = []
        for i in range(0, len(deks), self.batch):
            chunk = deks[i:i + self.batch]
            resp = self.client.secrets.transit.encrypt_data(
                name=self.key, plaintext=None, batch_input=[{"plaintext": base64.b64encode(d).decode()} for d in chunk])
            self.calls += 1
            out.extend(item["ciphertext"].encode() for item in resp["data"]["batch_results"])
        return out


def make_wrapper(local: bool, addr: str | None = None, token: str | None = None) -> KekWrapper:
    """Vault Transit if reachable, otherwise the clearly labelled local dev KEK (logged as a WARNING)."""
    if local:
        log.warning("kek_local_dev", reason="--local", note="wrapping DEKs with the insecure LOCAL dev KEK")
        return LocalDevKek()
    try:
        import hvac

        client = hvac.Client(url=addr or os.getenv("VAULT_ADDR", "http://localhost:8200"),
                             token=token or os.getenv("VAULT_TOKEN", "root"), timeout=3)
        if not client.is_authenticated():
            raise ConnectionError("vault not authenticated")
        return VaultTransitKek(client)
    except Exception as e:  # noqa: BLE001 - any failure means "Vault unreachable": fall back, loudly
        log.warning("kek_local_dev", reason="vault_unreachable", error=type(e).__name__,
                    note="FALLING BACK to the insecure LOCAL dev KEK; DEKs are NOT protected by Vault Transit")
        return LocalDevKek()
