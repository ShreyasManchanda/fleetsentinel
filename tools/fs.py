"""FleetSentinel developer CLI (cross-platform; the Makefile just delegates here).

  python tools/fs.py migrate [--to N]     apply db/migrations (ordered SQL; '-- @@' splits statements run in autocommit)
  python tools/fs.py topics [--rf N]      create Kafka topics from contracts/topics.yaml
  python tools/fs.py up|bootstrap|down|logs|ps  docker compose helpers (profiles via FS_PROFILES, default: infra);
                                          `bootstrap` = certs + up + migrate + topics

Env: FS_PG_DSN (superuser DSN for migrations), FS_APP_PASSWORD, FS_WORKER_PASSWORD, FS_KAFKA_BOOTSTRAP.
Dev defaults match deploy/compose/.env.example. Never commit real secrets.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "db" / "migrations"
COMPOSE = ROOT / "deploy" / "compose" / "docker-compose.yml"
SPLIT = re.compile(r"^-- @@\s*$", re.M)

PG_DSN = os.environ.get("FS_PG_DSN", "postgresql://postgres:postgres@localhost:5432/fleetsentinel")
APP_PW = os.environ.get("FS_APP_PASSWORD", "fs_app_dev")
WORKER_PW = os.environ.get("FS_WORKER_PASSWORD", "fs_worker_dev")
KAFKA = os.environ.get("FS_KAFKA_BOOTSTRAP", "localhost:19092")


def _migration_files() -> list[tuple[int, Path]]:
    out = []
    for f in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        out.append((int(f.name[:4]), f))
    return out


def ensure_roles(conn) -> None:
    from psycopg import sql

    for role, pw, extra in (("fs_app", APP_PW, "NOBYPASSRLS"), ("fs_worker", WORKER_PW, "BYPASSRLS")):
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(sql.SQL("{} ROLE {} LOGIN {} PASSWORD {}").format(
            sql.SQL(verb), sql.Identifier(role), sql.SQL(extra), sql.Literal(pw)))


def cmd_migrate(args: argparse.Namespace) -> int:
    import psycopg

    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        ensure_roles(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                     "(version int PRIMARY KEY, name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        for version, path in _migration_files():
            if args.to is not None and version > args.to:
                break
            if version in done:
                continue
            print(f"applying {path.name} ...", flush=True)
            text = path.read_text(encoding="utf-8")
            chunks = [c.strip() for c in SPLIT.split(text)] if SPLIT.search(text) else [text]
            for chunk in chunks:
                if chunk:
                    conn.execute(chunk)
            conn.execute("INSERT INTO schema_migrations (version, name) VALUES (%s, %s)", (version, path.name))
    print("migrations up to date")
    return 0


def cmd_topics(args: argparse.Namespace) -> int:
    import yaml
    from confluent_kafka.admin import AdminClient, NewTopic

    spec = yaml.safe_load((ROOT / "contracts" / "topics.yaml").read_text(encoding="utf-8"))
    admin = AdminClient({"bootstrap.servers": KAFKA})
    new = []
    for name, t in spec["topics"].items():
        cfg = {"compression.type": t.get("compression", "producer")}
        if t.get("cleanup_policy") == "compact":
            cfg["cleanup.policy"] = "compact"
        elif "retention_ms" in t:
            cfg["retention.ms"] = str(t["retention_ms"])
        new.append(NewTopic(name, num_partitions=t["partitions"], replication_factor=args.rf, config=cfg))
    for name, fut in admin.create_topics(new).items():
        try:
            fut.result()
            print(f"created {name}")
        except Exception as e:  # noqa: BLE001 - already-exists is expected on re-run
            print(f"{name}: {e}")
    return 0


def _compose(*extra: str) -> int:
    profiles = os.environ.get("FS_PROFILES", "infra").split(",")
    cmd = ["docker", "compose", "-f", str(COMPOSE)]
    for p in profiles:
        cmd += ["--profile", p.strip()]
    return subprocess.call([*cmd, *extra])


def main() -> int:
    ap = argparse.ArgumentParser(prog="fs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate")
    m.add_argument("--to", type=int, default=None)
    t = sub.add_parser("topics")
    t.add_argument("--rf", type=int, default=1)
    for name in ("up", "bootstrap", "down", "logs", "ps"):
        sub.add_parser(name)
    args = ap.parse_args()
    if args.cmd == "migrate":
        return cmd_migrate(args)
    if args.cmd == "topics":
        return cmd_topics(args)
    if args.cmd in ("up", "bootstrap"):
        subprocess.check_call([sys.executable, str(ROOT / "tools" / "gen_certs.py")])
        rc = _compose("up", "-d", "--wait")
        if rc or args.cmd == "up":
            return rc
        print("stack healthy - applying migrations and creating topics")
        cmd_migrate(argparse.Namespace(to=None))
        return cmd_topics(argparse.Namespace(rf=int(os.environ.get("FS_RF", "1"))))
    if args.cmd == "down":
        return _compose("down")
    if args.cmd == "logs":
        return _compose("logs", "-f", "--tail=100")
    if args.cmd == "ps":
        return _compose("ps")
    return 1


if __name__ == "__main__":
    sys.exit(main())
