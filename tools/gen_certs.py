"""Generate the dev CA + server cert + per-OEM-connector and gateway client certs for the mTLS MQTT edge.

Output: deploy/certs/{ca,server,gateway,OEM_A..OEM_D}.{crt,key}  (gitignored). Idempotent: skips if ca.crt exists (use --force).
Client cert CN == MQTT username == OEM code (ACL: publish only under telemetry/<CN>/# and event/<CN>/#).
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

OUT = Path(__file__).resolve().parent.parent / "deploy" / "certs"
CLIENTS = ["gateway", "OEM_A", "OEM_B", "OEM_C", "OEM_D"]


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FleetSentinel Dev"), x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _write(path: Path, key, cert) -> None:
    path.with_suffix(".key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    path.with_suffix(".crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _issue(ca_key, ca_cert, cn: str, *, server: bool, days: int = 825):
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    b = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(ca_cert.subject).public_key(key.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
         .not_valid_after(now + dt.timedelta(days=days))
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False))
    if server:
        b = b.add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"), x509.DNSName("mosquitto"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
    return key, b.sign(ca_key, hashes.SHA256())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if (OUT / "ca.crt").exists() and not args.force:
        print("certs exist (use --force to regenerate)")
        return 0
    ca_key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    ca_cert = (x509.CertificateBuilder().subject_name(_name("FleetSentinel Dev CA")).issuer_name(_name("FleetSentinel Dev CA"))
               .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=3650))
               .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
               .sign(ca_key, hashes.SHA256()))
    _write(OUT / "ca", ca_key, ca_cert)
    k, c = _issue(ca_key, ca_cert, "mosquitto", server=True)
    _write(OUT / "server", k, c)
    for cn in CLIENTS:
        k, c = _issue(ca_key, ca_cert, cn, server=False)
        _write(OUT / cn, k, c)
    print(f"wrote CA, server and {len(CLIENTS)} client certs to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
