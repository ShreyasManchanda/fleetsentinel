"""Install the local git pre-commit hook that runs the living-docs gate. Run once per clone: python tools/install_hooks.py"""
from __future__ import annotations

import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".git" / "hooks" / "pre-commit"
BODY = """#!/bin/sh
# Installed by tools/install_hooks.py. Keeps docs/PROGRESS.md, CHAT_LOG.md (and BUGS/decisions) current.
PY=python
command -v python >/dev/null 2>&1 || PY=python3
exec "$PY" tools/docs_check.py --staged
"""


def main() -> int:
    if not (ROOT / ".git").is_dir():
        print("not a git checkout; nothing to install", file=sys.stderr)
        return 1
    HOOK.write_text(BODY, encoding="utf-8", newline="\n")
    HOOK.chmod(HOOK.stat().st_mode | stat.S_IEXEC)
    print(f"installed {HOOK}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
