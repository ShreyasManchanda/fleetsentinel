"""Copilot unit tests: tools, guardrails, briefing, graph with a stub LLM, evals."""
from __future__ import annotations

import pytest
from fs_agent import briefing as B
from fs_agent import evals
from fs_agent.evals import GOLDEN
from fs_agent.graph import ask
from fs_agent.guardrails import (
    RetryPolicy,
    TurnBudget,
    degraded_answer,
    grounding_check,
    numbers_in,
)
from fs_agent.tools import BadToolArgs, ToolRegistry, UnknownTool


def _reg() -> ToolRegistry:
    return ToolRegistry({
        "explain_risk": lambda tenant_id, vin: {"vin": vin, "risk": 0.9, "reasons": ["P0300"]},
        "estimate_impact": lambda tenant_id, alert_id: {"alert_id": alert_id, "usd": 120.5},
        "get_vehicle": lambda tenant_id, vin: {"vin": vin, "tenant": tenant_id},
        "list_alerts": lambda tenant_id, **kw: {"alerts": [], "tenant": tenant_id},
        "run_report": lambda tenant_id, report_id, **kw: {"report": report_id, "rows": [{"depots": 3}]},
        "propose_action": lambda tenant_id, proposal: {"status": "pending", "proposal": proposal},
    })


def test_unknown_tool_and_bad_args_rejected():
    reg = _reg()
    with pytest.raises(UnknownTool):
        reg.call("t1", "drop_database", {})
    with pytest.raises(UnknownTool):
        reg.call("t1", "propose_action_typo", {"proposal": {}})
    with pytest.raises(BadToolArgs):
        reg.call("t1", "get_vehicle", {})
    assert reg.call("t1", "get_vehicle", {"vin": "V"})["tenant"] == "t1"  # injected, not from LLM


def test_grounding_check():
    ok, off = grounding_check("risk 0.9 saves 120.5", [{"risk": 0.9}, {"usd": 120.5}])
    assert ok and off == set()
    ok, off = grounding_check("risk 0.99 saves lots", [{"risk": 0.9}])
    assert not ok and off == {"0.99"}
    assert numbers_in("no digits") == set()


def test_budgets_and_retry():
    b = TurnBudget(max_calls=1)
    assert b.take() and not b.take()
    r = RetryPolicy(max_retries=1)
    assert r.again("x") and not r.again("x")
    d = degraded_answer("q?", [{"a": 1}])
    assert d["degraded"] and "1 record" in d["answer"]


def test_briefing_ranked_with_total():
    opps = [B.Opportunity("cost", "Idle", "d", 500.0),
            B.Opportunity("maintenance", "Inspect V", "p=0.9", 1200.0),
            B.Opportunity("safety", "Coach", "lever", 0.0)]
    out = B.build_briefing(opps, "2026-09-29T00:00:00Z")
    assert out["source"] == "rules" and out["total_impact_usd"] == 1700.0
    assert [i["kind"] for i in out["items"]] == ["maintenance", "cost", "safety"]
    assert B.build_briefing([], "t")["headline"].startswith("Nothing")


def _scripted_llm(route_resp, answer_resp):
    """Route calls arrive with a non-empty tools list; answer calls with []."""
    def llm(question, tools, history):
        if tools:
            return dict(route_resp)
        return dict(answer_resp)
    return llm


def test_graph_grounded_flow_through_stub():
    reg = _reg()
    llm = _scripted_llm(
        {"plan": [{"tool": "explain_risk", "args": {"vin": "V1"}}]},
        {"answer": "V1 risk is 0.9 because of P0300."},
    )
    out = ask(reg, llm, "t1", "Why is V1 flagged?")
    assert out["grounded"] and not out["degraded"]
    assert out["tool_calls"] and out["tool_calls"][0]["tool"] == "explain_risk"
    assert "0.9" in out["answer"]


def test_graph_repairs_bad_tool_call():
    reg = _reg()

    def bad_first(question, tools, history):
        if tools and "failed" not in question:
            return {"plan": [{"tool": "nope", "args": {}}]}
        if tools:
            return {"plan": [{"tool": "explain_risk", "args": {"vin": "V2"}}]}
        return {"answer": "V2 risk is 0.9."}

    out = ask(reg, bad_first, "t1", "Why?")
    repaired = [c for c in out["tool_calls"] if c.get("repaired")]
    assert len(repaired) == 1 and repaired[0]["tool"] == "explain_risk"
    assert out["grounded"] and not out["degraded"]


def test_graph_refuses_hallucinated_numbers():
    reg = _reg()
    llm = _scripted_llm(
        {"plan": [{"tool": "explain_risk", "args": {"vin": "V1"}}]},
        {"answer": "risk is 12345 percent"},
    )
    out = ask(reg, llm, "t1", "Why is V1 flagged?")
    assert out["degraded"] and out["grounded"]  # templated fallback, no invented numbers


def test_graph_budget_caps_tool_storm():
    reg = _reg()
    llm = lambda q, t, h: {"plan": [{"tool": "list_alerts", "args": {}}] * 50}  # noqa: E731
    out = ask(reg, llm, "t1", "spam?")
    assert len(out["tool_calls"]) <= 8


def test_eval_harness_with_stub():
    def answer_fn(q):
        for c in GOLDEN:
            if q == c.question:
                tool = c.must_call
                args = {"vin": "V1"} if tool in ("explain_risk", "get_vehicle",
                                                 "find_lookalike_vehicles") else (
                    {"alert_id": "A1"} if tool == "estimate_impact" else (
                        {"report_id": "r"} if tool == "run_report" else (
                            {"query": "q"} if tool == "search_knowledge" else {})))
                try:
                    res = _reg().call("t1", tool, args)
                    text = f"{tool} says {res}"
                    if c.must_contain and c.must_contain[0] not in text:
                        text += f" {c.must_contain[0]}"
                    return {"answer": text, "grounded": True,
                            "tool_calls": [{"tool": tool, "ok": True}]}
                except (UnknownTool, BadToolArgs):
                    return {"answer": "cannot", "grounded": True, "tool_calls": []}
        return {"answer": "refused", "grounded": True, "tool_calls": []}

    rep = evals.run_all(answer_fn)
    assert rep.tool_accuracy == 1.0 and rep.grounding_rate == 1.0
    assert rep.redteam_block_rate == 1.0 and rep.cross_tenant_leaks == 0
