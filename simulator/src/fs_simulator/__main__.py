"""python -m fs_simulator {seed|publish} ..."""
from __future__ import annotations

import sys


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    rest = sys.argv[2:]
    if cmd == "seed":
        from .seed import main as m
    elif cmd == "publish":
        from .publish import main as m
    else:
        print("usage: python -m fs_simulator {seed|publish} [options]")
        return 2
    return m(rest)


if __name__ == "__main__":
    raise SystemExit(main())
