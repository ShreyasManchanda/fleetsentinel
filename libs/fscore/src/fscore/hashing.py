"""Stable, cross-language partitioning hash. Kafka's default partitioner is murmur2; we reuse it so a
VIN maps to the same shard/partition in every producer (Python or otherwise)."""
from __future__ import annotations


def murmur2(data: bytes) -> int:
    """Kafka's murmur2 (32-bit), returned as an unsigned int."""
    length = len(data)
    seed = 0x9747B28C
    m = 0x5BD1E995
    r = 24
    h = (seed ^ length) & 0xFFFFFFFF
    length4 = length // 4
    for i in range(length4):
        i4 = i * 4
        k = (data[i4] & 0xFF) | ((data[i4 + 1] & 0xFF) << 8) | ((data[i4 + 2] & 0xFF) << 16) | ((data[i4 + 3] & 0xFF) << 24)
        k = (k * m) & 0xFFFFFFFF
        k ^= k >> r
        k = (k * m) & 0xFFFFFFFF
        h = (h * m) & 0xFFFFFFFF
        h ^= k
    rem = length % 4
    base = length4 * 4
    if rem == 3:
        h ^= (data[base + 2] & 0xFF) << 16
    if rem >= 2:
        h ^= (data[base + 1] & 0xFF) << 8
    if rem >= 1:
        h ^= data[base] & 0xFF
        h = (h * m) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    h ^= h >> 15
    return h


def vin_partition(vin: str, n_partitions: int) -> int:
    """Partition for a VIN, matching Kafka's default partitioner (positive murmur2 mod n)."""
    return (murmur2(vin.encode()) & 0x7FFFFFFF) % n_partitions


def vin_shard(vin: str, n_shards: int = 64) -> int:
    return vin_partition(vin, n_shards)
