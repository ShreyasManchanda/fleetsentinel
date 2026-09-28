"""Runtime settings (env prefix FS_GATEWAY_)."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FS_GATEWAY_", env_file=".env", extra="ignore")

    mqtt_host: str = "localhost"
    mqtt_port: int = 8883
    mqtt_username: str = "gateway"
    certs_dir: Path = Path("deploy/certs")
    ca_file: str = "ca.crt"
    cert_file: str = "gateway.crt"
    key_file: str = "gateway.key"
    shared_group: str = "gw"
    kafka_bootstrap: str = "localhost:19092"
    redis_url: str = "redis://localhost:6379/0"
    known_oems: str = "OEM_A,OEM_B,OEM_C,OEM_D"
    max_payload_bytes: int = 1_048_576
    queue_size: int = 10_000
    batch_size: int = 200
    batch_wait_ms: int = 20
    dedup_ttl_s: int = 3600
    bloom_capacity: int = 1_000_000
    spill_dir: Path = Path(".gateway_spill")
    spill_max_bytes: int = 512 * 1024 * 1024
    metrics_port: int = 9102
    traces_sample_bulk: float = 0.01

    def oems(self) -> frozenset[str]:
        return frozenset(o.strip() for o in self.known_oems.split(",") if o.strip())