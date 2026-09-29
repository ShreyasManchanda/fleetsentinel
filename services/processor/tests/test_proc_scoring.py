"""RiskScorer: rule baseline, fallback visibility, never-raises guarantee."""
from __future__ import annotations

import pytest
from fs_processor.domain.scoring import (
    BASELINE_HIGH,
    BASELINE_LOW,
    BASELINE_VERSION,
    RISK_THRESHOLD,
    Features,
    RiskScorer,
    baseline_score,
)


def test_baseline_scores():
    assert baseline_score(True) == BASELINE_HIGH
    assert baseline_score(False) == BASELINE_LOW
    assert BASELINE_HIGH >= RISK_THRESHOLD > BASELINE_LOW


def test_missing_model_falls_back_and_reports():
    seen = []
    s = RiskScorer(model_path="/nonexistent/model.txt", on_fallback=seen.append)
    assert s.fallback is True and seen == [True]
    score, version = s.score(Features(dtc_count=2), True)
    assert (score, version) == (BASELINE_HIGH, BASELINE_VERSION)
    high, _, _ = s.is_high_risk(Features(), False)
    assert high is False


def test_broken_model_falls_back_without_raising(monkeypatch):
    import sys
    import types

    class Boom:
        def predict(self, rows):
            raise RuntimeError("bad model")

    fake = types.ModuleType("lightgbm")
    fake.Booster = lambda model_file: Boom()
    monkeypatch.setitem(sys.modules, "lightgbm", fake)
    s = RiskScorer(model_path="whatever")
    assert s.fallback is False  # load "succeeded"; failure surfaces at predict time
    score, version = s.score(Features(dtc_count=0), False)
    assert (score, version) == (BASELINE_LOW, BASELINE_VERSION)
    assert s.fallback is True


def test_working_model_scores_and_clamps(monkeypatch):
    import sys
    import types

    class Fixed:
        def __init__(self, p):
            self.p = p

        def predict(self, rows):
            assert len(rows[0]) == 4  # feature vector width
            return [self.p]

    fake = types.ModuleType("lightgbm")
    fake.Booster = lambda model_file: Fixed(9.9)
    monkeypatch.setitem(sys.modules, "lightgbm", fake)
    s = RiskScorer(model_path="m")
    score, _ = s.score(Features(), False)
    assert score == pytest.approx(1.0)  # clamped to [0,1]
