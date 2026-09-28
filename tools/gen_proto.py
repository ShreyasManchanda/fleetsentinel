"""Generate Python protobuf modules from contracts/proto into libs/fscore/src/fleetsentinel.

Generated code is committed so parallel workstreams never need to regenerate it.
Usage: python tools/gen_proto.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROTO_DIR = ROOT / "contracts" / "proto"
OUT_DIR = ROOT / "libs" / "fscore" / "src"


def main() -> int:
    protos = sorted(PROTO_DIR.rglob("*.proto"))
    if not protos:
        print("no .proto files found", file=sys.stderr)
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "grpc_tools.protoc",
        f"-I{PROTO_DIR}",
        f"--python_out={OUT_DIR}",
        f"--pyi_out={OUT_DIR}",
        *[str(p) for p in protos],
    ]
    subprocess.run(cmd, check=True)
    for pkg in [OUT_DIR / "fleetsentinel", OUT_DIR / "fleetsentinel" / "v1"]:
        (pkg / "__init__.py").touch()
    print(f"generated {len(protos)} proto files -> {OUT_DIR / 'fleetsentinel'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
