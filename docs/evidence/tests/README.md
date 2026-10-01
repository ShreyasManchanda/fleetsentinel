# Test evidence (run 2026-10-02, Windows, Python 3.12)

| Run | Result | File |
|---|---|---|
| Full suite with coverage (`uv run pytest --cov`), Docker stopped | **665 passed, 6 skipped** in 136 s; line+branch coverage **90%** (8412 statements) | `pytest-coverage-output.txt`, `coverage.xml` |
| The 6 skipped tests (live Postgres: BOLA, audit tamper, `[erased]` replay, tenant isolation) re-run with only the Postgres container up | **8 passed** (includes the 6) | `live-postgres-security-tests.txt` |

Not run for this submission (so no evidence is claimed): k6 load at 100k events/s, soak, chaos kill-consumer, Semgrep / Trivy / ZAP scan outputs (they are configured in `.github/workflows/`), Pact against live services.
The HTML coverage report is in the Drive artifacts folder (too large for git).
