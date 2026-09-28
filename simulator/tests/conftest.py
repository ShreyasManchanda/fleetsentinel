from __future__ import annotations

import pytest

from fs_simulator.fleet import generate_fleet
from fs_simulator.routes import build_library


@pytest.fixture(scope="session")
def routes():
    return build_library(3)


@pytest.fixture(scope="session")
def fleet():
    return generate_fleet(7, 2000)


@pytest.fixture(scope="session")
def full_fleet():
    return generate_fleet(42)
