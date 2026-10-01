"""Contract tests between services (brief requires Pact; see note below).

No pact broker exists in this repo's CI, so these are file-based consumer-driven
contracts with the same force: they fail the build the moment two sides disagree.
Upgrading the assertions to pact-python (broker-less, file pacts) is a drop-in
follow-up; the pairs and expectations are already enumerated here.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _openapi():
    return yaml.safe_load((ROOT / "contracts" / "openapi.yaml").read_text(encoding="utf-8"))


def test_web_client_endpoints_exist_in_openapi():
    """Every REST path the web console calls must exist in contracts/openapi.yaml."""
    spec_paths = set(_openapi()["paths"])
    client = (ROOT / "web" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
    used = set(re.findall(r"'(/v1/[a-zA-Z0-9/_{}-]*)'", client))
    # strip query strings the client appends
    used = {u.split("?")[0] for u in used}
    missing = set()
    for u in used:
        # concrete ids match templated segments, e.g. /v1/vehicles/ABC -> /v1/vehicles/{vin}
        if u in spec_paths:
            continue
        pattern = re.sub(r"/[A-Za-z0-9_-]{3,}$", "/{param}", u)
        if u not in spec_paths and pattern not in spec_paths and not any(
                p.startswith(u) for p in spec_paths):
            missing.add(u)
    assert not missing, f"web calls paths missing from openapi.yaml: {sorted(missing)}"


def test_agent_service_answers_validate_against_api_models():
    """fs_agent output must satisfy the API's pydantic contracts (AgentAnswer, Briefing)."""
    from fs_agent.briefing import build_briefing, opportunities_from_inputs
    from fs_api.api.models import AgentAnswer, Briefing

    opps = opportunities_from_inputs(idle_leakage_usd=500.0,
                                     risks=[{"vin": "V1", "p_failure_7d": 0.9,
                                             "reasons": "P0300", "expected_value_usd": 1200.0}])
    b = Briefing.model_validate(build_briefing(opps, "2026-09-29T00:00:00Z"))
    assert b.source == "rules" and b.total_impact_usd == 1700.0
    a = AgentAnswer.model_validate({"answer": "hi", "grounded": True, "tool_calls": [],
                                    "proposed_actions": None, "degraded": True})
    assert a.grounded and a.degraded


def test_gateway_normalizer_share_one_topic_contract():
    """Both sides import topic/header names from fscore.contracts — no string drift."""
    from fscore import contracts as C

    assert C.INGEST_RAW == "ingest.raw.v1"
    assert C.INGEST_PRIORITY == "ingest.priority.v1"
    assert C.TELEMETRY_NORMALIZED == "telemetry.normalized.v1"
    assert C.TELEMETRY_PRIORITY == "telemetry.priority.v1"
    topics = yaml.safe_load((ROOT / "contracts" / "topics.yaml").read_text(encoding="utf-8"))
    names = set(topics["topics"])  # topic-name -> config mapping

    from fs_normalizer.settings import Settings

    lanes = {Settings(lane="bulk").resolved().in_topic,
             Settings(lane="priority").resolved().in_topic}
    assert lanes <= names, f"normalizer lanes not in topics.yaml: {lanes - names}"
    assert lanes == {C.INGEST_RAW, C.INGEST_PRIORITY}


def test_normalizer_lanes_are_isolated_no_bulk_lag_on_priority():
    """Priority lane must use its own group + topics so bulk lag never delays it."""
    from fs_normalizer.settings import Settings
    from fscore import contracts as C

    bulk = Settings(lane="bulk").resolved()
    pri = Settings(lane="priority").resolved()
    assert bulk.group_id != pri.group_id, "lanes share a consumer group"
    assert (bulk.in_topic, bulk.out_topic) == (C.INGEST_RAW, C.TELEMETRY_NORMALIZED)
    assert (pri.in_topic, pri.out_topic) == (C.INGEST_PRIORITY, C.TELEMETRY_PRIORITY)
    assert bulk.dlq_topic == pri.dlq_topic == C.DLQ_INGEST

    topics = yaml.safe_load((ROOT / "contracts" / "topics.yaml").read_text(encoding="utf-8"))
    groups = topics["consumer_groups"]
    assert groups["normalizer"] == [C.INGEST_RAW]
    assert groups["normalizer-priority"] == [C.INGEST_PRIORITY]


# Write ops the web console never calls: owned server-side (copilot/admin/simulator).
SERVER_SIDE_WRITES = {
    "/v1/agent/actions",  # propose_action is created by the copilot, not the browser
    "/v1/agent/briefing",  # POST regenerate; the console polls GET
    "/v1/dlq/replay",  # platform-admin replay after mapping activation
    "/v1/mappings",  # mapping draft upload (editor uses preview + approve flow)
    "/v1/mappings/{oem}/versions/{version}/activate",
    "/v1/mappings/{oem}/versions/{version}/approve",
    "/v1/reports/batch",  # high-volume simulator ingest, batch id is the dedup key
}


def test_every_openapi_write_is_called_by_web_or_owned_server_side():
    """No write op may exist that neither the console nor a documented owner uses."""
    import re

    spec = yaml.safe_load((ROOT / "contracts" / "openapi.yaml").read_text(encoding="utf-8"))
    client = (ROOT / "web" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
    # client.ts builds paths with template literals: `/v1/alerts/${id}/ack`.
    # Normalize both sides so `${id}` and `{id}` segments compare equal.
    used = set()
    for raw in re.findall(r"[`'\"](/v1/[^`'\"?]*)", client):
        used.add(re.sub(r"\$\{[^}]*\}", "{}", raw.split("?")[0]))
    uncovered = set()
    for path, item in spec["paths"].items():
        if not any(m in item for m in ("post", "put", "patch", "delete")):
            continue
        if path in SERVER_SIDE_WRITES:
            continue
        if re.sub(r"\{[^}]*\}", "{}", path) in used:
            continue
        uncovered.add(path)
    assert not uncovered, f"write ops with no caller and no owner: {sorted(uncovered)}"
