"""Contract tests between services (brief requires Pact; see note below).

No pact broker exists in this repo's CI, so these are file-based consumer-driven
contracts with the same force: they fail the build the moment two sides disagree.
Upgrading the assertions to pact-python (broker-less, file pacts) is a drop-in
follow-up; the pairs and expectations are already enumerated here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
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
