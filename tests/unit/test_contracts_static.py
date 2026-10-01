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


# Frozen registry: (mapping document, sample batch, envelope record key).
# oem_d_draft.yaml is NOT registered at startup (live-onboarding demo) but its
# sample must still exist and normalize cleanly.
MAPPING_SAMPLE_PAIRS = [
    ("oem_a.yaml", "oem_a.json", "records"),
    ("oem_b.yaml", "oem_b.json", "d"),
    ("oem_c.yaml", "oem_c.json", "events"),
    ("oem_d_draft.yaml", "oem_d.json", "rows"),
]


def test_all_four_mappings_exist_with_required_keys():
    import json

    maps = ROOT / "contracts" / "mappings"
    assert sorted(p.name for p in maps.glob("oem_*.yaml")) == sorted(p[0] for p in MAPPING_SAMPLE_PAIRS)
    for doc_name, sample_name, _ in MAPPING_SAMPLE_PAIRS:
        doc = yaml.safe_load((maps / doc_name).read_text(encoding="utf-8"))
        for key in ("oem", "version", "envelope", "fields"):
            assert key in doc, f"{doc_name} missing key {key}"
        assert "records" in doc["envelope"], f"{doc_name} envelope has no records path"
        sample = json.loads((maps / "samples" / sample_name).read_text(encoding="utf-8"))
        assert isinstance(sample, dict) and sample, f"{sample_name} is not a JSON object batch"


def test_envelope_records_key_matches_sample_shape():
    import json

    maps = ROOT / "contracts" / "mappings"
    for doc_name, sample_name, record_key in MAPPING_SAMPLE_PAIRS:
        doc = yaml.safe_load((maps / doc_name).read_text(encoding="utf-8"))
        assert record_key in doc["envelope"]["records"], f"{doc_name} records path mismatch"
        sample = json.loads((maps / "samples" / sample_name).read_text(encoding="utf-8"))
        assert record_key in sample, f"{sample_name} lacks envelope key {record_key!r}"
        if "columns" in doc["envelope"]:
            assert doc["envelope"]["columns"].split(".")[-1] in sample, f"{sample_name} lacks columnar key"


def test_no_orphan_samples():
    maps = ROOT / "contracts" / "mappings"
    expected = {p[1] for p in MAPPING_SAMPLE_PAIRS}
    actual = {p.name for p in (maps / "samples").glob("oem_*.json")}
    assert actual == expected, f"orphan or missing samples: {actual ^ expected}"


@pytest.mark.parametrize("doc_name,sample_name,_key", MAPPING_SAMPLE_PAIRS,
                         ids=[p[0] for p in MAPPING_SAMPLE_PAIRS])
def test_every_sample_normalizes_cleanly(doc_name, sample_name, _key):
    import json

    from fscore.mapping_engine import Mapping

    maps = ROOT / "contracts" / "mappings"
    doc = yaml.safe_load((maps / doc_name).read_text(encoding="utf-8"))
    batch = json.loads((maps / "samples" / sample_name).read_text(encoding="utf-8"))
    result = Mapping(doc).apply(batch, now_ms=1_790_590_600_000)
    assert len(result.ok) > 0, f"{sample_name} yields zero normalized records"
    assert len(result.failed) == 0, f"{sample_name} has failures: {result.failed[:2]}"


def test_startup_registry_is_exactly_oem_a_b_c():
    from fs_normalizer.settings import Settings

    assert Settings(lane="bulk").resolved().startup_mappings == "oem_a.yaml,oem_b.yaml,oem_c.yaml"


WRITE_METHODS = ("post", "put", "patch", "delete")
# POST-only exemptions reviewed 2026-10-01: chat/preview/mappings-activate are
# safe-to-retry reads-with-side-effects owned server-side; reports/batch is the
# high-volume simulator ingest where the batch id is the dedup key.
EXPECTED_IDEMPOTENCY_EXEMPT = {
    "/v1/reports/batch", "/v1/agent/chat", "/v1/mappings", "/v1/mappings/preview",
}


def test_all_write_ops_require_idempotency_key():
    spec = yaml.safe_load((ROOT / "contracts" / "openapi.yaml").read_text(encoding="utf-8"))
    validate(spec)
    exempt_found = set()
    for path, item in spec["paths"].items():
        for method in WRITE_METHODS:
            op = item.get(method)
            if not op:
                continue
            refs = [p.get("$ref", "") for p in op.get("parameters", [])]
            if any(r.endswith("IdempotencyKey") for r in refs):
                continue
            exempt_found.add(path)
    assert exempt_found == EXPECTED_IDEMPOTENCY_EXEMPT, (
        f"idempotency exemptions drifted: {sorted(exempt_found)}")


def test_topics_yaml_covers_all_fscore_contract_names():
    from fscore import contracts as C

    topics = yaml.safe_load((ROOT / "contracts" / "topics.yaml").read_text(encoding="utf-8"))
    assert set(C.ALL_TOPICS) <= set(topics["topics"]), (
        f"topics.yaml missing: {set(C.ALL_TOPICS) - set(topics['topics'])}")
    headers = [h.value if hasattr(h, "value") else h for h in topics["headers"]]
    for const in (C.H_OEM, C.H_BATCH_ID, C.H_RECEIVED_TS_MS, C.H_TRACEPARENT,
                  C.H_ERROR_REASON, C.H_MAPPING_VERSION):
        assert const in headers, f"header {const} missing from topics.yaml"
    for group in ("normalizer", "normalizer-priority"):
        assert group in topics["consumer_groups"], f"consumer group {group} missing"
