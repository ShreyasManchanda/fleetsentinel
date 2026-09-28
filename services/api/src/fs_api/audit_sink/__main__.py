"""`python -m fs_api.audit_sink`: run the audit sink consumer (connects to Postgres as fs_worker)."""
from __future__ import annotations

import os
import signal

from .adapters.consumer import make_consumer, run
from .adapters.pg_writer import PgAuditWriter
from .sink import AuditSink

DEFAULT_DSN = "postgresql://fs_worker:fs_worker_dev@localhost:5432/fleetsentinel"  # DEV-ONLY, same as tools/fs.py


def main() -> None:
    stopping = {"v": False}
    signal.signal(signal.SIGTERM, lambda *_: stopping.__setitem__("v", True))
    signal.signal(signal.SIGINT, lambda *_: stopping.__setitem__("v", True))
    sink = AuditSink(PgAuditWriter(os.environ.get("FS_WORKER_DSN", DEFAULT_DSN)))
    run(make_consumer(os.environ.get("FS_API_KAFKA_BOOTSTRAP", "localhost:9092")), sink, stop=lambda: stopping["v"])


if __name__ == "__main__":
    main()
