"""Alert lifecycle: cooldowns, occurrences, deterministic replay ids."""
from __future__ import annotations

from fs_processor.domain.lifecycle import AlertLedger
from fs_processor.domain.rules import Finding
from fscore import contracts as C

T0 = 1_790_590_000_000


def _finding(rule="TOW"):
    return Finding(rule=rule, summary=f"{rule}!", impact_usd=12.5)


def _ledger(now=0):
    return AlertLedger(clock=lambda: now)


def test_first_emit_and_deterministic_id():
    lg = _ledger()
    a = lg.maybe_emit("V1", "t1", _finding(), T0, T0 + 50, 1.0, 2.0, {}, T0)
    assert a is not None
    assert a.alert_id == C.alert_id("V1", "TOW", T0)
    assert a.tenant_id == "t1" and a.impact_usd == 12.5
    assert a.details["occurrences"] == "1"
    assert a.ingest_ts_ms == T0 + 50 and a.detected_ts_ms == 0


def test_cooldown_suppresses_and_counts_occurrences():
    lg = AlertLedger(clock=lambda: T0)
    a1 = lg.maybe_emit("V1", "t1", _finding(), T0, T0, 0.0, 0.0, {})
    assert a1 is not None
    lg._clock = lambda: T0 + 60_000  # inside the 1h TOW cooldown
    assert lg.maybe_emit("V1", "t1", _finding(), T0 + 60_000, T0, 0.0, 0.0, {}) is None
    lg._clock = lambda: T0 + 3_601_000  # past cooldown
    a2 = lg.maybe_emit("V1", "t1", _finding(), T0 + 3_601_000, T0, 0.0, 0.0, {})
    # occurrences counts every repeat incl. the suppressed one (1 initial + 1 suppressed + this one)
    assert a2 is not None and a2.details["occurrences"] == "3"


def test_crash_has_no_cooldown():
    lg = AlertLedger(clock=lambda: T0)
    assert lg.maybe_emit("V1", "t1", _finding("CRASH"), T0, T0, 0.0, 0.0, {}) is not None
    assert lg.maybe_emit("V1", "t1", _finding("CRASH"), T0 + 1, T0, 0.0, 0.0, {}) is not None


def test_replay_produces_identical_ids_no_duplicates():
    """Kill + restore: the same findings must yield the same alert ids (spec: replay determinism)."""
    lg = AlertLedger(clock=lambda: T0)
    ids_before = [lg.maybe_emit("V1", "t1", _finding("TOW"), T0, T0, 0.0, 0.0, {}).alert_id]
    snap = lg.snapshot()
    lg2 = AlertLedger(clock=lambda: T0 + 3_601_000)
    lg2.restore(snap)
    # same finding after restore but inside ORIGINAL cooldown state? occurrences carried over:
    a = lg2.maybe_emit("V1", "t1", _finding("TOW"), T0, T0, 0.0, 0.0, {})
    assert a is not None and a.alert_id == ids_before[0] and a.details["occurrences"] == "2"


def test_resolve_and_trace_id():
    lg = _ledger()
    lg.maybe_emit("V1", "t1", _finding(), T0, T0, 0.0, 0.0,
                  {C.H_TRACEPARENT: b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba9027b-01"})
    assert lg.resolve("V1", "TOW") is True and lg.resolve("V1", "TOW") is False
    assert len(lg) == 0
    a = lg.maybe_emit("V1", "t1", _finding(), T0, T0, 0.0, 0.0,
                      {C.H_TRACEPARENT: b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba9027b-01"})
    assert a.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    b = lg.maybe_emit("V2", "t1", _finding(), T0, T0, 0.0, 0.0, {})
    assert b.trace_id == ""
