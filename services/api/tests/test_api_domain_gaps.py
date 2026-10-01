"""Domain gap tests: report validation branches, DSR redaction, shared
application helpers, breaker/rate-limit edges, cursor edges, JWKS caching,
request-context parsing. Pure unit tests (no HTTP, no live infra)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fs_api.api.context import bearer_token, parse_purpose
from fs_api.application import common as C
from fs_api.application.compliance import redact_dsr
from fs_api.application.reports import validate_report_params
from fs_api.config import Settings
from fs_api.domain import pagination
from fs_api.domain.audit import AuditRecord, ListAuditPublisher, ResilientAuditPublisher, from_proto, to_proto
from fs_api.domain.breaker import BreakerProxy, CircuitBreaker, CircuitOpen
from fs_api.domain.privacy import k_anonymize
from fs_api.domain.ratelimit import (
    InMemoryBucketStore,
    Limit,
    RateLimiter,
    bucket_step,
    rate_headers,
)
from fs_api.domain.roles import Purpose
from fs_api.errors import ProblemError
from fs_api.security.jwt_auth import CachingJwksProvider
from starlette.requests import Request


def _settings() -> Settings:
    return Settings(env="dev", cursor_secret="test-cursor-secret", ws_tick_s=0.05, ws_auth_timeout_s=1.0)


# ---------------------------------------------------------------- report params
def test_report_params_rejects_bad_types_and_unknown_keys():
    with pytest.raises(ProblemError) as e:
        validate_report_params("fuel_idle_by_depot", {"bogus": 1})
    assert e.value.status == 422
    with pytest.raises(ProblemError):
        validate_report_params("fuel_idle_by_depot", {"min_idle_min": True})  # bool is not int
    with pytest.raises(ProblemError):
        validate_report_params("fuel_idle_by_depot", {"min_idle_min": 5.0})
    with pytest.raises(ProblemError):
        validate_report_params("carbon_by_fleet", {"from": 20260101})  # not a date string
    with pytest.raises(ProblemError):
        validate_report_params("carbon_by_fleet", {"from": "01/02/2026"})  # not YYYY-MM-DD
    assert validate_report_params("carbon_by_fleet", {"from": "2026-01-01", "to": "2026-01-31"}) == {
        "from": "2026-01-01", "to": "2026-01-31"}
    assert validate_report_params("carbon_by_fleet", None) == {}


# ---------------------------------------------------------------- DSR redaction
def test_redact_dsr_replaces_export_and_does_not_mutate_input():
    payload = {"id": "d1", "evidence": {"export": {"driver": {"name": "PII"}}, "kind": "ACCESS"}}
    out = redact_dsr(payload)
    assert out["evidence"]["export"] == "[redacted: available only in the original response]"
    assert out["evidence"]["kind"] == "ACCESS"
    assert payload["evidence"]["export"] == {"driver": {"name": "PII"}}  # input untouched
    bare = redact_dsr({"id": "d2"})
    assert bare["evidence"] == {}


# ---------------------------------------------------------------- common helpers
def test_pseudonym_stable_opaque_and_null_safe():
    a = C.pseudonym("s", "driver-1")
    assert a == C.pseudonym("s", "driver-1") and a.startswith("drv_")
    assert C.pseudonym("s", "driver-2") != a
    assert C.pseudonym("other", "driver-1") != a
    assert C.pseudonym("s", None) is None


def test_sanitize_twin_strips_identity_and_degrades_coords():
    twin = {"_id": "m1", "driver_id": "d1", "name": "truck",
            "pos": {"lat": 12.971598, "lon": 77.594562},
            "history": [{"driver_ref": "d1", "lat": 12.971598, "lon": 77.594562, "v": 1}]}
    out = C.sanitize_twin(twin, "geohash5")
    assert out is not None
    assert "_id" not in out and "driver_id" not in out
    assert out["name"] == "truck"
    assert (out["pos"]["lat"], out["pos"]["lon"]) != (12.971598, 77.594562)
    assert "driver_ref" not in out["history"][0] and out["history"][0]["v"] == 1
    assert C.sanitize_twin(None, "exact") is None


def test_masked_location_and_iso():
    assert C.masked_location(None, 1.0, "exact") == (None, None)
    assert C.masked_location(1.0, None, "exact") == (None, None)
    assert C.iso_from_ms(None) is None and C.iso_from_ms(0) is None
    assert C.iso_from_ms(1_700_000_000_000) == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def test_risk_detail_thresholds_and_filtering():
    s = _settings()
    be = s.cost_inspection_usd / s.cost_breakdown_usd
    row = {"p_failure_7d": 0.5, "model_name": "lgbm", "model_version": "v3",
           "reason_codes": [{"feature": "dtc", "contribution": 0.4, "human": "h"}, "junk", {"feature": "x"}],
           "predicted_at": "t"}
    out = C.risk_detail(row, s)
    assert out is not None and out["recommendation"] == "INSPECT" and out["fallback"] is False
    assert out["reason_codes"] == [{"feature": "dtc", "contribution": 0.4, "human": "h"}]
    assert out["expected_value_usd"] == round(0.5 * s.cost_breakdown_usd - s.cost_inspection_usd, 2)
    mid = C.risk_detail({**row, "p_failure_7d": be * 0.75}, s)
    assert mid is not None and mid["recommendation"] == "MONITOR"
    low = C.risk_detail({**row, "p_failure_7d": 0.0}, s)
    assert low is not None and low["recommendation"] == "NONE"
    rule = C.risk_detail({**row, "model_name": "rule-baseline"}, s)
    assert rule is not None and rule["fallback"] is True
    assert C.risk_detail(None, s) is None


def test_alert_out_latency():
    base = {"id": "a", "vin": "V", "rule_code": "TOW", "severity": "CRITICAL", "status": "open",
            "opened_at": 1, "impact_at_creation": None}
    det = datetime(2026, 1, 1, tzinfo=UTC)
    row = {**base, "detected_ts": det, "ingest_ts": det - timedelta(seconds=1.5)}
    assert C.alert_out(row)["latency_ms"] == 1500
    assert C.alert_out({**base, "detected_ts": det - timedelta(seconds=5), "ingest_ts": det})["latency_ms"] == 0
    assert C.alert_out(base)["latency_ms"] is None


# ---------------------------------------------------------------- breaker edges
def test_breaker_success_resets_consecutive_failures():
    clk = [0.0]
    b = CircuitBreaker("s", failure_threshold=3, reset_timeout_s=10, clock=lambda: clk[0])
    b.record_failure(OSError())
    b.record_failure(OSError())
    b.record_success()
    b.record_failure(OSError())
    b.record_failure(OSError())
    assert b.state == "closed"  # the earlier two do not count anymore


def test_breaker_default_trips_on_business_errors_and_reports_attrs():
    b = CircuitBreaker("d", failure_threshold=1, reset_timeout_s=5)
    b.record_failure(ValueError("business"))
    assert b.state == "open"
    try:
        b.before_call()
        raise AssertionError("should raise")
    except CircuitOpen as e:
        assert e.name == "d" and e.retry_after_s > 0


def test_breaker_proxy_counts_business_errors_by_default():
    class Svc:
        async def bad(self):
            raise ValueError("business")

    b = CircuitBreaker("p", failure_threshold=1, reset_timeout_s=100)
    p = BreakerProxy(Svc(), b)

    async def go():
        with pytest.raises(ValueError):
            await p.bad()
        with pytest.raises(CircuitOpen):
            await p.bad()

    asyncio.run(go())


# ---------------------------------------------------------------- rate-limit edges
def test_rate_headers_allowed_has_no_retry_after():
    from fs_api.domain.ratelimit import Decision
    h = rate_headers(Decision(True, 5, 2, 0, 9))
    assert h == {"RateLimit-Limit": "5", "RateLimit-Remaining": "2", "RateLimit-Reset": "9"}


def test_bucket_step_cost_above_capacity():
    ok, tokens, retry = bucket_step(10.0, 0.0, 0.0, 10, 1.0, 11)
    assert not ok and tokens == 10.0 and retry == 1.0


def test_bucket_keys_are_independent():
    async def go():
        store = InMemoryBucketStore()
        d1 = await store.take("a", 1, 1 / 60)
        d2 = await store.take("b", 1, 1 / 60)
        assert d1.allowed and d2.allowed
        d3 = await store.take("a", 1, 1 / 60)
        assert not d3.allowed

    asyncio.run(go())


def test_limiter_short_circuits_on_first_denial():
    now = [1000.0]
    lim = RateLimiter(InMemoryBucketStore(clock=lambda: now[0]))

    async def go():
        await lim.check([Limit("user", "u1", 1)])
        ok, d = await lim.check([Limit("user", "u1", 1), Limit("tenant", "t1", 1000)])
        assert not ok and d.limit == 1  # the exhausted user bucket drives the decision

    asyncio.run(go())


# ---------------------------------------------------------------- pagination edges
def test_pagination_edges():
    assert pagination.clamp_limit(-5) == 1
    assert pagination.build_page([], 10, str) == ([], None)
    with pytest.raises(pagination.BadCursor):
        pagination.decode_cursor("s", "k", "t", "no-separator-here")
    with pytest.raises(pagination.BadCursor):
        pagination.decode_cursor("s", "k", "t", "e30.truncated!")


# ---------------------------------------------------------------- JWKS caching
def test_caching_jwks_ttl_and_unknown_kid_backoff():
    calls, clock = [], [0.0]
    store = {"k1": "KEY1"}

    def fetch():
        calls.append(1)
        return dict(store)

    p = CachingJwksProvider(fetch, ttl_s=10, min_refresh_s=30, clock=lambda: clock[0])
    assert p.get_key("k1") == "KEY1" and len(calls) == 1
    assert p.get_key("k1") == "KEY1" and len(calls) == 1  # cached
    clock[0] = 11.0  # ttl expired -> refresh
    assert p.get_key("k1") == "KEY1" and len(calls) == 2
    with pytest.raises(KeyError):
        p.get_key("nope")  # refresh just happened; no hammering
    assert len(calls) == 2
    clock[0] = 50.0  # backoff elapsed -> one more refresh attempt
    with pytest.raises(KeyError):
        p.get_key("nope")
    assert len(calls) == 3
    store["k2"] = "KEY2"  # rotation picked up on the next refresh
    clock[0] = 100.0
    assert p.get_key("k2") == "KEY2"


# ---------------------------------------------------------------- request context
def _req(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "headers": raw})


def test_bearer_and_purpose_parsing():
    assert bearer_token(_req({"authorization": "Bearer abc"})) == "abc"
    with pytest.raises(ProblemError):
        bearer_token(_req({}))
    with pytest.raises(ProblemError):
        bearer_token(_req({"authorization": "Bearer   "}))
    with pytest.raises(ProblemError):
        bearer_token(_req({"authorization": "Basic abc"}))
    assert parse_purpose(None) is Purpose.OPERATIONS
    assert parse_purpose("  ") is Purpose.OPERATIONS
    assert parse_purpose("recovery") is Purpose.RECOVERY
    with pytest.raises(ProblemError) as e:
        parse_purpose("BOGUS")
    assert e.value.status == 422


# ---------------------------------------------------------------- audit extras
def test_audit_negative_latency_clamped_and_flush_empty_ok():
    r = AuditRecord(action="a", decision="ALLOW", latency_ms=-5)
    assert to_proto(r).latency_ms == 0
    assert from_proto(to_proto(r).SerializeToString()).action == "a"
    pub = ResilientAuditPublisher(ListAuditPublisher())
    asyncio.run(pub.flush())
    assert pub.buffered == 0


def test_resilient_publisher_drains_oldest_first():
    inner = ListAuditPublisher()
    pub = ResilientAuditPublisher(inner, max_buffer=10)

    async def go():
        for i in range(3):
            await pub.publish(AuditRecord(action=f"a{i}", decision="ALLOW"))

    asyncio.run(go())
    assert [r.action for r in inner.records] == ["a0", "a1", "a2"]
    assert pub.published == 3 and pub.buffered == 0


def test_k_anonymize_empty_is_safe():
    out = k_anonymize([], k=10)
    assert out["cells"] == [] and out["suppressed_cells"] == 0
