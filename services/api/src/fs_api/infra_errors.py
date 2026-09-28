"""Classification of infrastructure failures (these trip circuit breakers and map to 503, never to 500)."""
from __future__ import annotations

import asyncio
from functools import lru_cache

from .domain.breaker import CircuitOpen
from .errors import ProblemError


@lru_cache
def infra_error_types() -> tuple[type[BaseException], ...]:
    types: list[type[BaseException]] = [OSError, TimeoutError, asyncio.TimeoutError, ConnectionError]
    try:
        from sqlalchemy.exc import InterfaceError, OperationalError

        types += [OperationalError, InterfaceError]
    except ImportError:  # pragma: no cover
        pass
    try:
        import psycopg

        types += [psycopg.OperationalError, psycopg.InterfaceError]
    except ImportError:  # pragma: no cover
        pass
    try:
        from redis.exceptions import RedisError

        types.append(RedisError)
    except ImportError:  # pragma: no cover
        pass
    try:
        from pymongo.errors import PyMongoError

        types.append(PyMongoError)
    except ImportError:  # pragma: no cover
        pass
    try:
        from httpx import TransportError

        types.append(TransportError)
    except ImportError:  # pragma: no cover
        pass
    try:
        from confluent_kafka import KafkaException

        types.append(KafkaException)
    except ImportError:  # pragma: no cover
        pass
    return tuple(types)


def is_infra_error(exc: BaseException) -> bool:
    if isinstance(exc, (ProblemError, CircuitOpen)):
        return False
    return isinstance(exc, infra_error_types())
