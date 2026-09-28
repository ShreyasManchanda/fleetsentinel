"""Living-docs freshness gate (stdlib only, so it runs anywhere: pre-commit hook, CI, Windows).

Rule: a change that touches substantive project paths must also touch docs/PROGRESS.md and docs/CHAT_LOG.md.
Bug fixes and decisions are additionally expected in docs/BUGS.md and docs/decisions.md (reported as warnings, not failures,
because not every change is a bug or a decision).

  python tools/docs_check.py --staged            # pre-commit: staged files
  python tools/docs_check.py --base origin/main  # CI on a PR: files changed since the merge base
Escape hatch (visible, use only for pure-generated changes): DOCS_CHECK_SKIP=1
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

SUBSTANTIVE_PREFIXES = (
    "libs/", "services/", "simulator/", "ml/", "db/", "contracts/", "deploy/", "infra/", "tools/", "web/",
    ".github/", "tests/", "pyproject.toml", "Makefile",
)
REQUIRED_ALWAYS = ("docs/PROGRESS.md", "docs/CHAT_LOG.md")
RECOMMENDED = {
    "docs/BUGS.md": ("fix", "bug"),
    "docs/decisions.md": ("decision", "contract"),
}
REQUIRED_MARKERS = {
    "docs/PROGRESS.md": "Last updated:",
    "docs/BUGS.md": "| ID |",
    "docs/CHAT_LOG.md": "# CHAT_LOG",
    "docs/decisions.md": "| Date |",
}


def evaluate(changed: list[str]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a set of changed paths (posix, repo-relative)."""
    norm = [c.replace("\\", "/") for c in changed]
    substantive = [c for c in norm if c.startswith(SUBSTANTIVE_PREFIXES)]
    errors: list[str] = []
    warnings: list[str] = []
    if not substantive:
        return errors, warnings
    for req in REQUIRED_ALWAYS:
        if req not in norm:
            errors.append(f"{req} was not updated but {len(substantive)} substantive file(s) changed (e.g. {substantive[0]})")
    touched_contracts = any(c.startswith(("contracts/", "db/migrations/")) for c in norm)
    if touched_contracts and "docs/decisions.md" not in norm:
        warnings.append("contracts/ or db/migrations/ changed: is there a decision to record in docs/decisions.md?")
    if "docs/BUGS.md" not in norm:
        warnings.append("if this change fixes or finds a bug, log it in docs/BUGS.md")
    return errors, warnings


def check_files_exist() -> list[str]:
    problems = []
    for rel, marker in REQUIRED_MARKERS.items():
        p = ROOT / rel
        if not p.exists():
            problems.append(f"missing living doc: {rel}")
        elif marker not in p.read_text(encoding="utf-8"):
            problems.append(f"{rel} lost its required marker {marker!r}")
    return problems


def changed_files(args: argparse.Namespace) -> list[str]:
    if args.staged:
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRD"]
    else:
        cmd = ["git", "diff", "--name-only", f"{args.base}...HEAD"]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True).stdout  # noqa: S603
    return [line for line in out.splitlines() if line.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--staged", action="store_true")
    g.add_argument("--base", help="git ref to diff against, e.g. origin/main")
    args = ap.parse_args()
    if os.environ.get("DOCS_CHECK_SKIP") == "1":
        print("docs_check: SKIPPED via DOCS_CHECK_SKIP=1 (justify this in the commit message)")
        return 0
    problems = check_files_exist()
    errors, warnings = evaluate(changed_files(args))
    for w in warnings:
        print(f"docs_check warning: {w}")
    for e in [*problems, *errors]:
        print(f"docs_check ERROR: {e}", file=sys.stderr)
    if problems or errors:
        print("\nUpdate the living docs in the same commit (see CLAUDE.md): docs/PROGRESS.md, docs/CHAT_LOG.md "
              "(+ BUGS.md / decisions.md when relevant).", file=sys.stderr)
        return 1
    print("docs_check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
