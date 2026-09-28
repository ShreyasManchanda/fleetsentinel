import pytest
from fscore import crypto as c
from hypothesis import given
from hypothesis import strategies as st


class FakeTransit:
    """Minimal stand-in for the hvac transit secrets engine (base64 pass-through is fine for wiring tests)."""

    def __init__(self):
        self.calls = 0
        self.short = False

    def encrypt_data(self, name, plaintext=None, batch_input=None, mount_point="transit"):
        self.calls += 1
        assert name == "fs-kek"
        if batch_input is not None:
            res = [{"ciphertext": "vault:v1:" + x["plaintext"]} for x in batch_input]
            return {"data": {"batch_results": res[:-1] if self.short else res}}
        return {"data": {"ciphertext": "vault:v1:" + plaintext}}

    def decrypt_data(self, name, ciphertext=None, batch_input=None, mount_point="transit"):
        self.calls += 1
        if batch_input is not None:
            res = [{"plaintext": x["ciphertext"].removeprefix("vault:v1:")} for x in batch_input]
            return {"data": {"batch_results": res[:-1] if self.short else res}}
        return {"data": {"plaintext": ciphertext.removeprefix("vault:v1:")}}


class FakeClient:
    def __init__(self):
        self.secrets = type("S", (), {"transit": FakeTransit()})()


def test_roundtrip_and_format():
    dek = c.generate_dek()
    blob = c.encrypt(dek, "sk-1", b"Asha Rao")
    assert blob[0] == 4 and blob[1:5] == b"sk-1"
    assert c.key_id_of(blob) == "sk-1"
    assert len(blob) == 1 + 4 + c.NONCE_LEN + len(b"Asha Rao") + c.TAG_LEN
    assert c.decrypt(dek, blob) == b"Asha Rao"


@given(st.binary(max_size=200), st.binary(max_size=40))
def test_roundtrip_property(pt, aad):
    dek = c.generate_dek()
    assert c.decrypt(dek, c.encrypt(dek, "k", pt, aad), aad) == pt


def test_nonce_uniqueness():
    dek = c.generate_dek()
    nonces = set()
    for _ in range(2000):
        _, n, _ = c.parse_envelope(c.encrypt(dek, "k", b"same"))
        nonces.add(n)
    assert len(nonces) == 2000


def test_tamper_detection_every_region():
    dek = c.generate_dek()
    blob = bytearray(c.encrypt(dek, "sk-1", b"secret"))
    for i in range(1, len(blob)):  # flip one bit anywhere after the length byte
        t = bytearray(blob)
        t[i] ^= 0x01
        with pytest.raises(c.DecryptionError):
            c.decrypt(dek, bytes(t))


def test_wrong_key_and_wrong_aad():
    dek, other = c.generate_dek(), c.generate_dek()
    blob = c.encrypt(dek, "k", b"x", b"ctx")
    with pytest.raises(c.DecryptionError):
        c.decrypt(other, blob, b"ctx")
    with pytest.raises(c.DecryptionError):
        c.decrypt(dek, blob, b"other-ctx")
    with pytest.raises(c.DecryptionError):
        c.decrypt(b"short", blob, b"ctx")


def test_input_validation():
    with pytest.raises(c.CryptoError):
        c.encrypt(b"short", "k", b"x")
    with pytest.raises(c.CryptoError):
        c.encrypt(c.generate_dek(), "", b"x")
    with pytest.raises(c.CryptoError):
        c.encrypt(c.generate_dek(), "k" * 256, b"x")
    for bad in (b"", b"\x00abc", b"\x05ab", b"\x02\xff\xfe" + b"0" * 40):
        with pytest.raises(c.DecryptionError):
            c.parse_envelope(bad)


def test_local_dev_wrapper():
    w = c.LocalDevWrapper()
    assert w.INSECURE is True
    dek = c.generate_dek()
    wr = w.wrap(dek)
    assert wr != dek and w.unwrap(wr) == dek
    assert w.wrap(dek) != wr  # random nonce
    assert w.unwrap_many(w.wrap_many([dek, dek])) == [dek, dek]
    with pytest.raises(c.DecryptionError):
        c.LocalDevWrapper(b"k" * 32).unwrap(wr)
    with pytest.raises(c.DecryptionError):
        w.unwrap(b"tiny")
    with pytest.raises(c.CryptoError):
        c.LocalDevWrapper(b"short")


def test_vault_wrapper_single_and_batch():
    cl = FakeClient()
    w = c.VaultTransitWrapper(client=cl, batch_size=2)
    d = [c.generate_dek() for _ in range(5)]
    assert w.unwrap(w.wrap(d[0])) == d[0]
    wrapped = w.wrap_many(d)
    assert all(x.startswith(b"vault:v1:") for x in wrapped)
    before = cl.secrets.transit.calls
    assert w.unwrap_many(wrapped) == d
    assert cl.secrets.transit.calls - before == 3  # 5 items / batch of 2 => 3 Vault calls, not 5
    cl.secrets.transit.short = True
    with pytest.raises(c.CryptoError):
        w.wrap_many(d[:2])
    with pytest.raises(c.CryptoError):
        w.unwrap_many(wrapped[:2])


def test_vault_wrapper_builds_hvac_client_lazily(monkeypatch):
    import hvac

    seen = {}

    class Dummy:
        def __init__(self, url, token):
            seen.update(url=url, token=token)

    monkeypatch.setattr(hvac, "Client", Dummy)
    c.VaultTransitWrapper(url="http://v:8200", token="t")
    assert seen == {"url": "http://v:8200", "token": "t"}


def test_store_cache_and_destroyed():
    w = c.LocalDevWrapper()
    store = c.SubjectKeyStore(w, max_size=2)
    dek = c.generate_dek()
    wrapped = w.wrap(dek)
    assert store.dek("a", wrapped) == dek
    assert store.dek("a", wrapped) == dek
    assert store.unwraps == 1  # second call was a cache hit
    with pytest.raises(c.KeyDestroyed):
        store.dek("a", None)
    assert len(store) == 0  # destroyed key evicts any cached plaintext
    with pytest.raises(c.KeyNotFound):
        store.dek("a")  # no loader, no row


def test_store_lru_ttl_and_invalidate():
    w = c.LocalDevWrapper()
    now = [0.0]
    store = c.SubjectKeyStore(w, max_size=2, ttl_s=10, clock=lambda: now[0])
    rows = {k: w.wrap(c.generate_dek()) for k in "abc"}
    for k in "abc":
        store.dek(k, rows[k])
    assert len(store) == 2  # a evicted (LRU)
    store.dek("b", rows["b"])
    assert store.unwraps == 3
    now[0] = 11.0  # TTL expiry forces a re-unwrap
    store.dek("b", rows["b"])
    assert store.unwraps == 4
    assert store.invalidate("b") == 1
    assert store.invalidate("nope") == 0


def test_store_loader():
    w = c.LocalDevWrapper()
    dek = c.generate_dek()
    rows = {"a": c.SubjectKeyRow("a", w.wrap(dek)), "gone": c.SubjectKeyRow("gone", None)}
    store = c.SubjectKeyStore(w, loader=rows.get)
    assert store.dek("a") == dek
    with pytest.raises(c.KeyDestroyed):
        store.dek("gone")
    with pytest.raises(c.KeyNotFound):
        store.dek("missing")


def test_decrypt_or_erased_full_lifecycle():
    w = c.LocalDevWrapper()
    store = c.SubjectKeyStore(w)
    dek = c.generate_dek()
    wrapped = w.wrap(dek)
    blob = c.encrypt(dek, "sk-9", b"Ravi Kumar", b"driver.name")
    assert c.decrypt_or_erased(store, blob, wrapped, b"driver.name") == "Ravi Kumar"
    # erasure: the wrapped DEK is destroyed (NULL) -> the SAME ciphertext replays as [erased]
    assert c.decrypt_or_erased(store, blob, None, b"driver.name") == c.ERASED_MARKER == "[erased]"
    assert c.decrypt_or_erased(store, None) is None
    assert c.decrypt_or_erased(store, b"") is None


def test_tamper_is_not_reported_as_erased():
    w = c.LocalDevWrapper()
    store = c.SubjectKeyStore(w)
    dek = c.generate_dek()
    wrapped = w.wrap(dek)
    blob = bytearray(c.encrypt(dek, "sk", b"x"))
    blob[-1] ^= 1
    with pytest.raises(c.DecryptionError):
        c.decrypt_or_erased(store, bytes(blob), wrapped)


def test_vault_wrapped_dek_works_with_store():
    cl = FakeClient()
    w = c.VaultTransitWrapper(client=cl)
    store = c.SubjectKeyStore(w)
    dek = c.generate_dek()
    blob = c.encrypt(dek, "k", b"pii")
    assert c.decrypt_or_erased(store, blob, w.wrap(dek)) == "pii"
