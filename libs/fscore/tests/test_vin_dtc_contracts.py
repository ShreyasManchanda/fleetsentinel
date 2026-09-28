import random
from pathlib import Path

import pytest
import yaml
from fscore import contracts, dtc, hashing, vin
from hypothesis import given
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[3]


# ---- VIN ----
def test_brief_example_vin_is_valid():
    assert vin.is_valid_vin("1HGCM82633A004352")


@pytest.mark.parametrize("bad", ["", "1HGCM82633A00435", "1HGCM82633A0043521", "1HGCM82633I004352", "1HGCM82633O004352",
                                 "1HGCM82633Q004352", "1hgcm82633a004352", "1HGCM82643A004352"])
def test_invalid_vins(bad):
    assert not vin.is_valid_vin(bad)


def test_check_digit_x():
    # find a VIN whose check digit is X to cover the remainder-10 branch
    rng = random.Random(1)
    for _ in range(2000):
        v = vin.make_vin("1HG", rng)
        if v[8] == "X":
            assert vin.is_valid_vin(v)
            return
    pytest.fail("no X check digit produced in 2000 samples")


@given(st.integers(min_value=0, max_value=2**32))
def test_generated_vins_always_valid(seed):
    v = vin.make_vin("WVW", random.Random(seed))
    assert vin.is_valid_vin(v)


@given(st.integers(min_value=0, max_value=2**32), st.sampled_from("0123456789"))
def test_wrong_check_digit_never_validates(seed, digit):
    v = vin.make_vin("YV1", random.Random(seed))
    if digit == v[8]:
        return
    mutated = v[:8] + digit + v[9:]
    assert not vin.is_valid_vin(mutated)


def test_make_vin_rejects_bad_wmi():
    with pytest.raises(ValueError):
        vin.make_vin("IOQ", random.Random(0))
    with pytest.raises(ValueError):
        vin.make_vin("AB", random.Random(0))


def test_normalize_vin():
    assert vin.normalize_vin("  1hgcm82633a004352 ") == "1HGCM82633A004352"


# ---- DTC ----
@pytest.mark.parametrize("code", ["P0301", "P0A80", "U0100", "B0001", "C0035", "P1A00"])
def test_valid_dtc(code):
    assert dtc.is_valid_dtc(code)


@pytest.mark.parametrize("code", ["", "X0301", "P030", "P03011", "p0301", "P0G01", None, 123])
def test_invalid_dtc(code):
    assert not dtc.is_valid_dtc(code)  # type: ignore[arg-type]


def test_parse_dtc_fields():
    d = dtc.parse_dtc("p0301")
    assert d.code == "P0301" and d.system == "powertrain" and d.generic
    assert d.subsystem == "ignition_misfire" and "misfire" in (d.description or "").lower()
    assert not d.critical
    assert dtc.parse_dtc("P0AA6").critical
    assert dtc.parse_dtc("P1A00").generic is False
    assert dtc.parse_dtc("P3400").generic is True
    assert dtc.parse_dtc("P3000").generic is False
    assert dtc.parse_dtc("U0100").generic is True and dtc.parse_dtc("U1000").generic is False
    assert dtc.parse_dtc("U0100").subsystem is None


def test_parse_dtc_rejects_malformed():
    with pytest.raises(ValueError):
        dtc.parse_dtc("nope")


def test_parse_dtc_list():
    assert dtc.parse_dtc_list(None) == []
    assert dtc.parse_dtc_list("p0301, P0301;U0100  zzz") == ["P0301", "U0100"]
    assert dtc.parse_dtc_list(["P0300", "bad", "P0300"]) == ["P0300"]


# ---- contracts / hashing ----
def test_topic_constants_match_topics_yaml():
    spec = yaml.safe_load((ROOT / "contracts" / "topics.yaml").read_text(encoding="utf-8"))
    assert set(spec["topics"]) == set(contracts.ALL_TOPICS)
    assert spec["topics"][contracts.TELEMETRY_NORMALIZED]["partitions"] == contracts.N_PARTITIONS_BULK


def test_deterministic_ids():
    a = contracts.alert_id("1HGCM82633A004352", "TOW", 1000)
    assert a == contracts.alert_id("1HGCM82633A004352", "TOW", 1000)
    assert a != contracts.alert_id("1HGCM82633A004352", "TOW", 1001)
    assert contracts.trip_id("V", 1) != contracts.trip_id("V", 2)


KAFKA_MURMUR2_VECTORS = [  # from Kafka's clients UtilsTest (signed 32-bit)
    (b"21", -973932308),
    (b"foobar", -790332482),
    (b"a-little-bit-long-string", -985981536),
    (b"a-little-bit-longer-string", -1486304829),
    (b"lkjh234lh9fiuh90y23oiuhsafujhadof229phr9h19h89h8", -58897971),
    (b"abc", 479470107),
]


@pytest.mark.parametrize(("data", "signed"), KAFKA_MURMUR2_VECTORS)
def test_murmur2_matches_kafka_reference(data, signed):
    h = hashing.murmur2(data)
    assert (h - (1 << 32) if h >= (1 << 31) else h) == signed


def test_vin_partition_in_range_and_shard_alias():
    for v in ("1HGCM82633A004352", "WVWZZZ1JZ3W386752", "YV1LW5678X1234567"):
        p = hashing.vin_partition(v, 64)
        assert 0 <= p < 64 and p == hashing.vin_shard(v, 64)


def test_partition_spread_is_even():
    rng = random.Random(7)
    counts = [0] * 64
    for _ in range(64_000):
        counts[hashing.vin_partition(vin.make_vin("1HG", rng), 64)] += 1
    assert max(counts) / min(counts) < 1.25   # no hot partition for random VINs
