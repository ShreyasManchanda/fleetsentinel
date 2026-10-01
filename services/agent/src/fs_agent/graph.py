"""LangGraph answering loop with an injected LLM caller (stub in tests/CI, real model in prod).

Flow: route -> call allowlisted tools (budgeted, tenant-injected) -> guard (grounding,
retry-then-degrade) -> answer. The graph never sees raw driver PII: backends
pseudonymize before this layer (spec 11.3).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from .guardrails import MAX_TOOL_CALLS, RetryPolicy, TurnBudget, degraded_answer, grounding_check
from .tools import BadToolArgs, ToolRegistry, UnknownTool


class Turn(TypedDict, total=False):
    tenant_id: str
    question: str
    plan: list[dict]          # [{tool, args}] from the LLM
    results: list[dict]
    calls: list[dict]
    answer: str
    grounded: bool
    degraded: bool


LlmCaller = Callable[[str, list[dict], list[dict]], dict]
# llm(question, tool_descriptions, history) -> {"plan": [...]} or {"answer": "..."}


def build_graph(registry: ToolRegistry, llm: LlmCaller):
    g = StateGraph(Turn)

    def route(state: Turn) -> dict:
        out = llm(state["question"], registry.describe(), state.get("calls", [])) or {}
        if "plan" in out and isinstance(out["plan"], list):
            return {"plan": [p for p in out["plan"] if isinstance(p, dict)][:MAX_TOOL_CALLS]}
        return {"answer": str(out.get("answer", "")), "grounded": True}

    def act(state: Turn) -> dict:
        budget = TurnBudget()
        retries = RetryPolicy()
        results, calls = list(state.get("results", [])), list(state.get("calls", []))
        for step in state.get("plan", []):
            name = step.get("tool", "")
            args = step.get("args", {}) if isinstance(step.get("args"), dict) else {}
            if not budget.take():
                break
            try:
                res = registry.call(state["tenant_id"], name, args)
                results.append(res)
                calls.append({"tool": name, "args": args, "ok": True})
            except (UnknownTool, BadToolArgs) as e:
                calls.append({"tool": name, "args": args, "ok": False, "error": str(e)})
                if retries.again(name):
                    fix = llm(f"Tool call failed: {e}. Reply with a corrected plan step.",
                              registry.describe(), calls)
                    plan = fix.get("plan", []) if isinstance(fix, dict) else []
                    if plan and isinstance(plan[0], dict):
                        try:
                            res = registry.call(state["tenant_id"], plan[0].get("tool", ""),
                                                plan[0].get("args", {}))
                            results.append(res)
                            calls.append({"tool": plan[0].get("tool"), "args": plan[0].get("args"),
                                          "ok": True, "repaired": True})
                        except (UnknownTool, BadToolArgs) as e2:
                            calls.append({"tool": plan[0].get("tool"), "ok": False,
                                          "error": str(e2)})
        return {"results": results, "calls": calls}

    def answer(state: Turn) -> dict:
        if state.get("answer") and not state.get("results"):
            grounded, _ = grounding_check(state["answer"], [])
            if not grounded:
                return degraded_answer(state["question"], [])
            return {"answer": state["answer"], "grounded": True, "degraded": False}
        out = llm(f"Answer this question using ONLY these tool results: {state['question']}",
                  [], state.get("results", [])) or {}
        text = str(out.get("answer", ""))
        grounded, _ = grounding_check(text, state.get("results", []))
        if not grounded:
            once = llm("Rewrite using only numbers from the tool results.", [], state.get("results", [])) or {}
            text = str(once.get("answer", text))
            grounded, _ = grounding_check(text, state.get("results", []))
        if not grounded:
            return degraded_answer(state["question"], state.get("results", []))
        return {"answer": text, "grounded": True, "degraded": False}

    g.add_node("route", route)
    g.add_node("act", act)
    g.add_node("answer", answer)
    g.add_conditional_edges("route", lambda s: "answer" if s.get("answer") and not s.get("plan") else "act")
    g.add_edge("act", "answer")
    g.add_edge("answer", END)
    g.set_entry_point("route")
    return g.compile()


def ask(registry: ToolRegistry, llm: LlmCaller, tenant_id: str, question: str) -> dict[str, Any]:
    """One Q&A turn. Tenant scope enforced at every tool call."""
    app = build_graph(registry, llm)
    out = app.invoke({"tenant_id": tenant_id, "question": question})
    return {"answer": out.get("answer", ""), "grounded": bool(out.get("grounded", False)),
            "tool_calls": out.get("calls", []), "degraded": bool(out.get("degraded", False))}
