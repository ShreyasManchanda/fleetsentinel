"""Static contract checks that need no running services: SQL syntax, OpenAPI validity, mapping/sample presence."""
import re
from pathlib import Path

import pglast
import pytest
import yaml
from openapi_spec_validator import validate

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("path", sorted((ROOT / "db" / "migrations").glob("*.sql")), ids=lambda p: p.name)
def test_migration_sql_parses(path):
    text = path.read_text(encoding="utf-8")
    chunks = re.split(r"^-- @@\s*$", text, flags=re.M) if "-- @@" in text else [text]
    assert sum(len(pglast.parse_sql(c)) for c in chunks if c.strip()) > 0


def test_migrations_are_numbered_contiguously():
    nums = [int(p.name[:4]) for p in sorted((ROOT / "db" / "migrations").glob("[0-9]*.sql"))]
    assert nums == list(range(1, len(nums) + 1))


def test_openapi_is_valid_and_every_write_requires_idempotency_key():
    spec = yaml.safe_load((ROOT / "contracts" / "openapi.yaml").read_text(encoding="utf-8"))
    validate(spec)
    exempt = {"/v1/reports/batch", "/v1/agent/chat", "/v1/mappings", "/v1/mappings/preview"}
    for path, item in spec["paths"].items():
        post = item.get("post")
        if post and path not in exempt:
            refs = [p.get("$ref", "") for p in post.get("parameters", [])]
            assert any(r.endswith("IdempotencyKey") for r in refs), f"POST {path} lacks Idempotency-Key"


def test_every_mapping_has_a_sample():
    for m in (ROOT / "contracts" / "mappings").glob("oem_*.yaml"):
        letter = m.stem.split("_")[1]
        assert (ROOT / "contracts" / "mappings" / "samples" / f"oem_{letter}.json").exists(), m.name
