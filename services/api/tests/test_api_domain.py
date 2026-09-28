import asyncio

import pytest
from fs_api.domain import geo, pagination, privacy
from fs_api.domain.audit import (
    AuditRecord,
    ListAuditPublisher,
    ResilientAuditPublisher,
    from_proto,
    resource_type_of,
    to_proto,
    uuid7,
)
from fs_api.domain.breaker import CLOSED, HALF_OPEN, OPEN, BreakerProxy, CircuitBreaker, CircuitOpen
from fs_api.domain.ratelimit import InMemoryBucketStore, Limit, RateLimiter, bucket_step, rate_headers
from fs_api.domain.roles import (
    PERMISSIONS,
    Perm,
    Purpose,
    Role,
    can,
    location_precision,
    parse_roles,
    permissions_of,
    purpose_allowed,
)
from hypothesis import given
from hypothesis import strategies as st

SECRET = "s3cret"
T = "a1968e1a-1866-53ab-ada0-79dde61e800d"


# ---------------------------------------------------------------- pagination
def test_cursor_roundtrip_and_binding():
    c = pagination.encode_cursor(SECRET, "vehicles", T, [0.5, "VIN"])
    assert pagination.decode_cursor(SECRET, "vehicles", T, c) == [0.5, "VIN"]
    with pytest.raises(pagination.BadCursor):
        pagination.decode_cursor(SECRET, "alerts", T, c)  # other endpoint
    with pytest.raises(pagination.BadCursor):
        pagination.decode_cursor(SECRET, "vehicles", "another-tenant", c)  # other tenant
    with pytest.raises(pagination.BadCursor):
        pagination.decode_cursor("wrong-secret", "vehicles", T, c)


@given(st.text(max_size=80))
def test_garbage_cursor_never_crashes(junk):
    with pytest.raises(pagination.BadCursor):
        pagination.decode_cursor(SECRET, "vehicles", T, junk)


def test_cursor_tamper():
    c = pagination.encode_cursor(SECRET, "k", T, [1])
    body, sig = c.split(".")
    forged = pagination.encode_cursor("other", "k", T, [999]).split(".")[0] + "." + sig
    with pytest.raises(pagination.BadCursor):
        pagination.decode_cursor(SECRET, "k", T, forged)


def test_limits_and_page_builder():
    assert pagination.clamp_limit(None) == 50
    assert pagination.clamp_limit(0) == 1
    assert pagination.clamp_limit(10_000) == 200
    items, nxt = pagination.build_page([1, 2, 3], 2, lambda r: f"c{r}")
    assert items == [1, 2] and nxt == "c2"
    assert pagination.build_page([1, 2], 2, str) == ([1, 2], None)


# ---------------------------------------------------------------- roles
def test_role_matrix_is_deny_by_default():
    assert can(frozenset({Role.LENDER_AGENT}), Perm.FLEET_READ)
    assert not can(frozenset({Role.LENDER_AGENT}), Perm.DRIVER_READ)
    assert not can(frozenset({Role.VIEWER}), Perm.DSR)
    assert not can(frozenset({Role.FLEET_ADMIN}), Perm.MAPPING_APPROVE)  # activation is platform_admin only
    assert can(frozenset({Role.PLATFORM_ADMIN}), Perm.MAPPING_APPROVE)
    assert permissions_of(frozenset()) == frozenset()
    assert set(PERMISSIONS) == set(Role)


def test_parse_roles_ignores_unknown():
    assert parse_roles(["fleet_admin", "root", 5]) == frozenset({Role.FLEET_ADMIN})
    assert parse_roles("viewer") == frozenset({Role.VIEWER})
    assert parse_roles(None) == frozenset()
    assert parse_roles({"a": 1}) == frozenset()


def test_purpose_and_precision():
    lender, admin, viewer, analyst = (frozenset({r}) for r in (Role.LENDER_AGENT, Role.FLEET_ADMIN, Role.VIEWER, Role.ANALYST))
    assert not purpose_allowed(admin, Purpose.RECOVERY) and purpose_allowed(lender, Purpose.RECOVERY)
    assert purpose_allowed(admin, Purpose.SAFETY_REVIEW)
    assert location_precision(admin, Purpose.OPERATIONS) == "exact"
    assert location_precision(analyst, Purpose.OPERATIONS) == "geohash7"
    assert location_precision(viewer, Purpose.OPERATIONS) == "geohash6"
    assert location_precision(lender, Purpose.OPERATIONS) == "geohash5"
    assert location_precision(lender, Purpose.RECOVERY, vehicle_flagged=False) == "geohash5"
    assert location_precision(lender, Purpose.RECOVERY, vehicle_flagged=True) == "exact"
    assert location_precision(viewer, Purpose.SAFETY_REVIEW) == "geohash7"
    assert location_precision(admin, Purpose.COMPLIANCE) == "geohash5"


# ---------------------------------------------------------------- geo
def test_geohash_known_values():
    assert geo.geohash_encode(57.64911, 10.40744, 11) == "u4pruydqqvj"
    assert geo.geohash_encode(12.9716, 77.5946, 5) == "tdr1v"


@given(st.floats(-89.9, 89.9), st.floats(-179.9, 179.9), st.integers(1, 9))
def test_geohash_center_inside_cell_and_stable(lat, lon, n):
    gh = geo.geohash_encode(lat, lon, n)
    lo, hi, wo, ea = geo.geohash_bounds(gh)
    assert lo <= lat <= hi and wo <= lon <= ea
    c_lat, c_lon = geo.geohash_center(gh)
    assert geo.geohash_encode(c_lat, c_lon, n) == gh


def test_degrade_loses_precision():
    lat, lon = 12.971598, 77.594562
    assert geo.degrade(lat, lon, "exact") == (lat, lon)
    d5 = geo.degrade(lat, lon, "geohash5")
    d7 = geo.degrade(lat, lon, "geohash7")
    assert abs(d5[0] - lat) > abs(d7[0] - lat) or abs(d5[1] - lon) > abs(d7[1] - lon)
    assert geo.degrade(lat, lon, "geohash6") != (lat, lon)


def test_bbox_parsing():
    b = geo.parse_bbox("77.4,12.8,77.8,13.2")
    assert b.contains(13.0, 77.6) and not b.contains(14.0, 77.6) and b.center() == (13.0, 77.6)
    assert geo.parse_bbox([1, 2, 3, 4]).east == 3
    for bad in ("1,2,3", "a,b,c,d", "10,0,5,1", "0,10,5,5", "-200,0,5,1"):
        with pytest.raises(ValueError):
            geo.parse_bbox(bad)
    assert geo.cell_precision_for_zoom(3) == 3 and geo.cell_precision_for_zoom(6) == 4 and geo.cell_precision_for_zoom(8) == 5


# ---------------------------------------------------------------- k-anonymity
def test_k_anonymity_suppresses_small_cells_and_hides_ids():
    rows = [(v, 12.9716, 77.5946, 3600.0, 1) for v in range(12)]          # 12 distinct vehicles in one cell -> published
    rows += [(100 + v, 28.6139, 77.2090, 7200.0, 5) for v in range(9)]     # 9 vehicles -> suppressed
    rows += [(0, 12.9716, 77.5946, 3600.0, 1)]                              # same vehicle again: must not raise the count
    out = privacy.k_anonymize(rows)
    assert out["k_anonymity"] == 10 and out["location_precision"] == "geohash5"
    assert out["suppressed_cells"] == 1 and len(out["cells"]) == 1
    cell = out["cells"][0]
    assert cell["geohash"] == "tdr1v" and cell["vehicles_approx"] == 12 and cell["idle_hours"] == 13.0
    assert out["totals"]["vehicles_approx"] == 12  # totals cover PUBLISHED cells only (no differencing attack)
    assert "vin" not in str(out).lower() and "driver" not in str(out).lower()


@given(st.lists(st.tuples(st.integers(0, 40), st.floats(12.9, 13.1), st.floats(77.5, 77.7), st.floats(0, 1e4), st.integers(0, 5)), max_size=200))
def test_k_anonymity_invariant(rows):
    out = privacy.k_anonymize(rows, k=10)
    assert all(c["vehicles_approx"] >= 10 for c in out["cells"])


# ---------------------------------------------------------------- circuit breaker
class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_breaker_closed_open_half_open():
    clk, changes = Clock(), []
    b = CircuitBreaker("pg", failure_threshold=3, reset_timeout_s=10, clock=clk, on_state_change=lambda n, s: changes.append(s))

    async def boom():
        raise ConnectionError("down")

    async def fine():
        return "ok"

    async def go():
        for _ in range(3):
            with pytest.raises(ConnectionError):
                await b.call(boom)
        assert b.state == OPEN
        with pytest.raises(CircuitOpen) as ei:  # fast fail, dependency not touched
            await b.call(fine)
        assert ei.value.retry_after_s > 0
        clk.t = 11
        assert b.state == HALF_OPEN
        with pytest.raises(ConnectionError):  # failed probe re-opens
            await b.call(boom)
        assert b.state == OPEN
        clk.t = 22
        assert await b.call(fine) == "ok"  # successful probe closes
        assert b.state == CLOSED

    asyncio.run(go())
    assert changes == [OPEN, HALF_OPEN, OPEN, HALF_OPEN, CLOSED]


def test_breaker_half_open_allows_single_probe_and_ignores_business_errors():
    clk = Clock()
    b = CircuitBreaker("x", 1, 5, should_trip=lambda e: isinstance(e, OSError), clock=clk)
    b.record_failure(OSError())
    clk.t = 6
    b.before_call()  # the probe
    with pytest.raises(CircuitOpen):
        b.before_call()  # second concurrent caller is rejected while probing
    b.record_failure(ValueError("business"))  # not an infra failure: probe counts as proof of life
    assert b.state == CLOSED
    b.record_failure(ValueError("business"))
    assert b.state == CLOSED


def test_breaker_proxy_wraps_coroutines_only():
    class Svc:
        attr = 5

        async def ok(self):
            return 1

        async def bad(self):
            raise OSError

        def sync(self):
            return "s"

    b = CircuitBreaker("p", 1, 100)
    p = BreakerProxy(Svc(), b)
    assert p.attr == 5 and p.sync() == "s"

    async def go():
        assert await p.ok() == 1
        with pytest.raises(OSError):
            await p.bad()
        with pytest.raises(CircuitOpen):
            await p.ok()

    asyncio.run(go())


# ---------------------------------------------------------------- rate limiting
def test_bucket_step_refills_and_denies():
    ok, tokens, retry = bucket_step(2, 0, 0, 10, 1.0, 1)
    assert ok and tokens == 1 and retry == 0
    ok, tokens, retry = bucket_step(0, 0, 0, 10, 2.0, 1)
    assert not ok and retry == 0.5
    ok, tokens, _ = bucket_step(0, 0, 100, 10, 1.0, 1)  # refill is capped at capacity
    assert ok and tokens == 9


def test_limiter_enforces_and_reports_headers():
    now = [1000.0]
    store = InMemoryBucketStore(clock=lambda: now[0])
    lim = RateLimiter(store)
    limits = [Limit("tenant", "t", 1000), Limit("user", "u", 3)]

    async def go():
        for i in range(3):
            ok, d = await lim.check(limits)
            assert ok and d.remaining == 3 - (i + 1)
        ok, d = await lim.check(limits)
        assert not ok and d.retry_after_s >= 1 and d.limit == 3
        assert rate_headers(d)["Retry-After"] == str(d.retry_after_s)
        now[0] += 60  # a minute later the bucket is full again
        ok, _ = await lim.check(limits)
        assert ok

    asyncio.run(go())
    h = rate_headers(type("D", (), {"limit": 5, "remaining": 2, "reset_s": 9, "allowed": True, "retry_after_s": 0})())
    assert h == {"RateLimit-Limit": "5", "RateLimit-Remaining": "2", "RateLimit-Reset": "9"}


def test_limiter_degrades_to_local_when_redis_fails():
    class Broken:
        async def take(self, *a, **k):
            raise ConnectionError("redis down")

    lim = RateLimiter(Broken())

    async def go():
        ok, d = await lim.check([Limit("user", "u", 2)])
        assert ok
        await lim.check([Limit("user", "u", 2)])
        ok, _ = await lim.check([Limit("user", "u", 2)])
        assert not ok  # still limited (per instance) instead of failing open entirely

    asyncio.run(go())
    assert lim.degraded == 3


# ---------------------------------------------------------------- audit records
def test_audit_proto_roundtrip_and_uuid7():
    r = AuditRecord(action="GET /v1/vehicles/{vin}", decision="DENY", tenant_id=T, actor_id="u1", resource_ids=["V1"], purpose="RECOVERY",
                    reason="role", latency_ms=12, attributes={"k": "v"})
    back = from_proto(to_proto(r).SerializeToString())
    assert back.action == r.action and back.decision == "DENY" and back.resource_ids == ["V1"] and back.attributes == {"k": "v"}
    assert back.event_id == r.event_id and back.ts_ms == r.ts_ms
    a, b = uuid7(), uuid7()
    assert a != b and a[14] == "7"
    assert resource_type_of("/v1/vehicles/{vin}/trips") == "vehicle" and resource_type_of("/") == ""


def test_resilient_publisher_buffers_and_drains():
    class Flaky:
        def __init__(self):
            self.up, self.got = False, []

        async def publish(self, rec):
            if not self.up:
                raise ConnectionError
            self.got.append(rec)

    flaky = Flaky()
    pub = ResilientAuditPublisher(flaky, max_buffer=3)

    async def go():
        for i in range(5):
            await pub.publish(AuditRecord(action=f"a{i}", decision="ALLOW"))
        assert pub.buffered == 3 and pub.dropped == 2  # bounded, overflow is COUNTED not silent
        flaky.up = True
        await pub.publish(AuditRecord(action="a5", decision="ALLOW"))

    asyncio.run(go())
    assert [r.action for r in flaky.got] == ["a3", "a4", "a5"] and pub.buffered == 0 and pub.dropped == 3
    lst = ListAuditPublisher()
    asyncio.run(lst.publish(AuditRecord(action="x", decision="ALLOW")))
    assert len(lst.records) == 1
