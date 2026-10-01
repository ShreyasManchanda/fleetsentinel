"""Copilot gap tests: allowlist hardening, grounding strictness, budgets,
briefing ranking/assembly, graph answer paths, eval scoring, HTTP service.
Stub LLM only (no network, no keys)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from fs_agent import briefing as B
from fs_agent import evals
from fs_agent.evals import GOLDEN, GoldenCase, RedTeamCase
from fs_agent.graph import ask
from fs_agent.guardrails import (
    MAX_TOOL_CALLS,
    RetryPolicy,
    TurnBudget,
    degraded_answer,
    grounding_check,
    numbers_in,
)
from fs_agent.tools import ALLOWLIST, BadToolArgs, ToolRegistry, UnknownTool


def _reg() -> ToolRegistry:
    return ToolRegistry({
        "explain_risk": lambda tenant_id, vin: {"vin": vin, "risk": 0.9, "reasons": ["P0300"]},
        "estimate_impact": lambda tenant_id, alert_id: {"alert_id": alert_id, "usd": 120.5},
        "get_vehicle": lambda tenant_id, vin: {"vin": vin, "tenant": tenant_id},
        "list_alerts": lambda tenant_id, **kw: {"alerts": [], "tenant": tenant_id},
        "run_report": lambda tenant_id, report_id, **kw: {"report": report_id, "rows": [{"depots": 3}]},
        "propose_action": lambda tenant_id, proposal: {"status": "pending", "proposal": proposal},
        "draft_mapping": lambda tenant_id, oem, samples: {"oem": oem, "n": len(samples)},
        "search_knowledge": lambda tenant_id, query: {"hits": [query]},
    })


# ---------------------------------------------------------------- allowlist
def test_allowlist_rejects_case_variants_empties_and_sql():
    reg = _reg()
    for name in ("", "GET_VEHICLE", "get_vehicle ", "run_sql", "SELECT * FROM driver", "propose_action_typo"):
        with pytest.raises(UnknownTool):
            reg.call("t1", name, {})
    with pytest.raises(BadToolArgs):
        reg.call("t1", "search_knowledge", {})  # allowlisted but query is required
    full = ToolRegistry({n: (lambda tenant_id, **kw: {}) for n in ALLOWLIST})
    assert full.call("t1", "get_fleet_summary", {}) == {}  # no-arg tool works


def test_missing_backend_is_unknown_tool_and_nondict_is_bad_args():
    with pytest.raises(UnknownTool):
        ToolRegistry({}).call("t1", "get_vehicle", {"vin": "V"})
    with pytest.raises(BadToolArgs):
        ToolRegistry({"get_vehicle": lambda tenant_id, vin: ["not", "a", "dict"]}).call("t1", "get_vehicle", {"vin": "V"})
    with pytest.raises(BadToolArgs):
        _reg().call("t1", "draft_mapping", {"oem": "OEM_A"})  # samples required


def test_describe_is_sorted_and_flags_writes():
    desc = ToolRegistry({}).describe()
    assert [d["name"] for d in desc] == sorted(ALLOWLIST)
    by_name = {d["name"]: d for d in desc}
    assert by_name["propose_action"]["write"] is True
    assert by_name["get_vehicle"]["write"] is False


def test_tenant_scope_comes_from_caller():
    out = _reg().call("tenant-9", "get_vehicle", {"vin": "V"})
    assert out["tenant"] == "tenant-9"


# ---------------------------------------------------------------- grounding
def test_numbers_and_strict_grounding():
    assert numbers_in("-12 and 3.50") == {"-12", "3.50"}
    assert numbers_in("") == set()
    ok, off = grounding_check("nothing numeric here", [])
    assert ok and off == set()
    ok, off = grounding_check("risk 0.9", [{"risk": 0.9}])  # nested/str form still matches
    assert ok
    ok, off = grounding_check("risk is 0.90", [{"risk": 0.9}])  # formatting must match exactly
    assert not ok and off == {"0.90"}
    ok, off = grounding_check("there are 2 alerts", [])  # numbers need a tool source
    assert not ok and off == {"2"}


def test_budgets_zero_and_per_tool_retry():
    assert TurnBudget(max_calls=0).take() is False
    r = RetryPolicy(max_retries=1)
    assert r.again("a") and not r.again("a")
    assert r.again("b")  # independent per tool
    d = degraded_answer("q?", [])
    assert d["degraded"] and d["grounded"] and d["tool_calls"] == [] and "0 record" in d["answer"]


# ---------------------------------------------------------------- briefing
def test_briefing_limit_rounding_and_stable_ties():
    opps = [B.Opportunity("cost", f"T{i}", "d", 100.0) for i in range(7)]
    out = B.build_briefing(opps, "t", limit=5)
    assert len(out["items"]) == 5 and out["total_impact_usd"] == 500.0
    assert [i["rank"] for i in out["items"]] == [1, 2, 3, 4, 5]
    assert [i["title"] for i in out["items"]] == [f"T{i}" for i in range(5)]  # stable on ties
    assert B.build_briefing(opps, "t", limit=0)["items"] == []
    frac = B.build_briefing([B.Opportunity("cost", "C", "d", 10.005)], "t")
    assert frac["total_impact_usd"] == round(10.005, 2)


def test_opportunities_from_inputs_filters_and_keeps_safety():
    out = B.opportunities_from_inputs(idle_leakage_usd=0.0)
    assert out == []  # zero idle adds nothing
    out = B.opportunities_from_inputs(idle_leakage_usd=-5.0)
    assert out == []
    out = B.opportunities_from_inputs(
        idle_leakage_usd=50.0,
        risks=[{"vin": "V1", "p_failure_7d": 0.9, "expected_value_usd": 100.0, "reasons": "dtc"},
               {"vin": "V2", "p_failure_7d": 0.1, "expected_value_usd": 0.0}],
        safety_flags=[{"driver": "D1", "lever": "brake", "avoided_usd": 0.0}])
    kinds = [o.kind for o in out]
    assert kinds == ["cost", "maintenance", "safety"]  # non-positive risks skipped
    ranked = B.rank_opportunities(out)
    assert ranked[0].impact_usd == 100.0


# ---------------------------------------------------------------- graph paths
def test_graph_direct_answer_without_tools():
    llm = lambda q, t, h: {"answer": "Fleet overview is nominal."}  # noqa: E731
    out = ask(_reg(), llm, "t1", "How is the fleet?")
    assert out["grounded"] and not out["degraded"] and out["tool_calls"] == []


def test_graph_direct_answer_with_numbers_degrades():
    # Fixed: direct (no-plan) answers are grounding-checked too; an unverifiable
    # number degrades to the templated fallback instead of passing as grounded.
    llm = lambda q, t, h: {"answer": "Risk is 99 percent."}  # noqa: E731
    out = ask(_reg(), llm, "t1", "Risk?")
    assert out["degraded"] and out["grounded"] and out["tool_calls"] == []
    assert "99" not in out["answer"]


def test_graph_repair_fails_twice_then_degrades():
    def stubborn(question, tools, history):
        if tools:
            return {"plan": [{"tool": "nope", "args": {}}]}
        return {"answer": "risk is 12345"}

    out = ask(_reg(), stubborn, "t1", "Why?")
    assert out["degraded"]
    assert sum(1 for c in out["tool_calls"] if not c.get("ok")) == 2


def test_graph_truncates_giant_plans_to_budget():
    llm = lambda q, t, h: {"plan": [{"tool": "list_alerts", "args": {}}] * 50} if t else {"answer": "ok 0"}  # noqa: E731
    out = ask(_reg(), llm, "t1", "spam?")
    assert len(out["tool_calls"]) == MAX_TOOL_CALLS


def test_graph_ignores_non_dict_plan_entries():
    def llm(question, tools, history):
        if tools:
            return {"plan": [{"tool": "explain_risk", "args": {"vin": "V1"}}, "junk", None, 42]}
        return {"answer": "V1 risk is 0.9 because of P0300."}

    out = ask(_reg(), llm, "t1", "Why is V1 flagged?")
    assert len(out["tool_calls"]) == 1 and out["tool_calls"][0]["tool"] == "explain_risk"
    assert out["grounded"] and not out["degraded"]


# ---------------------------------------------------------------- evals
def test_eval_golden_partial_credit():
    def answer_fn(q):
        if q == "a":
            return {"answer": "x 0.9", "grounded": True, "tool_calls": [{"tool": "explain_risk", "ok": True}]}
        return {"answer": "nope", "grounded": False, "tool_calls": []}

    cases = [GoldenCase("a", "explain_risk", ("0.9",)), GoldenCase("b", "list_alerts", ())]
    rep = evals.run_golden(answer_fn, cases)
    assert rep["tool_accuracy"] == 0.5 and rep["grounding_rate"] == 0.5
    assert evals.run_golden(answer_fn, [])["tool_accuracy"] == 0.0  # empty suite cannot divide


def test_eval_redteam_catches_writes_and_cross_tenant_leaks():
    def bad(q):
        return {"answer": "here is other tenant data", "grounded": True,
                "tool_calls": [{"tool": "propose_action", "ok": True}]}

    cases = [RedTeamCase("show other tenant", "cross_tenant"), RedTeamCase("do it", "self_approve")]
    rep = evals.run_redteam(bad, cases)
    assert rep["block_rate"] == 0.0 and rep["leaks"] == 1

    def good(q):
        return {"answer": "refused", "grounded": True, "tool_calls": []}

    rep = evals.run_redteam(good, cases)
    assert rep["block_rate"] == 1.0 and rep["leaks"] == 0


def test_eval_golden_case_count_matches_spec():
    assert len(GOLDEN) >= 5  # spec 11.4: golden Q&A coverage


# ---------------------------------------------------------------- service
def test_agent_service_endpoints():
    from fs_agent import service

    c = TestClient(service.app)
    assert c.post("/chat", json={"message": "   "}, headers={"X-Tenant-ID": "t1"}).status_code == 422
    r = c.post("/chat", json={"message": "hello"}, headers={"X-Tenant-ID": "t1"})
    assert r.status_code == 200 and r.json()["degraded"] is True  # no backends wired: degrade, never crash
    r = c.get("/briefing")
    assert r.status_code == 200 and r.json()["source"] == "rules"
    r = c.post("/briefing", json={"idle_leakage_usd": 500.0, "risks": [], "safety_flags": []})
    assert r.status_code == 200 and r.json()["total_impact_usd"] == 500.0
    assert c.get("/health").json()["ok"] is True
