"""Copilot guardrails (spec 11.3): grounding, budgets, retry-then-degrade.

Pure functions so CI tests them without any LLM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_TOOL_CALLS = 8
MAX_TOOL_RETRIES = 2

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def numbers_in(text: str) -> set[str]:
    return set(_NUM.findall(text or ""))


def grounding_check(answer: str, tool_results: list[dict]) -> tuple[bool, set[str]]:
    """Every number in the answer must appear in some tool result of this turn.

    Returns (grounded, offenders). Empty offenders + True means fully grounded.
    """
    pool: set[str] = set()
    for r in tool_results:
        pool |= numbers_in(str(r))
    offenders = {n for n in numbers_in(answer) if n not in pool}
    return (not offenders, offenders)


@dataclass(slots=True)
class TurnBudget:
    max_calls: int = MAX_TOOL_CALLS
    used: int = 0

    def take(self) -> bool:
        if self.used >= self.max_calls:
            return False
        self.used += 1
        return True


@dataclass(slots=True)
class RetryPolicy:
    max_retries: int = MAX_TOOL_RETRIES
    attempts: dict[str, int] = field(default_factory=dict)

    def again(self, tool_name: str) -> bool:
        n = self.attempts.get(tool_name, 0) + 1
        self.attempts[tool_name] = n
        return n <= self.max_retries


def degraded_answer(question: str, records: list[dict]) -> dict:
    """Templated fallback: link raw records, assert nothing unverified."""
    return {"answer": (f"I couldn't complete that automatically. Here is what I found "
                       f"({len(records)} record(s)); open them in the console to continue. "
                       f"Your question was: {question}"),
            "grounded": True, "tool_calls": [], "degraded": True}
