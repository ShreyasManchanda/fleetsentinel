"""Runtime settings (env prefix FS_NORMALIZER_)."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FS_NORMALIZER_", env_file=".env", extra="ignore")

    lane: str = "bulk"                          # bulk | priority
    kafka_bootstrap: str = "localhost:19092"
    group_id: str = ""                          # default derived from lane
    in_topic: str = ""                          # default derived from lane
    out_topic: str = ""                         # default derived from lane
    dlq_topic: str = "dlq.ingest.v1"
    alerts_topic: str = "alerts.v1"
    config_topic: str = "config.mappings.v1"
    mappings_dir: Path = Path("contracts/mappings")
    startup_mappings: str = "oem_a.yaml,oem_b.yaml,oem_c.yaml"   # oem_d_draft.yaml is NOT registered (live demo)
    metrics_port: int = 9101
    batch_max_msgs: int = 500
    poll_timeout_s: float = 0.2
    use_fast_path: bool = True
    drift_window_s: int = 60
    drift_min_events: int = 5
    drift_threshold: float = 0.5
    drift_cooldown_s: int = 600
    driver_key_b64: str = ""                    # dev AES-GCM key (32 bytes, base64); empty => fixed dev key + warning
    driver_key_id: str = "dev-driver-key"

    def resolved(self) -> Settings:
        prio = self.lane == "priority"
        return self.model_copy(update={
            "group_id": self.group_id or ("normalizer-priority" if prio else "normalizer"),
            "in_topic": self.in_topic or ("ingest.priority.v1" if prio else "ingest.raw.v1"),
            "out_topic": self.out_topic or ("telemetry.priority.v1" if prio else "telemetry.normalized.v1"),
        })