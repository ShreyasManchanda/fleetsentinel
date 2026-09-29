"""KEK wrapper selection: fail-closed unless --local (reviewer HIGH, B-020)."""
from __future__ import annotations

import numpy as np
import pytest
from fs_simulator.crypto import (
    LOCAL_PREFIX,
    LocalDevKek,
    VaultTransitKek,
    decrypt_pii,
    encrypt_pii,
    make_wrapper,
    new_deks,
)


def test_local_explicit_returns_dev_wrapper():
    w = make_wrapper(local=True)
    assert isinstance(w, LocalDevKek)
    dek = new_deks(1)[0]
    blob = w.wrap_many([dek])[0]
    assert blob.startswith(LOCAL_PREFIX)
    assert w.unwrap(blob) == dek


def test_nonlocal_without_vault_raises_fail_closed(monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "http://127.0.0.1:1")  # nothing listens here
    monkeypatch.setenv("VAULT_TOKEN", "root")
    with pytest.raises(ConnectionError, match="--local"):
        make_wrapper(local=False)


def test_nonlocal_with_unreachable_vault_never_returns_dev_wrapper(monkeypatch):
    """The exact bug the reviewer found: a transient Vault outage must not silently
    wrap production DEKs under the public LOCAL_DEV_KEK."""
    monkeypatch.setenv("VAULT_ADDR", "http://127.0.0.1:1")
    monkeypatch.setenv("VAULT_TOKEN", "root")
    try:
        w = make_wrapper(local=False)
    except ConnectionError:
        return
    raise AssertionError(f"make_wrapper fell back to {type(w).__name__} instead of failing closed")


def test_pii_roundtrip_with_seeded_dek():
    rng = np.random.default_rng(7)
    dek = new_deks(1, rng)[0]
    blob = encrypt_pii(dek, "subj-1", "Ada Lovelace", nonce=b"0" * 12)
    assert decrypt_pii(dek, "subj-1", blob) == "Ada Lovelace"
    with pytest.raises(Exception):
        decrypt_pii(new_deks(1, rng)[0], "subj-1", blob)


def test_vault_wrapper_batches(monkeypatch):
    seen = {}

    class FakeTransit:
        def encrypt_data(self, name, plaintext, batch_input):
            seen["batches"] = seen.get("batches", 0) + 1
            assert name == "fs-kek"
            return {"data": {"batch_results": [{"ciphertext": f"vault:v1:{i}"} for i, _ in enumerate(batch_input)]}}

    class FakeSecrets:
        transit = FakeTransit()

    class FakeClient:
        secrets = FakeSecrets()

        def is_authenticated(self):
            return True

    import sys
    import types

    fake_hvac = types.ModuleType("hvac")
    fake_hvac.Client = lambda **kw: FakeClient()
    monkeypatch.setitem(sys.modules, "hvac", fake_hvac)
    w = make_wrapper(local=False)
    assert isinstance(w, VaultTransitKek)
    out = w.wrap_many([b"d" * 32 for _ in range(1200)])
    assert len(out) == 1200 and seen["batches"] == 3  # 500 + 500 + 200
