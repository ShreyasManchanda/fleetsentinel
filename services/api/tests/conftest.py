import pytest
from api_support import build
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(scope="session")
def rsa_private():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def rsa_other():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def h(rsa_private, rsa_other):
    return build(rsa_private, rsa_other)
